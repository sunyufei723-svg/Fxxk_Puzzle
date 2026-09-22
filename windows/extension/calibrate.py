"""Record fixed Chromium extension-page button positions as a UIA fallback."""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

import uiautomation as auto

from browser_automation import find_browser, open_extension_page, window_rect


HERE = Path(__file__).resolve().parent
OUTPUT = (Path(sys.executable).parent if getattr(sys, "frozen", False) else HERE) / "browser_positions.json"
VK_F8 = 0x77
VK_F9 = 0x78


def wait_key(vk: int):
    user32 = ctypes.windll.user32
    while user32.GetAsyncKeyState(vk) & 0x8000:
        time.sleep(0.05)
    while not user32.GetAsyncKeyState(vk) & 0x8000:
        time.sleep(0.05)
    while user32.GetAsyncKeyState(vk) & 0x8000:
        time.sleep(0.05)


def cursor_ratio(window):
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    left, top, right, bottom = window_rect(window)
    width = right - left
    height = bottom - top
    if not (left <= point.x < right and top <= point.y < bottom):
        raise RuntimeError("鼠标不在浏览器窗口内，请重新运行校准")
    return [
        round((point.x - left) / width, 6),
        round((point.y - top) / height, 6),
    ]


def calibrate(browser: str):
    executable = find_browser(browser)
    if not executable:
        raise SystemExit(f"未找到 {browser}")
    window = open_extension_page(browser, executable)

    print("把鼠标移到“开发者模式”开关中心，然后按 F8。")
    wait_key(VK_F8)
    developer = cursor_ratio(window)
    left, top, right, bottom = window_rect(window)
    auto.Click(round(left + (right - left) * developer[0]),
               round(top + (bottom - top) * developer[1]))
    time.sleep(1)
    print("把鼠标移到“加载已解压的扩展程序”按钮中心，然后按 F9。")
    wait_key(VK_F9)
    load = cursor_ratio(window)

    try:
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if data.get("schema") != 2 or data.get("window_mode") != "maximized":
        data = {"schema": 2, "window_mode": "maximized"}
    data[browser] = {"developer_mode": developer, "load_unpacked": load}
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {OUTPUT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("browser", choices=("chrome", "edge"))
    calibrate(parser.parse_args().browser)
