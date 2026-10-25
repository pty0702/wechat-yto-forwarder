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

from wechat_forwarder import Message, SafeSkipError, SendUncertainError
from windows_adapter import AdapterConfigurationError, _comparable, _normalized

_VENDOR = Path(__file__).resolve().parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

# 群聊消息正文在库里带 "发送者账号:\\n" 前缀，搬运前剥掉。
_TEXT_KIND = "文本"

# @ 流程中「确定未发出」的失败文案（at_members 的 abort 路径）：候选无
# 精确匹配 / 坐标异常 / 未选中 / 会话被切走 / 发送动作未生效——全部发生
# 在消息发出之前或已清场，跳过该条是安全的。
_SAFE_MENTION_FAILURES = (
    "候选弹层中没有", "候选坐标异常", "@ 未选中", "会话被切走",
    "回车与发送按钮均未能发出", "未打开目标会话",
)


def _response_message(response) -> str:
    return str(getattr(response, "message", "") or response)


def _is_safe_mention_failure(response) -> bool:
    message = _response_message(response)
    return any(prefix in message for prefix in _SAFE_MENTION_FAILURES)


def _strip_sender_prefix(content: str, sender: str) -> str:
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    for prefix in (f"{sender}:\n", f"{sender}:\r\n", f"{sender}:"):
        if content.startswith(prefix):
            return content[len(prefix):]
    return content


