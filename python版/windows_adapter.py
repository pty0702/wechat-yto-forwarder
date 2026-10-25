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


# 发送回显确认窗口：需覆盖消息渲染与微信多选往返读取身份的耗时。
ECHO_TIMEOUT_SECONDS = 12.0


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
        if key not in selector and key + "_regex" not in selector:
            continue
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


def select_retry(root, path, label="控件", attempts=11):
    """兼容开启后控件树可能稍晚才就绪；只重试匹配，不放宽选择器。"""
    for attempt in range(attempts):
        try:
            return select(root, path, label)
        except AdapterConfigurationError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


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


def _time_only(text):
    """整段文本只有空白与时间戳时，不是聊天消息行。"""
    lines = [line.strip() for line in (text or "").splitlines()]
    return all(not line or _TIME.fullmatch(line) for line in lines)


def parse_message(row, profile, namespace):
    """解析一条消息；返回 None 表示该行不是消息（时间分割等），调用方应跳过。"""
    parser = profile.get("parser", "selectors")
    if parser == "yto":
        # 客户管家 WebView2 布局：消息编号外壳内的 news-box 分为发送者行和正文。
        boxes = [node for node in walk(row, 10)
                 if node.ControlTypeName == "GroupControl" and "news-box" in str(node.ClassName).split()]
        if boxes:
            if len(boxes) != 1:
                raise AdapterConfigurationError("客户管家消息内有多个正文容器，无法确认来源")
            sections = boxes[0].GetChildren()
            bodies = [node for node in sections if "text-content" in str(node.ClassName).split()]
            headers = [node for node in sections if node not in bodies and any(
                child.ControlTypeName == "TextControl" for child in node.GetChildren())]
            if len(bodies) != 1 or len(headers) != 1:
                raise AdapterConfigurationError("客户管家发送者或正文分区不唯一")
            children = headers[0].GetChildren()
            body = bodies[0]
        else:
            # 保留旧的直接子控件布局，兼容已验证配置。
            children = row.GetChildren()
            groups = [n for n in children if n.ControlTypeName == "GroupControl"]
            body = groups[-1] if groups else None
        names = [n.Name.strip() for n in children
                 if n.ControlTypeName == "TextControl" and str(n.Name or "").strip()
                 and not _TIME.fullmatch(str(n.Name).strip())]
        if body is None:
            # 无正文容器时整行可能只是时间分割或系统提示，不应中断消息读取。
            if _time_only(_text(row)):
                return None
            raise AdapterConfigurationError("客户管家消息行结构发生变化，无法唯一识别发送者")
        if len(names) > 1:
            raise AdapterConfigurationError("客户管家消息行结构发生变化，无法唯一识别发送者")
        content = _text(body)
        if len(names) == 1:
            sender = names[0]
        elif not content.strip() or _time_only(content):
            return None
        else:
            # 本人气泡可能不显示昵称；仅当本账号昵称唯一配置时才可判定来源。
            self_names = [str(name).strip() for name in profile.get("self_names", [])
                          if str(name).strip()]
            if len(self_names) == 1:
                sender = self_names[0]
            else:
                raise AdapterConfigurationError("客户管家消息行结构发生变化，无法唯一识别发送者")
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
        parser = item.get("parser", "selectors")
        if parser not in {"selectors", "yto", "yto_dom", "wechat4", "wechat_db"}:
            raise AdapterConfigurationError(f"{side} 消息解析器不受支持")
        if parser == "wechat4" and side != "wx":
            raise AdapterConfigurationError("wechat4 解析器只能用于微信端")
        if parser == "wechat_db" and side != "wx":
            raise AdapterConfigurationError("wechat_db 解析器只能用于微信端")
        if parser == "yto_dom" and side != "kf":
            raise AdapterConfigurationError("yto_dom 解析器只能用于客户管家端")
        if parser == "wechat_db":
            # 数据库通道不读 UIA 树，只需本账号身份用于排除本人消息。
            if not item.get("self_names"):
                raise AdapterConfigurationError(f"{side} 缺少 self_names 配置")
        elif parser == "yto_dom":
            if not item.get("self_names"):
                raise AdapterConfigurationError(f"{side} 缺少 self_names 配置")
        else:
            for key in ("window", "group_title", "messages", "message_item", "input", "self_names"):
                if not item.get(key):
                    raise AdapterConfigurationError(f"{side} 缺少 {key} 配置")
        if not isinstance(item["self_names"], list) or not all(isinstance(s, str) and s.strip() for s in item["self_names"]):
            raise AdapterConfigurationError(f"{side}.self_names 必须填写本账号的完整群昵称列表")
        if item.get("parser", "selectors") == "selectors" and not item.get("sender"):
            raise AdapterConfigurationError(f"{side} 缺少 sender 选择器")
        mention_mode = item.get("mention_mode")
        if mention_mode is not None and (mention_mode != "wechat4" or side != "wx"
                                         or item.get("parser") != "wechat4"):
            raise AdapterConfigurationError("mention_mode 仅支持微信端 wechat4 解析器的 wechat4 模式")
        if not isinstance(item.get("mention_allow_duplicate_names", False), bool):
            raise AdapterConfigurationError("mention_allow_duplicate_names 必须为布尔值")
        if item.get("id_attribute", "runtime_id") not in {"runtime_id", "automation_id"}:
            raise AdapterConfigurationError(f"{side}.id_attribute 无效")
        empty_leaf = item.setdefault("text_pattern_empty_leaf", False)
        if not isinstance(empty_leaf, bool):
            raise AdapterConfigurationError(f"{side}.text_pattern_empty_leaf 必须为布尔值")
        if empty_leaf and (item.get("parser") != "yto" or item.get("process_name") != "YtoChatApp.exe"):
            raise AdapterConfigurationError(f"{side}.text_pattern_empty_leaf 仅允许已核对的客户管家 yto 配置")
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
        title = select_retry(self.window, self.profile["group_title"], "当前会话标题")
        if str(title.Name or "").strip() != self.group:
            raise RetryableSendError(f"{self.namespace}当前会话不是配置的群，请手动打开目标群：{self.group}")
        return self.window

    def _rows(self):
        window = self._bind()
        container = select(window, self.profile["messages"], "消息列表")
        rows = []
        for row in find_all(container, self.profile["message_item"]):
            parsed = parse_message(row, self.profile, self.namespace)
            if parsed is not None:
                rows.append(parsed)
        return rows

    def prepare(self):
        window = self._bind()
        edit = select_retry(window, self.profile["input"], "聊天输入框")
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
                # 已核对的客户管家空叶以单个替代字符表示空输入；不适用于其它嵌入对象。
                if (self.profile.get("text_pattern_empty_leaf", False) is True
                        and self.profile.get("parser") == "yto"
                        and self.profile.get("process_name") == "YtoChatApp.exe"
                        and value == "\ufffc"
                        and edit.ControlTypeName == "GroupControl"
                        and edit.AutomationId == "edit-content"
                        and edit.GetChildren() == []):
                    return ""
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

    def _compose_mention(self, edit, staff, text):
        """返回完整草稿及预期发送回显，富文本客户端可分别校验两者。"""
        if not self.profile.get("mention_item") or not self.profile.get("mention_token"):
            raise AdapterConfigurationError("尚未配置真实 @ 的候选控件和选中标记，不能以普通文本代替 @")
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
        return expected, expected

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
            if self._value(edit):
                raise RetryableSendError("读取消息期间输入框出现新草稿，已停止本次输入，不会覆盖")
            expected = _normalized(text)
            echo_expected = expected
            if staff:
                edited = True
                expected, echo_expected = self._compose_mention(edit, staff, text)
            else:
                pattern = edit.GetPattern(self.auto.PatternId.ValuePattern)
                edited = True
                if pattern and not pattern.IsReadOnly:
                    pattern.SetValue(text)
                else:
                    self._type(edit, text)
            draft = self._value(edit)
            if draft != expected:
                # UIA 可能稍晚才暴露完整输入；只等待回读，不补字、覆盖或清理草稿。
                draft_deadline = time.monotonic() + 2
                while draft != expected:
                    self._check_focus(edit)
                    remaining = draft_deadline - time.monotonic()
                    if remaining <= 0:
                        raise AdapterConfigurationError("输入结果与原文不同，已保留草稿，未提交发送")
                    time.sleep(min(0.05, remaining))
                    self._check_focus(edit)
                    # 占位符、失效控件等读取异常立即向外传播，不能作为暂时不匹配忽略。
                    draft = self._value(edit)
            self._check_focus(edit)
            # 自此即使按键 API 抛异常，也不能证明消息未发出。
            attempted = True
            self._enter(edit)
            deadline = time.monotonic() + ECHO_TIMEOUT_SECONDS
            last_scan = {"rows": 0, "anchor": False, "self_new": 0, "body_match": 0}
            while time.monotonic() < deadline:
                # 发送后客户端可能稍晚才清空草稿；先等清空，避免带草稿进入微信多选。
                if self._value(edit):
                    time.sleep(0.15)
                    continue
                after = self._rows()
                anchors = [i for i, row in enumerate(after) if row.message_id == anchor]
                # 控件整体重建后同文历史可能获得新ID；没有原尾锚点就不能证明追加。
                scan = {"rows": len(after), "anchor": len(anchors) == 1, "self_new": 0, "body_match": 0}
                if scan["anchor"] and not self._value(edit):
                    for row in after[anchors[0] + 1:]:
                        if row.message_id in before:
                            continue
                        if row.is_self:
                            scan["self_new"] += 1
                        if _normalized(row.content) == echo_expected:
                            scan["body_match"] += 1
                            if row.is_self:
                                return
                last_scan = scan
                time.sleep(0.15)
            # 只报告计数与结构原因，不把消息正文写入错误或日志。
            if not last_scan["anchor"]:
                reason = "发送前尾锚点在发送后未命中，可能列表重建"
            elif not last_scan["self_new"]:
                reason = "锚点后没有识别为本人的新消息"
            elif not last_scan["body_match"]:
                reason = "本人新消息与预期回显正文不一致"
            else:
                reason = "回显未同时满足本人身份与完整正文"
            raise SendUncertainError(
                f"发送后未找到本账号的完整消息回显（{reason}；"
                f"新消息 {last_scan['rows']} 条，本人新消息 {last_scan['self_new']} 条，"
                f"正文一致 {last_scan['body_match']} 条），请人工核对；已停止，避免重复发送"
            )
        except SendUncertainError:
            raise
        except Exception as exc:
            if attempted:
                raise SendUncertainError(f"发送结果不确定，请人工核对：{exc}") from exc
            if edited:
                # 草稿可能包含部分文本或 @ 标记，停止让用户检查，绝不自动清空。
                raise AdapterConfigurationError(f"输入未完成，未执行发送；请检查保留的草稿：{exc}") from exc
            raise

    def send(self, text, group: str = ""):
        self._check_target_group(group)
        self._send(text)

    def send_mention(self, staff, text, group: str = ""):
        self._check_target_group(group)
        self._send(text, staff=staff)

    def _check_target_group(self, group: str):
        """本适配器按 profile 绑定单一群；转发引擎带群参数调用时必须核对。

        多群配置（wx_groups 多项）走本路线时，非默认群的定向发送无法
        保障落点，直接拒绝而不是发错群。
        """
        if group and group.strip() != (self.group or "").strip():
            raise RetryableSendError(
                f"selectors/wechat4 适配器只支持单群，拒绝发往：{group}")


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
def create_live_adapters(cfg, log=print, need=None):
    """装配真实双端适配器；need 指定本次要准备的端（默认两端都准备）。

    单条发送验收只传发送端，避免为发一条消息强制核对另一端界面。
    """
    if need is None:
        need = ("wx", "kf")
    if not need or any(side not in ("wx", "kf") for side in need):
        raise AdapterConfigurationError("need 只能包含 wx 或 kf")
    profile_path = cfg.get("adapter_profile", "")
    if not profile_path:
        raise AdapterConfigurationError("请先选择客户端适配 JSON 文件")
    profile = load_profile(profile_path)
    wx_mention = (profile["wx"].get("parser") == "wechat4"
                  and profile["wx"].get("mention_mode") == "wechat4")
    if (cfg.get("at_keywords") and not wx_mention
            and not all(profile["wx"].get(key) for key in ("mention_item", "mention_token"))
            and profile["wx"].get("parser") != "wechat_db"):
        raise AdapterConfigurationError("设置了 @客服词，但微信适配配置尚未提供真实 @ 控件")
    auto = _automation()
    if not isinstance(cfg.get("wechat_accessibility", False), bool):
        raise AdapterConfigurationError("微信兼容开关必须为布尔值")
    with auto.UIAutomationInitializerInThread(), ExitStack() as contexts:
        wx = None
        kf = None
        if "wx" in need and profile["wx"].get("parser") == "wechat_db":
            # 读走微信本地库、发走坐标/OCR：不绑定 UIA 窗口，也不需要无障碍兼容。
            from wechat_db_adapter import WechatDbAdapter
            groups = cfg.get("wx_groups") or [cfg["wx_group"]]
            wx = WechatDbAdapter(profile["wx"], groups, log)
            wx.prepare()
        elif "wx" in need:
            wx_window = select(auto.GetRootControl(), profile["wx"]["window"], "微信窗口")
            pid = wx_window.ProcessId
            if cfg.get("wechat_accessibility", False):
                contexts.enter_context(_wechat_session(pid, log))
                # 等 UIA 树暴露 mmui 控件，避免刚开启就取标题/输入框落空。
                time.sleep(0.5)
            wx_class = WindowsChatAdapter
            if profile["wx"].get("parser") == "wechat4":
                from wechat4_adapter import Wechat4Adapter
                wx_class = Wechat4Adapter
            wx = wx_class(auto, profile["wx"], cfg["wx_group"], "微信", log, expected_pid=pid)
            wx.prepare()
        if "kf" in need:
            if profile["kf"].get("parser") == "yto_dom":
                from kf_dom_adapter import KfDomAdapter
                kf = KfDomAdapter(profile["kf"], cfg["kf_group"], log)
            else:
                kf = WindowsChatAdapter(auto, profile["kf"], cfg["kf_group"], "客户管家", log)
            kf.prepare()
        yield wx, wx, kf, kf


