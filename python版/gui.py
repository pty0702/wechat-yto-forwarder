# -*- coding: utf-8 -*-
"""
配置窗口：屏蔽词 / @客服词句 / 客服名 等全部由你自行填写，保存到 config.json。

运行：python gui.py
"""

import json
import os
import tkinter as tk
from tkinter import messagebox, ttk

from wechat_forwarder import CONFIG_FILE, load_config, save_config


class ConfigWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("微信⇄客户管家 搬运配置")
        self.geometry("520x560")
        self.minsize(480, 520)
        self.cfg = load_config()
        self._build()

    # ------------------------------------------------------------------ 界面
    def _build(self):
        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=8, pady=8)

        # 单行配置
        self.var_wx = tk.StringVar(value=self.cfg.get("wx_group", ""))
        self.var_kf = tk.StringVar(value=self.cfg.get("kf_group", ""))
        self.var_staff = tk.StringVar(value=self.cfg.get("service_staff", ""))
        self.var_bot = tk.StringVar(value=self.cfg.get("bot_name", "小圆"))

        rows = [
            ("微信群名（监控用）：", self.var_wx, "客户发消息的群，如：潼阳圆通出港群"),
            ("客服群名（转发目标）：", self.var_kf, "客户管家里的客服群，如：潼阳-张士祥客户服务群"),
            ("客服名（@的对象）：", self.var_staff, "命中@客服词时@的人，后期可随时改"),
            ("机器人名（只搬它的回复）：", self.var_bot, "客户管家里机器人的名字，默认：小圆"),
        ]
        for i, (label, var, tip) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="e", **pad)
            ent = ttk.Entry(frm, textvariable=var, width=28)
            ent.grid(row=i, column=1, sticky="we", **pad)
            ttk.Label(frm, text=tip, foreground="#888").grid(row=i, column=2, **pad)

        frm.columnconfigure(1, weight=1)

        # 屏蔽词（多行，一行一个）
        ttk.Label(frm, text="屏蔽词（命中即装作没看见，一行一个）：").grid(
            row=4, column=0, columnspan=3, sticky="w", **pad)
        self.txt_block = tk.Text(frm, height=5, width=40)
        self.txt_block.grid(row=5, column=0, columnspan=3, sticky="we", **pad)
        self.txt_block.insert("1.0", "\n".join(self.cfg.get("block_keywords", [])))

        # @客服词句（多行，一行一个）
        ttk.Label(frm, text="@客服词句（带YT且命中即在微信群@客服，一行一个）：").grid(
            row=6, column=0, columnspan=3, sticky="w", **pad)
        self.txt_at = tk.Text(frm, height=5, width=40)
        self.txt_at.grid(row=7, column=0, columnspan=3, sticky="we", **pad)
        self.txt_at.insert("1.0", "\n".join(self.cfg.get("at_keywords", [])))

        # 按钮
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bar, text="保存配置", command=self.on_save).pack(side="right", padx=4)
        ttk.Label(bar, text=f"保存位置：{os.path.basename(CONFIG_FILE)}").pack(side="left")

    # ------------------------------------------------------------------ 动作
    @staticmethod
    def _lines(widget) -> list:
        return [ln.strip() for ln in widget.get("1.0", "end").splitlines() if ln.strip()]

    def on_save(self):
        cfg = load_config()
        cfg.update({
            "wx_group": self.var_wx.get().strip(),
            "kf_group": self.var_kf.get().strip(),
            "service_staff": self.var_staff.get().strip(),
            "bot_name": self.var_bot.get().strip() or "小圆",
            "block_keywords": self._lines(self.txt_block),
            "at_keywords": self._lines(self.txt_at),
        })
        save_config(cfg)
        messagebox.showinfo("已保存", f"配置已写入 {CONFIG_FILE}")


if __name__ == "__main__":
    ConfigWindow().mainloop()
