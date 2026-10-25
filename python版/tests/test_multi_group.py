# -*- coding: utf-8 -*-
"""多微信群（一个客服群）与多客服 @ 的行为测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wechat_forwarder import (  # noqa: E402
    Message, PendingQueue, Forwarder, validate_config, split_staff,
)


def cfg_two_groups(**overrides):
    cfg = {
        "wx_group": "A群", "wx_groups": ["A群", "B群"],
        "kf_group": "客服群", "bot_name": "小圆-总公司",
        "service_staff": "客服A、客服B",
        "block_keywords": ["全局屏蔽"], "at_keywords": ["加急"],
        "group_policies": {"B群": {"block_keywords": ["B专属屏蔽"]}},
        "reset_seconds": 30, "poll_interval": 1,
    }
    cfg.update(overrides)
    return validate_config(cfg)


class RecordingSink:
    def __init__(self):
        self.sent = []       # (text, group)
        self.mentions = []   # (staff, text, group)

    def send(self, text, group=""):
        self.sent.append((text, group))

    def send_mention(self, staff, text, group=""):
        self.mentions.append((staff, text, group))


class ListSource:
    def __init__(self, messages):
        self.messages = messages

    def fetch_new(self):
        out, self.messages = self.messages, []
        return out

    def fetch_replies(self, bot_name):
        out, self.messages = self.messages, []
        return [m for m in out if m.sender == bot_name]


class KfRecordingSink:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def make_forwarder(cfg, wx_messages=(), kf_messages=()):
    sink = RecordingSink()
    kf_sink = KfRecordingSink()
    forwarder = Forwarder(
        cfg, ListSource(list(wx_messages)), sink,
        ListSource(list(kf_messages)), kf_sink,
        log=lambda *_: None,
    )
    return forwarder, sink, kf_sink


class SplitStaffTests(unittest.TestCase):
    def test_split_by_chinese_and_ascii_separators(self):
        self.assertEqual(split_staff("客服A、客服B"), ["客服A", "客服B"])
        self.assertEqual(split_staff("客服A，客服B, 客服C；客服D"),
                         ["客服A", "客服B", "客服C", "客服D"])
        self.assertEqual(split_staff("客服A、客服A"), ["客服A"])
        self.assertEqual(split_staff(""), [])


class ConfigTests(unittest.TestCase):
    def test_single_group_config_upgrades_to_list(self):
        cfg = validate_config({"wx_group": "旧群", "kf_group": "k",
                               "bot_name": "小圆-总公司"})
        self.assertEqual(cfg["wx_groups"], ["旧群"])
        self.assertEqual(cfg["wx_group"], "旧群")

    def test_policy_group_must_be_in_list(self):
        with self.assertRaises(ValueError):
            validate_config({"wx_group": "A群", "kf_group": "k",
                             "bot_name": "小圆-总公司",
                             "group_policies": {"别的群": {"block_keywords": ["x"]}}})


class MultiGroupRoutingTests(unittest.TestCase):
    def test_messages_carry_group_and_forward_records_origin(self):
        cfg = cfg_two_groups()
        wx_msg = Message("客户", "YT1111111111111 出港", group="A群")
        forwarder, sink, kf_sink = make_forwarder(cfg, wx_messages=[wx_msg])
        forwarder.poll_once()
        self.assertEqual(kf_sink.sent, ["YT1111111111111 出港"])
        self.assertEqual(sink.sent, [])
        self.assertEqual(forwarder.queue.group_of("YT1111111111111"), "A群")

    def test_reply_routes_back_to_origin_group(self):
        cfg = cfg_two_groups()
        wx_msg = Message("客户", "YT1111111111111 出港", group="B群")
        forwarder, sink, _kf = make_forwarder(cfg, wx_messages=[wx_msg])
        forwarder.poll_once()                     # 第一轮：转发，登记来源群
        forwarder.kf_src.messages.append(
            Message("小圆-总公司", "YT1111111111111 已处理", group="客服群"))
        forwarder.poll_once()                     # 第二轮：回复按运单号搬回原群
        self.assertEqual(sink.sent, [("YT1111111111111 已处理", "B群")])

    def test_policy_overrides_keywords_per_group(self):
        cfg = cfg_two_groups()
        a_msg = Message("客户", "YT1111111111111 B专属屏蔽", group="A群")   # A 群用全局词：不屏蔽
        b_msg = Message("客户", "YT2222222222222 B专属屏蔽", group="B群")   # B 群覆盖：屏蔽
        forwarder, sink, kf_sink = make_forwarder(cfg, wx_messages=[a_msg, b_msg])
        forwarder.poll_once()
        self.assertEqual(kf_sink.sent, ["YT1111111111111 B专属屏蔽"])

    def test_at_service_mentions_in_origin_group_with_staff_list(self):
        cfg = cfg_two_groups()
        msg = Message("客户", "YT1111111111111 加急", group="B群")
        forwarder, sink, kf_sink = make_forwarder(cfg, wx_messages=[msg])
        forwarder.poll_once()
        self.assertEqual(sink.mentions, [("客服A、客服B", "YT1111111111111 加急", "B群")])
        self.assertEqual(kf_sink.sent, [])


if __name__ == "__main__":
    unittest.main()
