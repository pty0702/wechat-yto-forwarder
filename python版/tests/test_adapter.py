"""使用模拟控件验证适配边界，不连接或操作真实客户端。"""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from message_snapshot import SnapshotTracker
from wechat_forwarder import Message, RetryableSendError, SendUncertainError
import windows_adapter as adapter


class Node:
    """只实现适配器实际使用的 UIA 契约。"""

    _next_id = 1

    def __init__(self, name="", automation_id="", kind="GroupControl", children=()):
        self.Name = name
        self.AutomationId = automation_id
        self.ControlTypeName = kind
        self.ClassName = "模拟控件"
        self.children = list(children)
        self.runtime_id = [7, Node._next_id]
        Node._next_id += 1
        self.patterns = {}
        self.on_focus = None
        self.on_click = None

    def GetChildren(self):
        return self.children

    def GetRuntimeId(self):
        return self.runtime_id

    def GetPattern(self, pattern_id):
        return self.patterns.get(pattern_id)

    def SetActive(self, **kwargs):
        return True

    def SetFocus(self):
        return self.on_focus() if self.on_focus else True

    def Click(self, **kwargs):
        if self.on_click:
            self.on_click()


class ValuePattern:
    IsReadOnly = False

    def __init__(self, auto):
        self.auto = auto

    @property
    def Value(self):
        return self.auto.draft

    def SetValue(self, value):
        self.auto.set_values.append(value)
        self.auto.draft = value
        if self.auto.after_set_value:
            self.auto.after_set_value()


class TextPattern:
    def __init__(self, auto):
        self.auto = auto
        self.DocumentRange = self

    def GetText(self, length):
        return self.auto.draft


def row(event_id, sender="客户", content="YT0719078698064 查件"):
    return Node(automation_id=event_id, children=[
        Node(sender, "sender", "TextControl"),
        Node(content, "content", "TextControl"),
    ])


def profile():
    return {
        "window": {"automation_id": "window"},
        "group_title": {"automation_id": "title"},
        "messages": {"automation_id": "messages"},
        "message_item": {"type": "GroupControl", "depth": 1},
        "input": {"automation_id": "input"},
        "sender": {"automation_id": "sender"},
        "content": {"automation_id": "content"},
        "self_names": ["本人"],
        "id_attribute": "automation_id",
    }


