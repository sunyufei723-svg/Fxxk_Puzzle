"""托盘侧的设置窗口。

完全静默时业务界面从不露面，settings.json 就没有任何入口能改；这个窗口跑在
launcher 进程里，不依赖业务子进程，改完直接落盘并把新值回报给主循环。
"""
import json
import threading
import tkinter as tk
from tkinter import ttk

# 与 app/main.py 的同名常量保持一致：那边读写，这边改，文件只有这一份。
SPEED_MIN, SPEED_MAX, SPEED_STEP = 100, 3000, 50
DEFAULT_SPEED = 600
DEFAULT_SHOW_WINDOW = False
FUNCTION_KEYS = [f"F{i}" for i in range(1, 13)]
DEFAULT_HOTKEYS = {"select": "F8", "execute": "F9", "cancel": "F2"}
ACTION_LABELS = (("select", "截图 / 识别"), ("execute", "识别 + 执行"), ("cancel", "取消"))


def valid_speed(value):
    return isinstance(value, int) and SPEED_MIN <= value <= SPEED_MAX


def parse(data):
    """把 settings.json 的内容整理成 (speed, hotkeys, show_window)，非法值退回默认。"""
    speed, show_window = DEFAULT_SPEED, DEFAULT_SHOW_WINDOW
    hotkeys = dict(DEFAULT_HOTKEYS)
    if isinstance(data, dict):
        if valid_speed(data.get("drag_speed")):
            speed = data["drag_speed"]
        configured = data.get("hotkeys")
        if isinstance(configured, dict):
            for action, _ in ACTION_LABELS:
                name = configured.get(action)
                if isinstance(name, str) and name in FUNCTION_KEYS:
                    hotkeys[action] = name
        if isinstance(data.get("show_window_on_target"), bool):
            show_window = data["show_window_on_target"]
    return speed, hotkeys, show_window


def load(path):
    try:
        return parse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return parse(None)


def hotkeys(path, configured):
    """安装向导选的快捷键写在 config.json 里；用户在设置窗口改过一次之后，
    settings.json 才是准的，否则每次启动都会被 config.json 盖回去。"""
    if not path.exists():
        return parse({'hotkeys': configured})[1]
    return load(path)[1]


def save(path, speed, hotkeys, show_window):
    """原子写，键集合与 app/main.py 的 _save_settings 一致，别留多余键。"""
    data = {"drag_speed": speed, "hotkeys": dict(hotkeys),
            "show_window_on_target": bool(show_window)}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class _Dialog:
    def __init__(self, root, path, events):
        self.root, self.path, self.events = root, path, events
        speed, hotkeys, show_window = load(path)
        self.names = dict(hotkeys)
        root.title("拼图助手 设置")
        root.geometry("420x250")
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        panel = ttk.Frame(root, padding=14)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="拖动速度").grid(row=0, column=0, sticky="w")
        self.drag_speed = speed
        self.speed_value = tk.StringVar(value=f"{speed} px/s")
        scale = ttk.Scale(panel, from_=SPEED_MIN, to=SPEED_MAX, orient="horizontal",
                          command=self._on_speed)
        scale.set(speed)
        scale.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        ttk.Label(panel, textvariable=self.speed_value, width=10).grid(row=0, column=2, sticky="w")

        self.keys = {}
        for row, (action, label) in enumerate(ACTION_LABELS, start=1):
            ttk.Label(panel, text=f"{label} 快捷键").grid(row=row, column=0, sticky="w", pady=4)
            # StringVar 必须挂在 self 上：被 GC 时 tkinter 会连带 unset，下拉框就空了
            var = tk.StringVar(value=self.names[action])
            ttk.Combobox(panel, textvariable=var, values=FUNCTION_KEYS,
                         state="readonly", width=8).grid(row=row, column=1, sticky="w", pady=4)
            self.keys[action] = var

        self.show_window = tk.BooleanVar(value=show_window)
        ttk.Checkbutton(panel, text="检测到目标页面时自动弹出操作窗口（取消勾选则完全静默）",
                        variable=self.show_window).grid(row=4, column=0, columnspan=3,
                                                        sticky="w", pady=(10, 0))
        self.message = tk.StringVar(value="所有设置都写进 settings.json，保存后立即生效")
        ttk.Label(panel, textvariable=self.message, foreground="#506078",
                  wraplength=390).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        actions = ttk.Frame(panel)
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="保存", command=self.save).pack(side="right")
        ttk.Button(actions, text="关闭", command=self.close).pack(side="right", padx=6)

    def _on_speed(self, value):
        stepped = round(float(value) / SPEED_STEP) * SPEED_STEP
        self.drag_speed = min(SPEED_MAX, max(SPEED_MIN, stepped))
        self.speed_value.set(f"{self.drag_speed} px/s")

    def save(self):
        names = {action: var.get() for action, var in self.keys.items()}
        if len(set(names.values())) != len(names):
            self.message.set("两个动作用了同一个快捷键，没有保存")
            return
        save(self.path, self.drag_speed, names, bool(self.show_window.get()))
        self.events.put(("settings", names))
        self.close()

    def close(self):
        self.root.destroy()


_open = False


def open_async(path, events):
    """在独立线程里跑 Tk 主循环：watcher 主循环要持续轮询地址栏，不能被对话框挡住。"""
    global _open
    if _open:
        return False
    _open = True

    def run():
        global _open
        try:
            root = tk.Tk()
            _Dialog(root, path, events)
            root.mainloop()
        finally:
            _open = False

    threading.Thread(target=run, daemon=True).start()
    return True
