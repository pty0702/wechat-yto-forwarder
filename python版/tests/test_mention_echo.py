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
