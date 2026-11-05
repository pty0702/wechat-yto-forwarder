# -*- coding: utf-8 -*-
"""kf_nim_adapter（DOM 读取 + 云信发送混合通道）单元测试：CDP 全 mock。

读取行为继承自 KfDomAdapter，由 test_kf_dom_adapter.py 覆盖；这里聚焦
prepare（云信实例/群解析）与 send（SDK 回执判定）。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kf_nim_adapter import KfNimAdapter, _HOOK_JS, _TEAMS_JS  # noqa: E402
from wechat_forwarder import RetryableSendError, SendUncertainError  # noqa: E402
from windows_adapter import AdapterConfigurationError  # noqa: E402


class FakeCdp:
    """按表达式关键字返回预置结果的假 CDP 客户端。

    预置值原样返回：str 模拟页面 stringify 的结果，list 模拟
    returnByValue 直接回传的数组（KfDomAdapter._rows 依赖后者）。
    """

    def __init__(self, script):
        self.script = script
        self.calls = []

    def evaluate(self, expression):
        self.calls.append(expression)
        for key, value in self.script.items():
            if key in expression:
                return value
        return None


TEAMS = [{"teamId": "2870478248", "name": "路晓燕客户服务群"},
         {"teamId": "2869353312", "name": "潼阳-梁小龙客户服务群"}]

HOOK_OK = {"ok": True, "account": "f6ccb7afc370498fad7f68fd9635e5fb"}

# DOM 基线（KfDomAdapter.prepare 需要）：标题匹配 + 空消息行
DOM_BASE = {"p.p-t-14 span.cursor-default": "路晓燕客户服务群",
            "document.querySelectorAll('div[id]')": []}


def make_adapter(extra, group="路晓燕客户服务群"):
    script = dict(DOM_BASE)
    for k, v in extra.items():
        script[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
    cdp = FakeCdp(script)
    ad = KfNimAdapter({"self_names": ["小圆", "小海"], "cdp_url": "http://127.0.0.1:9223"},
                      group, log=lambda *a: None, cdp=cdp)
    return ad, cdp


class TestPrepare(unittest.TestCase):
    def test_prepare_ok_binds_team(self):
        ad, _ = make_adapter({"imLayout": HOOK_OK, "imTeamInfoSession": TEAMS})
        ad.prepare()
        self.assertEqual(ad._team_id, "2870478248")
        self.assertTrue(ad._baseline_done)

    def test_prepare_hook_fail(self):
        ad, _ = make_adapter({"imLayout": {"ok": False, "why": "页面未就绪"}})
        with self.assertRaises(AdapterConfigurationError):
            ad.prepare()

    def test_prepare_requires_exact_dom_title(self):
        """混合通道群名必须与客户端当前会话精确一致（DOM 读取硬约束，
        模糊匹配只存在于云信 team 缓存层，过不了标题校验）。"""
        ad, _ = make_adapter({"imLayout": HOOK_OK, "imTeamInfoSession": TEAMS},
                             group="路晓燕客户服务群（备用）")
        with self.assertRaises(Exception):
            ad.prepare()


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
        with self.assertRaises(AdapterConfigurationError):
            ad.send_mention("客服", "text")


class TestJsPayloads(unittest.TestCase):
    """JS 载荷静态检查：防止手滑改坏关键结构。"""

    def test_hook_uses_imlayout_and_sendtext(self):
        self.assertIn("imLayout", _HOOK_JS)
        self.assertIn("sendText", _HOOK_JS)

    def test_send_promise_waits_receipt(self):
        from kf_nim_adapter import _send_js
        js = _send_js("你好", "123")
        self.assertIn("new Promise", js)
        self.assertIn("idServer", js)

    def test_teams_from_localstorage(self):
        self.assertIn("imTeamInfoSession", _TEAMS_JS)


if __name__ == "__main__":
    unittest.main()
