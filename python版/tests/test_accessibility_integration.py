"""模拟窗口、兼容上下文及文件输出，验证兼容开关接入和退出顺序。"""

import json
from io import StringIO
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import windows_adapter as adapter
import wechat4_adapter
from wechat_forwarder import validate_config
from test_adapter import FakeAuto, profile


class RecordedContext:
    """记录上下文生命周期与异常，不调用 COM 或进程接口。"""

    def __init__(self, name, events):
        self.name = name
        self.events = events
        self.enter_count = 0
        self.exit_count = 0
        self.exit_exception = None
        self.enter_error = None
        self.exit_error = None

    def __enter__(self):
        self.enter_count += 1
        self.events.append(self.name + ":enter")
        if self.enter_error:
            raise self.enter_error
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exit_count += 1
        self.exit_exception = exc
        self.events.append(self.name + ":exit")
        if self.exit_error:
            raise self.exit_error
        return False


class DiagnosticNode:
    def __init__(self, name, children=(), pid=4321):
        self.Name = name
        self.children = list(children)
        self.ProcessId = pid
        self.ControlTypeName = "WindowControl"
        self.AutomationId = "mock-window"
        self.ClassName = "模拟窗口"
        self.HelpText = ""
        self.BoundingRectangle = SimpleNamespace(left=0, top=0, right=100, bottom=100)
        self.before_children = None

    def GetChildren(self):
        if self.before_children:
            self.before_children()
        return self.children

    def GetRuntimeId(self):
        return [7, self.ProcessId]

    def GetPattern(self, pattern):
        return None

    def GetPropertyValue(self, property_id):
        return ""


class AccessibilityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.logs = []
        self.com = RecordedContext("com", self.events)
        self.gate = RecordedContext("gate", self.events)
        self.wx_window = DiagnosticNode("微信", [DiagnosticNode("模拟消息")])
        self.kf_window = DiagnosticNode("客户管家", pid=5432)
        self.root = DiagnosticNode("桌面", [self.wx_window, self.kf_window])
        self.auto = SimpleNamespace(
            UIAutomationInitializerInThread=Mock(return_value=self.com),
            GetRootControl=Mock(return_value=self.root),
            PatternId=SimpleNamespace(LegacyIAccessiblePattern=1, ValuePattern=2, TextPattern=3),
            PropertyId=SimpleNamespace(FullDescriptionProperty=1),
        )
        self.profile = {
            "wx": {"window": {"name": "微信"}},
            "kf": {"window": {"name": "客户管家"}},
        }
        self.wx = SimpleNamespace(prepare=Mock(side_effect=lambda: self.events.append("wx:prepare")))
        self.kf = SimpleNamespace(prepare=Mock(side_effect=lambda: self.events.append("kf:prepare")))
        self.cfg = {"adapter_profile": "模拟配置.json", "wx_group": "微信测试群", "kf_group": "客户管家测试群"}
        self.patchers = [
            patch.object(adapter, "_automation", return_value=self.auto),
            patch.object(adapter, "load_profile", return_value=self.profile),
            patch.object(adapter, "_wechat_session", return_value=self.gate),
            patch.object(adapter, "WindowsChatAdapter", side_effect=[self.wx, self.kf]),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text", return_value=1),
        ]
        mocks = [patcher.start() for patcher in self.patchers]
        self.automation, self.load_profile, self.factory, self.chat_adapter, self.mkdir, self.write_text = mocks
        self.output_dir = ROOT.parent / "diagnostics" / "integration-test"

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def report(self):
        return json.loads(self.write_text.call_args.args[0])

    def test_live_default_does_not_create_gate_and_pins_selected_wechat_pid(self):
        with adapter.create_live_adapters(self.cfg, self.logs.append) as adapters:
            self.assertEqual(adapters, (self.wx, self.wx, self.kf, self.kf))
        self.factory.assert_not_called()
        self.assertEqual(self.com.enter_count, 1)
        self.assertEqual(self.com.exit_count, 1)
        self.assertEqual(self.chat_adapter.call_args_list[0].kwargs["expected_pid"], 4321)
        self.assertEqual(self.events, ["com:enter", "wx:prepare", "kf:prepare", "com:exit"])

    def test_wechat4_parser_selects_specialized_adapter_and_keeps_customer_adapter(self):
        self.profile["wx"]["parser"] = "wechat4"
        self.chat_adapter.side_effect = [self.kf]
        with patch.object(wechat4_adapter, "Wechat4Adapter", return_value=self.wx) as specialized:
            with adapter.create_live_adapters(self.cfg, self.logs.append) as adapters:
                self.assertEqual(adapters, (self.wx, self.wx, self.kf, self.kf))
        specialized.assert_called_once_with(self.auto, self.profile["wx"], "微信测试群", "微信",
                                            self.logs.append, expected_pid=4321)
        self.chat_adapter.assert_called_once_with(self.auto, self.profile["kf"], "客户管家测试群",
                                                 "客户管家", self.logs.append)
        self.assertEqual(self.events, ["com:enter", "wx:prepare", "kf:prepare", "com:exit"])
        self.factory.assert_not_called()

    def test_wechat4_mention_mode_allows_keywords_without_legacy_token_selectors(self):
        self.profile["wx"].update(parser="wechat4", mention_mode="wechat4",
                                  mention_allow_duplicate_names=True)
        self.cfg["at_keywords"] = ["人工"]
        self.chat_adapter.side_effect = [self.kf]
        with patch.object(wechat4_adapter, "Wechat4Adapter", return_value=self.wx) as specialized:
            with adapter.create_live_adapters(self.cfg, self.logs.append):
                pass
        specialized.assert_called_once()
        self.wx.prepare.assert_called_once()
        self.kf.prepare.assert_called_once()

    def test_wechat4_keywords_without_mention_mode_fail_before_automation(self):
        self.profile["wx"]["parser"] = "wechat4"
        self.cfg["at_keywords"] = ["人工"]
        with self.assertRaisesRegex(adapter.AdapterConfigurationError, "真实 @ 控件"):
            with adapter.create_live_adapters(self.cfg, self.logs.append):
                self.fail("未启用真实提及适配时不能运行 @ 规则")
        self.automation.assert_not_called()
        self.chat_adapter.assert_not_called()
        self.factory.assert_not_called()

    def test_mention_mode_does_not_bypass_parser_requirement(self):
        # 即使调用方替换配置加载器，factory 仍须同时检查解析器与模式。
        self.profile["wx"]["mention_mode"] = "wechat4"
        self.cfg["at_keywords"] = ["人工"]
        with self.assertRaisesRegex(adapter.AdapterConfigurationError, "真实 @ 控件"):
            with adapter.create_live_adapters(self.cfg, self.logs.append):
                self.fail("普通解析器不能借用微信 4 的 @ 模式")
        self.automation.assert_not_called()

    def test_live_enabled_enters_once_before_prepare_and_exits_before_com(self):
        self.cfg["wechat_accessibility"] = True
        with adapter.create_live_adapters(self.cfg, self.logs.append):
            self.events.append("body")
            self.assertEqual(self.gate.exit_count, 0)
        self.factory.assert_called_once()
        self.assertEqual(self.factory.call_args.args[0], 4321)
        self.assertEqual(self.gate.enter_count, 1)
        self.assertEqual(self.gate.exit_count, 1)
        self.assertEqual(self.events, [
            "com:enter", "gate:enter", "wx:prepare", "kf:prepare", "body", "gate:exit", "com:exit",
        ])

    def test_wechat_prepare_failure_restores_and_propagates_original_error(self):
        self.cfg["wechat_accessibility"] = True
        failure = RuntimeError("微信控件未准备好")
        self.wx.prepare.side_effect = failure
        with self.assertRaises(RuntimeError) as raised:
            with adapter.create_live_adapters(self.cfg, self.logs.append):
                self.fail("准备失败不能进入收发正文")
        self.assertIs(raised.exception, failure)
        self.assertIs(self.gate.exit_exception, failure)
        self.assertEqual(self.gate.exit_count, 1)
        self.assertEqual(self.com.exit_count, 1)
        self.kf.prepare.assert_not_called()

    def test_customer_service_prepare_failure_restores_and_propagates(self):
        self.cfg["wechat_accessibility"] = True
        failure = RuntimeError("客户管家控件未准备好")
        self.kf.prepare.side_effect = failure
        with self.assertRaises(RuntimeError) as raised:
            with adapter.create_live_adapters(self.cfg, self.logs.append):
                self.fail("准备失败不能进入收发正文")
        self.assertIs(raised.exception, failure)
        self.assertIs(self.gate.exit_exception, failure)
        self.assertEqual(self.gate.exit_count, 1)
        self.assertEqual(self.com.exit_count, 1)

    def test_live_body_exception_restores_and_propagates(self):
        self.cfg["wechat_accessibility"] = True
        failure = LookupError("业务循环异常")
        with self.assertRaises(LookupError) as raised:
            with adapter.create_live_adapters(self.cfg, self.logs.append):
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertIs(self.gate.exit_exception, failure)
        self.assertIs(self.com.exit_exception, failure)
        self.assertEqual(self.gate.exit_count, 1)

    def test_failed_gate_enter_does_not_build_adapters_and_propagates(self):
        self.cfg["wechat_accessibility"] = True
        failure = RuntimeError("门控启用失败")
        self.gate.enter_error = failure
        with self.assertRaises(RuntimeError) as raised:
            with adapter.create_live_adapters(self.cfg, self.logs.append):
                self.fail("启用失败不能进入收发正文")
        self.assertIs(raised.exception, failure)
        self.chat_adapter.assert_not_called()
        self.assertEqual(self.gate.enter_count, 1)
        # enter 失败的上下文自行处理回滚，ExitStack 不重复调用其 exit。
        self.assertEqual(self.gate.exit_count, 0)
        self.assertIs(self.com.exit_exception, failure)

    def test_live_restore_error_propagates_after_com_cleanup(self):
        self.cfg["wechat_accessibility"] = True
        failure = RuntimeError("门控恢复失败")
        self.gate.exit_error = failure
        with self.assertRaises(RuntimeError) as raised:
            with adapter.create_live_adapters(self.cfg, self.logs.append):
                pass
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.gate.exit_count, 1)
        self.assertEqual(self.com.exit_count, 1)
        self.assertIs(self.com.exit_exception, failure)

    def test_diagnose_default_does_not_create_gate(self):
        result = adapter.diagnose(self.output_dir, self.logs.append)
        self.factory.assert_not_called()
        self.assertEqual(result.parent, self.output_dir)
        self.assertEqual(len(self.report()["windows"]), 2)
        self.assertEqual(self.com.exit_count, 1)

    def test_diagnose_enabled_restores_before_writing_report(self):
        self.write_text.side_effect = lambda *args, **kwargs: self.events.append("write") or 1
        adapter.diagnose(self.output_dir, self.logs.append, enable_wechat=True)
        self.factory.assert_called_once()
        self.assertEqual(self.factory.call_args.args[0], 4321)
        self.assertEqual(self.gate.enter_count, 1)
        self.assertEqual(self.gate.exit_count, 1)
        self.assertLess(self.events.index("gate:exit"), self.events.index("write"))
        self.assertLess(self.events.index("com:exit"), self.events.index("write"))

    def test_diagnose_preset_stop_does_not_enter_gate_or_write_report(self):
        stop = threading.Event()
        stop.set()
        result = adapter.diagnose(self.output_dir, self.logs.append, enable_wechat=True, stop_event=stop)
        self.assertIsNone(result)
        self.factory.assert_not_called()
        self.auto.GetRootControl.assert_not_called()
        self.write_text.assert_not_called()
        self.assertEqual(self.com.exit_count, 1)

    def test_diagnose_cancel_during_traversal_restores_and_marks_report(self):
        stop = threading.Event()
        self.wx_window.before_children = stop.set
        adapter.diagnose(self.output_dir, self.logs.append, enable_wechat=True, stop_event=stop)
        self.assertTrue(stop.is_set())
        self.assertTrue(self.report()["cancelled"])
        self.assertEqual(self.gate.enter_count, 1)
        self.assertEqual(self.gate.exit_count, 1)
        self.assertEqual(self.com.exit_count, 1)
        self.assertEqual(len(self.report()["windows"]), 1)

    def test_diagnose_gate_enter_error_propagates_and_com_exits(self):
        failure = RuntimeError("兼容版本不匹配")
        self.gate.enter_error = failure
        with self.assertRaises(RuntimeError) as raised:
            adapter.diagnose(self.output_dir, self.logs.append, enable_wechat=True)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.com.exit_count, 1)
        self.assertEqual(self.gate.exit_count, 0)
        self.write_text.assert_not_called()

    def test_diagnose_gate_exit_error_propagates_and_no_report_is_written(self):
        failure = RuntimeError("兼容恢复失败")
        self.gate.exit_error = failure
        with self.assertRaises(RuntimeError) as raised:
            adapter.diagnose(self.output_dir, self.logs.append, enable_wechat=True)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.gate.exit_count, 1)
        self.assertEqual(self.com.exit_count, 1)
        self.write_text.assert_not_called()

    def test_diagnose_window_read_error_is_recorded_after_gate_cleanup(self):
        self.wx_window.before_children = Mock(side_effect=RuntimeError("模拟树读取失败"))
        adapter.diagnose(self.output_dir, self.logs.append, enable_wechat=True)
        self.assertIn("模拟树读取失败", self.report()["windows"][0]["error"])
        self.assertEqual(self.gate.exit_count, 1)
        self.assertEqual(self.com.exit_count, 1)

    def test_report_write_failure_occurs_after_gate_restored(self):
        failure = OSError("无法保存诊断文件")
        self.write_text.side_effect = failure
        with self.assertRaises(OSError) as raised:
            adapter.diagnose(self.output_dir, self.logs.append, enable_wechat=True)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.gate.exit_count, 1)
        self.assertEqual(self.com.exit_count, 1)

    def test_non_boolean_live_switch_rejected_without_gate(self):
        for invalid in ("false", "true", 0, 1, 0.0, None, []):
            self.cfg["wechat_accessibility"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(adapter.AdapterConfigurationError):
                with adapter.create_live_adapters(self.cfg, self.logs.append):
                    self.fail("非法开关不能进入收发正文")
        self.factory.assert_not_called()
        self.chat_adapter.assert_not_called()

    def test_non_boolean_diagnostic_switch_rejected_before_automation(self):
        for invalid in ("false", "true", 0, 1, 0.0, None, []):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                adapter.diagnose(self.output_dir, enable_wechat=invalid)
        self.automation.assert_not_called()
        self.factory.assert_not_called()

    def test_validate_config_only_accepts_actual_booleans_and_defaults_false(self):
        self.assertIs(validate_config({})["wechat_accessibility"], False)
        for valid in (False, True):
            self.assertIs(validate_config({"wechat_accessibility": valid})["wechat_accessibility"], valid)
        for invalid in ("false", "true", "0", "1", 0, 1, -1, 0.0, 1.0, None, [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_config({"wechat_accessibility": invalid})


class ProfileParserTests(unittest.TestCase):
    def read_profile(self, config):
        with patch.object(Path, "open", return_value=StringIO(json.dumps(config))):
            return adapter.load_profile("模拟配置.json")

    def test_wechat4_mention_mode_and_explicit_duplicate_choice_are_accepted(self):
        for allowed in (False, True):
            config = {"wx": profile(), "kf": profile()}
            config["wx"].update(parser="wechat4", mention_mode="wechat4",
                                mention_allow_duplicate_names=allowed)
            with self.subTest(allowed=allowed):
                loaded = self.read_profile(config)
                self.assertEqual(loaded["wx"]["mention_mode"], "wechat4")
                self.assertIs(loaded["wx"]["mention_allow_duplicate_names"], allowed)

    def test_invalid_mention_modes_are_rejected(self):
        for invalid in ("", "text", "wechat3", True, False, 0, 1, [], {}):
            config = {"wx": profile(), "kf": profile()}
            config["wx"].update(parser="wechat4", mention_mode=invalid)
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    adapter.AdapterConfigurationError, "mention_mode"):
                self.read_profile(config)

    def test_mention_mode_requires_wechat4_parser_on_wechat_side(self):
        for side, parser in (("wx", "selectors"), ("wx", "yto"),
                             ("kf", "selectors"), ("kf", "yto")):
            config = {"wx": profile(), "kf": profile()}
            config[side].update(parser=parser, mention_mode="wechat4")
            with self.subTest(side=side, parser=parser), self.assertRaisesRegex(
                    adapter.AdapterConfigurationError, "mention_mode"):
                self.read_profile(config)

    def test_duplicate_name_permission_requires_actual_boolean_on_both_sides(self):
        for side in ("wx", "kf"):
            for invalid in ("true", "false", 0, 1, 0.0, 1.0, None, [], {}):
                config = {"wx": profile(), "kf": profile()}
                config[side]["mention_allow_duplicate_names"] = invalid
                with self.subTest(side=side, invalid=invalid), self.assertRaisesRegex(
                        adapter.AdapterConfigurationError, "mention_allow_duplicate_names"):
                    self.read_profile(config)

    def test_unknown_parser_is_rejected_on_either_side_before_automation(self):
        for side in ("wx", "kf"):
            config = {"wx": profile(), "kf": profile()}
            config[side]["parser"] = "unknown-parser"
            with self.subTest(side=side), patch.object(Path, "open", return_value=StringIO(json.dumps(config))), \
                    patch.object(adapter, "_automation") as automation:
                with self.assertRaisesRegex(adapter.AdapterConfigurationError, "解析器不受支持"):
                    adapter.load_profile("模拟配置.json")
                automation.assert_not_called()


class DraftReadinessTests(unittest.TestCase):
    def setUp(self):
        self.auto = FakeAuto(value_pattern=False)
        self.client = adapter.WindowsChatAdapter(self.auto, profile(), "目标群", "客户管家", lambda line: None)

    def test_prepare_rejects_unreadable_draft_before_building_message_baseline(self):
        self.auto.edit.patterns.clear()
        with patch.object(self.client.tracker, "update") as update:
            with self.assertRaisesRegex(adapter.AdapterConfigurationError, "不支持读取完整草稿"):
                self.client.prepare()
            update.assert_not_called()
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.sent, [])

    def test_embedded_object_placeholder_is_preserved_and_never_sent(self):
        # 即使把占位符误配成段落后缀，也必须在任何剥离操作之前拒绝。
        self.client.profile["text_pattern_suffix"] = "\ufffc"
        for draft in ("\ufffc", "原有草稿\ufffc", "第一行\ufffc\r\n第二行"):
            self.auto.draft = draft
            with self.subTest(draft=draft):
                with self.assertRaisesRegex(adapter.AdapterConfigurationError, "嵌入对象占位符"):
                    self.client.prepare()
                with self.assertRaisesRegex(adapter.AdapterConfigurationError, "嵌入对象占位符"):
                    self.client.send("YT0719078698064 测试正文")
                self.assertEqual(self.auto.draft, draft)
                self.assertEqual(self.auto.keys, [])
                self.assertEqual(self.auto.set_values, [])
                self.assertEqual(self.auto.sent, [])

    def test_normal_text_pattern_keeps_draft_content_and_supports_send(self):
        for draft, expected in (("", ""), ("普通草稿", "普通草稿"),
                                ("第一行\r\n第二行\r第三行", "第一行\n第二行\n第三行")):
            self.auto.draft = draft
            with self.subTest(draft=draft):
                self.assertEqual(self.client._value(self.auto.edit), expected)
                self.assertEqual(self.auto.draft, draft)
        self.auto.draft = ""
        self.client.prepare()
        self.client.send("YT0719078698064 正常文本")
        self.assertEqual(self.auto.sent, ["YT0719078698064 正常文本"])
        self.assertEqual(self.auto.draft, "")


if __name__ == "__main__":
    unittest.main()