def _diagnostic_read(record, key, read):
    """各字段独立读取，某个可选 UIA 属性失败不遮蔽其它证据。"""
    try:
        record[key] = read()
    except Exception as exc:
        record[key + "_error"] = f"{type(exc).__name__}: {exc}"


def _diagnostic_control(node):
    """身份摘要不取 Name，避免父级或焦点控件名称夹带聊天正文。"""
    if node is None:
        return None
    record = {}
    for key, attribute in (("type", "ControlTypeName"), ("automation_id", "AutomationId"),
                           ("process_id", "ProcessId"), ("native_window_handle", "NativeWindowHandle")):
        _diagnostic_read(record, key, lambda attribute=attribute: getattr(node, attribute))
    _diagnostic_read(record, "runtime_id", lambda: list(node.GetRuntimeId()))
    return record


def _diagnostic_parents(node):
    parents = []
    current = node
    for _ in range(8):
        try:
            current = current.GetParentControl()
        except Exception as exc:
            parents.append({"parent_error": f"{type(exc).__name__}: {exc}"})
            break
        if current is None:
            break
        parents.append(_diagnostic_control(current))
    return parents


def _diagnostic_rectangles(text_range):
    return [[r.left, r.top, r.right, r.bottom] for r in text_range.GetBoundingRectangles()]


