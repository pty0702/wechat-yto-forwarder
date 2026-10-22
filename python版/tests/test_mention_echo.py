"""验证富文本提及草稿和可读消息回显分开核验，全程只使用模拟控件。"""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import windows_adapter as adapter
from test_adapter import FakeAuto, profile, row
from wechat_forwarder import SendUncertainError


class MentionEchoAuto(FakeAuto):
    """输入保留对象字符，发送后按测试场景生成可读或错误回显。"""

    def __init__(self):
        super().__init__()
        self.echo_text = "@客服 示例正文"

    def SendKeys(self, keys, **kwargs):
        if keys != "{Enter}":
            return super().SendKeys(keys, **kwargs)
        self.keys.append(keys)
        self.sent.append(self.draft)
        self.draft = ""
        if self.echo:
            self.messages.children.append(row("mention-echo", self.echo_sender, self.echo_text))


class EmbeddedMentionAdapter(adapter.WindowsChatAdapter):
    """只模拟提及组合钩子，不替换基类发送、草稿和回显核验。"""

    def _compose_mention(self, edit, staff, text):
        draft = "\ufffc " + text
        self.auto.draft = draft
        return draft, "@" + staff + " " + text


class FakeClock:
    """通过推进虚拟时间验证轮询边界，不让测试等待真实秒数。"""

    def __init__(self):
        self.now = 0.0
        self.waits = []
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.waits.append(duration)
        self.now += duration
        if self.on_sleep:
            self.on_sleep()


class DraftReadbackTests(unittest.TestCase):
    def setUp(self):
        self.auto = FakeAuto(value_pattern=False)
        self.client = adapter.WindowsChatAdapter(self.auto, profile(), "目标群", "客户管家", lambda line: None)
        self.text = "示例完整正文"
        self.reported = "示例"
        self.clock = FakeClock()
        pattern = self.auto.edit.patterns[self.auto.PatternId.TextPattern]
        pattern.GetText = self.read_draft

    def read_draft(self, length):
        if self.auto.draft and not self.auto.sent:
            if isinstance(self.reported, Exception):
                raise self.reported
            return self.reported
        return self.auto.draft

    def send(self):
        with patch.object(adapter.time, "monotonic", side_effect=self.clock.monotonic), \
                patch.object(adapter.time, "sleep", side_effect=self.clock.sleep):
            self.client.send(self.text)

    def assert_preserved_without_submit(self):
        self.assertEqual(self.auto.sent, [])
        self.assertEqual(self.auto.draft, self.text)
        self.assertEqual(self.auto.set_values, [])
        self.assertEqual(self.auto.keys, [self.text])

    def test_partial_readback_then_exact_sends_once_without_retyping(self):
        def refresh():
            if len(self.clock.waits) == 2:
                self.reported = self.text

        self.clock.on_sleep = refresh
        self.send()
        self.assertEqual(self.clock.waits, [0.05, 0.05])
        self.assertEqual(self.auto.keys, [self.text, "{Enter}"])
        self.assertEqual(self.auto.sent, [self.text])
        self.assertEqual(self.auto.draft, "")

    def test_persistent_mismatch_times_out_at_two_seconds_and_preserves_draft(self):
        with self.assertRaisesRegex(adapter.AdapterConfigurationError, "输入结果与原文不同"):
            self.send()
        self.assertAlmostEqual(self.clock.now, 2.0)
        self.assertLessEqual(max(self.clock.waits), 0.05)
        self.assert_preserved_without_submit()

    def test_group_change_during_readback_wait_stops_without_submit(self):
        self.clock.on_sleep = lambda: setattr(self.auto.title, "Name", "另一群")
        with self.assertRaisesRegex(adapter.AdapterConfigurationError, "当前会话不是配置的群"):
            self.send()
        self.assertEqual(self.clock.waits, [0.05])
        self.assert_preserved_without_submit()

    def test_focus_loss_during_readback_wait_stops_without_submit(self):
        self.clock.on_sleep = lambda: setattr(self.auto, "focused", None)
        with self.assertRaisesRegex(adapter.AdapterConfigurationError, "焦点已变化"):
            self.send()
        self.assertEqual(self.clock.waits, [0.05])
        self.assert_preserved_without_submit()

    def test_embedded_placeholder_error_stops_immediately_without_waiting(self):
        self.reported = "\ufffc"
        with self.assertRaisesRegex(adapter.AdapterConfigurationError, "嵌入对象占位符"):
            self.send()
        self.assertEqual(self.clock.waits, [])
        self.assert_preserved_without_submit()

    def test_read_error_during_wait_is_not_treated_as_temporary_mismatch(self):
        failure = RuntimeError("模拟输入控件读取失败")
        self.clock.on_sleep = lambda: setattr(self, "reported", failure)
        with self.assertRaisesRegex(adapter.AdapterConfigurationError, "模拟输入控件读取失败") as raised:
            self.send()
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(self.clock.waits, [0.05])
        self.assert_preserved_without_submit()


