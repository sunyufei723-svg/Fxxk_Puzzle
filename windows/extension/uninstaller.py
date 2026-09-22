"""Per-user uninstaller registered in Windows Settings."""
from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import winreg
from ctypes import wintypes
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


PRODUCT_KEY = "FxxkPuzzleExtension"
DISPLAY_NAME = "拼图助手（Extension 版）"
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "FxxkPuzzleExtension"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{PRODUCT_KEY}"
PROTOCOL_KEY = r"Software\Classes\fxxk-puzzle"
PROCESS_PATHS = (Path("app") / "Fxxk_Puzzle.exe",)


def _delete_registry_tree(root, path: str):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            children = []
            index = 0
            while True:
                try:
                    children.append(winreg.EnumKey(key, index))
                    index += 1
                except OSError:
                    break
        for child in children:
            _delete_registry_tree(root, path + "\\" + child)
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass


def _terminate_installed_processes(install_dir: Path):
    targets = {
        str((install_dir / relative).resolve()).casefold()
        for relative in PROCESS_PATHS
    }
    psapi = ctypes.windll.psapi
    kernel32 = ctypes.windll.kernel32
    psapi.EnumProcesses.argtypes = [
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    psapi.EnumProcesses.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    process_ids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(process_ids, ctypes.sizeof(process_ids), ctypes.byref(needed)):
        return
    for process_id in process_ids[:needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if not process_id:
            continue
        handle = kernel32.OpenProcess(0x1001, False, process_id)
        if not handle:
            continue
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if (kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
                    and str(Path(buffer.value).resolve()).casefold() in targets):
                kernel32.TerminateProcess(handle, 0)
                kernel32.WaitForSingleObject(handle, 3000)
        finally:
            kernel32.CloseHandle(handle)


def _remove_tree(path: Path):
    if not path.exists():
        return
    last_error = None
    for _ in range(15):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2)
    raise last_error


def uninstall(install_dir: Path = INSTALL_DIR):
    _terminate_installed_processes(install_dir)
    _delete_registry_tree(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY)
    _remove_tree(install_dir)
    _delete_registry_tree(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)


def _relay_to_temporary_copy(quiet: bool):
    directory = Path(tempfile.mkdtemp(prefix=f"{PRODUCT_KEY}-uninstall-"))
    executable = directory / "Uninstall.exe"
    shutil.copy2(sys.executable, executable)
    arguments = [str(executable), "--remove"]
    if quiet:
        arguments.append("--quiet")
    subprocess.Popen(arguments, cwd=directory, creationflags=subprocess.CREATE_NO_WINDOW)


def _schedule_temporary_cleanup():
    executable = Path(sys.executable).resolve()
    script = Path(tempfile.gettempdir()) / f"{PRODUCT_KEY}-cleanup-{uuid.uuid4().hex}.cmd"
    script.write_text(
        "@echo off\n"
        "for /L %%i in (1,1,30) do (\n"
        f'  del /f /q "{executable}" >nul 2>&1\n'
        f'  if not exist "{executable}" goto done\n'
        "  timeout /t 1 /nobreak >nul\n"
        ")\n"
        ":done\n"
        f'rmdir /s /q "{executable.parent}" >nul 2>&1\n'
        'del /f /q "%~f0"\n',
        encoding="utf-8",
    )
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", str(script)],
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )


class Uninstaller:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"卸载 {DISPLAY_NAME}")
        self.root.geometry("520x260")
        self.root.resizable(False, False)
        frame = ttk.Frame(self.root, padding=24)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text=f"卸载 {DISPLAY_NAME}", font=("Microsoft YaHei UI", 16, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 16)
        )
        ttk.Label(
            frame,
            text="将停止拼图助手，删除程序与识别区域配置，并移除 fxxk-puzzle:// 启动协议。",
            wraplength=460,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 28))
        ttk.Button(frame, text="取消", command=self.root.destroy).grid(row=2, column=0, sticky="w")
        ttk.Button(frame, text="卸载", command=self.remove).grid(row=2, column=1, sticky="e")
        frame.columnconfigure(1, weight=1)

    def remove(self):
        try:
            uninstall()
        except Exception as exc:
            messagebox.showerror("卸载未完成", str(exc), parent=self.root)
            return
        messagebox.showinfo("卸载完成", f"{DISPLAY_NAME} 已从此电脑移除。", parent=self.root)
        self.root.destroy()
        if getattr(sys, "frozen", False):
            _schedule_temporary_cleanup()

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    args, _ = parser.parse_known_args()
    if args.smoke_test:
        app = Uninstaller()
        app.root.after(500, app.root.destroy)
        app.run()
        return
    if getattr(sys, "frozen", False) and not args.remove:
        _relay_to_temporary_copy(args.quiet)
        return
    if args.quiet:
        try:
            uninstall()
        finally:
            if getattr(sys, "frozen", False):
                _schedule_temporary_cleanup()
        return
    Uninstaller().run()


if __name__ == "__main__":
    main()
