# -*- coding: utf-8 -*-
"""微信数据库通道适配器的单元测试；不连接真实微信。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wechat_db_adapter import WechatDbAdapter, _strip_sender_prefix
from wechat_forwarder import Message, SendUncertainError
from windows_adapter import AdapterConfigurationError


class FakeDb:
    wxid = "me-wxid"

    def __init__(self):
        self.groups = {"潼阳圆通出港群": "gid@chatroom"}
        self.rows = []
        self.nicknames = {"me-wxid": "本人昵称", "bot-wxid": "小圆-总公司", "cust-wxid": "客户甲"}

    def group_name_to_id(self, name):
        return self.groups.get(name)

    def get_messages(self, user, limit=20, offset=0):
        return list(reversed(self.rows[-limit:])) if limit else []

    def get_new_messages(self, user, since_seq=0, limit=200):
        return [r for r in self.rows if r["sort_seq"] > since_seq]

    def get_nickname(self, user):
        return self.nicknames.get(user, "")


class FakeGui:
    def __init__(self, db=None):
        self.db = db
        self.calls = []
        self.response = SimpleNamespace(is_success=True, message="ok")
        self.on_send = None

    def send_msg(self, text, who=None, verify=False):
        self.calls.append(("send", who, text, verify))
        if self.on_send:
            self.on_send(text)
        return self.response

    def at_member(self, member, text, who=None, verify=False):
        self.calls.append(("at", who, member, text, verify))
        if self.on_send:
            self.on_send(f"@{member} {text}")
        return self.response


def row(seq, sender, content, kind="文本", local_id=1):
    return {
        "local_id": local_id, "type": kind, "sender_id": 1,
        "sender_username": sender, "create_time": seq // 1000,
        "content": content, "sort_seq": seq, "shard": "s1",
    }


class WechatDbAdapterTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        self.gui = FakeGui(self.db)
        self.logs = []
        self.client = WechatDbAdapter(
            {"self_names": ["本人昵称"]}, "潼阳圆通出港群",
            log=self.logs.append, db=self.db, gui=self.gui,
        )
        self._seq = 0

        def auto_echo(content):
            self._seq += 1000
            self.db.rows.append(row(self._seq, "me-wxid", f"me-wxid:\n{content}"))

        self.gui.on_send = auto_echo

    def test_strip_sender_prefix_only_removes_exact_header(self):
        self.assertEqual(_strip_sender_prefix("uid:\n正文", "uid"), "正文")
        self.assertEqual(_strip_sender_prefix("uid:\r\n正文", "uid"), "正文")
        self.assertEqual(_strip_sender_prefix("别的内容", "uid"), "别的内容")

    def test_prepare_requires_existing_group_and_sets_baseline(self):
        with self.assertRaises(AdapterConfigurationError):
            WechatDbAdapter({"self_names": ["本人昵称"]}, "不存在的群",
                            log=lambda m: None, db=self.db, gui=self.gui).prepare()
        self.db.rows = [row(1000, "cust-wxid", "cust-wxid:\nYT1 查件")]
        self.client.prepare()
        self.assertEqual(self.client._since_seq, 1000)
        self.assertTrue(self.logs)

    def test_fetch_new_maps_sender_content_kind_and_self(self):
        self.client.prepare()
        self.db.rows = [
            row(1000, "cust-wxid", "cust-wxid:\nYT1 查件", local_id=1),
            row(2000, "me-wxid", "me-wxid:\n我自己发的", local_id=2),
            row(3000, "bot-wxid", "bot-wxid:\n[图片]", kind="图片", local_id=3),
        ]
        messages = self.client.fetch_new()
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0].sender, "客户甲")
        self.assertEqual(messages[0].content, "YT1 查件")
        self.assertFalse(messages[0].is_self)
        self.assertEqual(messages[0].kind, "text")
        self.assertTrue(messages[1].is_self)
        self.assertEqual(messages[2].kind, "media")
        self.assertEqual(messages[2].sender, "小圆-总公司")
        self.assertEqual(messages[2].message_id, "wxdb:gid@chatroom:s1:3")

    def test_fetch_new_skips_rows_without_sender_and_advances_seq(self):
        self.client.prepare()
        self.db.rows = [
            row(1000, "", "无发送者"),
            row(2000, "cust-wxid", "cust-wxid:\nYT2"),
        ]
        messages = self.client.fetch_new()
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.client._since_seq, 2000)
        self.assertEqual(self.client.fetch_new(), [])

    def test_send_uses_gui_and_confirms_via_db(self):
        self.client.prepare()
        self.client.send("测试文本")
        self.assertEqual(self.gui.calls, [("send", "潼阳圆通出港群", "测试文本", False)])
        self.assertEqual(self.client._since_seq, 1000)

    def test_send_mention_delegates_at_member(self):
        self.client.prepare()
        self.client.send_mention("圆通快递", "请跟进")
        self.assertEqual(self.gui.calls, [("at", "潼阳圆通出港群", "圆通快递", "请跟进", False)])

    def test_send_failure_or_exception_is_uncertain(self):
        self.client.prepare()
        self.gui.response = SimpleNamespace(is_success=False, message="没发出去")
        with self.assertRaises(SendUncertainError):
            self.client.send("文本")
        self.gui.send_msg = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("点击失败"))
        with self.assertRaises(SendUncertainError):
            self.client.send("文本")

    def test_send_uncertain_when_db_never_sees_echo(self):
        self.client.prepare()
        self.gui.on_send = None
        with patch("wechat_db_adapter.time.monotonic", side_effect=[0, 0, 20]), \
                patch("wechat_db_adapter.time.sleep"):
            with self.assertRaises(SendUncertainError):
                self.client.send("查不到的文本")

    def test_empty_text_rejected_before_gui(self):
        self.client.prepare()
        for text in ("", None):
            with self.subTest(text=text), self.assertRaises(AdapterConfigurationError):
                self.client.send(text)
        self.assertEqual(self.gui.calls, [])


if __name__ == "__main__":
    unittest.main()
