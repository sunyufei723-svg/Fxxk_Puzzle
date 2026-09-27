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
                 speed=600, max_checks=60, max_reads=10, settle_delay=.12):
    """拖动 + 白像素反馈：粗略落点后一次一步下山，停在白色比例谷底再松手。"""
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

        # 反馈循环：粗略落点（箭头中心→缺口中心）只是起点，绝不在这里松手。
        # 白色比例越低说明拼块盖得越严，所以一次只挪一步下山：
        #   更小 → 同方向继续；向右挪变大 → 掉头向左；向左也变大 → 谷底就在右边一步，
        #   退回最优点松手；不变 → 没有信息，保持方向并把步长翻倍（见下）。
        # 粗略位移既可能偏小（拼块在照片最左、箭头在滑轨左侧）也可能偏大，两个方向都得能纠。
        # 判据用严格比较，不设任何经验阈值：比例是同一块像素的白色占比，同一帧两次结果
        # 逐位相同，加死区只会把真实的小幅改进当成噪声。
        if gap_screen_rect is None or grab is None or white_ratio is None:
            return
        gap_width = gap_screen_rect[2] - gap_screen_rect[0]
        # 细步长：缺口宽度的 4%，2~5px。每步都要等画面停住，步长太细就把时间全花在
        # 等待上；2px 相对拼块宽度仍然远小于网站允许的误差。
        probe_step = min(5, max(2, round(gap_width * .04)))
        stride_cap = probe_step * 4          # 跨平台时最多放大到 4 倍，别一步步地爬
        left_limit = start[0]                # 滑块已在轨道最左，再向左探没有意义
        right_limit = grab_region[2] - 5     # 不得探测到截图右边界之外
        # 缺口矩形换算成截图内坐标，才能和截屏一起交给 white_ratio
        local_rect = (gap_screen_rect[0] - grab_region[0],
                      gap_screen_rect[1] - grab_region[1],
                      gap_screen_rect[2] - grab_region[0],
                      gap_screen_rect[3] - grab_region[1])

        def measure():
            try:
                frame = grab(bbox=grab_region, all_screens=True)
            except Exception:
                return None
            return white_ratio(frame, local_rect)

        def probe(x):
            """移动到 x，等画面停住再读数：每 40ms 重读一次，直到连续两次读数相同，
            且至少读满 4 轮。
            页面重绘比指针慢时，刚挪完截到的是上一处的旧画面。只等"连续两次相同"不够——
            旧画面自己也是连续两次相同的，必须给页面留出重绘时间，否则下降段旧值会被当成
            "改进"（最优点记到真实谷底右边一步），平台段旧值会被当成"不变"（当场松手）。"""
            mouse.move(x, end[1])
            previous = None
            for round_ in range(1, max_reads + 1):
                wait(.04)
                current = measure()
                if current is None:
                    return None
                if current == previous and round_ >= 4:
                    return current
                previous = current
            return previous                 # 读满上限仍在变（页面有动画），用最后一次读数

        x = best_x = end[0]
        best_ratio = probe(x)
        if best_ratio is None:
            return
        direction, stride, stalled, flat = 1, probe_step, 0, 0
        for _ in range(max_checks):
            check()
            x += direction * stride
            if x < left_limit or x > right_limit:
                break
            ratio = probe(x)
            if ratio is None:
                break
            if ratio < best_ratio:
                best_x, best_ratio, stride, stalled, flat = x, ratio, probe_step, 0, 0
            elif ratio > best_ratio:
                if direction < 0:
                    break                            # 向左也变大：谷底在右边一步
                # 向右变大：掉头向左，起点退回最优点。不去重测已知的位置，否则必然读出
                # 一个"不变"，把下面的加速误触发，一步跨过真正的谷底。
                direction, stride, x, stalled, flat = -1, probe_step, best_x, 0, 0
            else:
                # 不变有两种成因，都不是"已经对齐"：拼块还没压到缺口上（错位超过一个
                # 拼块宽度时比例逐位相同），或者站点把拼块位置量化得比 1px 还粗，指针
                # 挪了画面没动。两种都只是"没有信息"，所以保持方向继续走。
                # 但加速要等连续两次不变——单次不变也可能只是一次没等到重绘的旧读数，
                # 就放大步长会让采样网格跨过谷底。
                flat += 1
                stalled += stride
                if flat >= 2:
                    stride = min(stride * 2, stride_cap)
                # 连着走了一个缺口宽度仍毫无变化，说明这个方向不会有信息，别烧探测预算。
                if stalled >= gap_width:
                    break
        if x != best_x:
            mouse.move(best_x, end[1])           # 退回观测到的谷底再松手
            wait(.05)
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
        # 76~79 = SM_X/YVIRTUALSCREEN + SM_CX/CYVIRTUALSCREEN：多屏合并后的虚拟桌面包围盒
        return tuple(self.api.GetSystemMetrics(index) for index in (76, 77, 78, 79))

    def key_down(self, key):
        return bool(self.api.GetAsyncKeyState(key) & 0x8000)

    def cancel_pressed(self):
        # 0x8001：当前按下或自上次查询后被按下过，取消键快速点按也不会漏
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
        # 2 = GA_ROOT：从子控件回溯到顶层窗口，用于校验起终点同属一个窗口
        hwnd = self.api.WindowFromPoint(wintypes.POINT(*point))
        return self.api.GetAncestor(hwnd, 2) if hwnd else 0

    def place(self, widget, x, y, width, height):
        widget.update_idletasks()
        hwnd = self.api.GetAncestor(widget.winfo_id(), 2)
        # -1 = HWND_TOPMOST，0x0010 = SWP_NOACTIVATE：框选覆盖层不抢焦点
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
                # 0x4000 = MOD_NOREPEAT：长按不重复触发
                if not self.api.RegisterHotKey(None, identifier, 0x4000, key):
                    raise RuntimeError("F2、F8 或 F9 已被其他程序占用，请关闭冲突程序后重试。")
                registered.append(identifier)
            self._ready.set()
            message = wintypes.MSG()
            while not self._stop.is_set():
                # 只取 WM_HOTKEY(0x0312)，PM_REMOVE(1)：取出即出队
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
