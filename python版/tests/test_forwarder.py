# -*- coding: utf-8 -*-
"""业务规则、可靠收发及配置文件的回归测试，不操作真实聊天窗口。"""

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import wechat_forwarder as core


WB1 = "YT0719078698064"
WB2 = "YT0719078611111"


class ForwarderTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.wx = core.MockWxSource()
        self.kf = core.MockKfSource()
        self.sent_wx = []
        self.sent_kf = []
        self.cfg = core.validate_config({
            "wx_group": "客户群", "kf_group": "客服群",
            "block_keywords": ["退回"], "at_keywords": ["投诉"],
            "service_staff": "客服张", "bot_name": "小圆", "poll_interval": 0.001,
        })
        self.forwarder = core.Forwarder(
            self.cfg, self.wx, core.MockWxSink(self.sent_wx),
            self.kf, core.MockKfSink(self.sent_kf), self.logs.append,
        )

    def test_same_text_distinct_events_forward_but_same_event_deduplicates(self):
        first = core.Message("客户", f"{WB1} 查件", 123.0)
        second = core.Message("客户", first.content, 123.0)
        self.assertNotEqual(first.message_id, second.message_id)
        self.assertEqual(first.ts, 123.0)
        self.wx.inbox = [first, first, second]
        self.forwarder.poll_wx()
        self.wx.inbox = [core.Message("客户", first.content, message_id=first.message_id)]
        self.forwarder.poll_wx()
        self.assertEqual(self.sent_kf, [first.content, second.content])

    def test_same_reply_text_distinct_events_are_all_returned(self):
        self.forwarder.queue.add(WB1)
        first = core.Message("小圆", "已经受理")
        second = core.Message("小圆", "已经受理")
        self.kf.inbox = [first, first, second]
        self.forwarder.poll_kf()
        self.kf.inbox = [first]
        self.forwarder.poll_kf()
        self.assertEqual(self.sent_wx, ["已经受理", "已经受理"])

    def test_rules_ignore_self_media_and_prioritize_block_keywords(self):
        cases = [
            (core.Message("客户", f"{WB1} 投诉退回"), core.Verdict.IGNORE),
            (core.Message("客户", f"{WB1} 投诉"), core.Verdict.AT_SERVICE),
            (core.Message("客户", f"{WB1} 查件"), core.Verdict.FORWARD),
            (core.Message("本人", f"{WB1} 查件", is_self=True), core.Verdict.IGNORE),
            (core.Message("客户", f"{WB1} 图片标题", kind="image"), core.Verdict.IGNORE),
            (core.Message("客户", f"查询 {WB1}"), core.Verdict.IGNORE),
            (core.Message("客户", "[图片]"), core.Verdict.IGNORE),
            (core.Message("客户", "YT1234567890123456 查件"), core.Verdict.IGNORE),
        ]
        for message, expected in cases:
            with self.subTest(content=message.content, kind=message.kind):
                self.assertEqual(core.classify(message, self.cfg), expected)

    def test_mentions_use_real_mention_interface(self):
        calls = []
        followups = []

        class MentionSink(core.WxSink):
            def send(self, text, group=""):
                # @ 提醒本身不允许走普通文本接口；跟进消息（转人工处理）是
                # 合法的普通文本，单独记录。
                followups.append((text, group))

            def send_mention(self, staff, text, group=""):
                calls.append((staff, text))

        self.forwarder.wx_snk = MentionSink()
        self.wx.inbox = [core.Message("客户", f"{WB1} 投诉")]
        self.forwarder.poll_wx()
        self.assertEqual(calls, [("客服张", "")])
        self.assertEqual(followups, [("转人工处理", "客户群")])
        self.assertEqual(self.sent_kf, [])

    def test_retryable_send_failure_skips_message_and_continues(self):
        """可重试失败同样跳过该条继续（用户策略：不阻塞下一条）。"""
        messages = [core.Message("客户", f"{WB1} 请求{index}") for index in range(3)]
        calls = []
        failures = [False, True, False]

        class FlakySink(core.KfSink):
            def send(self, text, group=""):
                if failures.pop(0):
                    raise core.RetryableSendError("发送前窗口暂时不可用")
                calls.append(text)

        self.forwarder.kf_snk = FlakySink()
        self.wx.inbox = messages[:]
        self.forwarder.poll_wx()          # 不抛错；第二条失败被跳过
        self.assertEqual(calls, [messages[0].content, messages[2].content])
        self.assertTrue(any("跳过" in line for line in self.logs))
        # 已跳过的消息不会在下一轮重发
        self.forwarder.poll_wx()
        self.assertEqual(len(calls), 2)

    def test_retryable_reply_failure_skips_reply_without_losing_queue(self):
        now = [100.0]
        failures = [True, False]
        delivered = []

        class FlakySink(core.WxSink):
            def send(self, text, group=""):
                if failures.pop(0):
                    raise core.RetryableSendError("确认尚未发出")
                delivered.append(text)

        self.forwarder.wx_snk = FlakySink()
        replies = [core.Message("小圆", f"{WB1} 已受理"), core.Message("小圆", "已完成")]
        with patch.object(core.time, "monotonic", side_effect=lambda: now[0]):
            self.forwarder.queue.add(WB1)
            now[0] = 110.0
            self.kf.inbox = replies
            self.forwarder.poll_kf()      # 第一条回复失败被跳过，第二条正常搬回
        self.assertEqual(delivered, ["已完成"])
        # 失败那条没 refresh，等待计时不变；第二条 refresh 后仍会被超时清理
        self.assertTrue(any("跳过" in line for line in self.logs))

    def test_old_reply_is_read_before_new_request_can_create_pending_entry(self):
        self.kf.inbox = [core.Message("小圆", f"{WB1} 转发前已有的旧回复")]
        self.wx.inbox = [core.Message("客户", f"{WB1} 查件")]
        self.forwarder.poll_once()
        self.assertEqual(self.sent_wx, [])
        self.assertEqual(self.sent_kf, [f"{WB1} 查件"])
        self.kf.inbox = [core.Message("小圆", f"{WB1} 新回复")]
        self.forwarder.poll_once()
        self.assertEqual(self.sent_wx, [f"{WB1} 新回复"])

    def test_expired_request_cannot_be_revived_by_late_reply(self):
        now = [100.0]
        with patch.object(core.time, "monotonic", side_effect=lambda: now[0]):
            self.forwarder.queue.add(WB1)
            now[0] = 130.0
            self.kf.inbox = [core.Message("小圆", f"{WB1} 迟到回复")]
            self.forwarder.poll_once()
        self.assertEqual(self.sent_wx, [])
        self.assertTrue(self.forwarder.queue.is_empty())

    def test_timeout_is_checked_again_after_slow_window_read(self):
        now = [100.0]

        class SlowSource(core.KfSource):
            def fetch_replies(self, bot_name):
                now[0] = 131.0
                return [core.Message("小圆", f"{WB1} 读取时已过期")]

        with patch.object(core.time, "monotonic", side_effect=lambda: now[0]):
            self.forwarder.queue.add(WB1)
            self.forwarder.kf_src = SlowSource()
            self.forwarder.poll_kf()
        self.assertEqual(self.sent_wx, [])

    def test_reply_without_waybill_refreshes_oldest_only(self):
        now = [100.0]
        with patch.object(core.time, "monotonic", side_effect=lambda: now[0]):
            self.forwarder.queue.add(WB1)
            now[0] = 110.0
            self.forwarder.queue.add(WB2)
            now[0] = 120.0
            self.kf.inbox = [core.Message("小圆", "正在处理")]
            self.forwarder.poll_kf()
        self.assertEqual(self.forwarder.queue._items[WB1][0], 120.0)
        self.assertEqual(self.forwarder.queue._items[WB2][0], 110.0)
        self.assertEqual(self.sent_wx, ["正在处理"])

    def test_core_checks_bot_exact_name_and_filters_self_and_media(self):
        class UnfilteredSource(core.KfSource):
            def fetch_replies(self, bot_name):
                return [
                    core.Message("小圆-总公司", f"{WB1} 名称不匹配"),
                    core.Message("小圆", f"{WB1} 本人", is_self=True),
                    core.Message("小圆", f"{WB1} 图片", kind="image"),
                    core.Message("人工客服", f"{WB1} 人工回复"),
                    core.Message("小圆", f"{WB1} 确切匹配"),
                ]

        self.forwarder.queue.add(WB1)
        self.forwarder.kf_src = UnfilteredSource()
        self.forwarder.poll_kf()
        self.assertEqual(self.sent_wx, [f"{WB1} 确切匹配"])

    def test_unknown_waybill_and_empty_queue_replies_are_ignored(self):
        self.kf.inbox = [core.Message("小圆", "队列为空")]
        self.forwarder.poll_kf()
        self.forwarder.queue.add(WB1)
        self.kf.inbox = [core.Message("小圆", f"{WB2} 非本次转发")]
        self.forwarder.poll_kf()
        self.assertEqual(self.sent_wx, [])

    def test_send_uncertainty_skips_message_and_continues_next(self):
        """不确定发送失败：跳过该条继续下一条，不再停机（用户策略）。"""
        sent = []

        class UncertainSink(core.KfSink):
            def send(self, text, group=""):
                sent.append(text)
                raise core.SendUncertainError("已按发送，但回执未确认")

        self.forwarder.kf_snk = UncertainSink()
        self.wx.inbox = [core.Message("客户", f"{WB1} 查件"),
                         core.Message("客户", f"{WB2} 第二条")]
        self.forwarder.poll_wx()          # 不应抛错停机
        # 第一条失败被跳过后，第二条仍被继续处理（各尝试一次，不重发）
        self.assertEqual(sent, [f"{WB1} 查件", f"{WB2} 第二条"])
        self.assertTrue(any("跳过" in line and "核对" in line for line in self.logs))

    def test_unclassified_send_exception_skips_and_continues(self):
        class UnknownSink(core.KfSink):
            def send(self, text, group=""):
                raise OSError("发送过程中窗口关闭")

        self.forwarder.kf_snk = UnknownSink()
        self.wx.inbox = [core.Message("客户", f"{WB1} 查件"),
                         core.Message("客户", f"{WB2} 第二条")]
        self.forwarder.poll_wx()          # 未分类异常同样跳过继续
        self.assertTrue(any("跳过" in line for line in self.logs))

    def test_run_continues_after_send_failure(self):
        """运行中某条发送失败：跳过继续轮询，不停机（用户策略）。"""
        attempts = []
        event = threading.Event()

        class FlakyOnceSink(core.KfSink):
            def send(self, text, group=""):
                attempts.append(text)
                if len(attempts) == 1:
                    raise core.RetryableSendError("窗口暂不可用")
                event.set()          # 第二条成功后结束运行

        self.forwarder.kf_snk = FlakyOnceSink()
        self.wx.inbox = [core.Message("客户", f"{WB1} 查件")]
        self.forwarder.poll_wx()     # 第一条失败被跳过
        self.wx.inbox = [core.Message("客户", f"{WB2} 再查")]
        self.forwarder.run(event)    # 第二条正常处理并结束
        self.assertEqual(attempts, [f"{WB1} 查件", f"{WB2} 再查"])
        self.assertTrue(any("跳过" in line for line in self.logs))

    def test_stop_interrupts_long_poll_wait(self):
        event = threading.Event()
        first_poll = threading.Event()
        self.forwarder.cfg["poll_interval"] = 60.0
        with patch.object(self.forwarder, "poll_once", side_effect=first_poll.set):
            worker = threading.Thread(target=self.forwarder.run, args=(event,), daemon=True)
            worker.start()
            self.assertTrue(first_poll.wait(1.0))
            event.set()
            worker.join(1.0)
        self.assertFalse(worker.is_alive())

    def test_recent_event_history_is_bounded(self):
        history = core._RecentIds(limit=3)
        for number in range(10):
            history.add(str(number))
        self.assertEqual(len(history), 3)
        self.assertNotIn("0", history)
        self.assertIn("9", history)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        # 测试文件始终建在项目目录内，不借用系统临时目录。
        self.temp = tempfile.TemporaryDirectory(prefix=".test-config-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.config_patch = patch.object(core, "CONFIG_FILE", str(self.path))
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        # 迁移源也指向临时目录（不存在），避免读到开发机真实配置
        self.legacy_patch = patch.object(core, "_LEGACY_CONFIG_FILE",
                                         str(self.path.parent / "legacy-none.json"))
        self.legacy_patch.start()
        self.addCleanup(self.legacy_patch.stop)

    def test_old_config_gets_defaults_and_is_not_mutated(self):
        old = {"wx_group": " 群A ", "block_keywords": ["退回", " 退回 ", ""]}
        normalized = core.validate_config(old)
        self.assertEqual(normalized["wx_group"], "群A")
        self.assertEqual(normalized["block_keywords"], ["退回"])
        self.assertEqual(normalized["mode"], "demo")
        self.assertEqual(old["wx_group"], " 群A ")
        normalized["wx_profile"]["title"] = "测试"
        self.assertEqual(core.DEFAULT_CONFIG["wx_profile"], {})

    def test_invalid_values_are_rejected(self):
        invalid = [
            [], {"poll_interval": 0}, {"poll_interval": True},
            {"reset_seconds": float("nan")}, {"reset_seconds": "inf"},
            {"block_keywords": "退回"}, {"at_keywords": [3]},
            {"bot_name": " "}, {"wx_profile": []}, {"mode": "unknown"},
        ]
        for cfg in invalid:
            with self.subTest(config=cfg), self.assertRaises(ValueError):
                core.validate_config(cfg)

    def test_live_requires_groups_and_staff_when_needed(self):
        with self.assertRaises(ValueError):
            core.validate_config({}, live=True)
        with self.assertRaises(ValueError):
            core.validate_config({"wx_group": "客户", "kf_group": "客服", "at_keywords": ["投诉"]}, live=True)
        normalized = core.validate_config({
            "wx_group": "客户", "kf_group": "客服", "at_keywords": ["投诉"],
            "service_staff": "张客服", "poll_interval": "0.25",
        }, live=True)
        self.assertEqual(normalized["poll_interval"], 0.25)

    def test_missing_file_uses_defaults_but_corrupt_file_is_preserved(self):
        self.assertEqual(core.load_config()["mode"], "demo")
        damaged = '{"wx_group":'
        self.path.write_text(damaged, encoding="utf-8")
        with self.assertRaises(ValueError):
            core.load_config()
        self.assertEqual(self.path.read_text(encoding="utf-8"), damaged)

    def test_utf8_bom_old_config_is_accepted(self):
        self.path.write_text(json.dumps({"wx_group": "中文群"}, ensure_ascii=False), encoding="utf-8-sig")
        self.assertEqual(core.load_config()["wx_group"], "中文群")

    def test_atomic_save_roundtrip_and_failed_replace_preserves_old_file(self):
        core.save_config({"wx_group": "原群"})
        before = self.path.read_bytes()
        with patch.object(core.os, "replace", side_effect=OSError("磁盘写入失败")):
            with self.assertRaises(OSError):
                core.save_config({"wx_group": "新群"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".config-*.tmp")), [])
        core.save_config({"wx_group": "新群"})
        self.assertEqual(core.load_config()["wx_group"], "新群")


if __name__ == "__main__":
    unittest.main()