def _diagnostic_compare(text_range, source, target_range, target):
    # uiautomation 2.0.29 的 CompareEndpoints 错把包装对象传给 COM；仅诊断绕过该包装。
    return text_range.textRange.CompareEndpoints(source, target_range.textRange, target)


def _diagnostic_range(auto, text_range, document_range):
    record = {}
    _diagnostic_read(record, "enclosing_element", lambda: _diagnostic_control(text_range.GetEnclosingControl()))
    _diagnostic_read(record, "rectangles", lambda: _diagnostic_rectangles(text_range))
    endpoints = auto.TextPatternRangeEndpoint
    comparisons = {}
    if document_range is not None:
        for source_name, source in (("start", endpoints.Start), ("end", endpoints.End)):
            for target_name, target in (("start", endpoints.Start), ("end", endpoints.End)):
                _diagnostic_read(comparisons, source_name + "_to_document_" + target_name,
                                 lambda source=source, target=target:
                                 _diagnostic_compare(text_range, source, document_range, target))
        record["compare_to_document"] = comparisons
    _diagnostic_read(record, "is_degenerate", lambda:
                     _diagnostic_compare(text_range, endpoints.Start, text_range, endpoints.End) == 0)
    start = comparisons.get("start_to_document_start")
    end = comparisons.get("end_to_document_end")
    inside = start is not None and end is not None and start >= 0 and end <= 0
    record["inside_document"] = inside
    # Chromium 的 GetSelection 可能给出同一网页其它区域的选区；全文仅存本输入框范围。
    try:
        text = text_range.GetText(-1)
        record["text_length"] = len(text)
        if inside:
            record["text"] = text
    except Exception as exc:
        record["text_error"] = f"{type(exc).__name__}: {exc}"
    return record


