"""微信 4 多选态发送者适配；所有界面操作限定于已绑定的进程和群。

右键比例点必须由本机诊断显式配置，本模块不从全宽消息行推测气泡方向。
self_names 只提供完整昵称匹配，昵称重名不能等同于唯一账号身份。
缓存无法证明未观察到的往返切群或同 ID 同正文复用；RuntimeId 不是服务器
消息 ID。发现群、容器或快照锚点变化时清空缓存，发送阶段始终重新读取发送者。
恢复或恢复复核失败后实例永久停用，必须核对界面并新建实例后才能继续。
"""

from collections import OrderedDict
import hashlib
import json
import math
import time

from wechat_forwarder import Message, RetryableSendError
from wechat_multiselect import SenderMappingError, WxRow, sender_mapping
from windows_adapter import (
    AdapterConfigurationError, WindowsChatAdapter, _normalized,
    find_all, matches, select, select_retry,
)


class Wechat4RecoveryError(AdapterConfigurationError):
    """保存操作与恢复两阶段异常，避免恢复失败遮蔽最初原因。"""

    def __init__(self, recovery_error, operation_error=None):
        self.operation_error = operation_error
        self.recovery_error = recovery_error
        detail = f"恢复失败：{type(recovery_error).__name__}: {recovery_error}"
        if operation_error is not None:
            detail = f"原操作失败：{type(operation_error).__name__}: {operation_error}；" + detail
        super().__init__(f"微信多选恢复或复核失败，当前实例已停用，请人工核对界面并重新创建会话；{detail}")


