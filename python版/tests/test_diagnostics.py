"""仅用内存模拟 UIA 与文件输出，验证诊断证据、隐私边界和可取消等待。"""

from contextlib import nullcontext
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import windows_adapter as adapter
from gui import ConfigWindow


class Node:
    def __init__(self, identity, name="", children=()):
        self.ControlTypeName = "GroupControl"
        self.AutomationId = identity
        self.Name = name
        self.ClassName = "模拟控件"
        self.ProcessId = 123
        self.NativeWindowHandle = 456
        self.HelpText = ""
        self.IsKeyboardFocusable = True
        self.HasKeyboardFocus = False
        self.BoundingRectangle = SimpleNamespace(left=0, top=0, right=100, bottom=100)
        self.children = list(children)
        self.parent = None
        self.patterns = {}
        self.Element = SimpleNamespace(GetCurrentPattern=Mock(return_value=None))
        for child in self.children:
            child.parent = self

    def GetRuntimeId(self):
        return [1, self.AutomationId]

    def GetParentControl(self):
        return self.parent

    def GetChildren(self):
        return self.children

    def GetPattern(self, pattern_id):
        return self.patterns.get(pattern_id)

    def GetPropertyValue(self, property_id):
        return ""


class RawRange:
    def __init__(self, start, end):
        self.points = [start, end]

    def CompareEndpoints(self, source, other, target):
        if not isinstance(other, RawRange):
            raise TypeError("必须传原始 COM 范围，不能传 Python 包装")
        return self.points[source] - other.points[target]


