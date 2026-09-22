"""Windows 屏幕坐标、全局热键和可取消的单次拖动。"""

import ctypes
from ctypes import wintypes
import os
import queue
import threading
import time


class DragCancelled(RuntimeError):
    pass


def perform_drag(mouse, start, end, cancel, gap_screen_rect=None,
                 grab_region=None, grab=None, white_ratio=None,
                 speed=600, white_threshold=.008, max_checks=25, settle_delay=.12):
    """拖动 + 白像素反馈循环：占比低于阈值才释放鼠标。"""
    def check():
        if cancel.is_set() or mouse.cancel_pressed():
            raise DragCancelled("已取消，鼠标已释放。")

    def wait(seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            check()
            cancel.wait(min(.01, max(0, until - time.monotonic())))
        check()

    check()
    if mouse.left_is_down():
        raise DragCancelled("检测到你仍按着鼠标左键，请松开后重新识别。")
    if speed <= 0:
        raise ValueError("速度必须大于 0。")
    mouse.move(*start)
    wait(.08)
    attempted_press = False
    try:
        check()
        attempted_press = True
        mouse.press()
        # 按速度移动：每帧前进 speed * dt 像素
        cx, cy = float(start[0]), float(start[1])
        tx, ty = float(end[0]), float(end[1])
        prev = time.monotonic()
        while True:
            check()
            now = time.monotonic()
            dt = now - prev
            prev = now
            dx, dy = tx - cx, ty - cy
            dist = (dx * dx + dy * dy) ** 0.5
            step = speed * dt
            if step >= dist:
                mouse.move(end[0], end[1])
                break
            cx += dx / dist * step
            cy += dy / dist * step
            mouse.move(round(cx), round(cy))
            wait(.01)
        wait(settle_delay)

        # 反馈循环：用白像素比例的变化趋势决定方向。
        if gap_screen_rect is None or grab is None or white_ratio is None:
            return
        current_x = end[0]
        prev_ratio = None
        going_right = True
        # 步长：缺口宽度的 2%，最大 5px，避免来回震荡
        step = min(5, max(1, int(round((gap_screen_rect[2] - gap_screen_rect[0]) * .02))))
        for attempt in range(max_checks):
            check()
            try:
                current = grab(bbox=grab_region, all_screens=True)
            except Exception:
                break
            local_rect = (
                gap_screen_rect[0] - grab_region[0],
                gap_screen_rect[1] - grab_region[1],
                gap_screen_rect[2] - grab_region[0],
                gap_screen_rect[3] - grab_region[1],
            )
            ratio = white_ratio(current, local_rect)
            if ratio < white_threshold:
                return
            if prev_ratio is not None:
                if ratio > prev_ratio:
                    going_right = not going_right
            direction = 1 if going_right else -1
            prev_ratio = ratio
            new_x = current_x + step * direction
            if not (start[0] < new_x < grab_region[2] - 5):
                break
            mouse.move(new_x, end[1])
            current_x = new_x
            wait(.08)
    finally:
        if attempted_press:
            mouse.release()


class MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class InputUnion(ctypes.Union):
    _fields_ = [("mi", MouseInput), ("ki", KeyboardInput), ("hi", HardwareInput)]


class Input(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]


def vk_for_function_key(name):
    """把 "F1".."F12" 转成 Windows 虚拟键码（VK_F1=0x70 起）。"""
    n = int(str(name).upper().lstrip("F"))
    if not 1 <= n <= 12:
        raise ValueError("快捷键仅支持 F1~F12")
    return 0x6F + n


def enable_dpi_awareness():
    if os.name != "nt":
        raise RuntimeError("此工具需要 Windows 10 或更新版本。")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        function = user32.SetProcessDpiAwarenessContext
        function.argtypes = [ctypes.c_void_p]
        function.restype = wintypes.BOOL
        if function(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    # 兼容尚不支持 Per-Monitor V2 的环境；必须早于 Tk 和截屏初始化。
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except OSError:
        user32.SetProcessDPIAware()


class WindowsDesktop:
    def __init__(self):
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        a = self.api
        a.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        a.SetCursorPos.restype = wintypes.BOOL
        a.GetAsyncKeyState.argtypes = [ctypes.c_int]
        a.GetAsyncKeyState.restype = ctypes.c_short
        a.GetSystemMetrics.argtypes = [ctypes.c_int]
        a.GetSystemMetrics.restype = ctypes.c_int
        a.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        a.SendInput.restype = wintypes.UINT
        a.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        a.GetAncestor.restype = wintypes.HWND
        a.WindowFromPoint.argtypes = [wintypes.POINT]
        a.WindowFromPoint.restype = wintypes.HWND
        a.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        a.SetWindowPos.restype = wintypes.BOOL
        a.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        a.RegisterHotKey.restype = wintypes.BOOL
        a.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        a.UnregisterHotKey.restype = wintypes.BOOL
        a.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT, wintypes.UINT]
        a.PeekMessageW.restype = wintypes.BOOL
        self._button_lock = threading.Lock()
        self._pressed = False
        # 取消键的虚拟码，可由配置改绑（默认 F2=0x71）。
        self.cancel_vk = 0x71

    def bounds(self):
        return tuple(self.api.GetSystemMetrics(index) for index in (76, 77, 78, 79))

    def key_down(self, key):
        return bool(self.api.GetAsyncKeyState(key) & 0x8000)

    def cancel_pressed(self):
        return bool(self.api.GetAsyncKeyState(self.cancel_vk) & 0x8001)

    def clear_cancel_history(self):
        self.api.GetAsyncKeyState(self.cancel_vk)

    def left_is_down(self):
        return self.key_down(1)

    def move(self, x, y):
        left, top, width, height = self.bounds()
        if not (left <= x < left + width and top <= y < top + height):
            raise RuntimeError("鼠标目标不在屏幕内，已取消。")
        if not self.api.SetCursorPos(x, y):
            raise ctypes.WinError(ctypes.get_last_error())

    def _send(self, flags):
        event = Input(type=0, mi=MouseInput(dwFlags=flags))
        if self.api.SendInput(1, ctypes.byref(event), ctypes.sizeof(Input)) != 1:
            raise RuntimeError("Windows 拒绝鼠标输入；请确保浏览器与工具均以普通权限运行。")

    def press(self):
        with self._button_lock:
            # 即使系统调用抛错，也保留释放责任。
            self._pressed = True
            self._send(0x0002)

    def release(self):
        with self._button_lock:
            if self._pressed:
                self._send(0x0004)
                self._pressed = False

    def root_window(self, point):
        hwnd = self.api.WindowFromPoint(wintypes.POINT(*point))
        return self.api.GetAncestor(hwnd, 2) if hwnd else 0

    def place(self, widget, x, y, width, height):
        widget.update_idletasks()
        hwnd = self.api.GetAncestor(widget.winfo_id(), 2)
        if not self.api.SetWindowPos(hwnd, ctypes.c_void_p(-1), x, y, width, height, 0x0010):
            raise ctypes.WinError(ctypes.get_last_error())


class Hotkeys:
    def __init__(self, desktop, keys=None):
        # keys: [(vk, action), ...]；默认 F8=select / F9=execute / F2=cancel，可由配置改绑。
        if keys is None:
            keys = [(0x77, "select"), (0x78, "execute"), (0x71, "cancel")]
        self.KEYS = {i + 1: (vk, action) for i, (vk, action) in enumerate(keys)}
        self.api = desktop.api
        self._actions = queue.SimpleQueue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._listen, name="global-hotkeys", daemon=False)
        self._thread.start()
        if not self._ready.wait(2):
            self.close()
            raise RuntimeError("热键监听启动超时，请重试。")
        if self._error is not None:
            self.close()
            raise self._error

    def _listen(self):
        # 注册、收消息和注销必须在同一线程；不能与 Tk 的消息循环竞争。
        registered = []
        try:
            for identifier, (key, _) in self.KEYS.items():
                if not self.api.RegisterHotKey(None, identifier, 0x4000, key):
                    raise RuntimeError("F2、F8 或 F9 已被其他程序占用，请关闭冲突程序后重试。")
                registered.append(identifier)
            self._ready.set()
            message = wintypes.MSG()
            while not self._stop.is_set():
                while not self._stop.is_set() and self.api.PeekMessageW(
                        ctypes.byref(message), None, 0x0312, 0x0312, 1):
                    item = self.KEYS.get(message.wParam)
                    if item:
                        self._actions.put(item[1])
                self._stop.wait(.01)
        except Exception as exc:
            self._error = exc
        finally:
            for identifier in registered:
                self.api.UnregisterHotKey(None, identifier)
            self._ready.set()

    def poll(self):
        # 供 Tk 主线程调用：只取 Python 队列，不触碰后台线程的 Windows 消息。
        if self._error is not None:
            raise RuntimeError(f"热键监听已停止：{self._error}") from self._error
        actions = []
        while True:
            try:
                actions.append(self._actions.get_nowait())
            except queue.Empty:
                return actions

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise RuntimeError("热键监听线程尚未退出。")
