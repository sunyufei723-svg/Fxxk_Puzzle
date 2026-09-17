"""入口：框选区域、识别缺口、确认后单次拖动。"""

import argparse
import json
import queue
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk, ImageGrab

from controls import WindowsDesktop, perform_drag, enable_dpi_awareness, Hotkeys, DragCancelled
from vision import detect, DetectionError


class RegionSelector:
    def __init__(self, master, desktop, screenshot, bounds, done):
        self.done = done
        self.bounds = bounds
        self.anchor = None
        self.finished = False
        self.window = tk.Toplevel(master)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        x, y, width, height = bounds
        self.window.geometry(f"{width}x{height}+0+0")
        self.canvas = tk.Canvas(self.window, highlightthickness=0, cursor="crosshair")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        self.background = ImageTk.PhotoImage(screenshot)
        self.canvas.create_image(0, 0, anchor="nw", image=self.background)
        self.rectangle = self.canvas.create_rectangle(0, 0, 0, 0, outline="#ed4245", width=3)
        self.canvas.create_rectangle(12, 12, 670, 56, fill="#182c42", outline="")
        self.canvas.create_text(28, 34, anchor="w", fill="white", font=("Microsoft YaHei UI", 12),
                                text="框住完整验证码（图片＋底部滑轨） F2 / Esc 取消")
        self.canvas.bind("<ButtonPress-1>", self.start)
        self.canvas.bind("<B1-Motion>", self.motion)
        self.canvas.bind("<ButtonRelease-1>", self.finish)
        self.window.bind("<Escape>", lambda event: self.cancel())
        self.window.deiconify()
        desktop.place(self.window, x, y, width, height)
        self.window.focus_force()

    def position(self, event):
        return (min(max(event.x, 0), self.bounds[2] - 1),
                min(max(event.y, 0), self.bounds[3] - 1))

    def start(self, event):
        self.anchor = self.position(event)
        self.motion(event)

    def motion(self, event):
        if self.anchor:
            self.canvas.coords(self.rectangle, *self.anchor, *self.position(event))

    def finish(self, event):
        if self.anchor is None or self.finished:
            return
        x1, y1 = self.anchor
        x2, y2 = self.position(event)
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        if right - left < 180 or bottom - top < 140:
            return
        if right - left > 1600 or bottom - top > 1200:
            return
        ox, oy, _, _ = self.bounds
        self.complete((left + ox, top + oy, right + ox, bottom + oy))

    def complete(self, region):
        if self.finished:
            return
        self.finished = True
        self.window.destroy()
        self.done(region)

    def cancel(self):
        self.complete(None)


