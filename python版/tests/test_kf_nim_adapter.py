# -*- coding: utf-8 -*-
"""kf_nim_adapter（客户管家云信 SDK 通道）单元测试：CDP 全部 mock。"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kf_nim_adapter import KfNimAdapter, _HOOK_JS, _TEAMS_JS, _INBOX_JS  # noqa: E402
from wechat_forwarder import RetryableSendError, SendUncertainError  # noqa: E402
from windows_adapter import AdapterConfigurationError  # noqa: E402


class FakeCdp:
    """按表达式关键字返回预置结果的假 CDP 客户端。"""

    def __init__(self, script):
        # script: {关键字: 返回值}，evaluate 按子串匹配；缺省回 '{}'
        self.script = script
        self.calls = []

    def evaluate(self, expression):
        self.calls.append(expression)
        for key, value in self.script.items():
            if key in expression:
                return value if isinstance(value, str) else json.dumps(value)
        return "{}"


TEAMS = [{"teamId": "2870478248", "name": "路晓燕客户服务群"},
         {"teamId": "2869353312", "name": "潼阳-梁小龙客户服务群"}]

HOOK_OK = {"ok": True, "account": "f6ccb7afc370498fad7f68fd9635e5fb"}


def make_adapter(script, group="路晓燕客户服务群"):
    cdp = FakeCdp(script)
    ad = KfNimAdapter({"self_names": ["小圆", "小海"], "cdp_url": "http://127.0.0.1:9223"},
                      group, log=lambda *a: None, cdp=cdp)
    return ad, cdp


class TestPrepare(unittest.TestCase):
    def test_prepare_ok_and_team_resolved(self):
        ad, cdp = make_adapter({"imLayout": HOOK_OK, "imTeamInfoSession": TEAMS})
        ad.prepare()
        self.assertEqual(ad._team_id, "2870478248")
        self.assertTrue(ad._baseline_done)

    def test_prepare_fuzzy_unique(self):
        ad, _ = make_adapter({"imLayout": HOOK_OK, "imTeamInfoSession": TEAMS},
                             group="路晓燕客户服务群（备用）")
        ad.prepare()  # 双向包含且唯一 → 绑定
        self.assertEqual(ad._team_id, "2870478248")

    def test_prepare_hook_fail(self):
        ad, _ = make_adapter({"imLayout": {"ok": False, "why": "页面未就绪"}})
        with self.assertRaises(AdapterConfigurationError):
            ad.prepare()

    def test_prepare_team_missing(self):
        ad, _ = make_adapter({"imLayout": HOOK_OK, "imTeamInfoSession": TEAMS},
                             group="不存在的群")
        with self.assertRaises(AdapterConfigurationError):
            ad.prepare()

    def test_fetch_before_prepare_rejected(self):
        ad, _ = make_adapter({})
        with self.assertRaises(AdapterConfigurationError):
            ad.fetch_new()


class TestFetch(unittest.TestCase):
    def _ready(self, inbox=None):
        script = {"imLayout": HOOK_OK, "imTeamInfoSession": TEAMS}
        ad, cdp = make_adapter(script)
        ad.prepare()
        cdp.script["window.__kfInbox"] = inbox or []
        # 让 _INBOX_JS 关键字命中队列数据（清空读取代第二次 evaluate）
        cdp.calls.clear()
        return ad, cdp

    def test_team_message_becomes_message(self):
        inbox = [{"id": "abc", "from": "x1", "fromNick": "小圆-总公司",
                  "type": "text", "scene": "team", "target": "2870478248",
                  "text": "YT123 已签收", "time": 1793800000000}]
        ad, _ = self._ready(inbox)
        msgs = ad.fetch_new()
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertEqual(m.sender, "小圆-总公司")
        self.assertEqual(m.content, "YT123 已签收")
        self.assertFalse(m.is_self)
        self.assertEqual(m.kind, "text")

    def test_other_group_filtered(self):
        inbox = [{"id": "a", "from": "x", "fromNick": "n", "type": "text",
                  "scene": "team", "target": "2869353312", "text": "别的群", "time": 1}]
        ad, _ = self._ready(inbox)
        self.assertEqual(ad.fetch_new(), [])

    def test_p2p_filtered(self):
        inbox = [{"id": "a", "from": "x", "fromNick": "n", "type": "text",
                  "scene": "p2p", "target": "someone", "text": "私聊", "time": 1}]
        ad, _ = self._ready(inbox)
        self.assertEqual(ad.fetch_new(), [])

    def test_dedupe_by_id(self):
        inbox = [{"id": "dup", "from": "x", "fromNick": "n", "type": "text",
                  "scene": "team", "target": "2870478248", "text": "hi", "time": 7}]
        ad, _ = self._ready(inbox)
        self.assertEqual(len(ad.fetch_new()), 1)
        self.assertEqual(ad.fetch_new(), [])  # 同 id 不再投递

    def test_media_kind_and_self(self):
        inbox = [{"id": "i2", "from": "f6ccb7afc370498fad7f68fd9635e5fb",
                  "fromNick": "小海", "type": "image", "scene": "team",
                  "target": "2870478248", "text": "", "time": 2}]
        ad, _ = self._ready(inbox)
        m = ad.fetch_new()[0]
        self.assertTrue(m.is_self)
        self.assertEqual(m.kind, "media")

    def test_fetch_replies_filters_bot(self):
        inbox = [{"id": "b1", "from": "bot", "fromNick": "小圆-总公司",
                  "type": "text", "scene": "team", "target": "2870478248",
                  "text": "回复", "time": 3},
                 {"id": "b2", "from": "h", "fromNick": "路人",
                  "type": "text", "scene": "team", "target": "2870478248",
                  "text": "闲聊", "time": 4}]
        ad, _ = self._ready(inbox)
        replies = ad.fetch_replies("小圆-总公司")
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].sender, "小圆-总公司")


class TestSend(unittest.TestCase):
    def _ready(self, receipt):
        ad, cdp = make_adapter({"imLayout": HOOK_OK, "imTeamInfoSession": TEAMS,
                                "resolve": receipt})
        ad.prepare()
        return ad, cdp

    def test_send_success_requires_id_server(self):
        ad, cdp = self._ready({"err": None, "idServer": "192634534359996396",
                               "status": "success"})
        ad.send("YT1 改地址")
        self.assertTrue(any("sendText" in c for c in cdp.calls))
        self.assertIn("2870478248", cdp.calls[-1])  # 发到绑定的群

    def test_send_err_is_retryable(self):
        ad, _ = self._ready({"err": "414 参数错误", "idServer": None, "status": "fail"})
        with self.assertRaises(RetryableSendError):
            ad.send("x")

    def test_send_no_receipt_is_uncertain(self):
        ad, _ = self._ready({"err": None, "idServer": None, "status": None})
        with self.assertRaises(SendUncertainError):
            ad.send("x")

    def test_send_empty_rejected(self):
        ad, _ = self._ready({"err": None, "idServer": "1", "status": "success"})
        with self.assertRaises(AdapterConfigurationError):
            ad.send("")

    def test_send_before_prepare_rejected(self):
        ad, _ = make_adapter({})  # 不调用 prepare
        with self.assertRaises(AdapterConfigurationError):
            ad.send("x")

    def test_send_mention_unsupported(self):
        ad, _ = self._ready({"err": None, "idServer": "1", "status": "success"})
        ad.prepare()
        with self.assertRaises(AdapterConfigurationError):
            ad.send_mention("客服", "text")


class TestJsPayloads(unittest.TestCase):
    """JS 载荷静态检查：防止手滑改坏关键结构。"""

    def test_hook_uses_imlayout_and_onmsg(self):
        self.assertIn("imLayout", _HOOK_JS)
        self.assertIn("onmsg", _HOOK_JS)
        self.assertIn("__kfInbox", _HOOK_JS)

    def test_inbox_clears_after_read(self):
        self.assertIn("__kfInbox = []", _INBOX_JS)

    def test_teams_from_localstorage(self):
        self.assertIn("imTeamInfoSession", _TEAMS_JS)


if __name__ == "__main__":
    unittest.main()