class FakeAuto:
    class PatternId:
        ValuePattern = 1
        TextPattern = 2

    def __init__(self, value_pattern=True):
        self.title = Node("目标群", "title", "TextControl")
        self.messages = Node(automation_id="messages", children=[row("1")])
        self.edit = Node(automation_id="input", kind="EditControl")
        self.window = Node(automation_id="window", children=[self.title, self.messages, self.edit])
        self.root = Node(children=[self.window])
        self.focused = None
        self.draft = ""
        self.keys = []
        self.set_values = []
        self.sent = []
        self.send_error = None
        self.echo = True
        self.echo_sender = "本人"
        self.after_set_value = None
        self.edit.on_focus = self._focus
        if value_pattern:
            self.edit.patterns[self.PatternId.ValuePattern] = ValuePattern(self)
        else:
            self.edit.patterns[self.PatternId.TextPattern] = TextPattern(self)

    def _focus(self):
        self.focused = self.edit
        return True

    def GetRootControl(self):
        return self.root

    def GetFocusedControl(self):
        return self.focused

    def SendKeys(self, keys, **kwargs):
        self.keys.append(keys)
        if keys == "{Enter}":
            self.sent.append(self.draft)
            if self.send_error:
                raise self.send_error
            if self.echo:
                self.messages.children.append(row(str(100 + len(self.sent)), self.echo_sender, self.draft))
                self.draft = ""
        elif keys == "{Shift}{Enter}":
            self.draft += "\n"
        elif keys != "{Ctrl}{End}":
            # 解码文字键入中的花括号；不实现真实键盘操作。
            index = 0
            while index < len(keys):
                if keys.startswith("{{}", index):
                    self.draft += "{"
                    index += 3
                elif keys.startswith("{}}", index):
                    self.draft += "}"
                    index += 3
                else:
                    self.draft += keys[index]
                    index += 1


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.tracker = SnapshotTracker(self.logs.append)

    def messages(self, *identifiers):
        return [Message("客户", "相同内容", message_id=identifier) for identifier in identifiers]

    def test_first_nonempty_snapshot_only_establishes_baseline(self):
        self.assertEqual(self.tracker.update([]), [])
        self.assertEqual(self.tracker.update([]), [])
        self.assertEqual(self.tracker.update(self.messages("1", "2")), [])
        new = self.tracker.update(self.messages("1", "2", "3"))
        self.assertEqual([message.message_id for message in new], ["3"])

    def test_same_text_different_ids_is_new_but_repeat_snapshot_is_not(self):
        self.tracker.update(self.messages("1"))
        self.assertEqual(len(self.tracker.update(self.messages("1", "2"))), 1)
        self.assertEqual(self.tracker.update(self.messages("1", "2")), [])

    def test_lost_overlap_rebuilds_baseline(self):
        self.tracker.update(self.messages("1", "2"))
        self.assertEqual(self.tracker.update(self.messages("8", "9")), [])
        self.assertTrue(self.logs)
        self.assertEqual([m.message_id for m in self.tracker.update(self.messages("9", "10"))], ["10"])

    def test_transient_empty_snapshot_preserves_anchor(self):
        self.tracker.update(self.messages("1"))
        self.tracker.update([])
        self.assertEqual([m.message_id for m in self.tracker.update(self.messages("1", "2"))], ["2"])

    def test_scrolling_up_with_overlap_does_not_forward_history(self):
        self.tracker.update(self.messages("2", "3"))
        self.assertEqual(self.tracker.update(self.messages("1", "2", "3")), [])
        self.assertEqual([m.message_id for m in self.tracker.update(self.messages("0", "1", "2", "3", "4"))], ["4"])

    def test_duplicate_or_missing_event_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            self.tracker.update(self.messages("1", "1"))
        broken = Message("客户", "内容")
        broken.message_id = ""
        with self.assertRaises(ValueError):
            self.tracker.update([broken])

    def test_cache_is_bounded_and_rejects_oversized_snapshot(self):
        tracker = SnapshotTracker(capacity=2)
        tracker.update(self.messages("1", "2"))
        tracker.update(self.messages("2", "3"))
        self.assertEqual(len(tracker._known), 2)
        with self.assertRaises(ValueError):
            tracker.update(self.messages("1", "2", "3"))
        with self.assertRaises(ValueError):
            SnapshotTracker(capacity=0)


class SelectorAndParserTests(unittest.TestCase):
    def test_unique_selector_rejects_ambiguous_or_missing_controls(self):
        root = Node(children=[Node("同名"), Node("同名")])
        for name in ("同名", "不存在"):
            with self.subTest(name=name), self.assertRaises(adapter.AdapterConfigurationError):
                adapter.select(root, {"name": name})

    def test_selector_path_can_scope_to_unique_descendant(self):
        wanted = Node("输入框")
        root = Node(children=[Node("会话一", children=[wanted]), Node("会话二", children=[Node("输入框")])])
        self.assertIs(adapter.select(root, [{"name": "会话一"}, {"name": "输入框"}]), wanted)

    def test_bad_selector_is_not_silently_accepted(self):
        root = Node(children=[Node()])
        for selector in ({"typo": "值"}, {"depth": 2}, {"name": "", "depth": 0}, {"name": "", "index": -1}):
            with self.subTest(selector=selector), self.assertRaises(adapter.AdapterConfigurationError):
                adapter.select(root, selector)

    def test_parse_same_content_different_ids_and_reused_controls(self):
        first = adapter.parse_message(row("1"), profile(), "微信")
        second = adapter.parse_message(row("2"), profile(), "微信")
        reused = adapter.parse_message(row("1", content="修改内容"), profile(), "微信")
        self.assertNotEqual(first.message_id, second.message_id)
        self.assertNotEqual(first.message_id, reused.message_id)
        self.assertEqual(first.message_id, adapter.parse_message(row("1"), profile(), "微信").message_id)

    def test_parse_self_and_media(self):
        item = row("1", sender="本人")
        item.children.append(Node(kind="ImageControl"))
        config = profile()
        config["media"] = {"type": "ImageControl"}
        parsed = adapter.parse_message(item, config, "微信")
        self.assertTrue(parsed.is_self)
        self.assertEqual(parsed.kind, "media")

    def test_yto_parse_preserves_multiline_body_and_sender(self):
        body = Node(children=[Node("第一行", kind="TextControl"), Node("第二行", kind="TextControl")])
        item = Node(automation_id="123", children=[
            Node(), Node("小圆", kind="TextControl"), Node("今天 10:35", kind="TextControl"), body,
        ])
        config = dict(profile(), parser="yto")
        parsed = adapter.parse_message(item, config, "客户管家")
        self.assertEqual((parsed.sender, parsed.content, parsed.kind), ("小圆", "第一行\n第二行", "text"))
        body.children.append(Node(kind="ButtonControl"))
        self.assertEqual(adapter.parse_message(item, config, "客户管家").kind, "media")

    def test_profile_rejects_missing_identity_or_sender(self):
        cases = []
        for change in ({"self_names": []}, {"self_names": [" "]}, {"id_attribute": "猜测"}, {"sender": {}}):
            config = {"wx": profile(), "kf": profile()}
            config["wx"].update(change)
            cases.append(config)
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as folder:
            path = Path(folder) / "profile.json"
            for config in cases:
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(config=config["wx"]), self.assertRaises(adapter.AdapterConfigurationError):
                    adapter.load_profile(path)