class Wechat4Adapter(WindowsChatAdapter):
    """临时切换多选读取发送者，恢复并复核成功后才接纳整批消息。"""

    CACHE_CAPACITY = 5000
    TEXT_CLASS = "mmui::ChatTextItemView"

    def __init__(self, auto, profile, group, namespace, log=print, expected_pid=None):
        super().__init__(auto, profile, group, namespace, log, expected_pid)
        points = self.profile.get("context_points")
        if not isinstance(points, (list, tuple)) or len(points) != 2:
            raise AdapterConfigurationError("微信 4 必须显式配置两个 context_points 比例点")
        for point in points:
            if (
                not isinstance(point, (list, tuple)) or len(point) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       or not 0 < value < 1 for value in point)
            ):
                raise AdapterConfigurationError("context_points 必须是两个控件内部的有限比例坐标")
        self._context_points = tuple(tuple(point) for point in points)
        if len(set(self._context_points)) != 2:
            raise AdapterConfigurationError("context_points 的两个比例点不能相同")
        self._menu_depth = self.profile.get("menu_depth", 8)
        if type(self._menu_depth) is not int or not 1 <= self._menu_depth <= 20:
            raise AdapterConfigurationError("menu_depth 必须是 1～20 的整数")
        if not isinstance(self.profile.get("window"), dict):
            raise AdapterConfigurationError("微信 4 window 必须是直接顶层窗口的单个选择器")
        names = self.profile.get("self_names")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
            raise AdapterConfigurationError("self_names 必须填写本人完整群昵称列表")
        self._sender_cache = OrderedDict()
        self._container_id = None
        self._previous_rows = None
        self._fresh_mapping = False
        self._unusable_reason = None
        self._window_identity = None
        self._operation_menu_identity = None
        self._menu_identity_captured = False
        self._menu_roots = {}

    def clear_sender_cache(self):
        """清除可能跨会话或控件重建失效的发送者信息。"""
        self._sender_cache.clear()
        self._container_id = None
        self._previous_rows = None

    def _send(self, text, staff=None):
        # 发送前快照与发送回显均重新观察身份，不能用旧缓存确认本人回显。
        previous = self._fresh_mapping
        self._fresh_mapping = True
        try:
            return super()._send(text, staff=staff)
        finally:
            self._fresh_mapping = previous

    def _bind(self):
        # 恢复失败后必须先人工核对，不得由再次绑定或清缓存解除停用状态。
        if self._unusable_reason is not None:
            raise AdapterConfigurationError(
                f"微信多选适配器当前实例已停用，请人工核对界面并重新创建会话：{self._unusable_reason}"
            )

        def collect():
            # 只匹配桌面直接子窗，不能为定位微信遍历其他进程的控件树。
            roots = self.auto.GetRootControl().GetChildren()
            return [node for node in roots
                    if (self._expected_pid is None or getattr(node, "ProcessId", None) == self._expected_pid)
                    and matches(node, self.profile["window"])]

        candidates = collect()
        if len(candidates) != 1:
            # 无障碍激活切换 Qt/mmui 窗口暴露时，新旧节点可能短暂并存或缺失；
            # 只做有限等待，不放宽名称或进程匹配条件，也不把歧义当唯一。
            deadline = time.monotonic() + 1.0
            while len(candidates) != 1 and time.monotonic() < deadline:
                time.sleep(0.1)
                candidates = collect()
        if len(candidates) != 1:
            raise AdapterConfigurationError(
                f"微信 4 初始进程窗口未唯一匹配（候选 {len(candidates)} 个），"
                "进程可能已变化或界面正在切换；请重新启动会话"
            )
        window = candidates[0]
        pid = getattr(window, "ProcessId", None)
        if type(pid) is not int or pid <= 0:
            raise AdapterConfigurationError("微信 4 窗口未提供有效进程标识")
        if self._expected_pid is None:
            self._expected_pid = pid
        if self.profile.get("process_name") and pid != self._process_id:
            process = next((p for p in self.auto.GetProcesses(False) if p.pid == pid), None)
            if process is None or process.exeName.lower() != self.profile["process_name"].lower():
                raise AdapterConfigurationError("窗口所属进程与微信适配配置不符")
            self._process_id = pid
        identity = self._window_key(window)
        if self._window_identity is None:
            self._window_identity = identity
        elif identity != self._window_identity:
            self.clear_sender_cache()
            raise AdapterConfigurationError("已绑定微信主窗口的 RuntimeId 或 NativeWindowHandle 发生变化，请重新创建会话")
        self.window = window
        title = select_retry(window, self.profile["group_title"], "当前会话标题")
        if str(title.Name or "").strip() != self.group:
            self.clear_sender_cache()
            raise RetryableSendError("微信当前会话不是配置的目标群，已拒绝操作")
        return window

    def _same_process_roots(self):
        return [node for node in self.auto.GetRootControl().GetChildren()
                if getattr(node, "ProcessId", None) == self._expected_pid]

    @staticmethod
    def _runtime_id(node):
        value = tuple(node.GetRuntimeId() or ())
        if not value or any(type(part) is not int for part in value):
            raise AdapterConfigurationError("微信文本行缺少完整整数 RuntimeId")
        return value

    @classmethod
    def _window_key(cls, node):
        handle = getattr(node, "NativeWindowHandle", None)
        if type(handle) is not int or handle < 0:
            raise AdapterConfigurationError("微信窗口缺少可核对的 NativeWindowHandle")
        return cls._runtime_id(node), handle

    def _snapshot(self, expected_type):
        container = select(self.window, self.profile["messages"], "微信消息列表")
        if getattr(container, "ProcessId", None) != self._expected_pid:
            raise AdapterConfigurationError("微信消息列表所属进程不符")
        controls = find_all(container, {"class_name": self.TEXT_CLASS, "depth": 1})
        rows, seen = [], set()
        for control in controls:
            if getattr(control, "ProcessId", None) != self._expected_pid:
                raise AdapterConfigurationError("微信文本行所属进程不符")
            if control.ControlTypeName != expected_type:
                raise AdapterConfigurationError("微信消息列表不处于预期普通态或多选态")
            runtime_id = self._runtime_id(control)
            if runtime_id in seen:
                raise AdapterConfigurationError("微信文本行包含重复 RuntimeId")
            seen.add(runtime_id)
            content = control.Name
            if not isinstance(content, str) or not content:
                raise AdapterConfigurationError("微信文本行正文为空或不可读")
            rows.append(WxRow(runtime_id, content))
        return rows, controls, container

    def _cancel_buttons(self):
        return find_all(self.window, {"name": "取消多选", "type": "ButtonControl"})

    def _menus(self):
        menus, seen = [], set()
        self._menu_roots = {}
        for root in self._same_process_roots():
            found = ([root] if getattr(root, "ClassName", None) == "mmui::XMenu" else [])
            found += find_all(root, {"class_name": "mmui::XMenu", "depth": self._menu_depth})
            for menu in found:
                if getattr(menu, "ProcessId", None) != self._expected_pid:
                    raise AdapterConfigurationError("消息菜单所属进程发生变化")
                key = self._runtime_id(menu)
                if key not in seen:
                    seen.add(key)
                    menus.append(menu)
                    self._menu_roots[key] = self._window_key(root)
        return menus

    def _empty_draft(self):
        edit = select(self.window, self.profile["input"], "微信输入框")
        if getattr(edit, "ProcessId", None) != self._expected_pid:
            raise AdapterConfigurationError("微信输入框所属进程不符")
        if self._value(edit):
            raise RetryableSendError("微信输入框已有草稿，已拒绝打开多选；请先处理草稿")

    def _mention_popovers(self):
        return find_all(self.window, {
            "type": "WindowControl", "automation_id": "MentionPopover", "class_name": "mmui::XPopover",
        })

    def _mention_selection(self, staff):
        """只读取本机已核对的原生 @ 弹层与直属候选，不按模糊昵称选人。"""
        popovers = self._mention_popovers()
        if len(popovers) != 1:
            raise AdapterConfigurationError("微信原生 @ 弹层未唯一匹配")
        popover = popovers[0]
        if getattr(popover, "ProcessId", None) != self._expected_pid:
            raise AdapterConfigurationError("微信原生 @ 弹层所属进程不符")
        tables = find_all(popover, {
            "automation_id": "chat_mention_list", "class_name": "mmui::XTableView",
        })
        if len(tables) != 1:
            raise AdapterConfigurationError("微信原生 @ 候选列表未唯一匹配")
        table = tables[0]
        if getattr(table, "ProcessId", None) != self._expected_pid:
            raise AdapterConfigurationError("微信原生 @ 候选列表所属进程不符")
        candidates = find_all(table, {
            "type": "ListItemControl", "class_name": "mmui::XTableCell", "name": staff, "depth": 1,
        })
        if not candidates:
            raise AdapterConfigurationError("微信原生 @ 列表没有完整昵称匹配的候选")
        if len(candidates) > 1 and self.profile.get("mention_allow_duplicate_names") is not True:
            raise AdapterConfigurationError("微信原生 @ 存在多个同名候选，未明确允许选择任一同名成员")
        if any(getattr(node, "ProcessId", None) != self._expected_pid
               for node in [popover, table, *candidates]):
            raise AdapterConfigurationError("微信原生 @ 弹层或候选所属进程不符")
        popover_id, table_id = self._window_key(popover), self._runtime_id(table)
        identities = [self._runtime_id(node) for node in candidates]
        if len(set(identities)) != len(identities):
            raise AdapterConfigurationError("微信原生 @ 候选包含重复 RuntimeId")
        return (popover, table, candidates[0]), (popover_id, table_id, identities[0])

    def _mention_input(self, expected_id):
        edit = select(self.window, self.profile["input"], "微信当前输入框")
        if (getattr(edit, "ProcessId", None) != self._expected_pid
                or self._runtime_id(edit) != expected_id):
            raise AdapterConfigurationError("微信原生 @ 输入框的进程或 RuntimeId 发生变化")
        return edit

    def _assert_mention_token(self, edit):
        value = edit.GetPattern(self.auto.PatternId.ValuePattern)
        text = edit.GetPattern(self.auto.PatternId.TextPattern)
        if not value or not text or value.Value != "\ufffc" or text.DocumentRange.GetText(-1) != "\ufffc":
            raise AdapterConfigurationError("微信原生 @ 标记未由 Value 与 Text 同时确认为单个嵌入对象")

    def _compose_mention(self, edit, staff, text):
        """点击原生候选并验证标记，分别返回输入草稿与可见消息的期望正文。"""
        if self.profile.get("mention_mode") != "wechat4":
            return super()._compose_mention(edit, staff, text)
        if not isinstance(staff, str) or not staff.strip():
            raise AdapterConfigurationError("微信原生 @ 必须填写客服完整名称")
        self._bind()
        if getattr(edit, "ProcessId", None) != self._expected_pid:
            raise AdapterConfigurationError("微信原生 @ 输入框所属进程不符")
        edit_id = self._runtime_id(edit)
        edit = self._mention_input(edit_id)
        self._foreground_is_ours()
        if self._value(edit):
            raise RetryableSendError("微信输入框已有草稿，已拒绝输入原生 @")
        if self._mention_popovers():
            raise AdapterConfigurationError("微信已有用户打开的 @ 弹层，已拒绝操作")
        prefix = "@" + staff
        # 微信 4 不一定用 Unicode 注入的 @ 唤起候选，先发送物理 Shift+2。
        self._check_focus(edit)
        self._foreground_is_ours()
        edit = self._mention_input(edit_id)
        if self._value(edit):
            raise RetryableSendError("输入物理 @ 前出现新草稿，已保留草稿并停止")
        self.auto.SendKeys("{Shift}2", charMode=False, waitTime=0.1)
        self._bind()
        edit = self._mention_input(edit_id)
        if self._value(edit) != "@":
            raise AdapterConfigurationError("物理 @ 输入结果不是单个 @，已保留草稿并停止")
        popovers = self._wait_for(self._mention_popovers, bool)
        if len(popovers) != 1 or getattr(popovers[0], "ProcessId", None) != self._expected_pid:
            raise AdapterConfigurationError("物理 @ 后未找到唯一同进程微信原生弹层，已保留草稿")
        self._bind()
        edit = self._mention_input(edit_id)
        self._check_focus(edit)
        self._foreground_is_ours()
        if self._value(edit) != "@":
            raise AdapterConfigurationError("输入客服昵称前 @ 草稿发生变化，已保留草稿并停止")
        # 只有原生弹层确实出现后才输入昵称，不能把普通 @ 文本当成真实提醒。
        self._type(edit, staff)
        popovers = self._wait_for(self._mention_popovers, bool)
        if len(popovers) != 1:
            raise AdapterConfigurationError("输入昵称后未找到唯一微信原生 @ 弹层")
        _, original_ids = self._mention_selection(staff)
        self._bind()
        edit = self._mention_input(edit_id)
        if self._value(edit) != prefix:
            raise AdapterConfigurationError("点击 @ 候选前草稿与完整昵称不同，已保留草稿")
        current, current_ids = self._mention_selection(staff)
        if current_ids != original_ids:
            raise AdapterConfigurationError("微信原生 @ 弹层、列表或候选身份发生变化")
        if not self._fully_visible(current[2], current[1]):
            raise AdapterConfigurationError("微信原生 @ 候选不完全可见，已拒绝点击")
        self._foreground_is_ours()
        # 只点击已复核的候选，不能用 Enter 代替选择，避免误提交消息。
        current[2].Click(waitTime=0.1)
        self._bind()
        if self._wait_for(self._mention_popovers, lambda value: not value):
            raise AdapterConfigurationError("选择原生 @ 后弹层仍存在，已保留草稿")
        edit = self._mention_input(edit_id)
        self._assert_mention_token(edit)
        self._focus(edit)
        self._check_focus(edit)
        edit = self._mention_input(edit_id)
        self._foreground_is_ours()
        self._assert_mention_token(edit)
        self.auto.SendKeys("{Ctrl}{End}", waitTime=0.05)
        body = _normalized(text)
        self._type(edit, " " + body)
        # 已核对微信 4.1.15.56 原生 @ 回显在昵称后附加 U+2005，保留正文空白。
        return "\ufffc " + body, prefix + "\u2005 " + body

    @staticmethod
    def _rectangle(node):
        rect = node.BoundingRectangle
        values = (rect.left, rect.top, rect.right, rect.bottom)
        if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
            raise AdapterConfigurationError("消息控件的可见位置不可读")
        return values

    def _fully_visible(self, node, container):
        if getattr(node, "IsOffscreen", False) or getattr(container, "IsOffscreen", False):
            return False
        left, top, right, bottom = self._rectangle(node)
        if left >= right or top >= bottom:
            return False
        for outer in (container, self.window):
            ol, ot, ore, ob = self._rectangle(outer)
            if not (ol <= left < right <= ore and ot <= top < bottom <= ob):
                return False
        return True

    def _foreground_is_ours(self, allow_menu=False):
        foreground = self.auto.GetForegroundControl()
        if foreground is None or getattr(foreground, "ProcessId", None) != self._expected_pid:
            raise AdapterConfigurationError("前台窗口不属于已绑定微信进程，已拒绝键鼠操作")
        allowed = {self._window_identity}
        if allow_menu and self._operation_menu_identity is not None:
            allowed.add(self._operation_menu_identity)
        if self._window_key(foreground) not in allowed:
            raise AdapterConfigurationError("前台窗口不是已绑定微信主窗或本次菜单根窗口，已拒绝键鼠操作")

    def _capture_menu_root(self, previous_roots):
        """只认本次右键后唯一新增的同进程桌面子窗，不接纳既有其他窗口。"""
        current = [self._window_key(root) for root in self._same_process_roots()]
        added = [identity for identity in current if identity not in previous_roots]
        if len(added) > 1:
            raise AdapterConfigurationError("右键后新增同进程窗口不唯一，无法确认本次菜单根窗口")
        self._operation_menu_identity = added[0] if added else None
        self._menu_identity_captured = True

    @staticmethod
    def _wait_for(read, ready, attempts=11):
        for attempt in range(attempts):
            value = read()
            if ready(value):
                return value
            if attempt < attempts - 1:
                time.sleep(0.05)
        return value

    def _restore(self, original, previous_roots):
        """仅清理由本次右键可能建立的状态；调用方不得对既有用户状态调用。"""
        try:
            self._bind()
            if not self._menu_identity_captured:
                self._capture_menu_root(previous_roots)
            buttons = self._cancel_buttons()
            if len(buttons) > 1:
                raise AdapterConfigurationError("取消多选按钮不唯一")
            if buttons:
                if getattr(buttons[0], "ProcessId", None) != self._expected_pid:
                    raise AdapterConfigurationError("取消多选按钮所属进程不符")
                self._foreground_is_ours()
                buttons[0].Click(waitTime=0.1)
                # 微信退出多选偶发慢于半秒；只延长等待，不点击第二次、不放宽复核。
                remaining = self._wait_for(self._cancel_buttons, lambda value: not value, attempts=41)
                if remaining:
                    raise AdapterConfigurationError("取消多选按钮仍存在")
            else:
                # 仅向已绑定主窗或本次唯一新增菜单根发送 Esc，不能影响同进程其他窗。
                self._foreground_is_ours(allow_menu=True)
                self.auto.SendKeys("{Esc}", waitTime=0.1)
            self._bind()
            if self._cancel_buttons() or self._menus():
                raise AdapterConfigurationError("本次菜单或多选状态未退出")
            if any(self._window_key(node) not in previous_roots for node in self._same_process_roots()):
                raise AdapterConfigurationError("本次右键后出现的同进程弹窗仍未退出")
            restored, _, _ = self._snapshot("ListItemControl")
            if restored != original:
                raise AdapterConfigurationError("多选恢复后消息 ID、正文或顺序发生变化")
        except Exception as exc:
            self._unusable_reason = str(exc)
            self.clear_sender_cache()
            raise Wechat4RecoveryError(exc) from exc

    def _read_senders(self, original, controls, container):
        self._empty_draft()
        if self._menus():
            raise AdapterConfigurationError("微信已有用户打开的消息菜单，已拒绝操作")
        target = next((row for row in reversed(controls) if self._fully_visible(row, container)), None)
        if target is None:
            raise AdapterConfigurationError("没有完全可见的微信文本行，不能安全打开多选")
        target_id = self._runtime_id(target)
        for x, y in self._context_points:
            self._bind()
            self._empty_draft()
            current, _, _ = self._snapshot("ListItemControl")
            if current != original or self._cancel_buttons() or self._menus():
                raise AdapterConfigurationError("右键前消息或界面状态已变化")
            self.window.SetActive(waitTime=0.1)
            self._bind()
            self._empty_draft()
            self._foreground_is_ours()
            current, current_controls, current_container = self._snapshot("ListItemControl")
            if current != original or self._cancel_buttons() or self._menus():
                raise AdapterConfigurationError("激活窗口后消息或界面状态发生变化")
            target = next(row for row in current_controls if self._runtime_id(row) == target_id)
            if not self._fully_visible(target, current_container):
                raise AdapterConfigurationError("右键前目标消息已不再完全可见")
            previous_roots = {self._window_key(node) for node in self._same_process_roots()}
            self._operation_menu_identity = None
            self._menu_identity_captured = False
            opened = False
            result = None
            operation_error = None
            try:
                # 调用前标记，右键 API 抛错也必须检查本次可能打开的菜单。
                opened = True
                target.RightClick(ratioX=x, ratioY=y, waitTime=0.1)
                self._capture_menu_root(previous_roots)
                self._bind()
                self._empty_draft()
                menus = self._wait_for(self._menus, bool)
                if len(menus) != 1:
                    continue
                menu_root = self._menu_roots[self._runtime_id(menus[0])]
                if menu_root not in {self._window_identity, self._operation_menu_identity}:
                    raise AdapterConfigurationError("多选菜单不属于已绑定主窗或本次新增菜单根窗口")
                choices = find_all(menus[0], {
                    "class_name": "mmui::XMenuView", "name": "多选", "depth": self._menu_depth,
                })
                if len(choices) != 1:
                    continue
                if getattr(choices[0], "ProcessId", None) != self._expected_pid:
                    raise AdapterConfigurationError("多选菜单项所属进程不符")
                self._bind()
                self._empty_draft()
                current, _, _ = self._snapshot("ListItemControl")
                if current != original:
                    raise AdapterConfigurationError("进入多选前消息发生变化")
                self._foreground_is_ours(allow_menu=True)
                choices[0].Click(waitTime=0.1)
                self._bind()
                buttons = self._wait_for(self._cancel_buttons, bool)
                if len(buttons) != 1:
                    raise AdapterConfigurationError("进入多选后未找到唯一取消多选按钮")
                selected, _, _ = self._snapshot("CheckBoxControl")
                if [r.runtime_id for r in selected] != [r.runtime_id for r in original]:
                    raise AdapterConfigurationError("多选时消息 ID 或顺序发生变化")
                result = sender_mapping(original, selected)
            except SenderMappingError as exc:
                operation_error = AdapterConfigurationError(f"微信多选发送者解析失败：{exc}")
                raise operation_error from exc
            except Exception as exc:
                operation_error = exc
                raise
            finally:
                if opened:
                    try:
                        self._restore(original, previous_roots)
                    except Wechat4RecoveryError as recovery:
                        if operation_error is not None:
                            raise Wechat4RecoveryError(recovery.recovery_error, operation_error) from recovery
                        raise
                    finally:
                        self._operation_menu_identity = None
                        self._menu_identity_captured = False
            if result is not None:
                return result
        raise AdapterConfigurationError("两个配置右键位置均未找到唯一的多选菜单项")

    def _rows(self):
        self._bind()
        if self._cancel_buttons():
            raise AdapterConfigurationError("微信已有用户多选状态，已拒绝读取且不会自动取消")
        normal, controls, container = self._snapshot("ListItemControl")
        container_id = self._runtime_id(container)
        keys = [(row.runtime_id, row.content) for row in normal]
        current_rows = set(keys)
        if ((self._container_id is not None and container_id != self._container_id)
                or (self._previous_rows and not current_rows.intersection(self._previous_rows))):
            self.clear_sender_cache()
        if not normal:
            return []
        if self._fresh_mapping or any(key not in self._sender_cache for key in keys):
            senders = self._read_senders(normal, controls, container)
            self._bind()
            restored, _, _ = self._snapshot("ListItemControl")
            if restored != normal:
                raise AdapterConfigurationError("读取发送者后消息再次变化，整批拒绝")
            self._empty_draft()
            for row in normal:
                key = (row.runtime_id, row.content)
                sender = senders[row.runtime_id]
                if key in self._sender_cache and self._sender_cache[key] != sender:
                    raise AdapterConfigurationError("缓存发送者与多选态身份不一致，整批拒绝")
            # 全批校验结束后再写缓存；返回列表不能依赖被容量裁剪掉的旧项。
            values = {key: senders[key[0]] for key in keys}
        else:
            values = {key: self._sender_cache[key] for key in keys}
        messages = []
        for row in normal:
            key = (row.runtime_id, row.content)
            sender = values[key]
            self._sender_cache[key] = sender
            self._sender_cache.move_to_end(key)
            digest = hashlib.sha256(json.dumps([sender, row.content], ensure_ascii=False).encode("utf-8")).hexdigest()[:20]
            identity = ":".join(map(str, row.runtime_id))
            messages.append(Message(sender, row.content,
                                    message_id=f"{self.namespace}:{self.group}:{identity}:{digest}",
                                    is_self=sender in self.profile["self_names"]))
        while len(self._sender_cache) > self.CACHE_CAPACITY:
            self._sender_cache.popitem(last=False)
        self._container_id = container_id
        self._previous_rows = current_rows
        return messages
