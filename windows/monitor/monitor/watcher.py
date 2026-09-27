"""Lightweight browser watcher, hotkeys and tray controller."""
import ctypes
import json
import queue
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pystray
from PIL import Image

from monitor import settings
from monitor.browser import (AddressBar, foreground_browser, foreground_process_id,
                             matches)
from monitor.process import BusinessProcess

ROOT = (Path(sys.executable).parent if getattr(sys, 'frozen', False)
        else Path(__file__).resolve().parents[1])
CONFIG = ROOT / 'config.json'
SETTINGS = ROOT / 'settings.json'
WM_QUIT = 0x0012


def load_config():
    data = json.loads(CONFIG.read_text(encoding='utf-8'))
    host = data.get('target_host', '')
    path = data.get('target_path_prefix', '/')
    if not isinstance(host, str) or not isinstance(path, str):
        raise ValueError('config.json needs target_host and target_path_prefix')
    host = host.strip().lower()
    if not host or '/' in host or not path.startswith('/'):
        raise ValueError('config.json needs target_host and target_path_prefix')
    data['target_host'] = host
    return data


class Hotkeys:
    def __init__(self, names, events):
        self.api = ctypes.windll.user32
        self.events = events
        self.names = names
        self.thread_id = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        """让注册线程退出：GetMessageW 收到 WM_QUIT 返回 0，finally 里会反注册。"""
        if not self.thread_id:
            return
        self.api.PostThreadMessageW(wintypes.DWORD(self.thread_id), WM_QUIT, 0, 0)
        self.thread.join(2)

    def _run(self):
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        registered = []
        try:
            for number, action in enumerate(('select', 'execute', 'cancel'), 1):
                name = self.names[action].upper()
                if name not in [f'F{i}' for i in range(1, 13)]:
                    raise ValueError(f'Invalid hotkey: {name}')
                vk = 0x70 + int(name[1:]) - 1
                if not self.api.RegisterHotKey(None, number, 0x4000, vk):
                    raise RuntimeError(f'Hotkey {name} is already in use')
                registered.append(number)
            self.events.put(('hotkeys', 'ready'))
            message = wintypes.MSG()
            while self.api.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312:
                    self.events.put(('key', ('SELECT', 'EXECUTE', 'CANCEL')[message.wParam - 1]))
        except Exception as exc:
            self.events.put(('error', str(exc)))
        finally:
            for number in registered:
                self.api.UnregisterHotKey(None, number)


def pick_browser_hwnd(browsers, enabled, child, last_hwnd):
    """Which browser window to watch this tick, or None when the user is off-task.

    The business GUI is a top-level window of its own. When it takes the foreground,
    foreground_browser() returns None, which used to be read as "the user left the
    page" and stopped the child; the child disappearing gave the foreground back to
    the browser, so the two oscillated and the GUI flickered until F8 was ever
    pressed. Our own window stealing focus is not the user leaving.
    """
    if not enabled:
        return None
    hwnd = foreground_browser(browsers)
    if hwnd:
        return hwnd
    if child and foreground_process_id() == child.pid:
        return last_hwnd
    return None


def read_verdict(url, host, path_prefix):
    """这次读栏结果算「在目标页」「已离开」还是「没读到」。

    地址栏偶尔会读空（页面重绘、焦点正在还给浏览器），这不代表用户走了。把它当成
    离开就会让常驻的业务进程一停一起，窗口跟着一闪一闪。
    """
    if not url:
        return 'unknown'
    return 'on' if matches(url, host, path_prefix) else 'off'


def target_after(verdict, target, unknown_reads, tolerance=3):
    """读栏结果 -> (是否还在目标页, 连续没读到的次数)。

    明确读到别的网址就立刻认输；读不到要连着几次才算，否则一次抖动就会把正在
    显示的窗口关掉，下一次读成功又把它拉起来。
    """
    if verdict == 'on':
        return True, 0
    if verdict == 'off':
        return False, 0
    unknown_reads += 1
    return target and unknown_reads < tolerance, unknown_reads