def _diagnostic_caret(auto, node, document_range):
    record = {"raw_com": True, "supported": None}
    try:
        # 当前 uiautomation 的 TextPattern2 包装没有 GetCaretRange，直接访问类型化 COM。
        raw = node.Element.GetCurrentPattern(auto.PatternId.TextPattern2)
        record["supported"] = bool(raw)
        if not raw:
            return record
        pattern = raw.QueryInterface(auto.GetPatternIdInterface(auto.PatternId.TextPattern2))
        active, caret = pattern.GetCaretRange()
        record["is_active"] = bool(active)
        record["range"] = (_diagnostic_range(auto, auto.TextRange(textRange=caret), document_range)
                           if caret else None)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def _diagnostic_input(auto, node, record, text):
    _diagnostic_read(record, "is_keyboard_focusable", lambda: node.IsKeyboardFocusable)
    _diagnostic_read(record, "has_keyboard_focus", lambda: node.HasKeyboardFocus)
    _diagnostic_read(record, "parent_chain", lambda: _diagnostic_parents(node))
    document = None
    if text is not None:
        try:
            document = text.DocumentRange
        except Exception as exc:
            record["document_range_error"] = f"{type(exc).__name__}: {exc}"
        _diagnostic_read(record, "selection", lambda: [
            _diagnostic_range(auto, selected, document) for selected in text.GetSelection()
        ])
    else:
        record["selection"] = None
    _diagnostic_read(record, "text_pattern2_caret", lambda: _diagnostic_caret(auto, node, document))


