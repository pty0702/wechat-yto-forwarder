# -*- coding: utf-8 -*-
"""客户管家 DOM 适配器单元测试；不连接真实客户端。"""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kf_dom_adapter import KfDomAdapter, _parse_row
from wechat_forwarder import RetryableSendError, SendUncertainError
from windows_adapter import AdapterConfigurationError


class FakeCdp:
    def __init__(self):
        self.title = "潘-祖海琪客户服务群"
        self.rows = []
        self.draft = ""
        self.evaluated = []
        self.inserted = []
        self.on_click_send = None

    def evaluate(self, expression):
        self.evaluated.append(expression)
        if "p-t-14" in expression:
            return self.title
        if "div[id]" in expression and "news-box" in expression:
            return self.rows
        if "edit-content" in expression:
            if "return !" in expression:
                return not self.draft.strip()
            if "activeElement" in expression:
                return True
            return self.draft
        if "send-btn" in expression:
            if "disabled" in expression:
                return False
            if self.on_click_send:
                self.on_click_send()
            return True
        return None

    def insert_text(self, text):
        self.inserted.append(text)
        self.draft += text


def row(id_, sender, content, media=False):
    return {"id": id_, "sender": sender, "content": content, "media": media}


class KfDomAdapterTests(unittest.TestCase):
    def setUp(self):
        self.cdp = FakeCdp()
        self.cdp.rows = [
            row("192681338631881001", "客户甲", "YT1 查件"),
            row("192681338631881002", "小海", "YT1 已处理"),
        ]
        self.logs = []
        self.client = KfDomAdapter({"self_names": ["小海"]}, "潘-祖海琪客户服务群",
                                   log=self.logs.append, cdp=self.cdp)

    def test_parse_row_maps_self_and_message_id(self):
        msg = _parse_row(row("192681338631881002", "小海", "正文"), ["小海"])
        self.assertTrue(msg.is_self)
        self.assertEqual(msg.message_id, "客户管家:192681338631881002")
        self.assertEqual(msg.content, "正文")

    def test_prepare_checks_title_and_sets_baseline(self):
        self.cdp.title = "别的群"
        with self.assertRaises(RetryableSendError):
            self.client.prepare()
        self.cdp.title = "潘-祖海琪客户服务群"
        self.client.prepare()
        self.assertEqual(len(self.client.fetch_new()), 0)

    def test_fetch_new_only_returns_unknown_rows(self):
        self.client.prepare()
        self.cdp.rows.append(row("192681338631881003", "客户乙", "YT2"))
        messages = self.client.fetch_new()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].sender, "客户乙")
        self.assertEqual(self.client.fetch_new(), [])

    def test_send_types_clicks_and_confirms_echo(self):
        self.client.prepare()

        def after_click():
            self.cdp.draft = ""
            self.cdp.rows.append(row("192681338631881004", "小海", "联调文本"))

        self.cdp.on_click_send = after_click
        self.client.send("联调文本")
        self.assertEqual(self.cdp.inserted, ["联调文本"])

    def test_send_rejects_existing_draft_and_empty_text(self):
        self.client.prepare()
        self.cdp.draft = "用户草稿"
        with self.assertRaises(RetryableSendError):
            self.client.send("文本")
        self.cdp.draft = ""
        with self.assertRaises(AdapterConfigurationError):
            self.client.send("")

    def test_send_uncertain_when_echo_missing(self):
        self.client.prepare()
        self.cdp.on_click_send = lambda: setattr(self.cdp, "draft", "")
        with patch("kf_dom_adapter.time.monotonic", side_effect=[0, 0, 20]), \
                patch("kf_dom_adapter.time.sleep"):
            with self.assertRaises(SendUncertainError):
                self.client.send("不会出现在回显里的文本")


if __name__ == "__main__":
    unittest.main()