def run():
    data = load_config()
    icon_path = (ROOT / 'icon16.png' if getattr(sys, 'frozen', False)
                 else ROOT / 'assets' / 'icon16.png')
    image = Image.open(icon_path).convert('RGBA')
    events = queue.SimpleQueue()
    business = BusinessProcess(data.get('target_url', ''))
    bar = AddressBar()
    enabled = True
    state = 'IDLE'
    target = False
    last_hwnd = None
    next_read = 0
    unknown_reads = 0
    quit_requested = False

    def toggle(icon, item):
        events.put(('toggle', None))

    def open_settings(icon, item):
        settings.open_async(SETTINGS, events)

    def quit_app(icon, item):
        events.put(('quit', None))

    icon = pystray.Icon('FxxkPuzzleMonitor', image, '拼图助手 Monitor', menu=pystray.Menu(
        pystray.MenuItem(lambda item: f'状态：{state}', None, enabled=False),
        pystray.MenuItem(lambda item: '暂停' if enabled else '启用', toggle),
        # 静默启动时业务窗口从不露面，设置页（拖动速度、快捷键、是否自动弹窗）
        # 只有这一个入口，所以设为默认项，双击托盘图标就能打开。
        pystray.MenuItem('设置…', open_settings, default=True),
        pystray.MenuItem('退出', quit_app)))
    holder = {'hotkeys': None}

    def apply_hotkeys(names):
        if holder['hotkeys']:
            holder['hotkeys'].stop()
        holder['hotkeys'] = Hotkeys(names, events)

    apply_hotkeys(settings.hotkeys(SETTINGS, data.get('hotkeys', {})))
    icon.run_detached()
    try:
        while not quit_requested:
            for event in business.poll():
                if event == 'READY' and target and enabled:
                    state = 'PREPARED'
                elif event == 'RUNNING':
                    state = 'RUNNING'
                elif event == 'DONE':
                    state = 'PREPARED' if target and enabled else 'IDLE'
                    if state == 'IDLE':
                        business.stop()
                elif event == 'BYE':
                    # 子进程自己确认已经离开目标页了。这里别再认为"还在线上"，
                    # 否则 EXITED 分支会立刻把它重启，两边来回拉锯。
                    state = 'IDLE'
                    target = False
                    unknown_reads = 0
                elif event == 'EXITED':
                    state = 'IDLE'
                    if target and enabled:
                        business.start()
            try:
                while True:
                    kind, value = events.get_nowait()
                    if kind == 'quit':
                        quit_requested = True
                    elif kind == 'toggle':
                        enabled = not enabled
                        if not enabled:
                            business.stop()
                            state = 'IDLE'
                    elif kind == 'settings':
                        apply_hotkeys(value)
                        if business.child:
                            business.stop()   # 下次起来才会重读 settings.json
                    elif kind == 'key' and state in ('PREPARED', 'RUNNING'):
                        if value == 'CANCEL' or state == 'PREPARED':
                            business.send(value)
                    elif kind == 'error':
                        icon.notify(value, '拼图助手快捷键错误')
            except queue.Empty:
                pass
            hwnd = pick_browser_hwnd(data.get('browsers'), enabled, business.child, last_hwnd)
            if hwnd != last_hwnd:
                bar.clear()
                last_hwnd = hwnd
                next_read = 0
            now = time.monotonic()
            if not hwnd:
                target = False
                unknown_reads = 0
            elif now >= next_read:
                verdict = read_verdict(bar.read(hwnd), data['target_host'],
                                       data['target_path_prefix'])
                next_read = now + max(.5, float(data.get('check_interval_seconds', 1)))
                if verdict == 'unknown':
                    target, unknown_reads = target_after(verdict, target, unknown_reads)
                else:
                    unknown_reads = 0
                    target = verdict == 'on'
            if target and not business.child:
                business.start()
            elif not target and business.child and business.desired:
                business.stop()
                state = 'IDLE'
            time.sleep(.1)
    finally:
        business.stop()
        deadline = time.monotonic() + 6
        while business.child and time.monotonic() < deadline:
            business.poll()
            time.sleep(.1)
        icon.stop()


if __name__ == '__main__':
    run()
