"""Install the unpacked Extension edition into selected Chromium browsers."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
import winreg
from ctypes import wintypes
from pathlib import Path

import uiautomation as auto


EXTENSION_PAGES = {
    "chrome": "chrome://extensions",
    "edge": "edge://extensions",
}

BROWSER_EXES = {
    "chrome": "chrome.exe",
    "edge": "msedge.exe",
}

BROWSER_TITLE_HINTS = {
    "chrome": ("Google Chrome",),
    "edge": ("Microsoft Edge",),
}

DEVELOPER_NAMES = (
    "开发者模式",
    "开发人员模式",
    "Developer mode",
)

LOAD_NAMES = (
    "加载未打包的扩展",
    "加载未打包的扩展程序",
    "加载已解压的扩展程序",
    "加载解压缩的扩展",
    "加载解压缩扩展",
    "Load unpacked",
)

SELECT_FOLDER_NAMES = (
    "选择文件夹",
    "选择文件夹(&S)",
    "Select Folder",
    "Select Folder (&S)",
)


class BrowserAutomationError(RuntimeError):
    pass


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _app_path_from_registry(executable: str) -> Path | None:
    subkey = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for flags in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, subkey, 0, flags) as key:
                    value, _ = winreg.QueryValueEx(key, None)
            except OSError:
                continue
            path = Path(value)
            if path.is_file():
                return path
    return None


def find_browser(browser: str) -> Path | None:
    executable = BROWSER_EXES[browser]
    registry_path = _app_path_from_registry(executable)
    if registry_path:
        return registry_path

    local = Path(os.environ.get("LOCALAPPDATA", ""))
    program_files = Path(os.environ.get("PROGRAMFILES", ""))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", ""))
    candidates = {
        "chrome": (
            local / "Google/Chrome/Application/chrome.exe",
            program_files / "Google/Chrome/Application/chrome.exe",
            program_files_x86 / "Google/Chrome/Application/chrome.exe",
        ),
        "edge": (
            program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
            program_files / "Microsoft/Edge/Application/msedge.exe",
            local / "Microsoft/Edge/Application/msedge.exe",
        ),
    }
    return next((path for path in candidates[browser] if path.is_file()), None)


def selected_browser_paths(selected: list[str]) -> list[tuple[str, Path]]:
    result = []
    for browser in ("chrome", "edge"):
        if browser not in selected:
            continue
        path = find_browser(browser)
        if not path:
            raise BrowserAutomationError(f"未找到已选择的浏览器：{browser}")
        result.append((browser, path))
    if not result:
        raise BrowserAutomationError("请至少选择一个已安装的浏览器")
    return result


def _find_control(window, names: tuple[str, ...], timeout: float = 1.0):
    for name in names:
        control = auto.Control(searchFromControl=window, Name=name, searchDepth=15)
        if control.Exists(timeout, 0.2):
            return control
    return None


def _window_process_name(handle: int) -> str:
    """Return the executable name that owns a top-level window."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
    if not process_id.value:
        return ""

    process = kernel32.OpenProcess(0x1000, False, process_id.value)
    if not process:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(process)


def _is_browser_window(window, browser: str) -> bool:
    if window.ClassName != "Chrome_WidgetWin_1":
        return False
    handle = int(window.NativeWindowHandle or 0)
    if handle and _window_process_name(handle) == BROWSER_EXES[browser]:
        return True
    return any(
        hint.lower() in (window.Name or "").lower()
        for hint in BROWSER_TITLE_HINTS[browser]
    )


def _browser_windows(browser: str):
    result = []
    for window in auto.GetRootControl().GetChildren():
        if _is_browser_window(window, browser):
            result.append(window)
    return result


def _wait_browser_window(browser: str, previous_handles=None, timeout: float = 20.0):
    previous_handles = set(previous_handles or ())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        windows = _browser_windows(browser)
        for window in windows:
            if window.NativeWindowHandle not in previous_handles:
                window.SetActive()
                return window
        # Chromium may reuse an existing window despite --new-window.  Once that
        # browser owns the foreground window, it is the window the launch acted on.
        ctypes.windll.user32.GetForegroundWindow.restype = wintypes.HWND
        foreground_handle = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        for window in windows:
            if int(window.NativeWindowHandle or 0) == foreground_handle:
                window.SetActive()
                return window
        time.sleep(0.25)
    raise BrowserAutomationError("浏览器窗口没有在规定时间内出现")


def window_rect(window):
    handle = int(window.NativeWindowHandle)
    if not handle:
        raise BrowserAutomationError("没有取得浏览器顶层窗口句柄")
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(handle, ctypes.byref(rect)):
        raise BrowserAutomationError("无法读取浏览器窗口尺寸")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise BrowserAutomationError(f"浏览器窗口尺寸无效：{width} x {height}")
    return rect.left, rect.top, rect.right, rect.bottom


def _set_clipboard_and_paste(text: str):
    previous = None
    try:
        previous = auto.GetClipboardText()
    except Exception:
        pass
    auto.SetClipboardText(text)
    auto.SendKeys("{Ctrl}v")
    # SendKeys queues input; keep the replacement text on the clipboard until
    # Chromium has consumed the paste before restoring the user's clipboard.
    time.sleep(0.25)
    if previous is not None:
        try:
            auto.SetClipboardText(previous)
        except Exception:
            pass


