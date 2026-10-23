"""用纯内存 UIA 控件验证微信 4 多选读取，不连接真实微信。"""

from collections import OrderedDict
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wechat4_adapter import Wechat4Adapter
from wechat_forwarder import RetryableSendError, SendUncertainError
from windows_adapter import AdapterConfigurationError, WindowsChatAdapter


class Rect:
    def __init__(self, left=0, top=0, right=800, bottom=600):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class Node:
    """仅模拟适配器使用的控件属性，并记录所有可变操作。"""

    _next_id = 10000

    def __init__(self, name="", aid="", kind="GroupControl", cls="模拟控件",
                 children=(), pid=41, rid=None, rect=None):
        self.Name, self.AutomationId = name, aid
        self.ControlTypeName, self.ClassName = kind, cls
        self.ProcessId = pid
        self.runtime_id = rid if rid is not None else (7, Node._next_id)
        self.NativeWindowHandle = Node._next_id
        Node._next_id += 1
        self.BoundingRectangle = rect or Rect()
        self.IsOffscreen, self.IsEnabled = False, True
        self.children = list(children)
        self.patterns = {}
        self.parent = None
        self.on_click = self.on_right_click = self.on_focus = self.on_active = None
        self.clicks, self.right_clicks, self.reads = [], [], 0
        self.forbid_children = False
        for child in self.children:
            child.parent = self

    def GetChildren(self):
        self.reads += 1
        if self.forbid_children:
            raise AssertionError("禁止遍历外部进程窗口")
        return list(self.children)

    def GetRuntimeId(self):
        return self.runtime_id

    def GetParentControl(self):
        return self.parent

    def GetPattern(self, pattern_id):
        return self.patterns.get(pattern_id)

    def SetActive(self, **kwargs):
        if self.on_active:
            self.on_active()
        return True

    def SetFocus(self):
        return self.on_focus() if self.on_focus else True

    def Exists(self, *args, **kwargs):
        return True

    def Click(self, **kwargs):
        self.clicks.append(kwargs)
        if self.on_click:
            return self.on_click()
        return True

    def RightClick(self, **kwargs):
        self.right_clicks.append(kwargs)
        if self.on_right_click:
            return self.on_right_click()
        return True


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


def make_row(rid, content, sender="Zlient", *, kind="ListItemControl",
             cls="mmui::ChatTextItemView", top=100, bottom=150):
    node = Node(content, kind=kind, cls=cls, rid=rid,
                rect=Rect(100, top, 700, bottom))
    node.sender = sender
    return node


def profile():
    return {
        "parser": "wechat4_multiselect",
        "window": {"automation_id": "window", "depth": 1},
        "group_title": {"automation_id": "title"},
        "messages": {"automation_id": "messages"},
        "message_item": {"class_name": "mmui::ChatTextItemView", "depth": 1},
        "input": {"automation_id": "input"},
        "self_names": ["nide"],
        "context_points": [[0.25, 0.6], [0.75, 0.6]],
    }


class FakeAuto:
    class PatternId:
        ValuePattern = 1
        TextPattern = 2

    def __init__(self):
        self.normal_rows = [make_row((7, 1), "本人消息", "nide"),
                            make_row((7, 2), "YT0719078698064 查件", top=160, bottom=210)]
        self.title = Node("目标群", "title", "TextControl")
        self.messages = Node(aid="messages", kind="ListControl", cls="mmui::RecyclerListView",
                             children=self.normal_rows, rect=Rect(100, 80, 700, 500))
        self.edit = Node(aid="input", kind="EditControl", rid=(7, 99))
        self.window = Node(aid="window", kind="WindowControl", cls="mmui::MainWindow",
                           children=[self.title, self.messages, self.edit])
        self.root = Node(kind="PaneControl", pid=0, children=[self.window])
        self.root_reads = 0
        self.foreground = self.window
        self.window.on_active = self.activate
        self.cancel = Node("取消多选", kind="ButtonControl", rid=(7, 80))
        self.cancel.on_click = self.leave_multiselect
        self.draft, self.focused = "", None
        self.keys, self.set_values, self.sent = [], [], []
        self.opened_menus, self.menu_items = [], []
        self.menu_mode = "normal"
        self.menu_factory = None
        self.after_activate = None
        self.after_open_menu = None
        self.after_escape = None
        self.selected_transform = None
        self.after_cancel = None
        self.cancel_error = None
        self.keep_selected_after_cancel = False
        self.after_send = None
        self.edit.patterns[self.PatternId.ValuePattern] = ValuePattern(self)
        self.edit.on_focus = self.focus
        self.attach_rows()

    def activate(self):
        self.foreground = self.window
        if self.after_activate:
            self.after_activate()

    def attach_rows(self):
        self.messages.children = list(self.normal_rows)
        for row in self.normal_rows:
            row.parent = self.messages
            row.on_right_click = self.open_menu

    def focus(self):
        self.focused = self.edit
        return True

    def GetRootControl(self):
        self.root_reads += 1
        return self.root

    def GetFocusedControl(self):
        return self.focused

    def GetForegroundControl(self):
        return self.foreground

    def GetProcesses(self, detailed):
        return [SimpleNamespace(pid=41, exeName="Weixin.exe")]

    def open_menu(self):
        if self.menu_factory:
            menu_window = self.menu_factory()
        else:
            names = {"normal": ["多选"], "duplicate": ["多选", "多选"],
                     "wrong": ["删除"], "near_name": [" 多选"]}[self.menu_mode]
            items = [Node(name, kind="MenuItemControl", cls="mmui::XMenuView") for name in names]
            for item in items:
                item.on_click = self.enter_multiselect
            self.menu_items.extend(items)
            menu = Node(kind="MenuControl", cls="mmui::XMenu", children=items)
            menu_window = Node(kind="WindowControl", cls="Qt51514QWindowPopupSaveBits",
                               children=[menu])
        self.opened_menus.append(menu_window)
        menu_window.parent = self.root
        self.root.children.append(menu_window)
        self.foreground = menu_window
        if self.after_open_menu:
            self.after_open_menu()
        return True

    def close_owned_menus(self):
        self.root.children = [node for node in self.root.children
                              if node not in self.opened_menus or node.ProcessId != 41]
        if self.foreground.ProcessId == 41:
            self.foreground = self.window

    def enter_multiselect(self):
        self.close_owned_menus()
        selected = [make_row(row.runtime_id, row.sender + " " + row.Name, row.sender,
                             kind="CheckBoxControl", cls=row.ClassName,
                             top=row.BoundingRectangle.top, bottom=row.BoundingRectangle.bottom)
                    for row in self.normal_rows]
        if self.selected_transform:
            selected = self.selected_transform(selected)
        self.messages.children = selected
        for row in selected:
            row.parent = self.messages
        if self.cancel not in self.window.children:
            self.window.children.append(self.cancel)
        self.cancel.parent = self.window
        return True

    def leave_multiselect(self):
        if self.cancel_error:
            raise self.cancel_error
        if self.keep_selected_after_cancel:
            return True
        self.window.children.remove(self.cancel)
        self.attach_rows()
        if self.after_cancel:
            self.after_cancel()
        return True

    def SendKeys(self, keys, **kwargs):
        self.keys.append(keys)
        if keys.lower() in {"{esc}", "{escape}"}:
            self.close_owned_menus()
            if self.after_escape:
                self.after_escape()
        elif keys == "{Enter}":
            text = self.draft
            self.sent.append(text)
            self.draft = ""
            self.normal_rows.append(make_row((7, 100 + len(self.sent)), text, "nide", top=220, bottom=270))
            self.attach_rows()
            if self.after_send:
                self.after_send()
        else:
            raise AssertionError(f"测试未声明的键盘动作：{keys}")


