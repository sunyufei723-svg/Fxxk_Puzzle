"""Read Chromium's current page URL without changing focus or clipboard."""

import ctypes
import re
import time
from ctypes import wintypes
from urllib.parse import parse_qs, urlsplit


BROWSER_NAMES = ("chrome.exe", "msedge.exe")
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
)
kernel32.OpenProcess.argtypes = (
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)


def browser_window_info(hwnd):
    """Return ``(hwnd, executable name)`` when hwnd belongs to Chrome or Edge."""
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            name = buffer.value.rsplit("\\", 1)[-1].lower()
            if name in BROWSER_NAMES:
                return hwnd, name
    finally:
        kernel32.CloseHandle(handle)
    return None


def foreground_browser_info():
    return browser_window_info(user32.GetForegroundWindow())


class AddressBar:
    def __init__(self):
        self.hwnd = None
        self.element = None

    def clear(self):
        self.hwnd = self.element = None

    def read(self, hwnd):
        try:
            import uiautomation as auto

            if hwnd != self.hwnd or self.element is None:
                self.clear()
                window = auto.ControlFromHandle(hwnd)
                # Chromium exposes the omnibox as an editable ComboBox or Edit.
                for kind in ("EditControl", "ComboBoxControl"):
                    element = getattr(window, kind)(
                        searchDepth=8,
                        Name="Address and search bar",
                    )
                    if element.Exists(0.1, 0.05):
                        self.element = element
                        break
                if self.element is None:
                    # Localized browsers often expose another accessible name.
                    for control, _depth in auto.WalkControl(
                        window,
                        includeTop=False,
                        maxDepth=8,
                    ):
                        if control.ControlTypeName not in (
                            "EditControl",
                            "ComboBoxControl",
                        ):
                            continue
                        aid = (control.AutomationId or "").lower()
                        name = (control.Name or "").lower()
                        if "address" in aid or "address" in name or "地址" in name:
                            self.element = control
                            break
                self.hwnd = hwnd
            if self.element is None:
                return None
            pattern = self.element.GetPattern(auto.PatternId.ValuePattern)
            return pattern.Value if pattern else None
        except Exception:
            self.clear()
            return None


def bare_url(value):
    """去掉协议头和根路径的结尾斜杠。

    Chrome/Edge 的地址栏显示的是 `example.com` 而不是 `https://example.com/`，
    安装时写进扩展的 targetUrl 却带协议头。两边都归一化之前，target 形如
    `https://xxx/` 会永远匹配不上地址栏，离开页面也就不会自动退出。
    """
    value = str(value or "").strip().casefold()
    return re.sub(r"^[a-z0-9+.-]+://", "", value).rstrip("/")


def matches_target(url, target_url):
    """Keep the Extension version's intentionally broad substring semantics."""
    if not isinstance(url, str):
        return False
    target = bare_url(target_url)
    return bool(target) and target in bare_url(url)


def target_from_launch_uri(value):
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "fxxk-puzzle" or parsed.netloc.casefold() != "launch":
        return ""
    values = parse_qs(parsed.query).get("target", [])
    return values[0].strip() if values else ""


def _query_flag(value, name):
    parsed = urlsplit(value) if isinstance(value, str) else None
    if not parsed or parsed.scheme.casefold() != "fxxk-puzzle":
        return None
    if parsed.netloc.casefold() not in ("launch", "settings"):
        return None
    values = parse_qs(parsed.query).get(name, [])
    if not values:
        return None
    flag = values[0].strip().casefold()
    if flag in ("1", "true", "on", "show"):
        return True
    if flag in ("0", "false", "off", "hide"):
        return False
    return None


def show_window_from_launch_uri(value):
    """读 URI 里的 &show=0/1 —— 扩展 popup 的「静默启动」就靠它落到 settings.json。

    没带这个参数返回 None：手动双击程序、或旧版扩展拉起时都不该改用户的设置。
    """
    return _query_flag(value, "show")


def is_settings_uri(value):
    """fxxk-puzzle://settings?... 只改设置，不弹操作界面。"""
    return isinstance(value, str) and urlsplit(value).netloc.casefold() == "settings"


class TargetPageGuard:
    """Close only after a target page was observed and is then left.

    The protocol may briefly leave an external-protocol tab active. Waiting until
    the real target URL is observed prevents that launch transition from being
    mistaken for the user leaving the page.
    """

    def __init__(
        self,
        target_url,
        hwnd=None,
        *,
        address_bar=None,
        foreground_browser=None,
        clock=None,
        check_interval=0.5,
        max_read_failures=3,
    ):
        self.target_url = target_url
        self.hwnd = hwnd
        self.address_bar = address_bar or AddressBar()
        self.foreground_browser = foreground_browser or foreground_browser_info
        self.clock = clock or time.monotonic
        self.check_interval = max(0.1, float(check_interval))
        self.max_read_failures = max(1, int(max_read_failures))
        self.next_check = 0.0
        self.seen_target = False
        self.read_failures = 0
        self.current_url = None

    def _select_browser_window(self):
        info = self.foreground_browser()
        if info and info[0] != self.hwnd:
            self.hwnd = info[0]
            self.address_bar.clear()
            self.read_failures = 0

    def should_close(self):
        now = self.clock()
        if now < self.next_check:
            return False
        self.next_check = now + self.check_interval
        self._select_browser_window()
        if not self.hwnd:
            return False

        url = self.address_bar.read(self.hwnd)
        self.current_url = url
        if matches_target(url, self.target_url):
            self.seen_target = True
            self.read_failures = 0
            return False
        if url:
            self.read_failures = 0
            return self.seen_target

        self.read_failures += 1
        if self.seen_target:
            return self.read_failures >= self.max_read_failures
        if self.read_failures >= self.max_read_failures:
            self.hwnd = None
            self.address_bar.clear()
            self.read_failures = 0
        return False