class PuzzleAssistant:
    def __init__(self, root, desktop, register_hotkeys=True, region_path=None):
        self.root, self.desktop = root, desktop
        self.region_path = Path(region_path) if region_path else _project_dir() / "region.json"
        self.hotkeys = Hotkeys(desktop) if register_hotkeys else None
        self._pending, self._poll_id, self.gen = set(), None, 0
        self.state, self.closing = "idle", False
        self.region = self.shot = self.result = self.last_result = None
        self.captured_at, self.capture_bounds = 0., None
        self.selector, self.worker = None, None
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self.preview_image, self._auto = None, False
        self.status = tk.StringVar(value="按 F8 框选验证区域，F9 执行识别和拖动")
        self.details = tk.StringVar(value="所有图像识别在本机完成，不上传任何数据")
        self._build_ui()
        self.load_region()
        self.set_buttons()
        self.log("程序已就绪，等待操作")
        self._poll_id = root.after(20, self.poll)

    def _build_ui(self):
        root = self.root
        root.title("拼图助手")
        root.geometry("420x550")
        root.minsize(320, 420)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        notebook = ttk.Notebook(root)
        notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        panel = ttk.Frame(notebook, padding=12)
        notebook.add(panel, text="预览")
        log_panel = ttk.Frame(notebook)
        notebook.add(log_panel, text="日志")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)
        self.step_hint = tk.StringVar(value="第一步：按 F8 框选验证区域（仅首次）")
        ttk.Label(panel, textvariable=self.step_hint, font=("Microsoft YaHei UI", 11, "bold"), wraplength=520).grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(panel, textvariable=self.status, wraplength=520).grid(row=1, column=0, sticky="w")
        ttk.Label(panel, textvariable=self.details, wraplength=520).grid(row=2, column=0, sticky="w", pady=(0, 8))
        preview = ttk.Frame(panel)
        preview.grid(row=3, column=0, sticky="nsew")
        preview.rowconfigure(0, weight=1)
        preview.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(preview, bg="#e9edf2", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.create_text(30, 35, anchor="nw", fill="#506078", font=("Microsoft YaHei UI", 13), text="按 F8 框选验证区域\n红框：拼图缺口\n绿点：鼠标按下位置\n红点：鼠标松开位置")
        actions = ttk.Frame(panel)
        actions.grid(row=4, column=0, sticky="ew")
        self.execute_button = ttk.Button(actions, text="执行(F9)", command=self.execute)
        self.execute_button.grid(row=0, column=0, padx=4)
        ttk.Button(actions, text="取消(F2)", command=self.cancel).grid(row=0, column=1, padx=4)
        self.save_button = ttk.Button(actions, text="保存诊断", command=self.save_debug)
        self.save_button.grid(row=0, column=2, padx=4)
        log_panel.rowconfigure(0, weight=1)
        log_panel.columnconfigure(0, weight=1)
        self.log_widget = tk.Text(log_panel, wrap="word", state="disabled", font=("Microsoft YaHei UI", 10))
        self.log_widget.grid(row=0, column=0, sticky="nsew")
        self.log_widget.tag_configure("error", foreground="#b42318")
        self.log_widget.tag_configure("info", foreground="#243751")
        self.desktop.clear_cancel_history()

    def log(self, text, level="info"):
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n", level)
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def load_region(self):
        try:
            data = json.loads(self.region_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("格式错误")
            corners = (data.get("top_left"), data.get("bottom_right"))
            if any(not isinstance(p, list) or len(p) != 2 or any(type(v) is not int for v in p) for p in corners):
                raise ValueError("坐标无效")
            (l, t), (r, b) = corners
            if not (180 <= r - l <= 1600 and 140 <= b - t <= 1200):
                raise ValueError("尺寸无效")
            x, y, w, h = self.desktop.bounds()
            if not (x <= l < r <= x + w and y <= t < b <= y + h):
                raise ValueError("超出屏幕")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self.log(f"未恢复：{exc}", "error")
            return
        self.region = (l, t, r, b)
        self.step_hint.set("第二步：按 F9 一键完成")
        self.status.set(f"已恢复选区 {l},{t}")

    def save_region(self):
        if not self.region:
            return
        data = {"top_left": list(self.region[:2]), "bottom_right": list(self.region[2:])}
        try:
            tmp = self.region_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.region_path)
        except OSError as exc:
            self.log(f"保存失败：{exc}", "error")

    def set_buttons(self):
        busy = self.state in ("waiting", "selecting", "capturing", "dragging")
        self.execute_button.configure(state="normal" if self.state == "ready" else "disabled")
        self.save_button.configure(state="normal" if self.shot and not busy else "disabled")

    def later(self, delay, cb):
        gen = self.gen
        def guarded():
            self._pending.discard(i)
            if gen == self.gen and not self.closing:
                try:
                    cb()
                except Exception as exc:
                    self.fail(str(exc))
        i = self.root.after(delay, guarded)
        self._pending.add(i)

    def request_capture(self, reselect=True):
        if self.state not in ("idle", "ready") or self.closing:
            return
        if not reselect and not self.region:
            return
        self.gen += 1
        self.cancel_event.clear()
        self.desktop.clear_cancel_history()
        self.result = None
        self.state = "waiting"
        self.set_buttons()
        self.root.withdraw()
        def released():
            if self.desktop.key_down(0x77) or self.desktop.left_is_down():
                self.later(20, released)
            else:
                self.later(300, self.select_region if reselect else self.capture)
        self.later(20, released)

    def select_region(self):
        self.capture_bounds = self.desktop.bounds()
        shot = ImageGrab.grab(all_screens=True)
        if shot.size != self.capture_bounds[2:]:
            raise RuntimeError("截图尺寸不符")
        self.state = "selecting"
        self.selector = RegionSelector(self.root, self.desktop, shot, self.capture_bounds, self.selected)

    def selected(self, region):
        self.selector = None
        if not region:
            self.cancel()
            return
        self.region = region
        self.save_region()
        self.step_hint.set("第二步：按 F9 一键完成")
        self.state = "capturing"
        self.later(200, self.capture)

    def capture(self):
        self.state = "capturing"
        self.capture_bounds = self.desktop.bounds()
        self.shot = ImageGrab.grab(bbox=self.region, all_screens=True)
        self.captured_at = time.monotonic()
        self.last_result = None
        try:
            self.result = detect(self.shot)
            self.last_result = self.result
        except DetectionError:
            self.draw()
            raise
        self.state = "ready"
        self.log(f"定位：{self.result.distance}px")
        self.draw()
        if self._auto:
            self._auto = False
            self.execute()
            return
        self.status.set("已定位")
        self.show()

    def draw(self):
        if not self.shot:
            return
        self.preview_image = ImageTk.PhotoImage(self.shot)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.preview_image)
        self.canvas.configure(scrollregion=(0, 0, self.shot.width, self.shot.height))
        if not self.last_result:
            self.details.set("未定位")
            return
        g = self.last_result.gap
        self.canvas.create_rectangle(g.x, g.y, g.right, g.bottom, outline="#e03131", width=2)
        try:
            start, end = self.last_result.points()
        except DetectionError as exc:
            self.details.set(str(exc))
            return
        self.canvas.create_line(start[0], start[1], end[0], end[1], fill="#2563eb", width=2, arrow="last")
        for pt, c in ((start, "#16a34a"), (end, "#e03131")):
            self.canvas.create_oval(pt[0] - 6, pt[1] - 6, pt[0] + 6, pt[1] + 6, outline=c, width=3)
        self.details.set(f"移动{end[0] - start[0]}px")

    def execute(self):
        if self.state == "idle" and self.region:
            self._auto = True
            self.request_capture(False)
            return
        if self.state != "ready" or not self.result:
            return
        if time.monotonic() - self.captured_at > 20:
            self.fail("超时，重按F9")
            return
        try:
            pts = self.result.points(self.region[:2])
        except DetectionError as exc:
            self.status.set(str(exc))
            return
        self.state = "dragging"
        self.cancel_event.clear()
        self.set_buttons()
        self.root.withdraw()
        self.later(300, lambda: self.start_drag(pts))

    def start_drag(self, pts):
        region = self.region
        # 计算缺口在屏幕坐标系中的位置
        gap = self.result.gap
        gap_screen = (gap.x + region[0], gap.y + region[1],
                      gap.right + region[0], gap.bottom + region[1])
        def work():
            try:
                if self.cancel_event.is_set():
                    raise DragCancelled("已取消")
                w1, w2 = self.desktop.root_window(pts[0]), self.desktop.root_window(pts[1])
                if not w1 or w1 != w2:
                    raise DetectionError("起终点不在同一窗口")
                from controls import perform_drag
                from PIL import ImageGrab
                from vision import white_ratio
                perform_drag(self.desktop, pts[0], pts[1], self.cancel_event,
                             gap_screen_rect=gap_screen, grab_region=region,
                             grab=ImageGrab.grab, white_ratio=white_ratio)
                self.events.put((True, "已执行"))
            except Exception as exc:
                self.events.put((False, str(exc)))
        self.worker = threading.Thread(target=work, daemon=False)
        self.worker.start()

    def cancel(self):
        self.gen += 1
        self.cancel_event.set()
        self._auto = False
        self.result = None
        if self.selector:
            s, self.selector = self.selector, None
            s.cancel()
            return
        if self.worker and self.worker.is_alive():
            self.status.set("取消中…")
            return
        self.state = "idle"
        self.status.set("已取消")
        if not self.region:
            self.step_hint.set("第一步：按 F8 框选验证区域（仅首次）")
        self.show()

    def fail(self, text):
        self._auto = False
        self.result = None
        self.state = "idle"
        self.status.set(text)
        self.log(text, "error")
        if not self.region:
            self.step_hint.set("第一步：按 F8 框选验证区域（仅首次）")
        self.show()

    def show(self):
        if self.closing:
            return
        self.root.deiconify()
        self.root.lift()
        self.set_buttons()

    def save_debug(self):
        if not self.shot:
            return
        path = _project_dir() / "debug_last.png"
        try:
            self.shot.save(path)
            self.log(f"已保存{path}")
            self.status.set(f"已保存{path.name}")
        except OSError as exc:
            self.fail(f"保存失败：{exc}")

    def poll(self):
        if self.closing:
            return
        if self.hotkeys:
            try:
                actions = self.hotkeys.poll()
            except RuntimeError as exc:
                self.hotkeys.close()
                self.hotkeys = None
                self.cancel()
                self.fail(str(exc))
                actions = []
            if "cancel" in actions:
                self.cancel()
            else:
                for a in actions:
                    if a == "select":
                        self.request_capture()
                    elif a == "execute":
                        self.execute()
        try:
            ok, text = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.result = None
            self.state = "idle"
            self.status.set(text)
            self.log(text, "info" if ok else "error")
            self.show()
        self._poll_id = self.root.after(20, self.poll)

    def close(self):
        if not self.closing:
            self.closing = True
            self.cancel_event.set()
            self.gen += 1
            if self._poll_id:
                self.root.after_cancel(self._poll_id)
            for i in self._pending:
                self.root.after_cancel(i)
            self._pending.clear()
            if self.selector:
                self.selector.window.destroy()
                self.selector = None
        if self.worker and self.worker.is_alive():
            self.root.after(20, self.close)
            return
        try:
            self.desktop.release()
        finally:
            try:
                if self.hotkeys:
                    self.hotkeys.close()
            finally:
                self.root.destroy()


def _project_dir():
    return Path(sys.executable if getattr(sys, 'frozen', False) else __file__).parent


def main():
    parser = argparse.ArgumentParser(description="\u767d\u8272\u62fc\u56fe\u9a8c\u8bc1\u7684\u672c\u5730\u8f85\u52a9\u5de5\u5177")
    parser.add_argument("--smoke-test", action="store_true", help="\u4ec5\u542f\u52a8\u754c\u9762\u540e\u9000\u51fa\uff0c\u4e0d\u622a\u5c4f\u3001\u4e0d\u64cd\u4f5c\u9f20\u6807")
    args, _ = parser.parse_known_args()
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        app = PuzzleAssistant(root, WindowsDesktop(), register_hotkeys=not args.smoke_test)
    except Exception as exc:
        messagebox.showerror("\u542f\u52a8\u5931\u8d25", str(exc), parent=root)
        root.destroy()
        return 1
    if args.smoke_test:
        root.after(400, app.close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