class MentionEchoTests(unittest.TestCase):
    def setUp(self):
        self.auto = MentionEchoAuto()
        self.client = EmbeddedMentionAdapter(self.auto, profile(), "目标群", "微信", lambda line: None)

    def send(self):
        # 最多执行一轮回显核验，失败分支无需实际等待三秒。
        with patch.object(adapter.time, "monotonic", side_effect=[0, 0, 4]), \
                patch.object(adapter.time, "sleep"):
            self.client.send_mention("客服", "示例正文")

    def test_readable_self_echo_confirms_embedded_mention_draft(self):
        self.send()
        self.assertEqual(self.auto.sent, ["\ufffc 示例正文"])
        self.assertEqual(self.auto.draft, "")
        self.assertEqual(self.auto.messages.children[-1].children[-1].Name, "@客服 示例正文")

    def test_raw_embedded_object_echo_does_not_confirm_readable_mention(self):
        self.auto.echo_text = "\ufffc 示例正文"
        with self.assertRaises(SendUncertainError):
            self.send()
        self.assertEqual(self.auto.sent, ["\ufffc 示例正文"])
        self.assertEqual(self.auto.keys.count("{Enter}"), 1)

    def test_wrong_target_or_body_echo_is_uncertain_without_retry(self):
        for echo in ("@另一客服 示例正文", "@客服 被截断", "示例正文"):
            with self.subTest(echo=echo):
                self.setUp()
                self.auto.echo_text = echo
                with self.assertRaises(SendUncertainError):
                    self.send()
                self.assertEqual(self.auto.keys.count("{Enter}"), 1)

    def test_matching_other_sender_echo_is_not_confirmation(self):
        self.auto.echo_sender = "其他成员"
        with self.assertRaises(SendUncertainError):
            self.send()
        self.assertEqual(self.auto.keys.count("{Enter}"), 1)

    def test_delayed_draft_clear_waits_before_reading_rows_and_submits_once(self):
        original_send = self.auto.SendKeys
        original_rows = self.client._rows
        waits = []

        def delayed_send(keys, **kwargs):
            if keys != "{Enter}":
                return original_send(keys, **kwargs)
            self.auto.keys.append(keys)
            self.auto.sent.append(self.auto.draft)
            # 模拟消息已追加、输入框稍晚才由客户端清空，程序不得主动清除。
            self.auto.messages.children.append(row("delayed-echo", "本人", "@客服 示例正文"))

        def client_progress(duration):
            waits.append(duration)
            if len(waits) == 2:
                self.auto.draft = ""

        def read_rows():
            if self.auto.sent:
                self.assertEqual(self.auto.draft, "", "草稿未清空时不能进入消息或多选读取")
            return original_rows()

        with patch.object(self.auto, "SendKeys", side_effect=delayed_send), \
                patch.object(self.client, "_rows", side_effect=read_rows) as rows, \
                patch.object(adapter.time, "monotonic", side_effect=[0, 0, 0.1, 0.2]), \
                patch.object(adapter.time, "sleep", side_effect=client_progress):
            self.client.send_mention("客服", "示例正文")
        self.assertEqual(waits, [0.15, 0.15])
        self.assertEqual(rows.call_count, 2)
        self.assertEqual(self.auto.keys.count("{Enter}"), 1)
        self.assertEqual(self.auto.sent, ["\ufffc 示例正文"])
        self.assertEqual(self.auto.draft, "")

    def test_changed_draft_is_preserved_and_never_submitted(self):
        def changed_draft(edit, staff, text):
            self.auto.draft = "\ufffc 意外内容"
            return "\ufffc " + text, "@" + staff + " " + text

        with patch.object(self.client, "_compose_mention", side_effect=changed_draft):
            with self.assertRaisesRegex(adapter.AdapterConfigurationError, "保留的草稿"):
                self.send()
        self.assertEqual(self.auto.draft, "\ufffc 意外内容")
        self.assertEqual(self.auto.sent, [])
        self.assertNotIn("{Enter}", self.auto.keys)


if __name__ == "__main__":
    unittest.main()
