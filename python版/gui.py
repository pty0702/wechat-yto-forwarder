# -*- coding: utf-8 -*-
"""微信与圆通客户管家转发工具：配置、运行控制与实时日志。"""

import copy
import ctypes
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
import threading
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from runtime import run_session
from wechat_forwarder import DEFAULT_CONFIG, CONFIG_FILE, load_config, save_config, validate_config


PROJECT_DIR = Path(__file__).resolve().parent.parent


def initialize_dpi():
    """在创建 Tk 前与 UIA 统一 DPI 模式，避免首次检查时窗口突然缩小。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


class ConfigWindow(tk.Tk):
    def __init__(self):
        initialize_dpi()
        super().__init__()
        self.title("微信 ⇄ 圆通客户管家 · 消息转发")
        scale = max(1, self.winfo_fpixels("1i") / 96)
        self.geometry(f"{round(1000 * scale)}x{round(800 * scale)}")
        self.minsize(round(840 * scale), round(680 * scale))
        self.cfg = copy.deepcopy(DEFAULT_CONFIG)
        self._config_error = ""
        self._worker = None
        self._worker_kind = ""
        self._worker_finished = False
        self._close_requested = False
        self._events = Queue()
        self._stop_event = threading.Event()
        self._configuration_widgets = []
        self._load_config()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Control-d>", lambda event: self.on_diagnose())
        self.bind("<Control-t>", lambda event: self.on_preflight())
        self.bind("<Control-Shift-T>", lambda event: self.on_send_check())
        self.after(100, self._drain_events)
        if self._config_error:
            self.after(150, self._show_config_error)

    def _load_config(self):
        try:
            self.cfg = load_config()
            self._config_error = ""
        except (OSError, ValueError, TypeError) as exc:
            self._config_error = f"配置读取失败：{exc}"

    def _show_config_error(self):
        messagebox.showerror(
            "配置读取失败", f"{self._config_error}\n\n文件：{CONFIG_FILE}\n"
            "请修复该文件后点击“重新读取”。为避免覆盖原文件，当前禁止保存和真实运行；仍可演示。",
            parent=self,
        )

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        header = ttk.Frame(self, padding=(16, 12))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="微信 ⇄ 圆通客户管家", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        self.var_status = tk.StringVar(value="配置读取失败，可使用演示" if self._config_error else "已停止")
        ttk.Label(header, textvariable=self.var_status).pack(side="right")
        notebook = ttk.Notebook(self)
        notebook.grid(row=1, column=0, sticky="nsew", padx=14)
        config = ttk.Frame(notebook, padding=14)
        logs = ttk.Frame(notebook, padding=10)
        notebook.add(config, text="配置与运行")
        notebook.add(logs, text="运行日志")
        self.notebook = notebook
        self.log_tab = logs
        config.columnconfigure(1, weight=1)

        # 每次打开都选择演示，真实模式由用户在当前会话主动选择。
        self.var_mode = tk.StringVar(value="演示（内置样例）")
        self.var_wx = tk.StringVar()
        self.var_kf = tk.StringVar()
        self.var_staff = tk.StringVar()
        self.var_bot = tk.StringVar()
        self.var_profile = tk.StringVar()
        self.var_poll = tk.StringVar()
        self.var_timeout = tk.StringVar()
        self.var_wechat_accessibility = tk.BooleanVar(value=False)
        ttk.Label(config, text="运行模式").grid(row=0, column=0, sticky="w", pady=5)
        modes = ttk.Combobox(config, textvariable=self.var_mode,
                             values=("演示（内置样例）", "真实客户端"), state="readonly")
        modes.grid(row=0, column=1, sticky="ew", pady=5)
        self._configuration_widgets.append((modes, "readonly"))
        ttk.Label(config, text="演示使用独立样例，不依赖群名和真实客户端；下方设置仅用于保存和真实运行。",
                  foreground="#586174").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))
        fields = [
            ("微信群名", self.var_wx), ("客户管家群名", self.var_kf),
            ("被 @ 的客服名", self.var_staff), ("机器人发送者名称", self.var_bot),
        ]
        for row, (label, variable) in enumerate(fields, start=2):
            self._entry_row(config, row, label, variable)
        self._entry_row(config, 6, "适配配置 JSON", self.var_profile)
        browse = ttk.Button(config, text="选择文件…", command=self._choose_profile)
        browse.grid(row=6, column=2, padx=(8, 0))
        self._configuration_widgets.append((browse, "normal"))
        ttk.Label(config, text="真实运行需使用与当前客户端版本匹配的控件配置；先读取窗口诊断，再按说明填写。",
                  foreground="#586174").grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 6))
        compatibility = ttk.Checkbutton(
            config, text="微信4兼容（实验性，停止后恢复）", variable=self.var_wechat_accessibility,
        )
        compatibility.grid(row=8, column=0, columnspan=3, sticky="w", pady=(2, 4))
        self._configuration_widgets.append((compatibility, "normal"))
        ttk.Label(
            config, text="仅对已验证的固定微信版本临时启用控件；诊断结束或停止时恢复。演示不使用此选项。",
            foreground="#586174",
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(0, 6))
        timing = ttk.Frame(config)
        timing.grid(row=10, column=0, columnspan=3, sticky="ew", pady=4)
        for label, variable in (("轮询间隔（秒）", self.var_poll), ("无回复超时（秒）", self.var_timeout)):
            ttk.Label(timing, text=label).pack(side="left", padx=(0, 8))
            entry = ttk.Entry(timing, textvariable=variable, width=9)
            entry.pack(side="left", padx=(0, 24))
            self._configuration_widgets.append((entry, "normal"))
        keywords = ttk.Frame(config)
        keywords.grid(row=11, column=0, columnspan=3, sticky="nsew", pady=(10, 4))
        config.rowconfigure(11, weight=1)
        keywords.columnconfigure((0, 1), weight=1)
        keywords.rowconfigure(1, weight=1)
        ttk.Label(keywords, text="屏蔽词（一行一个，命中即忽略）").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(keywords, text="@ 客服词句（一行一个，命中则提醒客服）").grid(row=0, column=1, sticky="w", pady=(0, 4), padx=(12, 0))
        self.txt_block = tk.Text(keywords, height=7, width=30, wrap="word", undo=True)
        self.txt_at = tk.Text(keywords, height=7, width=30, wrap="word", undo=True)
        self.txt_block.grid(row=1, column=0, sticky="nsew")
        self.txt_at.grid(row=1, column=1, sticky="nsew", padx=(12, 0))
        self._configuration_widgets.extend([(self.txt_block, "normal"), (self.txt_at, "normal")])
        send_check = ttk.Button(config, text="单次发送验收…", command=self.on_send_check)
        send_check.grid(row=12, column=0, columnspan=3, sticky="e", pady=(8, 0))
        self._configuration_widgets.append((send_check, "normal"))

        logs.columnconfigure(0, weight=1)
        logs.rowconfigure(1, weight=1)
        ttk.Label(logs, text="日志只显示本次窗口会话，最多保留约 3000 行。诊断文件可能包含当前可见聊天内容。",
                  foreground="#586174").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.txt_log = tk.Text(logs, state="disabled", wrap="word", font=("Microsoft YaHei UI", 10))
        self.txt_log.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(logs, orient="vertical", command=self.txt_log.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=scrollbar.set)
        ttk.Button(logs, text="清空日志", command=self._clear_log).grid(row=2, column=0, sticky="e", pady=(8, 0))

        footer = ttk.Frame(self, padding=(14, 12))
        footer.grid(row=2, column=0, sticky="ew")
        self.btn_reload = ttk.Button(footer, text="重新读取", command=self.on_reload)
        self.btn_reload.pack(side="left")
        self.btn_save = ttk.Button(footer, text="保存配置", command=self.on_save)
        self.btn_save.pack(side="left", padx=6)
        self.btn_diagnose = ttk.Button(footer, text="窗口诊断 (Ctrl+D)", command=self.on_diagnose)
        self.btn_diagnose.pack(side="left")
        self.btn_preflight = ttk.Button(footer, text="适配检查 (Ctrl+T)", command=self.on_preflight)
        self.btn_preflight.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(footer, text="停止", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="right", padx=(6, 0))
        self.btn_start = ttk.Button(footer, text="开始运行", command=self.on_start)
        self.btn_start.pack(side="right")
        ttk.Label(self, text=f"配置文件：{CONFIG_FILE}", foreground="#586174", padding=(14, 0, 14, 8)).grid(row=3, column=0, sticky="w")
        self._fill_form()

    def _entry_row(self, parent, row, label, variable):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        self._configuration_widgets.append((entry, "normal"))

    def _fill_form(self):
        for variable, key, default in (
            (self.var_wx, "wx_group", ""), (self.var_kf, "kf_group", ""),
            (self.var_staff, "service_staff", ""), (self.var_bot, "bot_name", "小圆-总公司"),
            (self.var_profile, "adapter_profile", ""), (self.var_poll, "poll_interval", 3),
            (self.var_timeout, "reset_seconds", 30),
        ):
            variable.set(self.cfg.get(key, default))
        self.var_wechat_accessibility.set(self.cfg.get("wechat_accessibility", False) is True)
        for widget, key in ((self.txt_block, "block_keywords"), (self.txt_at, "at_keywords")):
            widget.delete("1.0", "end")
            widget.insert("1.0", "\n".join(self.cfg.get(key, [])))

    @staticmethod
    def _lines(widget):
        return [line.strip() for line in widget.get("1.0", "end").splitlines() if line.strip()]

    def _collect(self, live=False):
        cfg = copy.deepcopy(self.cfg)
        try:
            poll = float(self.var_poll.get().strip())
            timeout = float(self.var_timeout.get().strip())
        except ValueError as exc:
            raise ValueError("轮询间隔和无回复超时必须填写数字") from exc
        profile = self.var_profile.get().strip()
        if profile:
            path = Path(profile).expanduser()
            profile = str((PROJECT_DIR / path).resolve() if not path.is_absolute() else path.resolve())
        cfg.update({
            "mode": "live" if self.var_mode.get() == "真实客户端" else "demo",
            "wx_group": self.var_wx.get().strip(), "kf_group": self.var_kf.get().strip(),
            "service_staff": self.var_staff.get().strip(), "bot_name": self.var_bot.get().strip(),
            "block_keywords": self._lines(self.txt_block), "at_keywords": self._lines(self.txt_at),
            "adapter_profile": profile, "poll_interval": poll, "reset_seconds": timeout,
            "wechat_accessibility": bool(self.var_wechat_accessibility.get()),
        })
        cfg = validate_config(cfg, live=live)
        if live and (not profile or not Path(profile).is_file()):
            raise ValueError("请先选择存在的适配配置 JSON 文件；可用“窗口诊断”获取当前窗口控件信息")
        return cfg

    def _choose_profile(self):
        selected = filedialog.askopenfilename(
            title="选择当前客户端对应的适配配置", initialdir=str(PROJECT_DIR),
            filetypes=[("JSON 配置", "*.json"), ("所有文件", "*.*")], parent=self,
        )
        if selected:
            self.var_profile.set(selected)

    def on_reload(self):
        if self._worker:
            return
        self._load_config()
        if self._config_error:
            self._show_config_error()
            self.var_status.set("配置读取失败，可使用演示")
            return
        self._fill_form()
        self.var_status.set("配置已重新读取")
        self._append_log("[配置] 已重新读取配置文件。")

    def on_save(self):
        if self._worker:
            return
        if self._config_error:
            self._show_config_error()
            return
        try:
            cfg = self._collect()
            save_config(cfg)
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self.cfg = cfg
        self._append_log(f"[配置] 已保存：{CONFIG_FILE}")
        self.var_status.set("配置已保存")

    def on_start(self):
        if self._worker or self._close_requested:
            return
        live = self.var_mode.get() == "真实客户端"
        if live and self._config_error:
            self._show_config_error()
            return
        try:
            # 演示不读取表单参数，未填写真实配置时同样可以验证流程。
            cfg = self._collect(live=True) if live else {"mode": "demo"}
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("无法启动", str(exc), parent=self)
            return
        self._stop_event = threading.Event()
        self._start_worker("运行", lambda: run_session(cfg, self._stop_event, self._queue_log))
        self.var_status.set("真实客户端运行任务中" if live else "演示运行中")

    def on_diagnose(self):
        if self._worker or self._close_requested:
            return
        enable_wechat = bool(self.var_wechat_accessibility.get())
        self._stop_event = threading.Event()

        def action():
            from windows_adapter import diagnose
            output = diagnose(
                PROJECT_DIR / "diagnostics", self._queue_log,
                enable_wechat=enable_wechat, stop_event=self._stop_event, capture_delay=5,
            )
            self._queue_log(f"[诊断] 已保存：{output}" if output else "[诊断] 已取消。")

        self._start_worker("诊断", action)
        self.var_status.set("正在临时启用微信4兼容并诊断" if enable_wechat else "正在读取窗口控件信息")

    def on_preflight(self):
        if self._worker or self._close_requested:
            return
        if self._config_error:
            self._show_config_error()
            return
        try:
            cfg = self._collect(live=True)
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("无法检查", str(exc), parent=self)
            return
        self._start_live_check(cfg)

    def on_send_check(self):
        if self._worker or self._close_requested:
            return
        if self._config_error:
            self._show_config_error()
            return
        try:
            cfg = self._collect(live=True)
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("无法验收", str(exc), parent=self)
            return
        from live_checks import TEST_MESSAGE
        dialog = tk.Toplevel(self)
        dialog.title("单次发送验收")
        dialog.transient(self)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="向选定群发送一条测试消息，核对本账号的发送回显。",
                  wraplength=520).pack(anchor="w", pady=(0, 10))
        side = tk.StringVar(value="wx")
        mention = tk.BooleanVar(value=False)
        ttk.Radiobutton(body, text=f"微信：{cfg['wx_group']}", variable=side,
                        value="wx").pack(anchor="w", pady=4)
        ttk.Radiobutton(body, text=f"客户管家：{cfg['kf_group']}", variable=side,
                        value="kf").pack(anchor="w", pady=4)
        mention_box = ttk.Checkbutton(body, text=f"同时真实 @ 微信客服：{cfg['service_staff']}",
                                      variable=mention)
        mention_box.pack(anchor="w", pady=4)
        preview = tk.StringVar()
        ttk.Label(body, textvariable=preview, wraplength=520,
                  padding=(0, 14)).pack(anchor="w")

        def refresh_preview(*_):
            if side.get() != "wx":
                if mention.get():
                    mention.set(False)
                mention_box.configure(state="disabled")
            else:
                mention_box.configure(state="normal")
            prefix = f"@{cfg['service_staff']} " if mention.get() else ""
            preview.set(f"消息内容：\n{prefix}{TEST_MESSAGE}")

        side.trace_add("write", refresh_preview)
        mention.trace_add("write", refresh_preview)
        refresh_preview()
        ttk.Label(body, text="请保持两个目标群打开、输入框为空。验收结束后停止，并保存本地结果报告。",
                  wraplength=520).pack(anchor="w", pady=(0, 14))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")

        def confirm():
            selected = side.get()
            with_mention = mention.get()
            dialog.destroy()
            self._start_live_check(cfg, side=selected, mention=with_mention)

        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left")
        ttk.Button(buttons, text="发送这一条", command=confirm).pack(side="right")
        dialog.update_idletasks()
        x = max(0, self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2)
        y = max(0, self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()

    def _start_live_check(self, cfg, side=None, mention=False):
        self._stop_event = threading.Event()

        def action():
            from live_checks import run_live_check
            run_live_check(cfg, PROJECT_DIR / "diagnostics", self._stop_event,
                           self._queue_log, side=side, mention=mention)

        self._start_worker("验收" if side else "检查", action)
        self.var_status.set("正在验收单条发送" if side else "正在检查真实客户端适配，不发送消息")

    def _start_worker(self, kind, action):
        self._worker_kind = kind
        self._worker_finished = False

        def worker():
            try:
                action()
            except Exception as exc:
                self._events.put(("error", f"{type(exc).__name__}：{exc}"))
            finally:
                self._events.put(("finished", ""))

        self._worker = threading.Thread(target=worker, name="消息转发" if kind == "运行" else "窗口诊断", daemon=False)
        self._set_busy(True)
        self.notebook.select(self.log_tab)
        self._append_log(f"[{kind}] 任务开始。")
        self._worker.start()

    def _set_busy(self, busy):
        for widget, normal_state in self._configuration_widgets:
            widget.configure(state="disabled" if busy else normal_state)
        for button in (self.btn_start, self.btn_save, self.btn_reload, self.btn_diagnose, self.btn_preflight):
            button.configure(state="disabled" if busy else "normal")
        self.btn_stop.configure(state="normal" if busy and self._worker_kind in ("运行", "诊断", "检查", "验收") else "disabled")

    def on_stop(self):
        if self._worker and self._worker_kind in ("运行", "诊断", "检查", "验收"):
            self._stop_event.set()
            self.btn_stop.configure(state="disabled")
            self.var_status.set("正在停止，等待当前操作结束…")
            self._append_log("[停止] 已请求停止，等待当前任务结束并恢复本次兼容状态。")

    def on_close(self):
        if self._worker:
            self._close_requested = True
            self._stop_event.set()
            self._set_busy(True)
            self.btn_stop.configure(state="disabled")
            self.var_status.set("正在退出，等待工作线程结束…")
        else:
            self.destroy()

    def _queue_log(self, line):
        # 工作线程只投递队列，不调用 Tk，包括 after()。
        self._events.put(("log", str(line)))

    def _drain_events(self):
        for _ in range(250):
            try:
                kind, text = self._events.get_nowait()
            except Empty:
                break
            if kind == "log":
                self._append_log(text)
            elif kind == "error":
                self._append_log(f"[错误] {text}")
                if not self._close_requested:
                    messagebox.showerror(f"{self._worker_kind}已中止", text, parent=self)
            elif kind == "finished":
                self._worker_finished = True
        if self._worker and self._worker_finished and not self._worker.is_alive():
            self._worker.join(timeout=0)
            kind = self._worker_kind
            self._worker = None
            self._worker_kind = ""
            self._worker_finished = False
            self._append_log(f"[{kind}] 任务已结束。")
            if self._close_requested:
                self.destroy()
                return
            self._set_busy(False)
            self.var_status.set("已停止")
        self.after(100, self._drain_events)

    def _append_log(self, text):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"{datetime.now():%H:%M:%S} {text}\n")
        lines = int(self.txt_log.index("end-1c").split(".")[0])
        if lines > 3000:
            self.txt_log.delete("1.0", f"{lines - 2800}.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _clear_log(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")


if __name__ == "__main__":
    ConfigWindow().mainloop()
