# -*- coding: utf-8 -*-
"""微信侧数据库通道适配器：读消息走本地库，发送走 wechatauto 坐标/OCR。

与 UIA 多选路线相比：发送者直接取自库内 Name2Id 映射，不需要右键多选；
发送后的确认走 wechatauto 的数据库读回（verify=True），不再比对 UIA 回显。
要求微信 4.x 已登录并在本机运行；数据库读写仅限本机微信自身数据文件。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from wechat_forwarder import Message, SendUncertainError
from windows_adapter import AdapterConfigurationError, _normalized

_VENDOR = Path(__file__).resolve().parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

# 群聊消息正文在库里带 "发送者账号:\\n" 前缀，搬运前剥掉。
_TEXT_KIND = "文本"


def _strip_sender_prefix(content: str, sender: str) -> str:
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    for prefix in (f"{sender}:\n", f"{sender}:\r\n", f"{sender}:"):
        if content.startswith(prefix):
            return content[len(prefix):]
    return content


class WechatDbAdapter:
    """微信消息来源与发送端；读用本地库，发用坐标/OCR。"""

    def __init__(self, profile, group, log=print, db=None, gui=None):
        self.profile = profile
        self.group = group
        self.log = log
        self.self_names = [str(name).strip() for name in profile.get("self_names", [])
                           if str(name).strip()]
        self._db = db
        self._gui = gui
        self._chat_id = None
        self._since_seq = 0

    # ------------------------------------------------------------------
    # 库与发送器（惰性；测试可直接注入）
    # ------------------------------------------------------------------
    def _wechat_db(self):
        if self._db is None:
            from wechatauto.db import WeChatDB
            self._db = WeChatDB()
        return self._db

    def _wechat_gui(self):
        if self._gui is None:
            from wechatauto.guia import WeChatGUI
            self._gui = WeChatGUI()
        return self._gui

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def prepare(self):
        db = self._wechat_db()
        chat_id = db.group_name_to_id(self.group)
        if not chat_id:
            raise AdapterConfigurationError(f"微信数据库中找不到目标群：{self.group}")
        self._chat_id = chat_id
        rows = db.get_messages(chat_id, limit=1)
        self._since_seq = rows[0]["sort_seq"] if rows else 0
        self.log(f"[适配] 微信数据库已绑定 {self.group}，基线 sort_seq={self._since_seq}")

    def fetch_new(self):
        if self._chat_id is None:
            raise AdapterConfigurationError("微信数据库适配器尚未 prepare")
        rows = self._wechat_db().get_messages(self._chat_id, limit=50)
        messages = []
        for row in rows:
            seq = int(row.get("sort_seq") or 0)
            if seq <= self._since_seq:
                continue
            msg = self._to_message(row)
            if msg is not None:
                messages.append(msg)
        if rows:
            self._since_seq = max(self._since_seq, max(int(r.get("sort_seq") or 0) for r in rows))
        return list(reversed(messages))

    def fetch_replies(self, bot_name):
        return [msg for msg in self.fetch_new() if msg.sender == bot_name]

    def _to_message(self, row):
        sender_id = str(row.get("sender_username") or "").strip()
        if not sender_id:
            return None
        db = self._wechat_db()
        display = str(db.get_nickname(sender_id) or "").strip() or sender_id
        content = _strip_sender_prefix(row.get("content") or "", sender_id)
        kind = "text" if row.get("type") == _TEXT_KIND else "media"
        is_self = sender_id == db.wxid or display in self.self_names
        message_id = "wxdb:%s:%s:%s" % (
            self._chat_id, row.get("shard") or "", row.get("local_id"),
        )
        return Message(display, content, message_id=message_id, is_self=is_self, kind=kind)

    # ------------------------------------------------------------------
    # 发送（WeChatGUI + 本地库读回确认）
    # ------------------------------------------------------------------
    def _send(self, text, staff=None):
        if not isinstance(text, str) or not text:
            raise AdapterConfigurationError("发送文本不能为空")
        if self._chat_id is None:
            raise AdapterConfigurationError("微信数据库适配器尚未 prepare")
        db = self._wechat_db()
        rows = db.get_messages(self._chat_id, limit=1)
        before_seq = int(rows[0].get("sort_seq") or 0) if rows else self._since_seq
        gui = self._wechat_gui()
        try:
            # verify 交给本模块自己做：wechatauto 内置读回在本机有误报。
            if staff:
                response = gui.at_member(staff, text, who=self.group, verify=False)
            else:
                response = gui.send_msg(text, who=self.group, verify=False)
        except Exception as exc:
            raise SendUncertainError(f"发送结果不确定，请人工核对：{exc}") from exc
        if not getattr(response, "is_success", False):
            raise SendUncertainError(f"发送结果不确定，请人工核对：{response}")
        expected = text.strip()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            # 微信写库异步：get_new_messages 在落库初期可能漏行，改扫最近消息。
            try:
                recent = db.get_messages(self._chat_id, limit=30)
            except Exception:
                time.sleep(0.4)
                continue
            for row in recent:
                if int(row.get("sort_seq") or 0) <= before_seq:
                    continue
                msg = self._to_message(row)
                if msg is None or not msg.is_self:
                    continue
                if staff:
                    matched = expected in msg.content and staff in msg.content
                else:
                    matched = _normalized(msg.content).strip() == _normalized(text).strip()
                if matched:
                    self._since_seq = max(self._since_seq, int(row.get("sort_seq") or 0))
                    return
            time.sleep(0.4)
        raise SendUncertainError(
            "发送后未在微信数据库确认到本账号消息，请人工核对；已停止，避免重复发送")

    def send(self, text):
        self._send(text)

    def send_mention(self, staff, text):
        self._send(text, staff=staff)
