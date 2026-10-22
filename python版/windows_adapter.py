"""Windows UI Automation 适配器：严格校验当前群、消息标识和发送回显。

控件结构通过 JSON 描述。未暴露可访问消息控件的客户端会明确拒绝启动。
不读取聊天数据库。实验性微信兼容通过独立上下文临时开启已核对构建的控件。
"""

import argparse
import hashlib
import json
import re
import sys
import time
from contextlib import contextmanager, ExitStack
from datetime import datetime
from pathlib import Path

from message_snapshot import SnapshotTracker
from wechat_forwarder import Message, RetryableSendError, SendUncertainError


class AdapterConfigurationError(ValueError):
    """客户端控件与配置不匹配，需要诊断后调整。"""


def walk(root, depth=20, include_root=False, limit=10000):
    """有深度和节点上限的前序遍历，保持消息在控件树中的顺序。"""
    stack = [(root, 0)]
    count = 0
    while stack:
        node, level = stack.pop()
        count += 1
        if count > limit:
            raise AdapterConfigurationError("控件树过大，请缩小选择器范围")
        if include_root or level:
            yield node
        if level < depth:
            stack.extend((child, level + 1) for child in reversed(node.GetChildren()))


def matches(control, selector):
    properties = {"name": "Name", "automation_id": "AutomationId",
                  "class_name": "ClassName", "type": "ControlTypeName"}
    allowed = set(properties) | {key + "_regex" for key in properties} | {"depth", "index"}
    unknown = set(selector) - allowed
    if unknown:
        raise AdapterConfigurationError(f"选择器含未知字段：{', '.join(sorted(unknown))}")
    if not any(key in selector for key in allowed - {"depth", "index"}):
        raise AdapterConfigurationError("选择器至少需要名称、类型或控件标识")
    for key, prop in properties.items():
        value = str(getattr(control, prop, "") or "")
        if key in selector and value != str(selector[key]):
            return False
        if key + "_regex" in selector and not re.fullmatch(selector[key + "_regex"], value):
            return False
    return True


def find_all(root, selector):
    depth = int(selector.get("depth", 20))
    if not 1 <= depth <= 40:
        raise AdapterConfigurationError("控件搜索深度必须为 1～40")
    return [node for node in walk(root, depth) if matches(node, selector)]


def select(root, path, label="控件"):
    steps = path if isinstance(path, list) else [path]
    if not steps:
        raise AdapterConfigurationError(f"{label}选择器为空")
    node = root
    for selector in steps:
        if not isinstance(selector, dict):
            raise AdapterConfigurationError(f"{label}选择器必须为对象或对象数组")
        found = find_all(node, selector)
        if "index" in selector:
            index = int(selector["index"])
            if index < 0 or index >= len(found):
                raise AdapterConfigurationError(f"{label}索引越界，共找到 {len(found)} 项")
            node = found[index]
        elif len(found) != 1:
            raise AdapterConfigurationError(f"{label}需要唯一匹配，实际找到 {len(found)} 项；请导出窗口诊断")
        else:
            node = found[0]
    return node


def _format(value, **values):
    if isinstance(value, str):
        for key, replacement in values.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, list):
        return [_format(item, **values) for item in value]
    if isinstance(value, dict):
        return {key: _format(item, **values) for key, item in value.items()}
    return value


def _text(control):
    """优先读取文本叶子，避免父子 Name 重复拼接。"""
    leaves = [str(n.Name) for n in walk(control, 12, include_root=True)
              if n.ControlTypeName == "TextControl" and n.Name]
    return "\n".join(leaves) if leaves else str(control.Name or "")


