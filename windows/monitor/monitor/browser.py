"""Monitor-specific browser matching; address-bar reading comes from app.browser."""
import ctypes
from ctypes import wintypes

from app.browser import AddressBar, foreground_browser_info

__all__ = ('AddressBar', 'foreground_browser', 'foreground_browser_info',
           'foreground_process_id', 'matches')

user32 = ctypes.windll.user32
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))


def foreground_process_id():
    """PID owning the foreground window, or 0. Used to ignore our own business window."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def foreground_browser(allowed=None):
    """Foreground Chromium window handle, limited to the installed browser list."""
    info = foreground_browser_info()
    if info and (allowed is None or info[1] in allowed):
        return info[0]
    return None


def matches(url, host, path_prefix):
    """Exact host plus path prefix: stricter than the Extension edition's substring."""
    from urllib.parse import urlsplit
    if not url or not host:
        return False
    parsed = urlsplit(url if '://' in url else 'https://' + url)
    # 地址栏显示根路径时不带结尾斜杠（example.com），urlsplit 给的 path 就是空串，
    # 直接 startswith('/') 会永远匹配不上，站点首页这种情况会漏判。
    path = parsed.path or '/'
    return (parsed.scheme in ('http', 'https')
            and (parsed.hostname or '').lower() == host.lower()
            and path.startswith(path_prefix))