class SendTests(unittest.TestCase):
    def setUp(self):
        self.auto = FakeAuto()
        self.profile = profile()
        self.client = adapter.WindowsChatAdapter(self.auto, self.profile, "目标群", "微信", lambda message: None)

    def assert_not_submitted(self):
        self.assertEqual(self.auto.sent, [])
        self.assertNotIn("{Enter}", self.auto.keys)

    def test_prepare_skips_history_and_fetches_only_added_message(self):
        self.client.prepare()
        self.assertEqual(self.client.fetch_new(), [])
        self.auto.messages.children.append(row("2"))
        self.assertEqual(len(self.client.fetch_new()), 1)
        self.assertEqual(self.client.fetch_new(), [])

    def test_wrong_group_is_rejected_before_editing(self):
        self.auto.title.Name = "另一个群"
        with self.assertRaises(RetryableSendError):
            self.client.send("测试")
        self.assertEqual(self.auto.set_values, [])
        self.assert_not_submitted()

    def test_ambiguous_input_is_rejected_before_editing(self):
        self.auto.window.children.append(Node(automation_id="input", kind="EditControl"))
        with self.assertRaises(adapter.AdapterConfigurationError):
            self.client.send("测试")
        self.assertEqual(self.auto.set_values, [])
        self.assert_not_submitted()

    def test_existing_draft_including_spaces_is_never_overwritten(self):
        for draft in ("用户的草稿", "   ", "\n"):
            self.auto.draft = draft
            with self.subTest(draft=draft), self.assertRaises(RetryableSendError):
                self.client.send("测试")
            self.assertEqual(self.auto.draft, draft)
        self.assertEqual(self.auto.set_values, [])
        self.assert_not_submitted()

    def test_success_requires_new_self_echo(self):
        self.client.send("精确文本")
        self.assertEqual(self.auto.sent, ["精确文本"])

    def test_process_name_rejects_window_from_other_application(self):
        self.auto.window.ProcessId = 123
        self.client.profile["process_name"] = "Weixin.exe"
        self.auto.GetProcesses = lambda detailed: [SimpleNamespace(pid=123, exeName="Other.exe")]
        with self.assertRaises(adapter.AdapterConfigurationError):
            self.client.send("测试")
        self.assertEqual(self.auto.set_values, [])
        self.assert_not_submitted()

    def test_process_name_accepts_case_difference_and_rechecks_new_pid(self):
        self.auto.window.ProcessId = 123
        self.client.profile["process_name"] = "Weixin.exe"
        self.auto.GetProcesses = lambda detailed: [SimpleNamespace(pid=123, exeName="WEIXIN.EXE")]
        self.client.prepare()
        self.auto.window.ProcessId = 124
        with self.assertRaises(adapter.AdapterConfigurationError):
            self.client.send("测试")
        self.assert_not_submitted()

    def test_text_pattern_suffix_only_removes_explicit_structural_ending(self):
        self.auto = FakeAuto(value_pattern=False)
        self.client = adapter.WindowsChatAdapter(self.auto, profile(), "目标群", "微信")
        self.auto.draft = "正文\n\r"
        self.assertEqual(self.client._value(self.auto.edit), "正文\n\n")
        self.client.profile["text_pattern_suffix"] = "\r"
        self.assertEqual(self.client._value(self.auto.edit), "正文\n")

    def test_group_or_focus_change_after_input_prevents_submission(self):
        for change in (lambda: setattr(self.auto.title, "Name", "另一个群"),
                       lambda: setattr(self.auto, "focused", Node())):
            self.auto.title.Name = "目标群"
            self.auto.draft = ""
            self.auto.after_set_value = change
            with self.assertRaises(adapter.AdapterConfigurationError):
                self.client.send("保留的草稿")
            self.assertEqual(self.auto.draft, "保留的草稿")
            self.assert_not_submitted()

    def test_send_key_exception_is_uncertain_and_only_attempted_once(self):
        self.auto.send_error = RuntimeError("按键结果不可确认")
        with self.assertRaises(SendUncertainError):
            self.client.send("测试")
        self.assertEqual(self.auto.sent, ["测试"])

    def test_missing_or_other_sender_echo_is_uncertain(self):
        for echo, sender in ((False, "本人"), (True, "别人")):
            self.auto.echo, self.auto.echo_sender = echo, sender
            self.auto.draft = ""
            with self.subTest(echo=echo, sender=sender):
                with patch.object(adapter.time, "monotonic", side_effect=[0, 0, 4]), patch.object(adapter.time, "sleep"):
                    with self.assertRaises(SendUncertainError):
                        self.client.send("测试")

    def test_existing_identical_self_message_is_not_send_confirmation(self):
        self.auto.messages.children.append(row("2", "本人", "测试"))
        self.auto.echo = False
        with patch.object(adapter.time, "monotonic", side_effect=[0, 0, 4]), patch.object(adapter.time, "sleep"):
            with self.assertRaises(SendUncertainError):
                self.client.send("测试")

    def test_literal_braces_and_multiline_text_do_not_trigger_extra_send(self):
        self.auto = FakeAuto(value_pattern=False)
        self.client = adapter.WindowsChatAdapter(self.auto, profile(), "目标群", "微信")
        text = "正文{Enter}\n下一行{Ctrl}{A}"
        self.client.send(text)
        self.assertEqual(self.auto.sent, [text])
        self.assertEqual(self.auto.keys.count("{Enter}"), 1)
        self.assertEqual(self.auto.keys.count("{Shift}{Enter}"), 1)

    def test_conflicting_send_and_newline_keys_rejected_before_edit(self):
        self.client.profile.update(send_keys="{Ctrl}{Enter}", newline_keys="{Ctrl}{Enter}")
        with self.assertRaises(adapter.AdapterConfigurationError):
            self.client.send("第一行\n第二行")
        self.assertEqual(self.auto.set_values, [])
        self.assert_not_submitted()

    def test_missing_mention_selectors_never_sends_plain_at_text(self):
        with self.assertRaises(adapter.AdapterConfigurationError):
            self.client.send_mention("客服", "内容")
        self.assertEqual(self.auto.draft, "")
        self.assert_not_submitted()

    def mention_setup(self, token=True, candidate_count=1):
        self.client.profile.update(
            mention_item={"automation_id": "candidate", "name": "{staff}"},
            mention_token={"automation_id": "mention-token", "name": "{staff}"},
        )
        candidates = [Node("客服", "candidate") for _ in range(candidate_count)]
        self.auto.window.children.extend(candidates)

        def choose():
            self.auto.draft = "@客服 "
            if token:
                self.auto.window.children.append(Node("客服", "mention-token"))

        for candidate in candidates:
            candidate.on_click = choose

    def test_ambiguous_mention_candidate_stops_with_draft(self):
        self.mention_setup(candidate_count=2)
        with self.assertRaises(adapter.AdapterConfigurationError):
            self.client.send_mention("客服", "内容")
        self.assertEqual(self.auto.draft, "@客服")
        self.assert_not_submitted()

    def test_mention_requires_selected_token_before_submission(self):
        self.mention_setup(token=False)
        with self.assertRaises(adapter.AdapterConfigurationError):
            self.client.send_mention("客服", "内容")
        self.assert_not_submitted()

    def test_mention_succeeds_after_unique_candidate_and_token(self):
        self.mention_setup()
        self.client.send_mention("客服", "内容")
        self.assertEqual(self.auto.sent, ["@客服  内容"])


if __name__ == "__main__":
    unittest.main()
