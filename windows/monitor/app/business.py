"""Business process: keep existing puzzle UI and code isolated from the watcher."""
import argparse
import ctypes
import queue
import sys
import threading
from ctypes import wintypes
from multiprocessing.connection import Client

import tkinter as tk

from app.browser import TargetPageGuard
from app.controls import WindowsDesktop, enable_dpi_awareness
from app.main import PuzzleAssistant

user32 = ctypes.windll.user32
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)


def window_pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def run(pipe, auth, target_url=''):
    enable_dpi_awareness()
    connection = Client(pipe, family='AF_PIPE', authkey=bytes.fromhex(auth))
    # main.py's _build_ui marks the root topmost so the preview floats above the web
    # page. In silent mode the root stays withdrawn, and a hidden topmost window still
    # takes the foreground and never returns it, which leaves the user unable to type
    # into the browser — so silent mode clears topmost and hands the focus back. The
    # focus hand-back has to be retried (Tk re-activates the window after the
    # constructor returns), but only the focus: re-raising our own window each retry
    # makes a visible window blink.
    foreground_before = user32.GetForegroundWindow()
    root = tk.Tk()
    root.withdraw()
    # 离开目标页就自己退出，判据和 Extension 版共用同一个 TargetPageGuard；
    # 父进程仍然可以随时下发 EXIT 要求停止。
    page_guard = TargetPageGuard(target_url) if target_url else None
    assistant = PuzzleAssistant(root, WindowsDesktop(), register_hotkeys=False,
                                monitor_mode=True, page_guard=page_guard)

    def present():
        if assistant.show_window_on_target:
            # 和 Extension 版一样：窗口该露脸时就压在浏览器上面，用户能在网页里
            # 打字靠的是下面那次"只交还焦点"，不是把窗口藏起来。
            root.attributes("-topmost", True)
            root.deiconify()
        else:
            root.attributes("-topmost", False)
            root.withdraw()
        hand_back_focus(0)

    def hand_back_focus(attempt):
        # 只重试"把键盘焦点还给浏览器"这一步。原来每次重试都会再 deiconify+lift 一
        # 次，等于刚把焦点交出去就把自己的窗口提回最上面，看起来就是一闪一闪。
        current = user32.GetForegroundWindow()
        if not current or not foreground_before:
            return
        if window_pid(current) != ctypes.windll.kernel32.GetCurrentProcessId():
            return
        if not user32.SetForegroundWindow(foreground_before) and attempt < 10:
            root.after(100, lambda: hand_back_focus(attempt + 1))

    root.after(50, present)
    stopping = False
    busy = False
    said_bye = False
    commands = queue.SimpleQueue()

    def send_event(event):
        try:
            connection.send(event)
            return True
        except (EOFError, OSError):
            return False

    if not send_event('READY'):
        assistant.close()
        connection.close()
        return

    def receive():
        while True:
            try:
                command = connection.recv()
            except (EOFError, OSError):
                command = 'EXIT'
            commands.put(command)
            if command == 'EXIT':
                return

    def handle(command):
        nonlocal stopping, busy
        if command == 'EXIT':
            stopping = True
            assistant.cancel()
            if not busy:
                assistant.close()
            else:
                root.after(100, finish)
            return
        if stopping or busy:
            return
        if command in ('SELECT', 'EXECUTE'):
            busy = True
            root.deiconify()
            root.lift()
            if command == 'SELECT':
                # _on_select() sets _select_only, so selected() stores the region and
                # returns without capturing. Setting _auto_drag here would leave it
                # armed and make the next 截图 drag on its own.
                assistant._on_select()
            else:
                assistant._auto_drag = True
                assistant.request_capture(False)
            if not send_event('RUNNING'):
                stopping = True
                assistant.cancel()
            root.after(100, finish)
        elif command == 'CANCEL':
            assistant.cancel()

    def finish():
        nonlocal busy
        if assistant.worker and assistant.worker.is_alive():
            root.after(100, finish)
            return
        if assistant.selector or assistant.state != 'idle':
            root.after(100, finish)
            return
        if busy:
            busy = False
            send_event('DONE')
        if stopping:
            assistant.close()

    def pump():
        nonlocal said_bye
        try:
            while True:
                handle(commands.get_nowait())
        except queue.Empty:
            pass
        if assistant.closing and not stopping and not said_bye:
            # 是页面守卫让我们退出的，不是父进程要求的：说一声，免得父进程以为
            # 进程意外死了又立刻重启，两边来回拉锯。
            said_bye = True
            send_event('BYE')
        if not assistant.closing:
            root.after(50, pump)

    threading.Thread(target=receive, daemon=True).start()
    root.after(50, pump)
    try:
        root.mainloop()
    finally:
        connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pipe', required=True)
    parser.add_argument('--auth', required=True)
    parser.add_argument('--target-url', default='')
    args = parser.parse_args()
    run(args.pipe, args.auth, args.target_url)
