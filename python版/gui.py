# -*- coding: utf-8 -*-
"""微信与圆通客户管家转发工具：配置、运行控制与实时日志。"""

import copy
import json
import ctypes
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
import threading
import sys
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from runtime import run_session
from wechat_forwarder import DEFAULT_CONFIG, CONFIG_FILE, load_config, save_config, validate_config
import license_client
import setup_wizard


PROJECT_DIR = Path(__file__).resolve().parent.parent
APP_VERSION = "2.1"   # 交付版本：每轮打包递增，客户报障时对版本
LICENSE_CHECK_INTERVAL = 10 * 60 * 1000   # 运行期每 10 分钟复查一次卡密
ERROR_LOG = PROJECT_DIR / "logs" / "error.log"


def _write_error_log(text: str) -> None:
    """错误详情落盘：打包版无控制台，弹窗一关就拿不到 traceback。"""
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a", encoding="utf-8") as stream:
            stream.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n{text}")
    except OSError:
        pass


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
        self.title(f"微信 ⇄ 圆通客户管家 · 消息转发 v{APP_VERSION}")
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
        self.var_kf_self = tk.StringVar()
        self.var_followup = tk.StringVar()
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
        # 多群：一行一个微信群名；var_wx 保留旧字段兼容（保存时取首个）。
        ttk.Label(config, text="微信群列表（一行一群）").grid(row=2, column=0, sticky="nw", padx=(0, 12), pady=5)
        self.txt_wx_groups = tk.Text(config, height=3, width=34, wrap="none", undo=True)
        self.txt_wx_groups.grid(row=2, column=1, sticky="ew", pady=5)
        self._configuration_widgets.append((self.txt_wx_groups, "normal"))
        fields = [
            ("客户管家群名", self.var_kf),
            ("你在客户管家的显示名（多个用顿号分隔）", self.var_kf_self),
            ("被 @ 的客服名（多人用顿号分隔）", self.var_staff),
            ("@ 后跟进消息（留空不发）", self.var_followup),
            ("机器人发送者名称", self.var_bot),
        ]
        for row, (label, variable) in enumerate(fields, start=3):
            self._entry_row(config, row, label, variable)
        self._entry_row(config, 7, "适配配置 JSON", self.var_profile)
        browse = ttk.Button(config, text="选择文件…", command=self._choose_profile)
        browse.grid(row=7, column=2, padx=(8, 0))
        self._configuration_widgets.append((browse, "normal"))
        ttk.Label(config, text="真实运行需使用与当前客户端版本匹配的控件配置；先读取窗口诊断，再按说明填写。",
                  foreground="#586174").grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 6))
        compatibility = ttk.Checkbutton(
            config, text="微信4兼容（实验性，停止后恢复）", variable=self.var_wechat_accessibility,
        )
        compatibility.grid(row=9, column=0, columnspan=3, sticky="w", pady=(2, 4))
        self._configuration_widgets.append((compatibility, "normal"))
        ttk.Label(
            config, text="仅对已验证的固定微信版本临时启用控件；诊断结束或停止时恢复。演示不使用此选项。",
            foreground="#586174",
        ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(0, 6))
        timing = ttk.Frame(config)
        timing.grid(row=11, column=0, columnspan=3, sticky="ew", pady=4)
        for label, variable in (("轮询间隔（秒）", self.var_poll), ("无回复超时（秒）", self.var_timeout)):
            ttk.Label(timing, text=label).pack(side="left", padx=(0, 8))
            entry = ttk.Entry(timing, textvariable=variable, width=9)
            entry.pack(side="left", padx=(0, 24))
            self._configuration_widgets.append((entry, "normal"))
        keywords = ttk.Frame(config)
        keywords.grid(row=12, column=0, columnspan=3, sticky="nsew", pady=(10, 4))
        config.rowconfigure(12, weight=1)
        keywords.columnconfigure((0, 1, 2), weight=1)
        keywords.rowconfigure(1, weight=1)
        ttk.Label(keywords, text="屏蔽词（全局，一行一个）").grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Label(keywords, text="@ 客服词句（消息含这些词才提醒，一行一个）").grid(row=0, column=1, sticky="w", pady=(0, 4), padx=(12, 0))
        ttk.Label(keywords, text="按群覆盖（群名|屏蔽词|@词；空段=清空，缺段=用全局）").grid(row=0, column=2, sticky="w", pady=(0, 4), padx=(12, 0))
        self.txt_block = tk.Text(keywords, height=7, width=26, wrap="word", undo=True)
        self.txt_at = tk.Text(keywords, height=7, width=26, wrap="word", undo=True)
        self.txt_policies = tk.Text(keywords, height=7, width=30, wrap="none", undo=True)
        self.txt_block.grid(row=1, column=0, sticky="nsew")
        self.txt_at.grid(row=1, column=1, sticky="nsew", padx=(12, 0))
        self.txt_policies.grid(row=1, column=2, sticky="nsew", padx=(12, 0))
        self._configuration_widgets.extend([
            (self.txt_block, "normal"), (self.txt_at, "normal"), (self.txt_policies, "normal")])
        send_check = ttk.Button(config, text="单次发送验收…", command=self.on_send_check)
        send_check.grid(row=13, column=0, columnspan=3, sticky="e", pady=(8, 0))
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
        self.btn_export = ttk.Button(footer, text="导出诊断包", command=self.on_export_diagnostics)
        self.btn_export.pack(side="left", padx=6)
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

    @staticmethod
    def _read_profile_kf_self(profile_path):
        """读 adapter profile 里客户管家 self_names（显示名），读失败返回空。"""
        try:
            data = json.loads(Path(profile_path).read_text(encoding="utf-8-sig"))
            names = data.get("kf", {}).get("self_names", [])
            return "、".join(n.strip() for n in names if isinstance(n, str) and n.strip())
        except (OSError, ValueError, AttributeError):
            return ""

    @staticmethod
    def _write_profile_kf_self(profile_path, names):
        """把显示名写回 adapter profile（原子替换），改错名不再需要删 json。"""
        path = Path(profile_path)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        data.setdefault("kf", {})["self_names"] = names
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(path)

    def _fill_form(self):
        groups = self.cfg.get("wx_groups") or (
            [self.cfg["wx_group"]] if self.cfg.get("wx_group") else [])
        self.txt_wx_groups.delete("1.0", "end")
        self.txt_wx_groups.insert("1.0", "\n".join(groups))
        for variable, key, default in (
            (self.var_kf, "kf_group", ""),
            (self.var_staff, "service_staff", ""),
            (self.var_followup, "mention_followup", "转人工处理"),
            (self.var_bot, "bot_name", "小圆-总公司"),
            (self.var_profile, "adapter_profile", ""), (self.var_poll, "poll_interval", 3),
            (self.var_timeout, "reset_seconds", 30),
        ):
            variable.set(self.cfg.get(key, default))
        self.var_kf_self.set(
            self._read_profile_kf_self(str(self.cfg.get("adapter_profile") or "")))
        self.var_wechat_accessibility.set(self.cfg.get("wechat_accessibility", False) is True)
        for widget, key in ((self.txt_block, "block_keywords"), (self.txt_at, "at_keywords")):
            widget.delete("1.0", "end")
            widget.insert("1.0", "\n".join(self.cfg.get(key, [])))
        policies = self.cfg.get("group_policies", {})
        lines = []
        for group, policy in policies.items():
            block = ",".join(policy.get("block_keywords", []))
            at = ",".join(policy.get("at_keywords", []))
            lines.append(f"{group}|{block}|{at}")
        self.txt_policies.delete("1.0", "end")
        self.txt_policies.insert("1.0", "\n".join(lines))

    @staticmethod
    def _lines(widget):
        return [line.strip() for line in widget.get("1.0", "end").splitlines() if line.strip()]

    @staticmethod
    def _parse_policies(lines, known_groups):
        """解析「群名|屏蔽词,词|@词,词」行；群名必须在群列表内。

        段存在即显式设置（空段=清空该类词），整段缺失=继承全局——与
        引擎 group_policies 的语义一致，保存/回读 round-trip 不丢清空设置。
        """
        policies = {}
        for line in lines:
            parts = [p.strip() for p in line.split("|")]
            if not parts or not parts[0]:
                continue
            group = parts[0]
            if group not in known_groups:
                raise ValueError(f"按群覆盖里的群「{group}」不在微信群列表内")
            policy = {}
            if len(parts) > 1:
                policy["block_keywords"] = [w for w in parts[1].split(",") if w.strip()]
            if len(parts) > 2:
                policy["at_keywords"] = [w for w in parts[2].split(",") if w.strip()]
            if policy:
                policies[group] = policy
        return policies

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
        # 客户管家显示名：表单里改了就同步写回 profile，客户不用再手改/删 json。
        kf_self = self.var_kf_self.get().strip()
        if profile and Path(profile).is_file() and kf_self:
            from wechat_forwarder import split_staff
            names = split_staff(kf_self)
            if names and self._read_profile_kf_self(profile) != kf_self:
                try:
                    self._write_profile_kf_self(profile, names)
                except OSError as exc:
                    raise ValueError(f"客户管家显示名写入失败：{exc}") from exc
        groups = self._lines(self.txt_wx_groups)
        policies = self._parse_policies(self._lines(self.txt_policies), set(groups))
        cfg.update({
            "mode": "live" if self.var_mode.get() == "真实客户端" else "demo",
            "wx_group": groups[0] if groups else "",
            "wx_groups": groups,
            "group_policies": policies,
            "kf_group": self.var_kf.get().strip(),
            "service_staff": self.var_staff.get().strip(),
            "mention_followup": self.var_followup.get().strip(),
            "bot_name": self.var_bot.get().strip(),
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
                detail = f"{type(exc).__name__}：{exc}"
                _write_error_log(f"[{kind}] {detail}\n"
                                 + traceback.format_exc() + "\n")
                self._events.put(("error", detail))
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

    def begin_license_watch(self, status):
        """启动运行期卡密复查（离线容错见 license_client.ensure_licensed）。"""
        expire_text = datetime.fromtimestamp(status.expire_at).strftime("%Y-%m-%d %H:%M") \
            if status.expire_at else "未知"
        self._append_log(f"[许可] 卡密有效，到期 {expire_text}"
                         + ("（当前离线容错）" if status.offline else ""))
        self.after(LICENSE_CHECK_INTERVAL, self._license_watch)

    def _license_watch(self):
        status = license_client.ensure_licensed()
        if status.ok:
            if status.offline:
                self._append_log("[许可] 验证服务器不可达，离线容错运行中")
            self.after(LICENSE_CHECK_INTERVAL, self._license_watch)
            return
        self._append_log(f"[许可] 卡密已失效：{status.message}")
        if self._worker:
            self._stop_event.set()
        dialog = ActivationDialog(self, status.message)
        self.wait_window(dialog)
        if dialog.granted:
            self.after(LICENSE_CHECK_INTERVAL, self._license_watch)
        else:
            self.on_close()

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

    def on_export_diagnostics(self):
        """一键导出诊断包：界面日志、判定与错误日志、wechatauto 详细日志、配置。"""
        import zipfile
        from datetime import datetime as _dt
        default_name = f"诊断包_{_dt.now():%Y%m%d_%H%M}.zip"
        target = filedialog.asksaveasfilename(
            title="导出诊断包", initialfile=default_name,
            defaultextension=".zip", filetypes=[("ZIP 压缩包", "*.zip")], parent=self)
        if not target:
            return
        candidates = [
            ("界面日志.txt", None),          # 特殊处理：来自日志文本框
            (f"config.json", Path(CONFIG_FILE)),
            (f"profile.json", Path(str(self.cfg.get("adapter_profile") or ""))),
            ("error.log", ERROR_LOG),
        ]
        wx_logs = sorted(Path(PROJECT_DIR).glob("wechatauto_logs/app_*.log"))
        for i, log_file in enumerate(wx_logs[-3:]):     # 最近 3 天的详细日志
            candidates.append((f"wechatauto_{i}_{log_file.name}", log_file))
        written = []
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
                gui_log = self.txt_log.get("1.0", "end").strip()
                if gui_log:
                    bundle.writestr("界面日志.txt", gui_log)
                    written.append("界面日志.txt")
                for name, path in candidates:
                    if path is None:
                        continue
                    try:
                        if path.is_file():
                            bundle.writestr(name, path.read_text(encoding="utf-8", errors="replace"))
                            written.append(name)
                    except OSError:
                        pass
                bundle.writestr("说明.txt",
                                 "诊断包内容：界面日志（含每条消息的判定原因）、配置、"
                                 "错误日志、wechatauto 详细日志（@ 每步粘贴/候选/坐标）。\n"
                                 "不含卡密等敏感信息，可放心发给作者。\n"
                                 f"版本 v{APP_VERSION}；导出时间：{_dt.now():%Y-%m-%d %H:%M:%S}\n")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        self._append_log(f"[诊断] 已导出 {len(written)} 项到 {target}")
        messagebox.showinfo("导出成功",
                            f"诊断包已保存：\n{target}\n请把该文件发给作者。", parent=self)

    def _clear_log(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")


class ActivationDialog(tk.Toplevel):
    """卡密激活对话框；模态，wait_window 结束后读 granted / license_key。"""

    def __init__(self, parent, notice=""):
        super().__init__(parent)
        self.title("卡密激活 · 微信 ⇄ 圆通客户管家")
        self.transient(parent)
        self.resizable(False, False)
        self.granted = False
        self.license_key = ""
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="首次使用请输入卡密激活；每张卡密绑定一台设备。",
                  wraplength=420).pack(anchor="w", pady=(0, 10))
        self.var_key = tk.StringVar()
        entry = ttk.Entry(body, textvariable=self.var_key, width=44)
        entry.pack(fill="x", pady=(0, 8))
        entry.focus_set()
        self.var_status = tk.StringVar(value=notice or "请输入卡密后点击「激活」")
        ttk.Label(body, textvariable=self.var_status, wraplength=420,
                  foreground="#7a5c00").pack(anchor="w", pady=(0, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="退出", command=self.destroy).pack(side="left")
        self.btn_activate = ttk.Button(buttons, text="激活", command=self.on_activate)
        self.btn_activate.pack(side="right")
        self.bind("<Return>", lambda _event: self.on_activate())
        self.update_idletasks()
        x = max(0, parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2)
        y = max(0, parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")
        self.grab_set()

    def on_activate(self):
        key = self.var_key.get().strip()
        if not key:
            self.var_status.set("请先输入卡密")
            return
        # 网络验证放后台线程：同步请求会把窗口冻结最多 8 秒，像假死。
        self.var_status.set("正在激活…")
        self.btn_activate.configure(state="disabled")

        def done(status):
            self.btn_activate.configure(state="normal")
            if status.ok:
                self.granted = True
                self.license_key = key
                self.destroy()
                return
            self.var_status.set(status.message or "激活失败，请核对卡密")

        def work():
            status = license_client.activate(key)
            try:
                self.after(0, lambda: done(status))
            except tk.TclError:
                pass   # 对话框已被用户关闭

        threading.Thread(target=work, name="license-activate", daemon=True).start()


def _license_notice(status):
    """无有效许可时的提示语；首次使用不显示吓人的错误。"""
    if status.message == "未找到已激活的卡密":
        return ""
    return status.message


class SetupWizard(tk.Toplevel):
    """首次运行向导：环境检测 → 填群名/昵称 → 生成配置。模态。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("首次运行向导 · 微信 ⇄ 圆通客户管家")
        self.transient(parent)
        self.resizable(False, False)
        self.completed = False
        self.body = ttk.Frame(self, padding=20)
        self.body.pack(fill="both", expand=True)
        self._show_check_page()
        self.update_idletasks()
        x = max(0, parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2)
        y = max(0, parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")
        self.grab_set()

    def _clear(self):
        for child in self.body.winfo_children():
            child.destroy()

    def _show_check_page(self):
        self._clear()
        ttk.Label(self.body, text="第 1 步 · 环境检测", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(self.body, text="请先满足以下两个条件，再点击「重新检测」：\n"
                                  "1. 微信已登录并在运行；\n"
                                  "2. 客户管家用本包附带的「启动客户管家」脚本启动（普通双击启动无法对接）。",
                  wraplength=460, justify="left").pack(anchor="w", pady=(6, 10))
        self.var_wx = tk.StringVar(value="检测中…")
        self.var_cdp = tk.StringVar(value="检测中…")
        ttk.Label(self.body, textvariable=self.var_wx).pack(anchor="w", pady=2)
        ttk.Label(self.body, textvariable=self.var_cdp).pack(anchor="w", pady=2)
        buttons = ttk.Frame(self.body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="重新检测", command=self._run_checks).pack(side="right")
        self.after(100, self._run_checks)

    def _run_checks(self):
        wx_ok = setup_wizard.detect_wechat_running()
        cdp_ok = setup_wizard.detect_cdp()
        self.var_wx.set(("✓ 微信正在运行" if wx_ok else "✗ 未检测到微信，请先登录微信"))
        self.var_cdp.set(("✓ 客户管家调试口已就绪" if cdp_ok
                          else "✗ 客户管家调试口未就绪，请用附带脚本启动客户管家"))
        if wx_ok and cdp_ok:
            self.after(400, self._show_form_page)

    def _show_form_page(self):
        self._clear()
        ttk.Label(self.body, text="第 2 步 · 填写群与昵称", font=("", 12, "bold")).pack(anchor="w")
        auto_name = setup_wizard.read_wechat_self_name()
        ttk.Label(self.body, text="微信群列表（一行一个群，可填多个）").pack(anchor="w", pady=(6, 2))
        self.txt_groups = tk.Text(self.body, height=3, width=46, undo=True)
        self.txt_groups.pack(fill="x")
        self._vars = {
            "var_kf_group": tk.StringVar(),
            "var_staff": tk.StringVar(),
            "var_bot": tk.StringVar(value="小圆-总公司"),
            "var_kf_self": tk.StringVar(),
            "var_wx_self": tk.StringVar(value=auto_name),
        }
        rows = [
            ("客户管家群名称（客服群）", "var_kf_group"),
            ("微信客服昵称（@提醒对象，多人用顿号分隔）", "var_staff"),
            ("机器人昵称", "var_bot"),
            ("你在客户管家里的显示名", "var_kf_self"),
            ("你在微信群里的昵称", "var_wx_self"),
        ]
        for label, attr in rows:
            row = ttk.Frame(self.body)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=28).pack(side="left")
            ttk.Entry(row, textvariable=self._vars[attr], width=28).pack(side="left", fill="x", expand=True)
        ttk.Label(self.body, text="「客户管家显示名」查看方法：在客服群里随便发一条消息，"
                                  "消息上方显示的名字就是。填错会导致收不到自己发出的回复确认。",
                  wraplength=460, foreground="#7a5c00", justify="left").pack(anchor="w", pady=(8, 0))
        self.var_msg = tk.StringVar()
        ttk.Label(self.body, textvariable=self.var_msg, wraplength=460,
                  foreground="#c02020").pack(anchor="w", pady=(4, 0))
        buttons = ttk.Frame(self.body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="跳过（稍后在主窗口配置）", command=self.destroy).pack(side="left")
        ttk.Button(buttons, text="生成配置", command=self.on_generate).pack(side="right")

    def on_generate(self):
        values = {key: var.get().strip() for key, var in self._vars.items()}
        groups = [line.strip() for line in
                  self.txt_groups.get("1.0", "end").splitlines() if line.strip()]
        missing = [label for label, value in [
            ("微信群列表", "\n".join(groups)), ("客户管家群名称", values["var_kf_group"]),
            ("微信客服昵称", values["var_staff"]), ("客户管家显示名", values["var_kf_self"]),
            ("微信昵称", values["var_wx_self"])] if not value]
        if missing:
            self.var_msg.set("请填写：" + "、".join(missing))
            return
        try:
            setup_wizard.write_setup(
                groups, values["var_kf_group"],
                values["var_staff"], values["var_bot"] or "小圆-总公司",
                values["var_wx_self"], [values["var_kf_self"]])
        except OSError as exc:
            self.var_msg.set(f"写入配置失败：{exc}")
            return
        self.completed = True
        self.destroy()


def main():
    app = ConfigWindow()
    if setup_wizard.setup_needed():
        wizard = SetupWizard(app)
        app.wait_window(wizard)
        if wizard.completed:
            app._load_config()
            app._fill_form()
            app._append_log("[向导] 首次配置已生成，建议先点「双端检查」验证。")
            app.var_status.set("首次配置完成，请先双端检查")
    status = license_client.ensure_licensed()
    if not status.ok:
        dialog = ActivationDialog(app, _license_notice(status))
        app.wait_window(dialog)
        if not dialog.granted:
            app.destroy()
            return
        status = license_client.ensure_licensed()
    app.begin_license_watch(status)
    app.mainloop()


if __name__ == "__main__":
    main()
