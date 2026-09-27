"""入口：框选区域、识别缺口、确认后单次拖动。"""

import argparse
import ctypes
import json
import queue
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox
from PIL import ImageTk, ImageGrab

from app.browser import (TargetPageGuard, foreground_browser_info, is_settings_uri,
                         show_window_from_launch_uri, target_from_launch_uri)
from app.controls import (WindowsDesktop, enable_dpi_awareness, Hotkeys,
                          DragCancelled, vk_for_function_key)
from app.vision import detect, DetectionError, white_ratio

# 可自定义的运行参数（两个平台各自维护一份，互不共享）。
FUNCTION_KEYS = [f"F{i}" for i in range(1, 13)]
ACTIONS = ("select", "execute", "cancel")
# 空闲态：只有这两个状态允许开始新一轮截图/识别，其余状态一律视为忙
IDLE_STATES = ("idle", "ready")
SPEED_MIN, SPEED_MAX, SPEED_STEP = 100, 3000, 50
DEFAULT_SPEED = 600
DEFAULT_HOTKEYS = {"select": "F8", "execute": "F9", "cancel": "F2"}


def _valid_size(width, height):
    """框选器接受的尺寸区间，与 load_region 恢复选区时的校验必须一致。"""
    return 180 <= width <= 1600 and 140 <= height <= 1200


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
                                text="框住完整验证码（图片＋底部滑轨） 按取消键 / Esc 取消")
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
        if not _valid_size(right - left, bottom - top):
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
    def __init__(self, root, desktop, register_hotkeys=True, region_path=None,
                 page_guard=None, monitor_mode=False, settings_path=None):
        self.root, self.desktop = root, desktop
        self.region_path = Path(region_path) if region_path else _project_dir() / "region.json"
        self.settings_path = Path(settings_path) if settings_path else _project_dir() / "settings.json"
        self.register_hotkeys = register_hotkeys
        self.page_guard = page_guard
        # monitor_mode 只决定没存过设置时的默认值：Monitor 版后台常驻，默认静默；
        # Extension 版可能被直接双击打开，默认得看得见窗口。「是否弹窗」这一项
        # 两版都能在程序设置页和浏览器扩展里改，落的是同一个 settings.json 键。
        self.monitor_mode = monitor_mode
        self.drag_speed, self.hotkey_names, self.show_window_on_target = self._load_settings()
        self._apply_hotkey_config()
        self.hotkeys = self._make_hotkeys() if register_hotkeys else None
        self._pending, self._poll_id, self.gen = set(), None, 0
        self.state, self.closing = "idle", False
        self.region = self.shot = self.result = self.last_result = None
        self.captured_at, self.capture_bounds = 0., None
        self.selector, self.worker = None, None
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self.preview_image, self._auto_drag, self._select_only = None, False, False
        self.status = tk.StringVar(value=self._status_hint())
        self.details = tk.StringVar(value="所有图像识别在本机完成，不上传任何数据")
        self._build_ui()
        self.load_region()
        self.set_buttons()
        self.log("程序已就绪，等待操作")
        self._poll_id = root.after(20, self.poll)

    # ---- 配置读写：region.json 与 settings.json 都走同一套原子写 ----
    def _write_json(self, path, data, error):
        try:
            _write_json_atomic(path, data)
        except OSError as exc:
            self.log(f"{error}：{exc}", "error")

    # ---- 设置：拖动速度 + 可改快捷键（持久化到 settings.json） ----
    def _load_settings(self):
        speed, hotkeys = DEFAULT_SPEED, dict(DEFAULT_HOTKEYS)
        # 默认值按版本分：Monitor 版是后台常驻，默认静默；extension 版可能被用户
        # 直接双击程序打开，默认不弹的话就完全没有反馈了。
        show_window = not self.monitor_mode
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                s = data.get("drag_speed")
                if isinstance(s, int) and SPEED_MIN <= s <= SPEED_MAX:
                    speed = s
                hk = data.get("hotkeys")
                if isinstance(hk, dict):
                    for action in ACTIONS:
                        v = hk.get(action)
                        if isinstance(v, str) and v in FUNCTION_KEYS:
                            hotkeys[action] = v
                flag = data.get("show_window_on_target")
                if isinstance(flag, bool):
                    show_window = flag
        except (OSError, ValueError):
            pass
        return speed, hotkeys, show_window

    def _save_settings(self):
        data = {"drag_speed": self.drag_speed, "hotkeys": dict(self.hotkey_names),
                "show_window_on_target": bool(self.show_window_on_target)}
        self._write_json(self.settings_path, data, "设置保存失败")

    def _apply_hotkey_config(self):
        self.select_vk = vk_for_function_key(self.hotkey_names["select"])
        self.desktop.cancel_vk = vk_for_function_key(self.hotkey_names["cancel"])

    def _make_hotkeys(self):
        keys = [(vk_for_function_key(self.hotkey_names[a]), a) for a in ACTIONS]
        return Hotkeys(self.desktop, keys)

    def _rebuild_hotkeys(self):
        if not self.register_hotkeys:
            return
        if self.hotkeys:
            try:
                self.hotkeys.close()
            except Exception:
                pass
            self.hotkeys = None
        try:
            self.hotkeys = self._make_hotkeys()
        except Exception as exc:
            self.fail(f"热键注册失败：{exc}")

    def _status_hint(self):
        h = self.hotkey_names
        return f"{h['select']} 截图+识别，{h['execute']} 识别+执行，{h['cancel']} 取消"

    def _key(self, action):
        # 回退到默认值：使未走 __init__ 的部分构造（如单测 __new__）也能安全取键名。
        return getattr(self, "hotkey_names", DEFAULT_HOTKEYS)[action]

    def _hint1(self):
        return f"第一步：按 {self._key('select')} 框选验证区域（仅首次）"

    def _hint2(self):
        return f"第二步：按 {self._key('execute')} 一键完成"

    def _set_hint(self, text):
        hint = getattr(self, "step_hint", None)
        if hint is not None:
            hint.set(text)

    def _preview_hint_text(self):
        return (f"按 {self.hotkey_names['select']} 框选验证区域\n红框：拼图缺口\n"
                f"绿点：鼠标按下位置\n红点：鼠标松开位置")

    def _refresh_hotkey_labels(self):
        try:
            self.status.set(self._status_hint())
            self.cancel_button.configure(text=f"取消({self.hotkey_names['cancel']})")
            self.canvas.itemconfigure(self._preview_hint_id, text=self._preview_hint_text())
        except (AttributeError, tk.TclError):
            pass

    def _on_speed(self, value):
        stepped = round(float(value) / SPEED_STEP) * SPEED_STEP
        self.drag_speed = min(SPEED_MAX, max(SPEED_MIN, stepped))
        self.speed_value.set(f"{self.drag_speed} px/s")

    def _on_hotkey(self, action, combobox):
        name = combobox.get()
        if name == self.hotkey_names[action]:
            return
        for other in self.hotkey_vars:
            if other != action and self.hotkey_names[other] == name:
                combobox.set(self.hotkey_names[action])
                self.log(f"{name} 已被「{other}」占用，未修改", "error")
                return
        self.hotkey_names[action] = name
        self._apply_hotkey_config()
        self._refresh_hotkey_labels()
        self._rebuild_hotkeys()
        self._save_settings()
        self.log(f"{action} 快捷键已改为 {name}")

    def _build_ui(self):
        root = self.root
        root.title("拼图助手")
        root.geometry("420x550")
        root.minsize(320, 420)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        try:
            base = _project_dir()
            icon_path = base / "icon16.png"
            if not icon_path.exists() and getattr(sys, 'frozen', False):
                icon_path = base / "_internal" / "icon16.png"
            # PhotoImage 必须挂在 self 上：被 GC 后 Tk 会连带删掉图标
            self._icon_ref = ImageTk.PhotoImage(file=str(icon_path))
            root.iconphoto(True, self._icon_ref)
        except Exception:
            pass
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        notebook = ttk.Notebook(root)
        notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        panel = ttk.Frame(notebook, padding=12)
        notebook.add(panel, text="预览")
        log_panel = ttk.Frame(notebook)
        notebook.add(log_panel, text="日志")
        self._build_settings_tab(notebook)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)
        self.step_hint = tk.StringVar(value=self._hint1())
        ttk.Label(panel, textvariable=self.step_hint, font=("Microsoft YaHei UI", 11, "bold"), wraplength=520).grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(panel, textvariable=self.status, wraplength=520).grid(row=1, column=0, sticky="w")
        ttk.Label(panel, textvariable=self.details, wraplength=520).grid(row=2, column=0, sticky="w", pady=(0, 8))
        preview = ttk.Frame(panel)
        preview.grid(row=3, column=0, sticky="nsew")
        preview.rowconfigure(0, weight=1)
        preview.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(preview, bg="#e9edf2", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self._preview_hint_id = self.canvas.create_text(30, 35, anchor="nw", fill="#506078", font=("Microsoft YaHei UI", 13), text=self._preview_hint_text())
        actions = ttk.Frame(panel)
        actions.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        row1 = ttk.Frame(actions)
        row1.grid(row=0, column=0, sticky="ew")
        row2 = ttk.Frame(actions)
        row2.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.capture_button = ttk.Button(row1, text="截图", command=self._on_select)
        self.capture_button.grid(row=0, column=0, padx=4)
        self.recognize_button = ttk.Button(row1, text="识别", command=self.recognize)
        self.recognize_button.grid(row=0, column=1, padx=4)
        self.execute_button = ttk.Button(row2, text="执行", command=self.execute)
        self.execute_button.grid(row=0, column=0, padx=4)
        self.cancel_button = ttk.Button(row2, text=f"取消({self.hotkey_names['cancel']})", command=self.cancel)
        self.cancel_button.grid(row=0, column=1, padx=4)
        log_panel.rowconfigure(0, weight=1)
        log_panel.columnconfigure(0, weight=1)
        self.log_widget = tk.Text(log_panel, wrap="word", state="disabled", font=("Microsoft YaHei UI", 10))
        self.log_widget.grid(row=0, column=0, sticky="nsew")
        log_actions = ttk.Frame(log_panel)
        log_actions.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        self.save_button = ttk.Button(log_actions, text="保存诊断", command=self.save_debug)
        self.save_button.grid(row=0, column=0)
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
            if not _valid_size(r - l, b - t):
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
        self._set_hint(self._hint2())
        self.status.set(f"已恢复选区 {l},{t}")

    def save_region(self):
        if not self.region:
            return
        data = {"top_left": list(self.region[:2]), "bottom_right": list(self.region[2:])}
        self._write_json(self.region_path, data, "保存失败")

    def set_buttons(self):
        idle = self.state in IDLE_STATES
        self.capture_button.configure(state="normal" if idle else "disabled")
        self.recognize_button.configure(state="normal" if idle else "disabled")
        self.execute_button.configure(state="normal" if self.result else "disabled")
        self.save_button.configure(state="normal" if self.shot and idle else "disabled")

    def _build_settings_tab(self, notebook):
        panel = ttk.Frame(notebook, padding=12)
        notebook.add(panel, text="设置")
        panel.columnconfigure(1, weight=1)
        ttk.Label(panel, text="拖动速度").grid(row=0, column=0, sticky="w")
        self.speed_value = tk.StringVar(value=f"{self.drag_speed} px/s")
        self.speed_scale = ttk.Scale(panel, from_=SPEED_MIN, to=SPEED_MAX,
                                     orient="horizontal", command=self._on_speed)
        self.speed_scale.set(self.drag_speed)
        self.speed_scale.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        self.speed_scale.bind("<ButtonRelease-1>", lambda e: self._save_settings())
        ttk.Label(panel, textvariable=self.speed_value, width=10).grid(row=0, column=2, sticky="w")
        rows = (("select", "截图 / 识别"), ("execute", "识别 + 执行"), ("cancel", "取消"))
        self.hotkey_vars = {}
        for i, (action, label) in enumerate(rows):
            ttk.Label(panel, text=f"{label} 快捷键").grid(row=i + 1, column=0, sticky="w", pady=4)
            # StringVar 必须挂在 self 上：被 GC 时 tkinter 会连带 unset，下拉框就空了
            var = tk.StringVar(value=self.hotkey_names[action])
            cb = ttk.Combobox(panel, textvariable=var, values=FUNCTION_KEYS, state="readonly", width=8)
            cb.grid(row=i + 1, column=1, sticky="w", pady=4)
            cb.bind("<<ComboboxSelected>>", lambda e, a=action, c=cb: self._on_hotkey(a, c))
            self.hotkey_vars[action] = var
        hint_row = len(rows) + 1
        # BooleanVar 同样必须挂在 self 上，否则被 GC 后勾选框会失焦复位。
        self.show_window_var = tk.BooleanVar(value=self.show_window_on_target)
        ttk.Checkbutton(
            panel, text="进入验证码页面时自动弹出操作窗口（取消勾选＝静默，用 F8/F9 操作）",
            variable=self.show_window_var, command=self._on_show_window).grid(
            row=hint_row, column=0, columnspan=3, sticky="w", pady=(10, 0))
        hint_row += 1
        ttk.Label(panel, text="快捷键仅支持 F1~F12；改后立即生效并自动保存。",
                  foreground="#506078").grid(row=hint_row, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _on_show_window(self):
        self.show_window_on_target = bool(self.show_window_var.get())
        self._save_settings()
        self.log("已开启自动弹出操作窗口" if self.show_window_on_target
                 else "已改为静默启动：进入页面时不再弹窗，用 F8/F9 操作", "info")

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

    def _on_select(self):
        self._select_only = True
        self._auto_drag = False
        self.request_capture()

    def recognize(self):
        self._select_only = False
        self._auto_drag = False
        self.request_capture(False)

    def request_capture(self, reselect=True):
        if self.closing or self.state not in IDLE_STATES:
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
            if self.desktop.key_down(self.select_vk) or self.desktop.left_is_down():
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
        self._set_hint(self._hint2())
        # getattr 兜底同 _key：单测用 __new__ 构造的实例没有 _select_only
        if getattr(self, "_select_only", False):
            self._select_only = False
            self.state = "idle"
            self.status.set(f"已保存选区 {region[0]},{region[1]}")
            self.show()
            return
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
        if self._auto_drag:
            self._auto_drag = False
            self.later(20, self.execute)
            return
        self.status.set("已定位")
        self.show()

    def draw(self):
        if not self.shot:
            return
        self.preview_image = ImageTk.PhotoImage(self.shot, master=self.canvas)
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
        if not self.result:
            return
        if time.monotonic() - self.captured_at > 20:
            self.fail(f"超时，重按{self._key('execute')}")
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
                # 延迟导入：让 patch("app.controls.perform_drag") 能拦到这里
                from app.controls import perform_drag
                perform_drag(self.desktop, pts[0], pts[1], self.cancel_event,
                             gap_screen_rect=gap_screen, grab_region=region,
                             grab=ImageGrab.grab, white_ratio=white_ratio,
                             speed=self.drag_speed)
                self.events.put((True, "已执行"))
            except Exception as exc:
                self.events.put((False, str(exc)))
        self.worker = threading.Thread(target=work, daemon=False)
        self.worker.start()

    def cancel(self):
        self.gen += 1
        self.cancel_event.set()
        self._auto_drag = False
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
            self._set_hint(self._hint1())
        self.show()

    def fail(self, text):
        self._auto_drag = False
        self.result = None
        self.state = "idle"
        self.status.set(text)
        self.log(text, "error")
        self.details.set("异常，请切换到「日志」页保存诊断截图")
        if not self.region:
            self._set_hint(self._hint1())
        self.show(force=True)

    def show(self, force=False):
        if self.closing:
            return
        if not force and not self.show_window_on_target:
            # 静默启动：识别完成、框选、取消都不弹预览，只刷新按钮状态；
            # 出问题时 fail() 会带 force 把窗口叫出来，别让用户摸不着头脑。
            self.set_buttons()
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
        # 当前回调已经开始执行，不能再把它当作待取消的 after 任务。
        self._poll_id = None
        if self.closing:
            return
        if self.page_guard and self.page_guard.should_close():
            self.close()
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
                        self._on_select()
                    elif a == "execute":
                        self._auto_drag = True
                        self.request_capture(False)
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


def _write_json_atomic(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def persist_show_window(value, path=None):
    """把扩展 popup 推过来的静默开关写进 settings.json。

    只改这一个键，其余原样保留。即使已经有一个实例在跑也照写：那个实例下次
    启动就会读到。这样「程序里改」和「扩展里改」落的是同一份设置，谁都不会在
    下次拉起时被对方悄悄覆盖回去。
    """
    path = path or _project_dir() / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["show_window_on_target"] = bool(value)
    _write_json_atomic(path, data)


def _acquire_single_instance():
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.restype = wintypes.DWORD
    handle = kernel32.CreateMutexW(None, False, "Local\\FxxkPuzzleExtension")
    if not handle:
        raise ctypes.WinError()
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return None
    return handle


def main():
    parser = argparse.ArgumentParser(description="白色拼图验证的本地辅助工具")
    parser.add_argument("--smoke-test", action="store_true",
                        help="仅启动界面后退出，不截屏、不操作鼠标")
    parser.add_argument("launch_uri", nargs="?")
    args, _ = parser.parse_known_args()
    enable_dpi_awareness()
    # 扩展 popup 的开关走这条路进来：先落盘，再决定要不要起界面。已经有实例在跑
    # 也得写，否则「扩展里改了没生效」。
    show_window = show_window_from_launch_uri(args.launch_uri)
    if show_window is not None:
        persist_show_window(show_window)
    instance = _acquire_single_instance()
    if instance is None:
        return 0
    try:
        if is_settings_uri(args.launch_uri):
            return 0            # 只是来改设置的，不弹操作界面
        target_url = target_from_launch_uri(args.launch_uri)
        page_guard = None
        if target_url:
            # 记录启动它的浏览器窗口，用于离开目标页后自动退出
            browser_info = foreground_browser_info()
            page_guard = TargetPageGuard(target_url, browser_info[0] if browser_info else None)
        root = tk.Tk()
        # 先藏起来再建界面：静默模式下不闪一下，非静默也只是晚一帧出现。
        root.withdraw()
        try:
            app = PuzzleAssistant(
                root,
                WindowsDesktop(),
                register_hotkeys=not args.smoke_test,
                page_guard=page_guard,
            )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc), parent=root)
            root.destroy()
            return 1
        if args.smoke_test:
            root.after(400, app.close)
        elif app.show_window_on_target:
            app.show()
            if app.region:
                # 有选区文件就不必再点一次「识别」：启动即自动截图定位一次。
                # 「识别」按钮保留，用于换题或这次没定位到时重来。
                app.recognize()
        # 静默启动时窗口保持隐藏：F8/F9/F2 全局热键和页面守卫照常工作。
        root.mainloop()
        return 0
    finally:
        ctypes.windll.kernel32.CloseHandle(instance)


if __name__ == "__main__":
    raise SystemExit(main())