def diagnose(output_dir, log=print, enable_wechat=False, stop_event=None, capture_delay=0):
    """读取目标窗口；可选临时启用微信控件，完成或中止时恢复，不发送消息。"""
    if not isinstance(enable_wechat, bool):
        raise ValueError("微信兼容开关必须为布尔值")
    if isinstance(capture_delay, bool) or not isinstance(capture_delay, (int, float)) or not 0 <= capture_delay < float("inf"):
        raise ValueError("诊断延迟必须为有限非负秒数")
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
        if capture_delay:
            log(f"[诊断] {capture_delay:g}秒后读取，请将焦点置于待检查输入框；不会键入或发送")
            if stop_event is not None:
                if stop_event.wait(capture_delay):
                    return None
            else:
                time.sleep(capture_delay)
        report["capture_time"] = datetime.now().isoformat(timespec="seconds")
        if enable_wechat:
            wx_window = select(auto.GetRootControl(), {"name": "微信", "depth": 1}, "微信窗口")
            contexts.enter_context(_wechat_session(wx_window.ProcessId, log))
        _diagnostic_read(report, "focused_control", lambda: _diagnostic_control(auto.GetFocusedControl()))
        _diagnostic_read(report, "foreground_control", lambda: _diagnostic_control(auto.GetForegroundControl()))
        root_windows = auto.GetRootControl().GetChildren()
        target_pids = {window.ProcessId for window in root_windows
                       if window.Name in {"微信", "WeChat", "客户管家"}}
        for window in root_windows:
            if stop_event is not None and stop_event.is_set():
                report["cancelled"] = True
                break
            # 微信提及候选是独立同进程弹窗，不能只按主窗口标题筛选。
            if window.ProcessId not in target_pids:
                continue
            nodes = []
            try:
                stack = [(window, 0)]
                while stack and len(nodes) < 3000:
                    if stop_event is not None and stop_event.is_set():
                        report["cancelled"] = True
                        break
                    node, depth = stack.pop()
                    record = {"depth": depth}
                    for key, attribute in (("type", "ControlTypeName"), ("name", "Name"),
                                           ("automation_id", "AutomationId"), ("class_name", "ClassName")):
                        _diagnostic_read(record, key, lambda attribute=attribute: getattr(node, attribute))
                    _diagnostic_read(record, "runtime_id", lambda: list(node.GetRuntimeId()))
                    _diagnostic_read(record, "rectangle", lambda: [
                        node.BoundingRectangle.left, node.BoundingRectangle.top,
                        node.BoundingRectangle.right, node.BoundingRectangle.bottom])
                    _diagnostic_read(record, "help_text", lambda: node.HelpText)
                    _diagnostic_read(record, "full_description", lambda:
                                     node.GetPropertyValue(auto.PropertyId.FullDescriptionProperty))
                    try:
                        legacy = node.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
                        if legacy:
                            _diagnostic_read(record, "legacy_description", lambda: legacy.Description)
                            _diagnostic_read(record, "legacy_value", lambda: legacy.Value)
                    except Exception as exc:
                        record["legacy_pattern_error"] = str(exc)
                    is_input = record.get("automation_id") in {"chat_input_field", "edit-content"}
                    if is_input or record.get("type") == "ListItemControl":
                        try:
                            value = node.GetPattern(auto.PatternId.ValuePattern)
                            _diagnostic_read(record, "value_pattern", lambda: value.Value if value else None)
                        except Exception as exc:
                            record["value_pattern_error"] = str(exc)
                        text = None
                        try:
                            text = node.GetPattern(auto.PatternId.TextPattern)
                            _diagnostic_read(record, "text_pattern", lambda: text.DocumentRange.GetText(-1) if text else None)
                            if text:
                                _diagnostic_read(record, "text_rectangles", lambda: _diagnostic_rectangles(text.DocumentRange))
                        except Exception as exc:
                            record["text_pattern_error"] = str(exc)
                        if is_input:
                            _diagnostic_input(auto, node, record, text)
                    nodes.append(record)
                    if depth < 40:
                        stack.extend((child, depth + 1) for child in reversed(node.GetChildren()))
                report["windows"].append({"title": window.Name, "process_id": window.ProcessId,
                                          "nodes": nodes, "truncated": bool(stack)})
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