class Wechat4AdapterTests(unittest.TestCase):
    def setUp(self):
        self.auto = FakeAuto()
        self.client = Wechat4Adapter(self.auto, profile(), "目标群", "微信", log=lambda message: None,
                                     expected_pid=41)

    def assert_no_clicks(self):
        self.assertEqual(self.auto.opened_menus, [])
        self.assertEqual(self.auto.cancel.clicks, [])
        self.assertEqual(self.auto.set_values, [])
        self.assertEqual(self.auto.sent, [])

    def assert_permanently_disabled_without_ui_reads(self):
        reads = self.auto.root_reads
        menus, cancels, keys = len(self.auto.opened_menus), len(self.auto.cancel.clicks), list(self.auto.keys)
        for clear_cache in (False, True):
            if clear_cache:
                self.client.clear_sender_cache()
            for action in (self.client._bind, self.client._rows, lambda: self.client.send("禁止再次发送")):
                with self.assertRaises(AdapterConfigurationError):
                    action()
                self.assertEqual(self.auto.root_reads, reads)
                self.assertEqual(len(self.auto.opened_menus), menus)
                self.assertEqual(len(self.auto.cancel.clicks), cancels)
                self.assertEqual(self.auto.keys, keys)
                self.assertEqual(self.auto.sent, [])

    def test_reads_senders_then_uses_cache_without_more_ui_actions(self):
        self.assertIsInstance(self.client, WindowsChatAdapter)
        rows = self.client._rows()
        self.assertEqual([(row.sender, row.content, row.is_self) for row in rows],
                         [("nide", "本人消息", True), ("Zlient", "YT0719078698064 查件", False)])
        self.assertEqual(len(self.auto.opened_menus), 1)
        self.assertEqual(len(self.auto.cancel.clicks), 1)
        self.assertNotIn(self.auto.cancel, self.auto.window.children)
        self.assertEqual([row.message_id for row in self.client._rows()], [row.message_id for row in rows])
        self.assertEqual(len(self.auto.opened_menus), 1)
        self.assertEqual(len(self.auto.cancel.clicks), 1)

    def test_same_body_distinct_full_ids_stays_distinct(self):
        self.auto.normal_rows = [make_row((7, 1, 99), "同文", "nide"),
                                 make_row((7, 2, 99), "同文", "Zlient", top=160, bottom=210)]
        self.auto.attach_rows()
        rows = self.client._rows()
        self.assertEqual([row.sender for row in rows], ["nide", "Zlient"])
        self.assertNotEqual(rows[0].message_id, rows[1].message_id)

    def test_preserves_sender_spaces_and_matches_self_name_exactly(self):
        self.auto.normal_rows[0].sender = " nide"
        self.auto.normal_rows[1].sender = "A B 😀"
        rows = self.client._rows()
        self.assertEqual([row.sender for row in rows], [" nide", "A B 😀"])
        self.assertEqual([row.is_self for row in rows], [False, False])

    def test_non_text_rows_are_not_exposed_as_text_messages(self):
        self.auto.normal_rows.append(make_row((7, 3), "图片", cls="mmui::ChatBubbleReferItemView"))
        self.auto.attach_rows()
        self.assertEqual(len(self.client._rows()), 2)

    def test_draft_rejects_sender_probe_without_clicks(self):
        self.auto.draft = "保留我的草稿"
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.client._rows()
        self.assertEqual(self.auto.draft, "保留我的草稿")
        self.assert_no_clicks()

    def test_existing_multiselect_is_not_cancelled(self):
        self.auto.enter_multiselect()
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.client._rows()
        self.assert_no_clicks()
        self.assertIn(self.auto.cancel, self.auto.window.children)

    def test_existing_menu_is_not_clicked_or_dismissed(self):
        self.auto.open_menu()
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.client._rows()
        self.assertEqual(sum(len(item.clicks) for item in self.auto.menu_items), 0)
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(len(self.auto.opened_menus), 1)

    def test_wrong_pid_or_group_rejects_before_any_click(self):
        for change in (lambda: setattr(self.auto.window, "ProcessId", 42),
                       lambda: setattr(self.auto.title, "Name", "另一个群")):
            with self.subTest(change=change):
                self.auto.window.ProcessId, self.auto.title.Name = 41, "目标群"
                change()
                with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
                    self.client._rows()
                self.assert_no_clicks()

    def test_activation_changing_group_or_pid_never_right_clicks(self):
        for field, value in (("group", "另一个群"), ("pid", 42)):
            with self.subTest(field=field):
                self.setUp()
                target, attribute = ((self.auto.title, "Name") if field == "group"
                                     else (self.auto.window, "ProcessId"))
                self.auto.after_activate = lambda: setattr(target, attribute, value)
                with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
                    self.client._rows()
                self.assert_no_clicks()
                self.assertEqual(self.auto.keys, [])

    def test_bound_main_runtime_id_or_native_handle_change_is_rejected(self):
        for attribute, value in (("runtime_id", (99, 123)), ("NativeWindowHandle", 987654)):
            with self.subTest(attribute=attribute):
                self.setUp()
                self.client._bind()
                setattr(self.auto.window, attribute, value)
                with self.assertRaises(AdapterConfigurationError):
                    self.client._bind()
                self.assert_no_clicks()

    def test_foreground_wrapper_with_same_main_identity_is_accepted(self):
        wrapper = Node(kind="WindowControl", rid=self.auto.window.runtime_id)
        wrapper.NativeWindowHandle = self.auto.window.NativeWindowHandle
        self.auto.after_activate = lambda: setattr(self.auto, "foreground", wrapper)
        self.assertEqual(len(self.client._rows()), 2)
        self.assertEqual(len(self.auto.cancel.clicks), 1)

    def test_existing_same_process_unrelated_foreground_rejected_at_each_phase(self):
        for phase in ("激活", "菜单", "多选"):
            with self.subTest(phase=phase):
                self.setUp()
                unrelated = Node(kind="WindowControl", cls="无关微信窗口")
                self.auto.root.children.append(unrelated)
                steal = lambda: setattr(self.auto, "foreground", unrelated)
                if phase == "激活":
                    self.auto.after_activate = steal
                elif phase == "菜单":
                    self.auto.after_open_menu = steal
                else:
                    def selected_transform(rows):
                        steal()
                        return rows
                    self.auto.selected_transform = selected_transform
                with self.assertRaisesRegex(AdapterConfigurationError, "前台"):
                    self.client._rows()
                self.assertEqual(sum(len(item.clicks) for item in self.auto.menu_items),
                                 1 if phase == "多选" else 0)
                self.assertEqual(self.auto.cancel.clicks, [])
                self.assertEqual(self.auto.keys, [])
                self.assertEqual(len(self.auto.opened_menus), 0 if phase == "激活" else 1)
                if phase != "激活":
                    self.assert_permanently_disabled_without_ui_reads()

    def test_activation_or_retry_hiding_target_prevents_the_next_right_click(self):
        for phase in ("激活之后", "第二点之前"):
            with self.subTest(phase=phase):
                self.setUp()
                target = self.auto.normal_rows[-1]
                if phase == "激活之后":
                    self.auto.after_activate = lambda: setattr(
                        target, "BoundingRectangle", Rect(100, 490, 700, 510))
                else:
                    self.auto.menu_mode = "wrong"
                    self.auto.after_escape = lambda: setattr(target, "IsOffscreen", True)
                with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
                    self.client._rows()
                expected_clicks = 0 if phase == "激活之后" else 1
                self.assertEqual(len(self.auto.opened_menus), expected_clicks)
                self.assertEqual(sum(len(row.right_clicks) for row in self.auto.normal_rows), expected_clicks)
                self.assertEqual(self.auto.cancel.clicks, [])
                self.assertEqual(len(self.client._sender_cache), 0)

    def test_external_process_window_is_never_traversed(self):
        foreign = Node(kind="WindowControl", cls="mmui::XMenu", pid=999)
        foreign.forbid_children = True
        self.auto.root.children.append(foreign)
        rows = self.client._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(foreign.reads, 0)
        self.assertEqual(foreign.clicks, [])

    def test_foreign_menu_cannot_supply_multiselect_item(self):
        foreign_item = Node("多选", kind="MenuItemControl", cls="mmui::XMenuView", pid=999)
        foreign = Node(kind="WindowControl", cls="mmui::XMenu", children=[foreign_item], pid=999)
        foreign.forbid_children = True
        self.auto.menu_factory = lambda: foreign
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.client._rows()
        self.assertEqual(foreign.reads, 0)
        self.assertEqual(foreign_item.clicks, [])
        self.assertEqual(self.auto.cancel.clicks, [])

    def test_foreign_foreground_after_menu_never_clicks_or_sends_escape(self):
        foreign = Node(kind="WindowControl", pid=999)
        self.auto.after_open_menu = lambda: setattr(self.auto, "foreground", foreign)
        with self.assertRaisesRegex(AdapterConfigurationError, "恢复.*前台"):
            self.client._rows()
        self.assertEqual(len(self.auto.opened_menus), 1)
        self.assertEqual(sum(len(item.clicks) for item in self.auto.menu_items), 0)
        self.assertEqual(self.auto.cancel.clicks, [])
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(len(self.client._sender_cache), 0)

    def test_foreign_foreground_during_multiselect_never_clicks_cancel(self):
        foreign = Node(kind="WindowControl", pid=999)

        def steal_foreground(rows):
            self.auto.foreground = foreign
            return rows

        self.auto.selected_transform = steal_foreground
        with self.assertRaisesRegex(AdapterConfigurationError, "恢复.*前台"):
            self.client._rows()
        self.assertEqual(sum(len(item.clicks) for item in self.auto.menu_items), 1)
        self.assertEqual(self.auto.cancel.clicks, [])
        self.assertEqual(self.auto.keys, [])
        self.assertIn(self.auto.cancel, self.auto.window.children)
        self.assertEqual(len(self.client._sender_cache), 0)

    def test_incomplete_visibility_never_right_clicks(self):
        for row in self.auto.normal_rows:
            row.BoundingRectangle = Rect(100, 70, 700, 110)
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.client._rows()
        self.assert_no_clicks()

    def test_duplicate_normal_id_rejects_before_clicking(self):
        self.auto.normal_rows[1].runtime_id = self.auto.normal_rows[0].runtime_id
        with self.assertRaises(AdapterConfigurationError):
            self.client._rows()
        self.assert_no_clicks()

    def test_invalid_selected_snapshot_is_cancelled_and_not_cached(self):
        transforms = {
            "缺失": lambda rows: rows[:-1],
            "重复": lambda rows: rows + [rows[0]],
            "多余": lambda rows: rows + [make_row((7, 123), "陌生 新消息", kind="CheckBoxControl")],
            "正文变化": lambda rows: [make_row(rows[0].runtime_id, "nide 已变化", kind="CheckBoxControl"), rows[1]],
        }
        for label, transform in transforms.items():
            with self.subTest(label=label):
                self.setUp()
                self.auto.selected_transform = transform
                with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
                    self.client._rows()
                self.assertEqual(len(self.auto.cancel.clicks), 1)
                self.assertEqual(len(self.client._sender_cache), 0)

    def test_restore_change_rejects_whole_batch_before_caching(self):
        changes = {
            "正文变化": lambda: setattr(self.auto.normal_rows[0], "Name", "已经变化"),
            "新增": lambda: self.auto.messages.children.append(make_row((7, 555), "新消息")),
            "重排": lambda: self.auto.messages.children.reverse(),
            "缺失": lambda: self.auto.messages.children.pop(),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                self.setUp()
                self.auto.after_cancel = change
                with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
                    self.client._rows()
                self.assertEqual(len(self.auto.cancel.clicks), 1)
                self.assertEqual(len(self.client._sender_cache), 0)

    def test_draft_appearing_after_restore_rejects_cache_and_preserves_text(self):
        self.auto.after_cancel = lambda: setattr(self.auto, "draft", "用户刚输入的草稿")
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.client._rows()
        self.assertEqual(self.auto.draft, "用户刚输入的草稿")
        self.assertEqual(len(self.auto.cancel.clicks), 1)
        self.assertEqual(len(self.client._sender_cache), 0)
        self.assertEqual(self.auto.set_values, [])
        self.assertEqual(self.auto.sent, [])

    def test_cancel_failure_or_remaining_multiselect_is_error(self):
        for raises in (True, False):
            with self.subTest(raises=raises):
                self.setUp()
                if raises:
                    self.auto.cancel_error = RuntimeError("模拟取消失败")
                else:
                    self.auto.keep_selected_after_cancel = True
                with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
                    self.client._rows()
                self.assertEqual(len(self.auto.cancel.clicks), 1)
                self.assertEqual(len(self.client._sender_cache), 0)

    def test_recovery_or_verification_failure_permanently_disables_instance(self):
        for reason in ("取消失败", "恢复复核变化"):
            with self.subTest(reason=reason):
                self.setUp()
                if reason == "取消失败":
                    self.auto.cancel_error = RuntimeError("模拟取消失败")
                else:
                    self.auto.after_cancel = lambda: setattr(self.auto.normal_rows[0], "Name", "变化后的正文")
                with self.assertRaises(AdapterConfigurationError):
                    self.client._rows()
                self.assert_permanently_disabled_without_ui_reads()

    def test_operation_and_recovery_errors_are_both_preserved(self):
        def fail_read():
            raise RuntimeError("模拟读取失败")

        def corrupt_selected_read(rows):
            rows[0].GetRuntimeId = fail_read
            return rows

        self.auto.selected_transform = corrupt_selected_read
        self.auto.cancel_error = RuntimeError("模拟取消失败")
        with self.assertRaises(AdapterConfigurationError) as caught:
            self.client._rows()
        error = caught.exception
        for message in ("原操作失败", "恢复失败", "模拟读取失败", "模拟取消失败"):
            self.assertIn(message, str(error))
        self.assertIsInstance(error.operation_error, Exception)
        self.assertIsInstance(error.recovery_error, Exception)
        self.assertIn("模拟读取失败", str(error.operation_error))
        self.assertIn("模拟取消失败", str(error.recovery_error))
        self.assert_permanently_disabled_without_ui_reads()

    def test_ambiguous_menu_never_clicks_either_match(self):
        self.auto.menu_mode = "duplicate"
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.client._rows()
        self.assertEqual(sum(len(item.clicks) for item in self.auto.menu_items), 0)
        self.assertEqual(self.auto.cancel.clicks, [])

    def test_multiselect_text_outside_menu_container_is_not_clicked(self):
        decoy = Node("多选", kind="MenuItemControl", cls="mmui::XMenuView")
        self.auto.menu_factory = lambda: Node(kind="WindowControl", children=[decoy])
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.client._rows()
        self.assertEqual(decoy.clicks, [])
        self.assertEqual(self.auto.cancel.clicks, [])
        self.assertEqual(len(self.auto.keys), len(self.auto.opened_menus))
        self.assertTrue(self.auto.keys)
        self.assertTrue(all(key.lower() in {"{esc}", "{escape}"} for key in self.auto.keys))
        self.assertFalse(any(menu in self.auto.root.children for menu in self.auto.opened_menus))

    def test_unknown_same_process_popup_surviving_escape_stops_after_first_point(self):
        popup = Node(kind="WindowControl", cls="未识别的弹窗")
        self.auto.menu_factory = lambda: popup
        self.auto.close_owned_menus = lambda: None
        with self.assertRaisesRegex(AdapterConfigurationError, "弹窗.*未退出"):
            self.client._rows()
        self.assertEqual(len(self.auto.opened_menus), 1)
        self.assertEqual(sum(len(row.right_clicks) for row in self.auto.normal_rows), 1)
        self.assertEqual(self.auto.keys, ["{Esc}"])
        self.assertIn(popup, self.auto.root.children)
        self.assertEqual(self.auto.cancel.clicks, [])
        self.assertEqual(len(self.client._sender_cache), 0)
        self.assert_permanently_disabled_without_ui_reads()

    def test_unknown_popup_does_not_escape_into_existing_unrelated_window(self):
        unrelated = Node(kind="WindowControl", cls="既有无关窗口")
        self.auto.root.children.append(unrelated)
        self.auto.menu_factory = lambda: Node(kind="WindowControl", cls="未识别的弹窗")
        self.auto.after_open_menu = lambda: setattr(self.auto, "foreground", unrelated)
        with self.assertRaises(AdapterConfigurationError):
            self.client._rows()
        self.assertEqual(len(self.auto.opened_menus), 1)
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(unrelated.clicks, [])
        self.assert_permanently_disabled_without_ui_reads()

    def test_multiple_unknown_new_popups_refuse_escape_and_disable_instance(self):
        self.auto.menu_factory = lambda: Node(kind="WindowControl", cls="未识别弹窗一")

        def add_second_popup():
            self.auto.root.children.append(Node(kind="WindowControl", cls="未识别弹窗二"))

        self.auto.after_open_menu = add_second_popup
        with self.assertRaises(AdapterConfigurationError):
            self.client._rows()
        self.assertEqual(len(self.auto.opened_menus), 1)
        self.assertEqual(self.auto.keys, [])
        self.assertEqual(self.auto.cancel.clicks, [])
        self.assert_permanently_disabled_without_ui_reads()

    def test_ambiguous_cancel_button_does_not_guess_which_to_click(self):
        duplicate = Node("取消多选", kind="ButtonControl")

        def add_duplicate(rows):
            self.auto.window.children.append(duplicate)
            return rows

        self.auto.selected_transform = add_duplicate
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.client._rows()
        self.assertEqual(self.auto.cancel.clicks, [])
        self.assertEqual(duplicate.clicks, [])
        self.assertEqual(len(self.client._sender_cache), 0)

    def test_wrong_menu_or_near_name_only_escapes_its_own_menu(self):
        for mode in ("wrong", "near_name"):
            with self.subTest(mode=mode):
                self.setUp()
                self.auto.menu_mode = mode
                with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
                    self.client._rows()
                self.assertEqual(sum(len(item.clicks) for item in self.auto.menu_items), 0)
                self.assertTrue(self.auto.keys)
                self.assertTrue(all(key.lower() in {"{esc}", "{escape}"} for key in self.auto.keys))
                self.assertLessEqual(len(self.auto.opened_menus), 2)
                self.assertEqual(self.auto.cancel.clicks, [])

    def test_cache_is_bounded_and_uses_full_id_and_content(self):
        self.assertIsInstance(self.client._sender_cache, OrderedDict)
        for index in range(5000):
            self.client._sender_cache[((8, index), "旧正文")] = "旧发送者"
        self.client._rows()
        self.assertLessEqual(len(self.client._sender_cache), 5000)
        self.assertNotIn(((8, 0), "旧正文"), self.client._sender_cache)
        self.assertEqual(self.client._sender_cache[((7, 1), "本人消息")], "nide")
        self.auto.normal_rows[0].Name = "复用控件的新正文"
        rows = self.client._rows()
        self.assertEqual(rows[0].content, "复用控件的新正文")
        self.assertEqual(len(self.auto.opened_menus), 2)

    def test_container_replacement_discards_cached_sender_and_reprobes(self):
        self.client._rows()
        self.auto.messages.runtime_id = (9, 900)
        self.auto.normal_rows[0].sender = "容器重建后的发送者"
        rows = self.client._rows()
        self.assertEqual(rows[0].sender, "容器重建后的发送者")
        self.assertFalse(rows[0].is_self)
        self.assertEqual(len(self.auto.opened_menus), 2)

    def test_observed_group_change_clears_existing_cache_without_extra_clicks(self):
        self.client._rows()
        self.assertTrue(self.client._sender_cache)
        menus, cancels, keys = len(self.auto.opened_menus), len(self.auto.cancel.clicks), list(self.auto.keys)
        self.auto.title.Name = "另一个群"
        with self.assertRaises(RetryableSendError):
            self.client._rows()
        self.assertEqual(len(self.client._sender_cache), 0)
        self.assertIsNone(self.client._container_id)
        self.assertIsNone(self.client._previous_rows)
        self.assertEqual(len(self.auto.opened_menus), menus)
        self.assertEqual(len(self.auto.cancel.clicks), cancels)
        self.assertEqual(self.auto.keys, keys)

    def test_no_common_frame_anchor_discards_even_previously_cached_rows(self):
        first = self.auto.normal_rows[0]
        self.client._rows()
        # 第二帧保留一个共同锚点，旧首行缓存尚可留在容量范围内。
        self.auto.normal_rows = [self.auto.normal_rows[1], make_row((7, 3), "第三条消息", "客户")]
        self.auto.attach_rows()
        self.client._rows()
        self.assertIn((first.runtime_id, first.Name), self.client._sender_cache)
        # 第三帧与紧邻上一帧没有共同锚点，即便缓存命中也必须重新观察身份。
        first.sender = "重新出现的发送者"
        self.auto.normal_rows = [first]
        self.auto.attach_rows()
        rows = self.client._rows()
        self.assertEqual(rows[0].sender, "重新出现的发送者")
        self.assertEqual(len(self.auto.opened_menus), 3)
        self.assertEqual(list(self.client._sender_cache), [(first.runtime_id, first.Name)])

    def test_cached_send_baseline_and_echo_both_require_new_multiselect(self):
        self.client._rows()
        # 模拟同 RuntimeId、同正文的旧缓存，发送回显仍不能直接使用它。
        self.client._sender_cache[((7, 101), "测试发送")] = "nide"
        self.client.send("测试发送")
        self.assertEqual(self.auto.sent, ["测试发送"])
        self.assertEqual(len(self.auto.cancel.clicks), 3)
        self.assertFalse(self.client._fresh_mapping)

    def test_cached_self_identity_cannot_confirm_new_other_sender_echo(self):
        self.client._rows()
        self.client._sender_cache[((7, 101), "测试发送")] = "nide"
        self.auto.after_send = lambda: setattr(self.auto.normal_rows[-1], "sender", "Zlient")
        with patch("windows_adapter.time.monotonic", side_effect=[0, 0, 20]), \
                patch("windows_adapter.time.sleep"):
            with self.assertRaises(SendUncertainError):
                self.client.send("测试发送")
        self.assertEqual(self.auto.sent, ["测试发送"])
        self.assertEqual(len(self.auto.cancel.clicks), 3)
        self.assertFalse(self.client._fresh_mapping)

    def test_send_exception_restores_previous_fresh_mapping_flag(self):
        for previous in (False, True):
            with self.subTest(previous=previous):
                self.setUp()
                self.client._fresh_mapping = previous
                self.auto.draft = "已有草稿"
                with self.assertRaises(RetryableSendError):
                    self.client.send("不能覆盖草稿")
                self.assertIs(self.client._fresh_mapping, previous)
                self.assertEqual(self.auto.draft, "已有草稿")
                self.assert_no_clicks()

    def test_send_confirms_new_self_echo_through_multiselect(self):
        self.client.send("测试发送")
        self.assertEqual(self.auto.sent, ["测试发送"])
        self.assertEqual(self.auto.draft, "")
        self.assertEqual(len(self.auto.cancel.clicks), 2)

    def test_probe_failure_after_submission_is_send_uncertain(self):
        self.auto.after_send = lambda: setattr(self.auto, "cancel_error", RuntimeError("回显探测取消失败"))
        with self.assertRaises(SendUncertainError):
            self.client.send("测试发送")
        self.assertEqual(self.auto.sent, ["测试发送"])

    def test_context_points_requires_two_distinct_finite_interior_points(self):
        invalid = [None, [], [[0.25, 0.6]], [[0.25, 0.6, 0.1], [0.75, 0.6]],
                   [[0.25, 0.6], [0.25, 0.6]], [[0, 0.6], [0.75, 0.6]],
                   [[0.25, 1], [0.75, 0.6]], [[True, 0.6], [0.75, 0.6]],
                   [["0.25", 0.6], [0.75, 0.6]], [[float("nan"), 0.6], [0.75, 0.6]],
                   [[0.25, 0.6], [float("inf"), 0.6]]]
        for points in invalid:
            with self.subTest(points=points):
                config = profile()
                if points is None:
                    del config["context_points"]
                else:
                    config["context_points"] = points
                with self.assertRaises(AdapterConfigurationError):
                    Wechat4Adapter(self.auto, config, "目标群", "微信", expected_pid=41)
        self.assert_no_clicks()

    def test_invalid_menu_depth_rejected_without_ui_actions(self):
        for depth in (None, True, 0, -1, 21, 1.5, "8"):
            with self.subTest(depth=depth):
                config = profile()
                config["menu_depth"] = depth
                with self.assertRaises(AdapterConfigurationError):
                    Wechat4Adapter(self.auto, config, "目标群", "微信", expected_pid=41)
        self.assert_no_clicks()


class Wechat4MentionTests(unittest.TestCase):
    """只构造真实 @ 草稿；候选用 Click，测试中不提交发送键。"""

    def setUp(self):
        self.reset()

    def reset(self, names=None, allow_duplicates=False):
        self.auto = FakeAuto()
        config = profile()
        config.update(mention_mode="wechat4", mention_allow_duplicate_names=allow_duplicates)
        self.client = Wechat4Adapter(self.auto, config, "目标群", "微信",
                                     log=lambda message: None, expected_pid=41)
        self.staff, self.body = "客服 小王🙂", "转发工具联调测试，请忽略。"
        self.candidates = [Node(name, kind="ListItemControl", cls="mmui::XTableCell")
                           for name in (names if names is not None else [self.staff])]
        self.mention_list = Node(aid="chat_mention_list", kind="ListControl",
                                 cls="mmui::XTableView", children=self.candidates)
        self.popover = Node(aid="MentionPopover", kind="WindowControl",
                            cls="mmui::XPopover", children=[self.mention_list])
        self.popover.parent = self.auto.window
        self.token_value = "\ufffc"
        self.token_text = None
        self.text_override = None
        self.keep_popover = False
        self.trigger_draft = "@"
        self.trigger_popovers = [self.popover]
        self.after_trigger = None
        self.after_type = None
        self.after_click = None
        self.typed = []
        self.events = []
        self.text_range = SimpleNamespace(GetText=lambda length: (
            self.auto.draft if self.text_override is None else self.text_override
        ))
        self.auto.edit.patterns[FakeAuto.PatternId.TextPattern] = SimpleNamespace(
            DocumentRange=self.text_range
        )
        for candidate in self.candidates:
            candidate.on_click = self.select_candidate
        self.client._bind()
        self.auto.focus()

    def select_candidate(self):
        self.auto.draft = self.token_value
        self.text_override = self.token_text
        if not self.keep_popover:
            self.auto.window.children.remove(self.popover)
        if self.after_click:
            self.after_click()
        return True

    def type_text(self, edit, text):
        self.assertIs(edit, self.auto.edit)
        if text == self.staff:
            self.assertEqual(self.auto.draft, "@")
            self.assertEqual([node for node in self.auto.window.children
                              if node.AutomationId == "MentionPopover"], [self.popover],
                             "只有物理 @ 已打开唯一弹层后才能输入昵称")
        self.events.append(("type", text))
        self.typed.append(text)
        self.auto.draft += text.replace("\r\n", "\n").replace("\r", "\n")
        if text == self.staff:
            if self.after_type:
                self.after_type()

    def compose(self):
        def send_keys(keys, **kwargs):
            self.auto.keys.append(keys)
            if keys == "{Shift}2":
                self.assertIs(kwargs.get("charMode"), False)
                self.assertEqual(self.auto.draft, "")
                self.assertIs(self.auto.focused, self.auto.edit)
                self.assertIs(self.auto.foreground, self.auto.window)
                self.events.append(("physical_at", keys))
                self.auto.draft = self.trigger_draft
                self.auto.window.children.extend(self.trigger_popovers)
                if self.after_trigger:
                    self.after_trigger()
            else:
                self.assertEqual(keys, "{Ctrl}{End}", "构造真实 @ 时不得按发送键")

        with patch.object(self.client, "_type", side_effect=self.type_text), \
                patch.object(self.client, "_focus", side_effect=lambda edit: self.auto.focus()), \
                patch.object(self.auto, "SendKeys", side_effect=send_keys):
            return self.client._compose_mention(self.auto.edit, self.staff, self.body)

    def assert_no_candidate_click(self):
        self.assertTrue(all(not candidate.clicks for candidate in self.candidates))
        self.assertEqual(self.auto.sent, [])
        self.assertIn(self.auto.keys, ([], ["{Shift}2"]))

    def mutate_after_selection(self, mutate):
        original = self.client._mention_selection
        calls = 0

        def read_selection(staff):
            nonlocal calls
            value = original(staff)
            calls += 1
            if calls == 1:
                mutate()
            return value

        self.client._mention_selection = read_selection

    def test_exact_nickname_and_both_expected_forms_without_submit(self):
        result = self.compose()
        self.assertEqual(result, ("\ufffc " + self.body, "@" + self.staff + "\u2005 " + self.body))
        self.assertEqual(self.auto.draft, result[0])
        self.assertEqual(self.typed, [self.staff, " " + self.body])
        self.assertEqual(self.events[:2], [("physical_at", "{Shift}2"), ("type", self.staff)])
        self.assertEqual(len(self.candidates[0].clicks), 1)
        self.assertEqual(self.auto.keys, ["{Shift}2", "{Ctrl}{End}"])
        self.assertEqual(self.auto.sent, [])

    def test_near_name_cannot_replace_exact_nickname(self):
        for name in ("客服 小王", "客服 小王🙂 ", " 客服 小王🙂", "客服 小王🙂（外部）"):
            with self.subTest(name=name):
                self.reset(names=[name])
                with self.assertRaises(AdapterConfigurationError):
                    self.compose()
                self.assert_no_candidate_click()

    def test_exact_candidate_selected_among_similar_names(self):
        self.reset(names=["客服 小王", "客服 小王🙂", "客服 小王🙂（外部）"])
        self.compose()
        self.assertEqual([len(node.clicks) for node in self.candidates], [0, 1, 0])

    def test_duplicate_names_require_explicit_boolean_permission(self):
        for allow in (False, None, 1, "true"):
            with self.subTest(allow=allow):
                self.reset(names=["客服 小王🙂", "客服 小王🙂"], allow_duplicates=allow)
                with self.assertRaises(AdapterConfigurationError):
                    self.compose()
                self.assert_no_candidate_click()

    def test_duplicate_names_explicitly_allowed_click_first_once(self):
        self.reset(names=["客服 小王🙂", "客服 小王🙂"], allow_duplicates=True)
        self.compose()
        self.assertEqual([len(node.clicks) for node in self.candidates], [1, 0])

    def test_wrong_pid_on_each_mention_node_is_rejected(self):
        for target in ("popover", "mention_list", "candidate"):
            with self.subTest(target=target):
                self.reset()
                node = self.candidates[0] if target == "candidate" else getattr(self, target)
                node.ProcessId = 99
                with self.assertRaises(AdapterConfigurationError):
                    self.compose()
                self.assert_no_candidate_click()

    def test_changed_runtime_identity_on_each_mention_node_is_rejected(self):
        for target in ("popover", "mention_list", "candidate"):
            with self.subTest(target=target):
                self.reset()
                node = self.candidates[0] if target == "candidate" else getattr(self, target)
                self.mutate_after_selection(lambda: setattr(node, "runtime_id", (7, 987654)))
                with self.assertRaises(AdapterConfigurationError):
                    self.compose()
                self.assert_no_candidate_click()

    def test_draft_changed_before_candidate_click_is_rejected(self):
        self.mutate_after_selection(lambda: setattr(self.auto, "draft", "用户新草稿"))
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.compose()
        self.assert_no_candidate_click()
        self.assertEqual(self.auto.draft, "用户新草稿")

    def test_group_changed_before_candidate_click_is_rejected(self):
        self.mutate_after_selection(lambda: setattr(self.auto.title, "Name", "另一个群"))
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.compose()
        self.assert_no_candidate_click()

    def test_unrelated_same_pid_foreground_before_click_is_rejected(self):
        unrelated = Node(kind="WindowControl")
        self.auto.root.children.append(unrelated)
        self.mutate_after_selection(lambda: setattr(self.auto, "foreground", unrelated))
        with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
            self.compose()
        self.assert_no_candidate_click()

    def test_both_patterns_must_show_exactly_one_object_marker(self):
        for value, text in (("@客服 小王🙂", "\ufffc"), ("\ufffc", "@客服 小王🙂"),
                            ("\ufffc ", "\ufffc"), ("\ufffc", "\ufffc\n"),
                            ("\ufffc\ufffc", "\ufffc\ufffc"), ("", "")):
            with self.subTest(value=value, text=text):
                self.reset()
                self.token_value, self.token_text = value, text
                with self.assertRaises(AdapterConfigurationError):
                    self.compose()
                self.assertEqual(len(self.candidates[0].clicks), 1)
                self.assertEqual(self.typed, [self.staff])
                self.assertEqual(self.auto.keys, ["{Shift}2"])
                self.assertEqual(self.auto.sent, [])

    def test_missing_text_pattern_cannot_confirm_real_mention(self):
        self.auto.edit.patterns.pop(FakeAuto.PatternId.TextPattern)
        with self.assertRaises(AdapterConfigurationError):
            self.compose()
        self.assertEqual(self.typed, [self.staff])
        self.assertEqual(self.auto.sent, [])

    def test_remaining_popover_after_click_prevents_body_and_submit(self):
        self.keep_popover = True
        with self.assertRaises(AdapterConfigurationError):
            self.compose()
        self.assertEqual(len(self.candidates[0].clicks), 1)
        self.assertEqual(self.typed, [self.staff])
        self.assertEqual(self.auto.keys, ["{Shift}2"])

    def test_offscreen_or_clipped_candidate_is_not_clicked(self):
        for state in ("offscreen", "clipped"):
            with self.subTest(state=state):
                self.reset()
                if state == "offscreen":
                    self.candidates[0].IsOffscreen = True
                else:
                    self.candidates[0].BoundingRectangle = Rect(0, 0, 801, 600)
                with self.assertRaises(AdapterConfigurationError):
                    self.compose()
                self.assert_no_candidate_click()

    def test_replaced_input_control_is_rejected_at_entry_and_after_click(self):
        for phase in ("entry", "after_click"):
            with self.subTest(phase=phase):
                self.reset()
                replacement = Node(aid="input", kind="EditControl")
                replacement.patterns = self.auto.edit.patterns.copy()
                replacement.parent = self.auto.window

                def replace_input():
                    index = self.auto.window.children.index(self.auto.edit)
                    self.auto.window.children[index] = replacement

                if phase == "entry":
                    replace_input()
                else:
                    self.after_click = replace_input
                with self.assertRaises(AdapterConfigurationError):
                    self.compose()
                self.assertEqual(self.auto.sent, [])
                self.assertEqual(self.auto.keys, [] if phase == "entry" else ["{Shift}2"])
                if phase == "entry":
                    self.assert_no_candidate_click()
                    self.assertEqual(self.typed, [])
                else:
                    self.assertEqual(self.typed, [self.staff])

    def test_input_wrong_pid_is_rejected_at_entry_and_after_click(self):
        for phase in ("entry", "after_click"):
            with self.subTest(phase=phase):
                self.reset()
                if phase == "entry":
                    self.auto.edit.ProcessId = 99
                else:
                    self.after_click = lambda: setattr(self.auto.edit, "ProcessId", 99)
                with self.assertRaises(AdapterConfigurationError):
                    self.compose()
                self.assertEqual(self.auto.sent, [])
                self.assertEqual(self.auto.keys, [] if phase == "entry" else ["{Shift}2"])
                self.assertNotIn(" " + self.body, self.typed)

    def test_crlf_body_returns_normalized_draft_and_echo(self):
        self.body = "第一行\r\n第二行\r第三行"
        normalized = "第一行\n第二行\n第三行"
        result = self.compose()
        self.assertEqual(result, ("\ufffc " + normalized, "@" + self.staff + "\u2005 " + normalized))
        self.assertEqual(self.auto.draft, result[0])
        self.assertEqual(self.auto.sent, [])

    def test_observed_echo_requires_u2005_then_ascii_space(self):
        # 使用已捕获回显的精确码点；普通空格不能冒充真实 @ 后的 U+2005。
        self.staff = "圆通快递"
        self.candidates[0].Name = self.staff
        draft, echo = self.compose()
        self.assertEqual(draft, "\ufffc 转发工具联调测试，请忽略。")
        self.assertEqual(echo, "@圆通快递\u2005 转发工具联调测试，请忽略。")
        self.assertEqual(echo.count("\u2005"), 1)
        self.assertNotIn("\u2005", draft)
        self.assertNotEqual(echo, "@圆通快递  转发工具联调测试，请忽略。")
        self.assertNotEqual(echo, "@圆通快递 转发工具联调测试，请忽略。")
        self.assertEqual(self.auto.sent, [])

    def test_mention_preserves_body_edge_whitespace_and_only_normalizes_newlines(self):
        self.body = " \t第一行 \r\n第二行\r第三行\t  "
        normalized = " \t第一行 \n第二行\n第三行\t  "
        draft, echo = self.compose()
        self.assertEqual(draft, "\ufffc " + normalized)
        self.assertEqual(echo, "@" + self.staff + "\u2005 " + normalized)
        self.assertEqual(self.auto.draft, draft)
        self.assertEqual(self.typed[-1], " " + normalized)
        self.assertEqual(echo.count("\u2005"), 1)
        self.assertNotIn("\u2005", draft)
        self.assertTrue(draft.endswith("\t  "))
        self.assertTrue(echo.endswith("\t  "))
        self.assertEqual(self.auto.sent, [])

    def test_physical_at_wrong_draft_stops_before_typing_nickname(self):
        for draft in ("", "2", "＠", "@用户草稿"):
            with self.subTest(draft=draft):
                self.reset()
                self.trigger_draft = draft
                with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
                    self.compose()
                self.assertEqual(self.auto.draft, draft)
                self.assertEqual(self.typed, [])
                self.assert_no_candidate_click()

    def test_physical_at_requires_unique_same_pid_popover_before_nickname(self):
        for state in ("missing", "duplicate", "wrong_pid"):
            with self.subTest(state=state):
                self.reset()
                if state == "missing":
                    self.trigger_popovers = []
                elif state == "duplicate":
                    self.trigger_popovers.append(Node(aid="MentionPopover", kind="WindowControl",
                                                      cls="mmui::XPopover"))
                else:
                    self.popover.ProcessId = 99
                with self.assertRaises(AdapterConfigurationError):
                    self.compose()
                self.assertEqual(self.auto.draft, "@")
                self.assertEqual(self.typed, [])
                self.assert_no_candidate_click()

    def test_focus_group_or_foreground_change_after_physical_at_stops_nickname(self):
        for state in ("focus", "group", "foreground"):
            with self.subTest(state=state):
                self.reset()
                if state == "focus":
                    self.after_trigger = lambda: setattr(self.auto, "focused", None)
                elif state == "group":
                    self.after_trigger = lambda: setattr(self.auto.title, "Name", "另一个群")
                else:
                    self.after_trigger = lambda: setattr(self.auto, "foreground", Node(kind="WindowControl"))
                with self.assertRaises((AdapterConfigurationError, RetryableSendError)):
                    self.compose()
                self.assertEqual(self.auto.draft, "@")
                self.assertEqual(self.typed, [])
                self.assert_no_candidate_click()


if __name__ == "__main__":
    unittest.main()