def _normalized(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def literal_keys(text):
    """SendKeys 花括号必须转义，消息内容不能解释为快捷键。"""
    return "".join("{{}" if char == "{" else "{}}" if char == "}" else char for char in text)


_TIME = re.compile(r"(?:(?:今天|昨天|\d{1,2}月\d{1,2}日)\s*)?\d{1,2}:\d{2}(?::\d{2})?")


def parse_message(row, profile, namespace):
    parser = profile.get("parser", "selectors")
    if parser == "yto":
        # 客户管家 2.0 的消息行：可选日期、头像组、发送者、时间、正文组。
        children = row.GetChildren()
        names = [n.Name for n in children if n.ControlTypeName == "TextControl" and n.Name and not _TIME.fullmatch(n.Name.strip())]
        groups = [n for n in children if n.ControlTypeName == "GroupControl"]
        if len(names) != 1 or not groups:
            raise AdapterConfigurationError("客户管家消息行结构发生变化，无法唯一识别发送者")
        sender, body = names[0], groups[-1]
        content = _text(body)
        # 卡片中的摘要可能截断，不能当作机器人完整文本回复搬运。
        kind = "media" if any(n.ControlTypeName in {"ImageControl", "ButtonControl"}
                              for n in walk(body, 12)) else "text"
    elif parser == "selectors":
        sender = _text(select(row, profile["sender"], "消息发送者"))
        body = select(row, profile["content"], "消息正文") if profile.get("content") else row
        content = _text(body) if profile.get("content") else str(row.Name or "")
        kind = "media" if profile.get("media") and find_all(row, profile["media"]) else "text"
    else:
        raise AdapterConfigurationError(f"未知消息解析器：{parser}")
    if not sender.strip():
        raise AdapterConfigurationError("消息发送者为空，不能判断来源")
    if profile.get("id_attribute", "runtime_id") == "automation_id":
        event_id = str(row.AutomationId or "")
    else:
        runtime_id = row.GetRuntimeId()
        event_id = ":".join(map(str, runtime_id)) if runtime_id else ""
    if not event_id:
        raise AdapterConfigurationError("消息行未提供稳定标识")
    # 同一虚拟化控件被复用且内容变化时形成新事件；同文不同控件仍是不同消息。
    digest = hashlib.sha256((sender + "\0" + content).encode("utf-8")).hexdigest()[:20]
    return Message(sender.strip(), content, message_id=f"{namespace}:{event_id}:{digest}",
                   is_self=sender.strip() in profile["self_names"], kind=kind)


def load_profile(path):
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        profile = json.load(stream)
    if not isinstance(profile, dict):
        raise AdapterConfigurationError("适配配置顶层必须为对象")
    for side in ("wx", "kf"):
        item = profile.get(side)
        if not isinstance(item, dict):
            raise AdapterConfigurationError(f"适配配置缺少 {side} 对象")
        for key in ("window", "group_title", "messages", "message_item", "input", "self_names"):
            if not item.get(key):
                raise AdapterConfigurationError(f"{side} 缺少 {key} 配置")
        if not isinstance(item["self_names"], list) or not all(isinstance(s, str) and s.strip() for s in item["self_names"]):
            raise AdapterConfigurationError(f"{side}.self_names 必须填写本账号的完整群昵称列表")
        if item.get("parser", "selectors") == "selectors" and not item.get("sender"):
            raise AdapterConfigurationError(f"{side} 缺少 sender 选择器")
        if item.get("id_attribute", "runtime_id") not in {"runtime_id", "automation_id"}:
            raise AdapterConfigurationError(f"{side}.id_attribute 无效")
    return profile


class WindowsChatAdapter:
    """同一对象兼任来源和发送端，保证 COM 控件在创建它的线程中使用。"""

    def __init__(self, auto, profile, group, namespace, log=print, expected_pid=None):
        self.auto = auto
        self.profile = _format(profile, group=group)
        self.group = group
        self.namespace = namespace
        self.log = log
        self.tracker = SnapshotTracker(log)
        self.window = None
        self._process_id = None
        self._expected_pid = expected_pid

    def _bind(self):
        self.window = select(self.auto.GetRootControl(), self.profile["window"], f"{self.namespace}窗口")
        pid = getattr(self.window, "ProcessId", None)
        if self._expected_pid is not None and pid != self._expected_pid:
            raise AdapterConfigurationError(f"{self.namespace}进程已变化，请停止并重新启动转发会话")
        if pid is not None and self._expected_pid is None:
            self._expected_pid = pid
        if self.profile.get("process_name") and self.window.ProcessId != self._process_id:
            process = next((p for p in self.auto.GetProcesses(False) if p.pid == self.window.ProcessId), None)
            if process is None or process.exeName.lower() != self.profile["process_name"].lower():
                raise AdapterConfigurationError("窗口所属进程与适配配置不符")
            self._process_id = self.window.ProcessId
        title = select(self.window, self.profile["group_title"], "当前会话标题")
        if str(title.Name or "").strip() != self.group:
            raise RetryableSendError(f"{self.namespace}当前会话不是配置的群，请手动打开目标群：{self.group}")
        return self.window

    def _rows(self):
        window = self._bind()
        container = select(window, self.profile["messages"], "消息列表")
        return [parse_message(row, self.profile, self.namespace)
                for row in find_all(container, self.profile["message_item"])]

    def prepare(self):
        window = self._bind()
        edit = select(window, self.profile["input"], "聊天输入框")
        self._value(edit)
        self.tracker.update(self._rows())
        self.log(f"[适配] {self.namespace} 已核对目标群并建立历史快照")

    def fetch_new(self):
        return self.tracker.update(self._rows())

    def fetch_replies(self, bot_name):
        return [msg for msg in self.fetch_new() if msg.sender == bot_name]

    def _value(self, edit):
        pattern = edit.GetPattern(self.auto.PatternId.ValuePattern)
        if pattern:
            return _normalized(str(pattern.Value or ""))
        pattern = edit.GetPattern(self.auto.PatternId.TextPattern)
        if pattern:
            value = pattern.DocumentRange.GetText(-1)
            if "\ufffc" in value:
                raise AdapterConfigurationError("输入框包含 UIA 嵌入对象占位符，无法确认完整草稿；请核对输入控件适配")
            # 个别控件会追加结构性的段落结束符，必须经诊断确认后显式配置。
            suffix = self.profile.get("text_pattern_suffix", "")
            if suffix and value.endswith(suffix):
                value = value[:-len(suffix)]
            return _normalized(value)
        raise AdapterConfigurationError("输入框不支持读取完整草稿，无法校验输入结果")

    def _focus(self, edit):
        self.window.SetActive(waitTime=0.1)
        if not edit.SetFocus():
            raise RetryableSendError("无法聚焦聊天输入框")
        focused = self.auto.GetFocusedControl()
        if not focused or list(focused.GetRuntimeId()) != list(edit.GetRuntimeId()):
            raise RetryableSendError("聊天输入框没有获得焦点")

    def _check_focus(self, edit):
        self._bind()
        focused = self.auto.GetFocusedControl()
        if not focused or list(focused.GetRuntimeId()) != list(edit.GetRuntimeId()):
            raise RetryableSendError("发送前焦点已变化，请停止操作客户端后重试")

    def _type(self, edit, text):
        # 每段单独输入，换行使用明确配置的换行键，不能把正文当发送快捷键。
        lines = _normalized(text).split("\n")
        newline = self.profile.get("newline_keys", "{Shift}{Enter}")
        if newline not in {"{Shift}{Enter}", "{Ctrl}{Enter}"}:
            raise AdapterConfigurationError("newline_keys 只允许 Shift+Enter 或 Ctrl+Enter")
        for i, line in enumerate(lines):
            self._check_focus(edit)
            if i:
                self.auto.SendKeys(newline, waitTime=0.05)
            if line:
                self.auto.SendKeys(literal_keys(line), interval=0.002, waitTime=0.05)

    def _enter(self, edit):
        self._check_focus(edit)
        key = self.profile.get("send_keys", "{Enter}")
        if key not in {"{Enter}", "{Ctrl}{Enter}"}:
            raise AdapterConfigurationError("send_keys 只允许 Enter 或 Ctrl+Enter")
        self.auto.SendKeys(key, waitTime=0.1)

    def _send(self, text, staff=None):
        attempted = False
        edited = False
        try:
            if not isinstance(text, str) or not text:
                raise AdapterConfigurationError("发送文本不能为空")
            if self.profile.get("newline_keys", "{Shift}{Enter}") == self.profile.get("send_keys", "{Enter}"):
                raise AdapterConfigurationError("换行键与发送键不能相同，请核对客户端设置")
            window = self._bind()
            edit = select(window, self.profile["input"], "聊天输入框")
            if self._value(edit):
                raise RetryableSendError("目标输入框已有草稿，请先处理草稿；程序不会覆盖")
            before_rows = self._rows()
            if not before_rows:
                raise AdapterConfigurationError("当前消息列表为空，缺少可验证发送回显的历史锚点")
            before = {msg.message_id for msg in before_rows}
            anchor = before_rows[-1].message_id
            self._focus(edit)
            expected = _normalized(text)
            if staff:
                if not self.profile.get("mention_item") or not self.profile.get("mention_token"):
                    raise AdapterConfigurationError("尚未配置真实 @ 的候选控件和选中标记，不能以普通文本代替 @")
                edited = True
                self._type(edit, "@" + staff)
                candidate = select(self.window, _format(self.profile["mention_item"], staff=staff), "@客服候选项")
                if str(candidate.Name or "").strip() != staff:
                    raise AdapterConfigurationError("@候选项不是配置的客服昵称")
                self._bind()
                candidate.Click(waitTime=0.1)
                select(self.window, _format(self.profile["mention_token"], staff=staff), "已选中的真实@标记")
                self._focus(edit)
                prefix = self._value(edit)
                if "@" + staff not in prefix:
                    raise AdapterConfigurationError("@客服标记未进入输入框")
                self.auto.SendKeys("{Ctrl}{End}", waitTime=0.05)
                self._type(edit, " " + text)
                expected = _normalized(prefix + " " + text)
            else:
                pattern = edit.GetPattern(self.auto.PatternId.ValuePattern)
                edited = True
                if pattern and not pattern.IsReadOnly:
                    pattern.SetValue(text)
                else:
                    self._type(edit, text)
            if self._value(edit) != expected:
                raise AdapterConfigurationError("输入结果与原文不同，已保留草稿，未提交发送")
            self._check_focus(edit)
            # 自此即使按键 API 抛异常，也不能证明消息未发出。
            attempted = True
            self._enter(edit)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                after = self._rows()
                anchors = [i for i, row in enumerate(after) if row.message_id == anchor]
                # 控件整体重建后同文历史可能获得新ID；没有原尾锚点就不能证明追加。
                if len(anchors) == 1 and not self._value(edit):
                    for row in after[anchors[0] + 1:]:
                        if row.message_id not in before and row.is_self and _normalized(row.content) == expected:
                            return
                time.sleep(0.15)
            raise SendUncertainError("发送后未找到本账号的完整消息回显，请人工核对；已停止，避免重复发送")
        except SendUncertainError:
            raise
        except Exception as exc:
            if attempted:
                raise SendUncertainError(f"发送结果不确定，请人工核对：{exc}") from exc
            if edited:
                # 草稿可能包含部分文本或 @ 标记，停止让用户检查，绝不自动清空。
                raise AdapterConfigurationError(f"输入未完成，未执行发送；请检查保留的草稿：{exc}") from exc
            raise

    def send(self, text):
        self._send(text)

    def send_mention(self, staff, text):
        self._send(text, staff=staff)


def _automation():
    if sys.platform != "win32":
        raise AdapterConfigurationError("真实窗口对接只支持 Windows")
    try:
        import uiautomation as auto
    except ImportError as exc:
        raise AdapterConfigurationError("缺少真实模式依赖，请先安装 requirements.txt") from exc
    return auto


def _wechat_session(pid, log):
    from wechat_accessibility import WechatAccessibilitySession
    return WechatAccessibilitySession(pid, enable=True, log=log)


@contextmanager
def create_live_adapters(cfg, log=print):
    profile_path = cfg.get("adapter_profile", "")
    if not profile_path:
        raise AdapterConfigurationError("请先选择客户端适配 JSON 文件")
    profile = load_profile(profile_path)
    if cfg.get("at_keywords") and not all(profile["wx"].get(key) for key in ("mention_item", "mention_token")):
        raise AdapterConfigurationError("设置了 @客服词，但微信适配配置尚未提供真实 @ 控件")
    auto = _automation()
    if not isinstance(cfg.get("wechat_accessibility", False), bool):
        raise AdapterConfigurationError("微信兼容开关必须为布尔值")
    with auto.UIAutomationInitializerInThread(), ExitStack() as contexts:
        wx_window = select(auto.GetRootControl(), profile["wx"]["window"], "微信窗口")
        pid = wx_window.ProcessId
        if cfg.get("wechat_accessibility", False):
            contexts.enter_context(_wechat_session(pid, log))
        wx = WindowsChatAdapter(auto, profile["wx"], cfg["wx_group"], "微信", log, expected_pid=pid)
        kf = WindowsChatAdapter(auto, profile["kf"], cfg["kf_group"], "客户管家", log)
        wx.prepare()
        kf.prepare()
        yield wx, wx, kf, kf


def diagnose(output_dir, log=print, enable_wechat=False, stop_event=None):
    """读取目标窗口；可选临时启用微信控件，完成或中止时恢复，不发送消息。"""
    if not isinstance(enable_wechat, bool):
        raise ValueError("微信兼容开关必须为布尔值")
    auto = _automation()
    output = Path(output_dir).resolve()
    project = Path(__file__).resolve().parent.parent
    if output != project and project not in output.parents:
        raise ValueError("诊断文件必须保存在当前项目目录中")
    output.mkdir(parents=True, exist_ok=True)
    report = {"time": datetime.now().isoformat(timespec="seconds"), "windows": []}
    with auto.UIAutomationInitializerInThread(), ExitStack() as contexts:
        if stop_event is not None and stop_event.is_set():
            return None
        if enable_wechat:
            wx_window = select(auto.GetRootControl(), {"name": "微信", "depth": 1}, "微信窗口")
            contexts.enter_context(_wechat_session(wx_window.ProcessId, log))
        for window in auto.GetRootControl().GetChildren():
            if stop_event is not None and stop_event.is_set():
                report["cancelled"] = True
                break
            if window.Name not in {"微信", "WeChat", "客户管家"}:
                continue
            nodes = []
            try:
                stack = [(window, 0)]
                while stack and len(nodes) < 3000:
                    if stop_event is not None and stop_event.is_set():
                        report["cancelled"] = True
                        break
                    node, depth = stack.pop()
                    record = {"depth": depth, "type": node.ControlTypeName,
                              "name": node.Name, "automation_id": node.AutomationId,
                              "class_name": node.ClassName}
                    try:
                        rect = node.BoundingRectangle
                        record.update(runtime_id=list(node.GetRuntimeId()),
                                      rectangle=[rect.left, rect.top, rect.right, rect.bottom],
                                      help_text=node.HelpText,
                                      full_description=node.GetPropertyValue(auto.PropertyId.FullDescriptionProperty))
                        legacy = node.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
                        if legacy:
                            record["legacy_description"] = legacy.Description
                            record["legacy_value"] = legacy.Value
                        if node.AutomationId in {"chat_input_field", "edit-content"} or node.ControlTypeName == "ListItemControl":
                            value = node.GetPattern(auto.PatternId.ValuePattern)
                            text = node.GetPattern(auto.PatternId.TextPattern)
                            record["value_pattern"] = value.Value if value else None
                            record["text_pattern"] = text.DocumentRange.GetText(-1) if text else None
                            if text:
                                record["text_rectangles"] = [[r.left, r.top, r.right, r.bottom]
                                                             for r in text.DocumentRange.GetBoundingRectangles()]
                    except Exception as detail_error:
                        record["detail_error"] = str(detail_error)
                    nodes.append(record)
                    if depth < 40:
                        stack.extend((child, depth + 1) for child in reversed(node.GetChildren()))
                report["windows"].append({"title": window.Name, "nodes": nodes, "truncated": bool(stack)})
            except Exception as exc:
                report["windows"].append({"title": window.Name, "nodes": nodes, "error": str(exc)})
    path = output / ("windows-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[诊断] 已保存 {path}（可能含当前聊天内容，仅保存在本地）")
    if not report["windows"]:
        log("[诊断] 未找到微信或客户管家窗口，请先登录并打开目标群")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="窗口诊断（默认不启用实验性兼容）")
    parser.add_argument("--diagnose", action="store_true", help="导出微信和客户管家控件树")
    parser.add_argument("--enable-wechat", action="store_true", help="临时启用已验证微信构建的控件，诊断后恢复")
    args = parser.parse_args()
    if args.diagnose:
        try:
            diagnose(Path(__file__).resolve().parent.parent / "diagnostics", enable_wechat=args.enable_wechat)
        except Exception as error:
            print(f"诊断失败：{error}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
