"""Guided installer for the Extension distribution."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import winreg
from pathlib import Path
from tkinter import messagebox, ttk

from browser_automation import find_browser, install_selected_extensions


HERE = Path(__file__).resolve().parent
SOURCE = Path(sys.executable).parent if getattr(sys, "frozen", False) else HERE / "dist" / "Fxxk_Puzzle-extension"
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", HERE))
POSITIONS_PATH = BUNDLE_DIR / "browser_positions.json"
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", HERE)) / "Programs" / "FxxkPuzzleExtension"
SUFE_URL = "https://login.sufe.edu.cn/"
DEFAULT_KEYS = {"select": "F8", "execute": "F9", "cancel": "F2"}
FUNCTION_KEYS = tuple(f"F{number}" for number in range(1, 13))
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\FxxkPuzzleExtension"


def write_extension_config(extension_dir: Path, target_url: str):
    value = {"targetUrl": target_url.strip()}
    (extension_dir / "config.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def install_payload(source: Path, destination: Path, target_url: str, hotkeys: dict[str, str]) -> Path:
    app_source = source / "app"
    extension_source = source / "extensions"
    if not (app_source / "Fxxk_Puzzle.exe").is_file():
        raise FileNotFoundError(f"安装包缺少 app\\Fxxk_Puzzle.exe：{source}")
    if not (extension_source / "manifest.json").is_file():
        raise FileNotFoundError(f"安装包缺少 extensions\\manifest.json：{source}")
    if not (source / "Uninstall.exe").is_file():
        raise FileNotFoundError(f"安装包缺少 Uninstall.exe：{source}")

    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(app_source, destination / "app", dirs_exist_ok=True)
    shutil.copytree(extension_source, destination / "extensions", dirs_exist_ok=True)
    shutil.copy2(source / "Uninstall.exe", destination / "Uninstall.exe")
    write_extension_config(destination / "extensions", target_url)
    (destination / "app" / "settings.json").write_text(
        json.dumps({"drag_speed": 600, "hotkeys": hotkeys}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination / "app" / "Fxxk_Puzzle.exe"


def register_protocol(executable: Path):
    command = f'"{executable}" "%1"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\fxxk-puzzle") as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "URL:fxxk-puzzle")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Classes\fxxk-puzzle\shell\open\command",
    ) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)


def register_uninstall(install_dir: Path, executable: Path):
    uninstaller = install_dir / "Uninstall.exe"
    estimated_size = sum(
        path.stat().st_size for path in install_dir.rglob("*") if path.is_file()
    ) // 1024
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        values = {
            "DisplayName": "拼图助手（Extension 版）",
            "DisplayVersion": "1.0.0",
            "Publisher": "Fxxk Puzzle",
            "InstallLocation": str(install_dir),
            "DisplayIcon": str(executable),
            "UninstallString": f'"{uninstaller}"',
            "QuietUninstallString": f'"{uninstaller}" --quiet',
            "InstallDate": datetime.date.today().strftime("%Y%m%d"),
        }
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, estimated_size)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


class Installer:
    def __init__(self, root=None):
        self.root = root or tk.Tk()
        self.root.title("拼图助手安装向导（Extension 版）")
        self.root.geometry("620x430")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.cancel)
        self.chrome = tk.BooleanVar(value=False)
        self.edge = tk.BooleanVar(value=False)
        self.sufe = tk.BooleanVar(value=False)
        self.target_url = tk.StringVar(value="")
        self.hotkeys = {
            action: tk.StringVar(value=value) for action, value in DEFAULT_KEYS.items()
        }
        self.busy = False
        self._show_consent_page()

    def _clear(self, title: str, instruction: str):
        for child in self.root.winfo_children():
            child.destroy()
        frame = ttk.Frame(self.root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text=instruction, wraplength=560).pack(anchor="w", pady=(8, 22))
        return frame

    def _show_browser_page(self):
        frame = self._clear("选择浏览器", "请选择需要安装拼图助手扩展的浏览器，可多选。")
        chrome_state = "已检测" if find_browser("chrome") else "未检测到"
        edge_state = "已检测" if find_browser("edge") else "未检测到"
        ttk.Checkbutton(frame, text=f"Google Chrome（{chrome_state}）", variable=self.chrome).pack(anchor="w", pady=8)
        ttk.Checkbutton(frame, text=f"Microsoft Edge（{edge_state}）", variable=self.edge).pack(anchor="w", pady=8)
        ttk.Button(frame, text="下一步", command=self._browser_next).pack(anchor="e", side="bottom")

    def _show_consent_page(self):
        frame = self._clear(
            "安装前确认",
            "本程序会把拼图助手安装到当前用户目录、注册 fxxk-puzzle:// 启动协议，"
            "并自动操作所选浏览器的扩展管理页面以加载本地扩展。安装期间请保持桌面解锁。",
        )
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="不同意，退出安装", command=self.cancel).pack(side="left")
        ttk.Button(buttons, text="同意并继续", command=self._show_browser_page).pack(side="right")

    def _browser_next(self):
        selected = self.selected_browsers()
        if not selected:
            messagebox.showwarning("请选择浏览器", "Chrome 和 Edge 至少选择一个。", parent=self.root)
            return
        missing = [name for name in selected if not find_browser(name)]
        if missing:
            messagebox.showerror("浏览器未找到", "未找到：" + "、".join(missing), parent=self.root)
            return
        self._show_target_page()

    def _show_target_page(self):
        frame = self._clear("选择目标网址", "选择预设，或直接在文本框中输入需要监测的网址。")
        ttk.Checkbutton(frame, text="上财", variable=self.sufe, command=self._toggle_sufe).pack(anchor="w", pady=(0, 12))
        ttk.Label(frame, text="目标网址").pack(anchor="w")
        entry = ttk.Entry(frame, textvariable=self.target_url, width=70)
        entry.pack(fill="x", pady=(5, 18))
        entry.focus_set()
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="上一步", command=self._show_browser_page).pack(side="left")
        ttk.Button(buttons, text="下一步", command=self._target_next).pack(side="right")

    def _toggle_sufe(self):
        if self.sufe.get():
            self.target_url.set(SUFE_URL)
        elif self.target_url.get().strip() == SUFE_URL:
            self.target_url.set("")

    def _target_next(self):
        url = self.target_url.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("网址无效", "请输入以 http:// 或 https:// 开头的网址。", parent=self.root)
            return
        self._show_hotkey_page()

    def _show_hotkey_page(self):
        frame = self._clear(
            "设置快捷键",
            "为三个操作选择互不重复的快捷键。安装后仍可在应用设置中修改。",
        )
        labels = (("select", "框选识别区域"), ("execute", "识别并执行"), ("cancel", "取消操作"))
        for action, label in labels:
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=6)
            ttk.Label(row, text=label, width=18).pack(side="left")
            ttk.Combobox(
                row, textvariable=self.hotkeys[action], values=FUNCTION_KEYS,
                state="readonly", width=10,
            ).pack(side="left")
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="上一步", command=self._show_target_page).pack(side="left")
        ttk.Button(buttons, text="下一步", command=self._hotkey_next).pack(side="right")

    def selected_hotkeys(self):
        return {action: value.get() for action, value in self.hotkeys.items()}

    def _hotkey_next(self):
        hotkeys = self.selected_hotkeys()
        if len(set(hotkeys.values())) != len(hotkeys):
            messagebox.showwarning("快捷键重复", "三个操作必须使用不同的快捷键。", parent=self.root)
            return
        self._show_install_page()

    def _show_install_page(self):
        names = {"chrome": "Chrome", "edge": "Edge"}
        selected = "、".join(names[name] for name in self.selected_browsers())
        hotkeys = self.selected_hotkeys()
        frame = self._clear(
            "准备安装",
            f"浏览器：{selected}\n目标网址：{self.target_url.get().strip()}\n"
            f"快捷键：框选 {hotkeys['select']}，执行 {hotkeys['execute']}，取消 {hotkeys['cancel']}",
        )
        ttk.Label(frame, text=f"安装位置：{INSTALL_DIR}", wraplength=560).pack(anchor="w")
        ttk.Label(frame, text="安装后将依次打开浏览器并自动加载 extensions 文件夹。", wraplength=560).pack(anchor="w", pady=8)
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="上一步", command=self._show_hotkey_page).pack(side="left")
        ttk.Button(buttons, text="安装", command=self.install).pack(side="right")

    def selected_browsers(self):
        return [name for name, value in (("chrome", self.chrome), ("edge", self.edge)) if value.get()]

    def install(self):
        if self.busy:
            return
        self.busy = True
        target_url = self.target_url.get().strip()
        selected_browsers = self.selected_browsers()
        hotkeys = self.selected_hotkeys()
        frame = self._clear("正在安装", "请不要操作鼠标或键盘，浏览器扩展安装完成前请保持桌面解锁。")
        status = tk.StringVar(value="正在复制应用文件……")
        ttk.Label(frame, textvariable=status, wraplength=560).pack(anchor="w")
        bar = ttk.Progressbar(frame, mode="indeterminate")
        bar.pack(fill="x", pady=22)
        bar.start(10)

        def progress(text):
            self.root.after(0, status.set, text)

        def worker():
            try:
                executable = install_payload(SOURCE, INSTALL_DIR, target_url, hotkeys)
                progress("正在注册 fxxk-puzzle:// 启动协议……")
                register_protocol(executable)
                progress("正在注册 Windows 卸载程序……")
                register_uninstall(INSTALL_DIR, executable)
                installed = install_selected_extensions(
                    selected_browsers, INSTALL_DIR / "extensions", POSITIONS_PATH, progress
                )
            except Exception as exc:
                self.root.after(0, self._install_failed, str(exc))
                return
            self.root.after(0, self._install_done, installed)

        threading.Thread(target=worker, daemon=True).start()

    def _install_failed(self, detail: str):
        self.busy = False
        messagebox.showerror("安装未完成", detail, parent=self.root)
        self._show_install_page()

    def _install_done(self, installed: list[str]):
        self.busy = False
        names = {"chrome": "Chrome", "edge": "Edge"}
        done = "、".join(names[name] for name in installed)
        frame = self._clear(
            "安装完成",
            f"应用和扩展已经安装到：\n{INSTALL_DIR}\n\n已配置浏览器：{done}\n\n"
            f"目标网页打开并出现拼图验证码后，请按 {self.selected_hotkeys()['select']}，"
            "框选完整验证码图片和底部滑轨。",
        )
        ttk.Button(frame, text="完成", command=self.root.destroy).pack(side="bottom", anchor="e")
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        try:
            messagebox.showinfo(
                "接下来配置识别区域",
                "点击“确定”后将自动打开目标网址，浏览器会覆盖本安装程序。\n\n"
                f"请记住：网页出现拼图验证码后，按 {self.selected_hotkeys()['select']} "
                "框选完整验证码图片和底部滑轨。",
                parent=self.root,
            )
        finally:
            self.root.attributes("-topmost", False)
        self._open_target_url(installed[0])

    def _open_target_url(self, browser: str):
        executable = find_browser(browser)
        if not executable:
            messagebox.showerror("无法打开网页", f"未找到 {browser}。", parent=self.root)
            return
        subprocess.Popen([
            str(executable), "--new-window", "--start-maximized", self.target_url.get().strip()
        ])

    def cancel(self):
        if self.busy:
            messagebox.showinfo("正在安装", "请等待当前安装步骤完成。", parent=self.root)
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    args, _ = parser.parse_known_args()
    app = Installer()
    if args.smoke_test:
        app.root.after(500, app.root.destroy)
    app.run()


if __name__ == "__main__":
    main()