def open_extension_page(browser: str, executable: Path):
    """Open a new maximized browser window and navigate through its address bar."""
    enable_dpi_awareness()
    previous = {window.NativeWindowHandle for window in _browser_windows(browser)}
    subprocess.Popen([
        str(executable),
        "--new-window",
        "about:blank",
        "--start-maximized",
        "--force-renderer-accessibility",
    ])
    window = _wait_browser_window(browser, previous)
    handle = int(window.NativeWindowHandle)
    ctypes.windll.user32.ShowWindow(handle, 3)  # SW_MAXIMIZE, not F11 fullscreen.
    window.SetActive()
    time.sleep(0.8)
    auto.SendKeys("{Ctrl}l")
    _set_clipboard_and_paste(EXTENSION_PAGES[browser])
    auto.SendKeys("{Enter}")
    time.sleep(1.5)
    window_rect(window)
    return window


def _load_positions(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _click_calibrated(window, browser: str, action: str, positions: dict) -> bool:
    try:
        x_ratio, y_ratio = positions[browser][action]
        left, top, right, bottom = window_rect(window)
        x = round(left + (right - left) * float(x_ratio))
        y = round(top + (bottom - top) * float(y_ratio))
    except (KeyError, TypeError, ValueError, AttributeError, BrowserAutomationError):
        return False
    auto.Click(x, y)
    return True


def _activate_uia_control(control, action: str):
    """Use a semantic UIA pattern before falling back to the control's click point."""
    preferred_patterns = (
        (auto.PatternId.TogglePattern, "Toggle") if action == "developer_mode"
        else (auto.PatternId.InvokePattern, "Invoke"),
        (auto.PatternId.InvokePattern, "Invoke"),
        (auto.PatternId.TogglePattern, "Toggle"),
    )
    attempted = set()
    for pattern_id, method_name in preferred_patterns:
        if pattern_id in attempted:
            continue
        attempted.add(pattern_id)
        pattern = control.GetPattern(pattern_id)
        if pattern:
            getattr(pattern, method_name)()
            return
    legacy = control.GetLegacyIAccessiblePattern()
    if legacy and legacy.DefaultAction:
        legacy.DoDefaultAction()
        return
    control.Click()


def _click_page_action(window, browser: str, action: str, names: tuple[str, ...],
                       positions: dict):
    control = _find_control(window, names, timeout=1.5)
    if control:
        try:
            _activate_uia_control(control, action)
            return "uiautomation"
        except Exception:
            # A Chromium update can expose the element but reject its pattern.
            # In that case use the author-calibrated point as the last resort.
            pass
    if _click_calibrated(window, browser, action, positions):
        return "coordinates"
    raise BrowserAutomationError(
        f"UI Automation 和备用坐标均无法操作浏览器中的“{names[0]}”，"
        "请联系安装包作者更新兼容参数。"
    )


def _wait_folder_dialog(timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        foreground = auto.GetForegroundControl()
        if foreground and foreground.ClassName == "#32770":
            return foreground
        for window in auto.GetRootControl().GetChildren():
            if window.ClassName == "#32770":
                window.SetActive()
                return window
        time.sleep(0.2)
    raise BrowserAutomationError("没有出现选择扩展文件夹的窗口")


def _choose_extension_folder(dialog, extension_dir: Path):
    previous_clipboard = None
    try:
        previous_clipboard = auto.GetClipboardText()
    except Exception:
        pass
    auto.SetClipboardText(str(extension_dir))
    dialog.SetActive()
    auto.SendKeys("{Ctrl}l")
    auto.SendKeys("{Ctrl}v")
    auto.SendKeys("{Enter}")
    time.sleep(0.8)

    button = _find_control(dialog, SELECT_FOLDER_NAMES, timeout=1.5)
    if button:
        button.Click()
    else:
        auto.SendKeys("{Enter}")

    if previous_clipboard is not None:
        try:
            auto.SetClipboardText(previous_clipboard)
        except Exception:
            pass


def install_extension(browser: str, executable: Path, extension_dir: Path,
                      positions_path: Path | None = None):
    if not extension_dir.is_dir():
        raise BrowserAutomationError(f"扩展目录不存在：{extension_dir}")
    positions = _load_positions(positions_path)
    window = open_extension_page(browser, executable)

    load = _find_control(window, LOAD_NAMES, timeout=1.0)
    if not load:
        _click_page_action(window, browser, "developer_mode", DEVELOPER_NAMES, positions)
        time.sleep(0.8)
    _click_page_action(window, browser, "load_unpacked", LOAD_NAMES, positions)

    dialog = _wait_folder_dialog()
    _choose_extension_folder(dialog, extension_dir)
    time.sleep(1.5)
    if dialog.Exists(0.5, 0.2):
        raise BrowserAutomationError("文件夹选择窗口仍未关闭，扩展可能尚未加载")


def install_selected_extensions(selected: list[str], extension_dir: Path,
                                positions_path: Path | None = None,
                                progress=None):
    results = []
    with auto.UIAutomationInitializerInThread():
        for browser, executable in selected_browser_paths(selected):
            if progress:
                progress(f"正在为 {browser.title()} 安装扩展……")
            install_extension(browser, executable, extension_dir, positions_path)
            results.append(browser)
    return results