class WechatDbAdapter:
    """微信消息来源与发送端；读用本地库，发用坐标/OCR。

    group 接受单个群名或群名列表（多群模式）：列表时依次建立基线、
    合并返回新消息（Message.group 标记来源群），发送端按 group 参数
    定向发送，缺省发第一个群。
    """

    def __init__(self, profile, group, log=print, db=None, gui=None):
        self.profile = profile
        self.groups = [str(g).strip() for g in
                       (group if isinstance(group, (list, tuple)) else [group])
                       if str(g).strip()]
        self.group = self.groups[0] if self.groups else ""
        self.log = log
        self.self_names = [str(name).strip() for name in profile.get("self_names", [])
                           if str(name).strip()]
        self._db = db
        self._gui = gui
        self._chats = {}       # 群名 -> chat_id
        self._since = {}       # 群名 -> sort_seq 水位
        self._confirm_chats = {}   # 群名 -> chat_id（发送确认用，与读水位无关）

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
        if not self.groups:
            raise AdapterConfigurationError("微信数据库适配器没有配置任何群")
        for name in self.groups:
            chat_id = db.group_name_to_id(name)
            if not chat_id:
                raise AdapterConfigurationError(f"微信数据库中找不到目标群：{name}")
            self._chats[name] = chat_id
            self._confirm_chats[name] = chat_id
            rows = db.get_messages(chat_id, limit=1)
            self._since[name] = rows[0]["sort_seq"] if rows else 0
        self._chat_id = self._chats[self.group] if self.group else None
        self._since_seq = self._since.get(self.group, 0)
        self.log(f"[适配] 微信数据库已绑定 {'、'.join(self.groups)}，"
                 f"基线 {self._since}")

    def fetch_new(self):
        if not self._chats:
            raise AdapterConfigurationError("微信数据库适配器尚未 prepare")
        db = self._wechat_db()
        collected = []
        for name, chat_id in self._chats.items():
            rows = db.get_messages(chat_id, limit=50)
            messages = []
            for row in rows:
                seq = int(row.get("sort_seq") or 0)
                if seq <= self._since[name]:
                    continue
                msg = self._to_message(row, group=name)
                if msg is not None:
                    messages.append(msg)
            if rows:
                self._since[name] = max(self._since[name],
                                        max(int(r.get("sort_seq") or 0) for r in rows))
                if name == self.group:
                    self._since_seq = self._since[name]
            collected.extend(reversed(messages))
        return collected

    def fetch_replies(self, bot_name):
        return [msg for msg in self.fetch_new() if msg.sender == bot_name]

    def _to_message(self, row, group=""):
        sender_id = str(row.get("sender_username") or "").strip()
        if not sender_id:
            return None
        db = self._wechat_db()
        display = str(db.get_nickname(sender_id) or sender_id).strip() or sender_id
        content = _strip_sender_prefix(row.get("content") or "", sender_id)
        kind = "text" if row.get("type") == _TEXT_KIND else "media"
        is_self = sender_id == db.wxid or display in self.self_names
        message_id = "wxdb:%s:%s:%s" % (
            self._chats.get(group) or self._chat_id, row.get("shard") or "", row.get("local_id"),
        )
        return Message(display, content, message_id=message_id, is_self=is_self,
                       kind=kind, group=group)

    # ------------------------------------------------------------------
    # 发送（WeChatGUI + 本地库读回确认）
    # ------------------------------------------------------------------
    def _resolve_chat(self, group):
        """发送目标群：group 为空用默认群；不在配置列表里直接拒绝。"""
        name = (group or self.group).strip()
        if name not in self._confirm_chats:
            raise AdapterConfigurationError(
                f"发送目标群不在配置的微信群列表内：{name or '(空)'}")
        return name, self._confirm_chats[name]

    def _send(self, text, staff=None, group=""):
        if not isinstance(text, str):
            raise AdapterConfigurationError("发送文本必须是字符串")
        if not text and not staff:
            # @ 场景允许空正文（只发 @ 不带原文）；纯文本必须有内容。
            raise AdapterConfigurationError("发送文本不能为空")
        if not self._confirm_chats:
            raise AdapterConfigurationError("微信数据库适配器尚未 prepare")
        from wechat_forwarder import split_staff
        staff_list = split_staff(staff) if staff else []
        name, chat_id = self._resolve_chat(group)
        db = self._wechat_db()
        rows = db.get_messages(chat_id, limit=1)
        before_seq = int(rows[0].get("sort_seq") or 0) if rows else self._since.get(name, 0)
        gui = self._wechat_gui()
        try:
            # verify 交给本模块自己做：wechatauto 内置读回在本机有误报。
            if staff_list:
                response = gui.at_members(staff_list, text, who=name, verify=False)
            else:
                response = gui.send_msg(text, who=name, verify=False)
        except Exception as exc:
            raise SendUncertainError(f"发送结果不确定，请人工核对：{exc}") from exc
        if not getattr(response, "is_success", False):
            if staff_list and _is_safe_mention_failure(response):
                # @ 流程的确定未发送失败（候选无匹配/未选中/被清场）：
                # 跳过该条提醒继续运行，不再停机等人工。
                raise SafeSkipError(_response_message(response))
            raise SendUncertainError(f"发送结果不确定，请人工核对：{response}")
        expected = _comparable(text)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            # 微信写库异步：get_new_messages 在落库初期可能漏行，改扫最近消息。
            try:
                recent = db.get_messages(chat_id, limit=30)
            except Exception:
                time.sleep(0.4)
                continue
            for row in recent:
                if int(row.get("sort_seq") or 0) <= before_seq:
                    continue
                msg = self._to_message(row, group=name)
                if msg is None or not msg.is_self:
                    continue
                if staff_list:
                    matched = expected in _comparable(msg.content) and all(
                        _comparable(s) in _comparable(msg.content) for s in staff_list)
                else:
                    matched = _comparable(msg.content) == _comparable(text)
                if matched:
                    self._since[name] = max(self._since.get(name, 0),
                                            int(row.get("sort_seq") or 0))
                    if name == self.group:
                        self._since_seq = self._since[name]
                    return
            time.sleep(0.4)
        raise SendUncertainError(
            "发送后未在微信数据库确认到本账号消息，请人工核对；已停止，避免重复发送")

    def send(self, text, group=""):
        self._send(text, group=group)

    def send_mention(self, staff, text, group=""):
        self._send(text, staff=staff, group=group)
