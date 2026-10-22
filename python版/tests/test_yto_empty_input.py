"""验证客户管家已核对空叶的限定兼容，不读取真实窗口或发送真实消息。"""

from io import StringIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import windows_adapter as adapter
from wechat_forwarder import Message, RetryableSendError
from test_adapter import FakeAuto, Node, profile


def yto_profile(**changes):
    result = profile()
    result.update(parser="yto", process_name="YtoChatApp.exe", text_pattern_empty_leaf=True,
                  input={"automation_id": "edit-content"})
    result.update(changes)
    return result


class EmptyLeafProfileTests(unittest.TestCase):
    def load(self, item):
        data = json.dumps({"wx": profile(), "kf": item})
        with patch.object(Path, "open", return_value=StringIO(data)):
            return adapter.load_profile("模拟配置.json")

    def test_missing_option_defaults_false_for_both_sides(self):
        result = self.load(profile())
        self.assertIs(result["wx"]["text_pattern_empty_leaf"], False)
        self.assertIs(result["kf"]["text_pattern_empty_leaf"], False)

    def test_verified_yto_profile_can_explicitly_enable_option(self):
        result = self.load(yto_profile())
        self.assertIs(result["kf"]["text_pattern_empty_leaf"], True)

    def test_false_option_remains_available_to_other_adapters(self):
        result = self.load(dict(profile(), text_pattern_empty_leaf=False))
        self.assertIs(result["kf"]["text_pattern_empty_leaf"], False)

    def test_option_rejects_non_boolean_values(self):
        for invalid in ("false", "true", 0, 1, 0.0, None, [], {}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(adapter.AdapterConfigurationError, "必须为布尔值"):
                self.load(yto_profile(text_pattern_empty_leaf=invalid))

    def test_option_rejects_other_parser_or_process(self):
        for changes in ({"parser": "selectors"}, {"process_name": "Weixin.exe"},
                        {"process_name": "YtoChatApp.EXE"}, {"process_name": ""}):
            with self.subTest(changes=changes), self.assertRaisesRegex(adapter.AdapterConfigurationError, "仅允许"):
                self.load(yto_profile(**changes))


class EmptyLeafInputTests(unittest.TestCase):
    def setUp(self):
        self.auto = FakeAuto(value_pattern=False)
        self.auto.edit.ControlTypeName = "GroupControl"
        self.auto.edit.AutomationId = "edit-content"
        self.auto.window.ProcessId = 123
        self.auto.GetProcesses = lambda unused: [SimpleNamespace(pid=123, exeName="YtoChatApp.exe")]
        self.client = adapter.WindowsChatAdapter(self.auto, yto_profile(), "目标群", "客户管家", lambda _: None)

    def assert_placeholder_rejected(self):
        with self.assertRaisesRegex(adapter.AdapterConfigurationError, "嵌入对象占位符"):
            self.client._value(self.auto.edit)
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.sent, [])

    def test_exact_placeholder_in_verified_empty_leaf_becomes_empty(self):
        self.auto.draft = "\ufffc"
        self.assertEqual(self.client._value(self.auto.edit), "")
        self.assertEqual(self.auto.draft, "\ufffc")

    def test_option_is_off_without_explicit_true(self):
        self.auto.draft = "\ufffc"
        self.client.profile.pop("text_pattern_empty_leaf")
        self.assert_placeholder_rejected()
        for value in (False, 1, "true"):
            self.client.profile["text_pattern_empty_leaf"] = value
            with self.subTest(value=value):
                self.assert_placeholder_rejected()

    def test_directly_constructed_wrong_profile_cannot_bypass_scope(self):
        self.auto.draft = "\ufffc"
        for key, wrong_value in (("parser", "selectors"), ("process_name", "Weixin.exe")):
            original = self.client.profile[key]
            self.client.profile[key] = wrong_value
            with self.subTest(key=key):
                self.assert_placeholder_rejected()
            self.client.profile[key] = original

    def test_whitespace_mixed_and_multiple_placeholders_are_rejected_before_suffix(self):
        self.client.profile["text_pattern_suffix"] = "\ufffc"
        for draft in (" \ufffc", "\ufffc\n", "\ufffc\ufffc", "原有草稿\ufffc", "\ufffc原有草稿"):
            self.auto.draft = draft
            with self.subTest(draft=draft):
                self.assert_placeholder_rejected()
                self.assertEqual(self.auto.draft, draft)

    def test_placeholder_with_any_child_is_rejected(self):
        self.auto.draft = "\ufffc"
        for child in (Node("", kind="TextControl"), Node("隐藏草稿", kind="TextControl"), Node()):
            self.auto.edit.children = [child]
            with self.subTest(child=child.ControlTypeName):
                self.assert_placeholder_rejected()

    def test_wrong_input_type_or_id_is_rejected(self):
        self.auto.draft = "\ufffc"
        self.auto.edit.ControlTypeName = "EditControl"
        self.assert_placeholder_rejected()
        self.auto.edit.ControlTypeName = "GroupControl"
        self.auto.edit.AutomationId = "search-input"
        self.assert_placeholder_rejected()

    def test_unreadable_children_are_not_assumed_empty(self):
        self.auto.draft = "\ufffc"
        self.auto.edit.GetChildren = Mock(side_effect=RuntimeError("控件已失效"))
        with self.assertRaisesRegex(RuntimeError, "控件已失效"):
            self.client._value(self.auto.edit)
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.sent, [])

    def test_normal_text_and_configured_suffix_keep_existing_behavior(self):
        self.auto.edit.children = [Node("自己的草稿", kind="TextControl")]
        for draft, expected in (("", ""), ("普通草稿", "普通草稿"), ("第一行\r\n第二行", "第一行\n第二行")):
            self.auto.draft = draft
            with self.subTest(draft=draft):
                self.assertEqual(self.client._value(self.auto.edit), expected)
        self.client.profile["text_pattern_suffix"] = "\r"
        self.auto.draft = "正文\r"
        self.assertEqual(self.client._value(self.auto.edit), "正文")

    def test_existing_real_draft_is_never_overwritten(self):
        self.auto.draft = "原有草稿"
        with self.assertRaisesRegex(RetryableSendError, "已有草稿"):
            self.client.send("测试消息")
        self.assertEqual(self.auto.draft, "原有草稿")
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.sent, [])

    def test_mock_send_checks_written_text_and_return_to_empty_leaf(self):
        text_pattern = self.auto.edit.patterns[self.auto.PatternId.TextPattern]
        text_pattern.GetText = lambda length: self.auto.draft if self.auto.draft else "\ufffc"
        self.auto.edit.GetChildren = lambda: ([Node(self.auto.draft, kind="TextControl")] if self.auto.draft else [])
        anchor = Message("客户", "历史消息", message_id="anchor")

        def rows():
            return [anchor] + [Message("本人", text, message_id=f"sent-{i}", is_self=True)
                               for i, text in enumerate(self.auto.sent)]

        with patch.object(self.client, "_rows", side_effect=rows):
            self.client.prepare()
            self.client.send("YT0719078698064 第一行\n第二行")
        self.assertEqual(self.auto.sent, ["YT0719078698064 第一行\n第二行"])
        self.assertEqual(self.auto.draft, "")
        self.assertEqual(self.client._value(self.auto.edit), "")
        self.assertEqual(self.auto.keys.count("{Enter}"), 1)

    def test_value_pattern_does_not_apply_text_pattern_empty_leaf_rule(self):
        self.auto.edit.patterns[self.auto.PatternId.ValuePattern] = SimpleNamespace(Value="\ufffc")
        self.assertEqual(self.client._value(self.auto.edit), "\ufffc")


if __name__ == "__main__":
    unittest.main()