class TextRange:
    def __init__(self, text, start, end, enclosing):
        self.text = text
        self.textRange = RawRange(start, end)
        self.enclosing = enclosing

    def GetText(self, length):
        return self.text

    def GetEnclosingControl(self):
        return self.enclosing

    def GetBoundingRectangles(self):
        return [SimpleNamespace(left=10, top=20, right=30, bottom=40)]

    def CompareEndpoints(self, *args):
        raise AssertionError("不能调用 uiautomation 有缺陷的包装方法")


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.edit = Node("edit-content")
        self.window = Node("window", "客户管家", [self.edit])
        self.root = Node("root", "桌面", [self.window])
        self.document = TextRange("自己的草稿", 10, 15, self.edit)
        self.selection = TextRange("", 12, 12, self.edit)
        self.text = SimpleNamespace(DocumentRange=self.document, GetSelection=Mock(return_value=[self.selection]))
        self.edit.patterns[3] = self.text
        self.auto = SimpleNamespace(
            UIAutomationInitializerInThread=Mock(side_effect=nullcontext),
            GetRootControl=Mock(return_value=self.root),
            GetFocusedControl=Mock(return_value=self.edit),
            GetForegroundControl=Mock(return_value=self.window),
            PatternId=SimpleNamespace(LegacyIAccessiblePattern=1, ValuePattern=2, TextPattern=3, TextPattern2=4),
            PropertyId=SimpleNamespace(FullDescriptionProperty=1),
            TextPatternRangeEndpoint=SimpleNamespace(Start=0, End=1),
            GetPatternIdInterface=Mock(return_value="原始接口"),
            TextRange=Mock(side_effect=lambda textRange: textRange),
        )
        self.logs = []
        self.patchers = [patch.object(adapter, "_automation", return_value=self.auto),
                         patch.object(adapter, "_wechat_session"),
                         patch.object(Path, "mkdir"), patch.object(Path, "write_text", return_value=1),
                         patch.object(adapter.time, "sleep")]
        self.automation, self.gate, self.mkdir, self.write, self.sleep = [p.start() for p in self.patchers]
        for patcher in self.patchers:
            self.addCleanup(patcher.stop)

    def run_diagnose(self, **kwargs):
        adapter.diagnose(ROOT.parent / "diagnostics", self.logs.append, **kwargs)
        return json.loads(self.write.call_args.args[0])

    def input_report(self, **kwargs):
        return self.run_diagnose(**kwargs)["windows"][0]["nodes"][1]

    def test_input_focus_parent_chain_and_selection_positions_are_recorded(self):
        report = self.input_report()
        self.assertTrue(report["is_keyboard_focusable"])
        self.assertFalse(report["has_keyboard_focus"])
        self.assertEqual([p["automation_id"] for p in report["parent_chain"]], ["window", "root"])
        selected = report["selection"][0]
        self.assertTrue(selected["is_degenerate"])
        self.assertTrue(selected["inside_document"])
        self.assertEqual(selected["text_length"], 0)
        self.assertEqual(selected["text"], "")
        self.assertEqual(selected["enclosing_element"]["automation_id"], "edit-content")
        self.assertEqual(selected["compare_to_document"], {
            "start_to_document_start": 2, "start_to_document_end": -3,
            "end_to_document_start": 2, "end_to_document_end": -3,
        })
        self.assertEqual(selected["rectangles"], [[10, 20, 30, 40]])

    def test_other_area_selection_records_length_but_not_contents(self):
        self.selection.text = "其它区域敏感内容"
        self.selection.textRange = RawRange(20, 30)
        selected = self.input_report()["selection"][0]
        self.assertFalse(selected["inside_document"])
        self.assertEqual(selected["text_length"], len(self.selection.text))
        self.assertNotIn("text", selected)

    def test_failed_endpoint_comparison_never_exposes_selection_text(self):
        self.selection.textRange.CompareEndpoints = Mock(side_effect=RuntimeError("范围已过期"))
        selected = self.input_report()["selection"][0]
        self.assertFalse(selected["inside_document"])
        self.assertIn("start_to_document_start_error", selected["compare_to_document"])
        self.assertNotIn("text", selected)
        self.assertEqual(selected["rectangles"], [[10, 20, 30, 40]])

    def test_unrelated_property_errors_preserve_text_and_focus_evidence(self):
        self.edit.GetPropertyValue = Mock(side_effect=RuntimeError("属性不可用"))
        self.edit.patterns[1] = SimpleNamespace(Description="说明")
        report = self.input_report()
        self.assertIn("full_description_error", report)
        self.assertIn("legacy_value_error", report)
        self.assertEqual(report["legacy_description"], "说明")
        self.assertEqual(report["text_pattern"], "自己的草稿")
        self.assertEqual(len(report["selection"]), 1)
        self.assertTrue(report["is_keyboard_focusable"])

    def test_document_read_error_still_reads_selection_and_caret_support(self):
        self.document.GetText = Mock(side_effect=RuntimeError("正文读取失败"))
        report = self.input_report()
        self.assertIn("text_pattern_error", report)
        self.assertEqual(len(report["selection"]), 1)
        self.assertFalse(report["text_pattern2_caret"]["supported"])

    def test_global_focus_and_foreground_descriptions_omit_names(self):
        self.edit.Name = "不应出现在全局身份摘要中的内容"
        report = self.run_diagnose()
        self.assertEqual(report["focused_control"]["automation_id"], "edit-content")
        self.assertEqual(report["foreground_control"]["automation_id"], "window")
        self.assertNotIn("name", report["focused_control"])
        self.assertNotIn("name", report["foreground_control"])

    def test_global_focus_read_error_does_not_hide_foreground_or_input(self):
        self.auto.GetFocusedControl.side_effect = RuntimeError("焦点已变化")
        report = self.run_diagnose()
        self.assertIn("focused_control_error", report)
        self.assertEqual(report["foreground_control"]["automation_id"], "window")
        self.assertEqual(report["windows"][0]["nodes"][1]["text_pattern"], "自己的草稿")

    def test_parent_chain_stops_at_eight_levels_even_if_provider_cycles(self):
        self.edit.parent = self.edit
        parents = self.input_report()["parent_chain"]
        self.assertEqual(len(parents), 8)
        self.assertTrue(all(p["automation_id"] == "edit-content" for p in parents))

    def test_raw_text_pattern2_reports_active_caret_and_wraps_raw_range(self):
        pattern = SimpleNamespace(GetCaretRange=Mock(return_value=(1, self.selection)))
        raw = SimpleNamespace(QueryInterface=Mock(return_value=pattern))
        self.edit.Element.GetCurrentPattern.return_value = raw
        result = self.input_report()["text_pattern2_caret"]
        self.assertTrue(result["supported"])
        self.assertTrue(result["is_active"])
        self.assertTrue(result["range"]["is_degenerate"])
        raw.QueryInterface.assert_called_once_with("原始接口")
        self.auto.TextRange.assert_called_once_with(textRange=self.selection)

    def test_caret_interface_failure_is_recorded_and_does_not_hide_selection(self):
        self.edit.Element.GetCurrentPattern.side_effect = RuntimeError("接口不可用")
        report = self.input_report()
        self.assertIsNone(report["text_pattern2_caret"]["supported"])
        self.assertIn("接口不可用", report["text_pattern2_caret"]["error"])
        self.assertEqual(len(report["selection"]), 1)

    def test_inactive_caret_with_null_range_is_explicit(self):
        pattern = SimpleNamespace(GetCaretRange=Mock(return_value=(0, None)))
        self.edit.Element.GetCurrentPattern.return_value = SimpleNamespace(QueryInterface=Mock(return_value=pattern))
        result = self.input_report()["text_pattern2_caret"]
        self.assertTrue(result["supported"])
        self.assertFalse(result["is_active"])
        self.assertIsNone(result["range"])

    def test_capture_delay_without_event_uses_sleep_before_reading_focus(self):
        sequence = []
        self.sleep.side_effect = lambda seconds: sequence.append(("wait", seconds))
        self.auto.GetFocusedControl.side_effect = lambda: sequence.append(("focus", 0)) or self.edit
        self.run_diagnose(capture_delay=5)
        self.assertEqual(sequence, [("wait", 5), ("focus", 0)])
        self.assertIn("5秒后读取，请将焦点置于待检查输入框；不会键入或发送", self.logs[0])

    def test_cancel_during_delay_does_not_read_windows_or_enable_gate(self):
        stop = SimpleNamespace(is_set=Mock(return_value=False), wait=Mock(return_value=True))
        result = adapter.diagnose(ROOT.parent / "diagnostics", self.logs.append,
                                  enable_wechat=True, stop_event=stop, capture_delay=5)
        self.assertIsNone(result)
        stop.wait.assert_called_once_with(5)
        self.sleep.assert_not_called()
        self.gate.assert_not_called()
        self.auto.GetRootControl.assert_not_called()
        self.auto.GetFocusedControl.assert_not_called()
        self.write.assert_not_called()

    def test_non_cancelled_event_delay_uses_wait_not_sleep(self):
        stop = SimpleNamespace(is_set=Mock(return_value=False), wait=Mock(return_value=False))
        self.run_diagnose(stop_event=stop, capture_delay=0.5)
        stop.wait.assert_called_once_with(0.5)
        self.sleep.assert_not_called()

    def test_invalid_delays_fail_before_loading_automation(self):
        for delay in (True, "5", -1, float("nan"), float("inf"), None):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                adapter.diagnose(ROOT.parent / "diagnostics", capture_delay=delay)
        self.automation.assert_not_called()

    def test_gui_schedules_five_second_capture_and_reports_cancellation(self):
        logs = []
        window = SimpleNamespace(_worker=None, _close_requested=False,
                                 var_wechat_accessibility=SimpleNamespace(get=lambda: False),
                                 _queue_log=logs.append, var_status=SimpleNamespace(set=Mock()))
        window._start_worker = lambda kind, action: action()
        with patch.object(adapter, "diagnose", return_value=None) as diagnose:
            ConfigWindow.on_diagnose(window)
        self.assertEqual(diagnose.call_args.kwargs["capture_delay"], 5)
        self.assertIs(diagnose.call_args.kwargs["stop_event"], window._stop_event)
        self.assertEqual(logs, ["[诊断] 已取消。"])


if __name__ == "__main__":
    unittest.main()
