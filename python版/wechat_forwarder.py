# -*- coding: utf-8 -*-
"""
微信 ⇄ 圆通客户管家 消息搬运核心逻辑

方向① 微信 → 客户管家：
    只看【以 YT 开头】的客户消息，按顺序判定：
      1. 含屏蔽词           → 装作没看见（忽略）
      2. 含 @客服词句        → 微信群里 @客服名 + 原话，结束（不转发小圆）
      3. 都没有             → 正常转发到客户管家客服群
    不以 YT 开头的消息一律忽略（含图片、闲聊等）。

方向② 客户管家 → 微信：
    只搬"程序转发进去的那条消息"引来的小圆回复：
      待回复队列非空时，小圆的新回复按运单号关联（回复不带单号则按
      先进先出对应最早一条等待）→ 搬回微信群并刷新静默计时；
      队列为空时小圆回复别人的消息、群里任何其他信息一律不搬。
    超时：小圆正常两三秒回复；转发后 30 秒未等到对应回复即重置该条
      等待（打日志提醒），程序继续监听新消息，迟到的回复不再搬运。

窗口适配层负责首次历史快照和稳定的消息事件编号。无单号回复采用最早等待
规则，无法仅凭文本证明它属于哪条请求；多人同时向机器人提问时存在歧义。
"""

import copy
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "wx_group": "",       # 单群写法（兼容旧配置）；新配置用 wx_groups
    "wx_groups": [],      # 微信监控群列表，如：["潼阳圆通出港群", "潼阳圆通进港群"]
    "kf_group": "",       # 客户管家客服群名，如：潼阳-张士祥客户服务群
    "block_keywords": [],  # 屏蔽词（全局默认）：YT 消息命中即忽略
    "at_keywords": [],     # @客服词句（全局默认）：YT 消息命中即 @客服
    "group_policies": {},  # 按群覆盖：{"群名": {"block_keywords": [], "at_keywords": []}}
    "service_staff": "",   # 客服名：多人用顿号/逗号分隔，@ 时一条消息全部提醒
    "bot_name": "小圆-总公司",  # 必须与机器人显示的完整名称一致
    "poll_interval": 3,    # 轮询间隔（秒）
    "reset_seconds": 30,   # 转发后 30 秒未等到小圆回复 → 重置该条等待
    "mode": "demo",       # 默认演示；真实运行前另行验证窗口配置
    "wechat_accessibility": False,  # 明确启用后，仅对已核对构建临时开启控件
    "wx_profile": {},
    "kf_profile": {},
}


def split_staff(staff: str) -> List[str]:
    """客服名单拆分：顿号/英文逗号/中文逗号/分号分隔，去空去重保序。"""
    if not isinstance(staff, str):
        return []
    parts = re.split(r"[、，,；;]", staff)
    return list(dict.fromkeys(p.strip() for p in parts if p.strip()))


def validate_config(cfg: dict, live: bool = False) -> dict:
    """返回独立、规范化的配置；保留旧配置及适配器的额外字段。"""
    if not isinstance(cfg, dict):
        raise ValueError("配置必须是 JSON 对象")
    normalized = copy.deepcopy(DEFAULT_CONFIG)
    normalized.update(copy.deepcopy(cfg))
    for key in ("wx_group", "kf_group", "service_staff", "bot_name", "mode"):
        value = normalized[key]
        if not isinstance(value, str):
            raise ValueError(f"配置项 {key} 必须是字符串")
        normalized[key] = value.strip()
    if normalized["mode"] not in ("demo", "live"):
        raise ValueError("运行模式只能是 demo 或 live")
    if not isinstance(normalized["wechat_accessibility"], bool):
        raise ValueError("微信兼容开关必须是布尔值 true 或 false")
    if not normalized["bot_name"]:
        raise ValueError("机器人完整名称不能为空")
    # 群列表：wx_groups 优先；旧单群 wx_group 自动并入；两者并存时合并去重。
    groups_value = normalized.get("wx_groups", [])
    if isinstance(groups_value, str):
        groups_value = [groups_value]
    if not isinstance(groups_value, list) or any(
            not isinstance(g, str) or not g.strip() for g in groups_value):
        raise ValueError("配置项 wx_groups 必须是非空群名字符串列表")
    groups = list(dict.fromkeys(g.strip() for g in groups_value if g.strip()))
    if normalized["wx_group"]:
        groups = list(dict.fromkeys([normalized["wx_group"]] + groups))
    normalized["wx_groups"] = groups
    normalized["wx_group"] = groups[0] if groups else ""
    # 按群词表覆盖：只允许覆盖 block_keywords / at_keywords，群名必须在列表内。
    policies = normalized.get("group_policies", {})
    if not isinstance(policies, dict):
        raise ValueError("配置项 group_policies 必须是对象")
    clean_policies = {}
    for group, policy in policies.items():
        if group not in groups:
            raise ValueError(f"group_policies 里的群不在 wx_groups 列表内：{group}")
        if not isinstance(policy, dict):
            raise ValueError(f"group_policies[{group}] 必须是对象")
        clean = {}
        for key in ("block_keywords", "at_keywords"):
            words = policy.get(key)
            if words is None:
                continue
            if not isinstance(words, list) or any(not isinstance(w, str) for w in words):
                raise ValueError(f"group_policies[{group}].{key} 必须是字符串列表")
            clean[key] = list(dict.fromkeys(w.strip() for w in words if w.strip()))
        if clean:
            clean_policies[group] = clean
    normalized["group_policies"] = clean_policies
    for key in ("block_keywords", "at_keywords"):
        value = normalized[key]
        if not isinstance(value, list) or any(not isinstance(word, str) for word in value):
            raise ValueError(f"配置项 {key} 必须是字符串列表")
        normalized[key] = list(dict.fromkeys(word.strip() for word in value if word.strip()))
    for key in ("poll_interval", "reset_seconds"):
        value = normalized[key]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"配置项 {key} 必须是大于零的秒数")
        try:
            number = float(value)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"配置项 {key} 必须是大于零的秒数") from exc
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"配置项 {key} 必须是有限且大于零的秒数")
        normalized[key] = number
    for key in ("wx_profile", "kf_profile"):
        if not isinstance(normalized[key], dict):
            raise ValueError(f"配置项 {key} 必须是对象")
    if live:
        if not normalized["wx_groups"] or not normalized["kf_group"]:
            raise ValueError("真实运行前必须填写微信群名和客户管家客服群名")
        if normalized["at_keywords"] and not normalized["service_staff"]:
            raise ValueError("配置了 @客服词句时，必须填写客服完整名称")
    return normalized


def load_config() -> dict:
    """首次使用返回默认配置，已有配置损坏时明确报错并保留原文件。"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as stream:
            cfg = json.load(stream)
    except FileNotFoundError:
        return validate_config({})
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"配置文件无法解析，请修复后重试：{CONFIG_FILE}") from exc
    return validate_config(cfg)


def save_config(cfg: dict) -> None:
    """同目录写入并原子替换，写入失败时保留原配置。"""
    normalized = validate_config(cfg)
    filename = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=os.path.dirname(os.path.abspath(CONFIG_FILE)), prefix=".config-", suffix=".tmp",
        ) as stream:
            filename = stream.name
            json.dump(normalized, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(filename, CONFIG_FILE)
    finally:
        if filename is not None and os.path.exists(filename):
            os.unlink(filename)


# ---------------------------------------------------------------------------
# 消息模型与判定规则
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """一条消息事件；适配器重复读取同一事件时必须复用 message_id。"""
    sender: str
    content: str
    ts: float = field(default_factory=time.time)
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    is_self: bool = False
    kind: str = "text"
    group: str = ""   # 消息来源群名：多群模式下用于按群词表判定与回复路由

    def __post_init__(self):
        if not isinstance(self.message_id, str) or not self.message_id:
            raise ValueError("消息事件编号不能为空")

    @property
    def fingerprint(self) -> str:
        """兼容原属性名，身份不再取决于消息文本。"""
        return self.message_id


class Verdict(Enum):
    IGNORE = "忽略"          # 装作没看见
    AT_SERVICE = "艾特客服"   # 微信群 @客服名 + 原话
    FORWARD = "转发"         # 转发到客户管家客服群


# 圆通运单号：YT + 10~15 位数字（常见 13 位）
WAYBILL_RE = re.compile(r"YT[0-9]{10,15}(?![0-9])", re.IGNORECASE)


def starts_with_waybill(text: str) -> bool:
    """消息是否以 YT 单号开头（允许前导空白）。"""
    return bool(WAYBILL_RE.match(text.lstrip()))


def extract_waybill(text: str) -> Optional[str]:
    """提取消息中的第一个运单号，用于待回复队列关联。"""
    m = WAYBILL_RE.search(text)
    return m.group().upper() if m else None


def hit_any(text: str, keywords: List[str]) -> bool:
    return any(kw and kw.strip() and kw.strip() in text for kw in keywords)


# 微信/客户管家里的非文本消息占位样式
_MEDIA_RE = re.compile(r"^\s*(\[图片\]|\[视频\]|\[文件\]|\[语音\]|\[动画表情\]|\[链接\]|\[名片\])\s*$")


def _is_media(text: str) -> bool:
    """图片、语音等非文本消息：直接当没看见。"""
    return bool(_MEDIA_RE.match(text)) or not text.strip()


def classify(msg: Message, cfg: dict) -> Verdict:
    """方向① 的核心判定：按 屏蔽词 → @客服词 → 正常转发 的顺序。

    多群模式下优先用 group_policies[msg.group] 覆盖的词表，未覆盖的群
    用全局 block_keywords / at_keywords。
    """
    if msg.is_self or msg.kind != "text" or _is_media(msg.content):
        return Verdict.IGNORE
    if not starts_with_waybill(msg.content):
        return Verdict.IGNORE          # 不带 YT 开头：图片、闲聊等一律忽略
    policy = cfg.get("group_policies", {}).get(msg.group) or {}
    block_words = policy.get("block_keywords", cfg.get("block_keywords", []))
    at_words = policy.get("at_keywords", cfg.get("at_keywords", []))
    if hit_any(msg.content, block_words):
        return Verdict.IGNORE          # 命中屏蔽词：装作没看见
    if hit_any(msg.content, at_words):
        return Verdict.AT_SERVICE      # 命中 @客服词句：微信群 @客服
    return Verdict.FORWARD             # 正常转发客户管家


# ---------------------------------------------------------------------------
# 待回复队列（超时管理）
# ---------------------------------------------------------------------------

class PendingQueue:
    """转发到客户管家后等待小圆回复的记录；超过 reset_seconds 重置。"""

    def __init__(self, reset_seconds: float):
        self.reset_seconds = reset_seconds
        self._items: Dict[str, tuple] = {}   # 运单号 -> (转发时间戳, 来源群名)

    def add(self, waybill: str, group: str = "") -> None:
        self._items[waybill] = (time.monotonic(), group)

    def group_of(self, waybill: str) -> str:
        """该运单号的请求来自哪个微信群（搬回路由用）。"""
        item = self._items.get(waybill)
        return item[1] if item else ""

    def refresh(self, waybill: str) -> None:
        """搬回一条回复后刷新等待计时：小圆对同一单连发多条也能全部搬回，
        30 秒无新回复才算这轮对话结束。"""
        if waybill in self._items:
            self._items[waybill] = (time.monotonic(), self._items[waybill][1])

    def __contains__(self, waybill: str) -> bool:
        return waybill in self._items

    def peek_oldest(self) -> Optional[str]:
        """最早的等待运单号（不移除，供 FIFO 关联）。"""
        return next(iter(self._items), None)

    def is_empty(self) -> bool:
        return not self._items

    def sweep(self) -> List[str]:
        """返回超时应重置的运单号并移出队列。"""
        now = time.monotonic()
        expired = [wb for wb, (ts, _group) in self._items.items()
                   if now - ts >= self.reset_seconds]
        for wb in expired:
            del self._items[wb]
        return expired


# ---------------------------------------------------------------------------
# 窗口对接层接口（真实对接时替换为 uiautomation 实现）
# ---------------------------------------------------------------------------

class RetryableSendError(RuntimeError):
    """适配器确定消息尚未发出，可以安全地重试。"""


class SendUncertainError(RuntimeError):
    """发送动作已尝试，无法确认结果；必须停止并由用户核对。"""


class _StopRequested(Exception):
    """在下一次发送前响应停止请求。"""


class WxSource:
    """返回新事件；真实适配器需先建立历史基线，不能把旧消息当新消息。"""
    def fetch_new(self) -> List[Message]:
        raise NotImplementedError


class WxSink:
    """微信群发送：粘贴到输入框并回车。group 指定发到哪个群（多群模式）。"""
    def send(self, text: str, group: str = "") -> None:
        raise NotImplementedError

    def send_mention(self, staff: str, text: str, group: str = "") -> None:
        """选择真实群成员提及后发送，不能仅拼接普通的 @ 文本。"""
        raise NotImplementedError


class KfSource:
    """客户管家消息来源：返回客服群里机器人（小圆）的新回复。"""
    def fetch_replies(self, bot_name: str) -> List[Message]:
        raise NotImplementedError


class KfSink:
    """客户管家发送：粘贴到客服群输入框并回车。"""
    def send(self, text: str) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

class _RecentIds:
    """保留最近的事件编号，避免长期运行时去重集合无限增长。"""

    def __init__(self, limit: int = 10000):
        self.limit = limit
        self._ids = OrderedDict()

    def __contains__(self, message_id: str) -> bool:
        return message_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, message_id: str) -> None:
        self._ids[message_id] = None
        self._ids.move_to_end(message_id)
        while len(self._ids) > self.limit:
            self._ids.popitem(last=False)


class Forwarder:
    def __init__(self, cfg: dict, wx_src: WxSource, wx_snk: WxSink,
                 kf_src: KfSource, kf_snk: KfSink,
                 log: Callable[[str], None] = print):
        self.cfg = validate_config(cfg)
        self.wx_src = wx_src
        self.wx_snk = wx_snk
        self.kf_src = kf_src
        self.kf_snk = kf_snk
        self.log = log
        self.queue = PendingQueue(self.cfg["reset_seconds"])
        self._seen_wx = _RecentIds()
        self._seen_kf = _RecentIds()
        # 即使来源取出即删除，整批消息也先缓存；前一条发送失败不丢后续条目。
        # 重试缓冲未清空前不继续读取同方向，避免故障期间无限积压在内存中。
        self._wx_buffer = OrderedDict()
        self._kf_buffer = OrderedDict()
        self._stop_event = None
        self._uncertain_error = None

    def _send(self, send: Callable, *args) -> None:
        if self._uncertain_error is not None:
            raise self._uncertain_error
        if self._stop_event is not None and self._stop_event.is_set():
            raise _StopRequested
        try:
            send(*args)
        except RetryableSendError:
            raise
        except SendUncertainError as exc:
            self._uncertain_error = exc
            raise
        except Exception as exc:
            # 未分类的发送异常不能证明消息没发出，禁止盲目重发。
            self._uncertain_error = SendUncertainError(f"无法确认发送结果：{exc}")
            raise self._uncertain_error from exc

    def _flush_wx(self) -> None:
        while self._wx_buffer:
            message_id, msg = next(iter(self._wx_buffer.items()))
            verdict = classify(msg, self.cfg)
            target_group = msg.group or self.cfg["wx_group"]
            if verdict is Verdict.AT_SERVICE:
                staff = self.cfg["service_staff"]
                if not staff:
                    raise ValueError("命中 @客服词句，但客服完整名称未配置；消息保留待处理")
                self._send(self.wx_snk.send_mention, staff, msg.content, target_group)
                notice = (f"[艾特客服] 已在 {target_group} @{staff}：{msg.content[:40]}")
            elif verdict is Verdict.FORWARD:
                self._send(self.kf_snk.send, msg.content)
                self.queue.add(extract_waybill(msg.content), target_group)
                notice = (f"[转发] {target_group} → 客户管家 "
                          f"{self.cfg['kf_group']}：{msg.content[:40]}")
            else:
                notice = None
            # 只在发送明确成功或规则明确忽略后确认事件。
            self._seen_wx.add(message_id)
            del self._wx_buffer[message_id]
            if notice:
                self.log(notice)

    def poll_wx(self) -> None:
        """先完成待重试批次，再读取新事件，保持原消息次序。"""
        self.sweep_timeout()
        self._flush_wx()
        for msg in self.wx_src.fetch_new():
            if msg.message_id not in self._seen_wx:
                self._wx_buffer.setdefault(msg.message_id, msg)
        self._flush_wx()

    def _flush_kf(self) -> None:
        while self._kf_buffer:
            message_id, (reply, waybill) = next(iter(self._kf_buffer.items()))
            # 回复按运单号路由回原微信群；查不到来源（旧配置/异常）发默认群。
            target_group = self.queue.group_of(waybill) or self.cfg["wx_group"]
            self._send(self.wx_snk.send, reply.content, target_group)
            self.queue.refresh(waybill)
            self._seen_kf.add(message_id)
            del self._kf_buffer[message_id]
            self.log(f"[搬回] ← {reply.sender} → {target_group}：{reply.content[:40]}")

    def poll_kf(self) -> None:
        """先清超时，再关联本批回复；已及时收到的回复发送失败后仍可重试。"""
        self.sweep_timeout()
        self._flush_kf()
        bot = self.cfg["bot_name"]
        replies = self.kf_src.fetch_replies(bot)
        # 读取窗口可能耗时，关联前再次排除在读取过程中已到期的等待。
        self.sweep_timeout()
        for reply in replies:
            message_id = reply.message_id
            if message_id in self._seen_kf or message_id in self._kf_buffer:
                continue
            if (reply.is_self or reply.kind != "text" or _is_media(reply.content)
                    or reply.sender.strip() != bot):
                self._seen_kf.add(message_id)
                continue
            # 无单号只能使用最早等待规则；不能证明与其他人的机器人请求无关。
            waybill = extract_waybill(reply.content) or self.queue.peek_oldest()
            if waybill is None or waybill not in self.queue:
                self._seen_kf.add(message_id)
                continue
            self._kf_buffer[message_id] = (reply, waybill)
        self._flush_kf()

    def sweep_timeout(self) -> None:
        for waybill in self.queue.sweep():
            self.log(f"[结束] {waybill} 已 {self.cfg['reset_seconds']:g} 秒无小圆新回复，"
                     "本轮等待结束；后续回复不再搬运，如需结果请到客户管家查看")

    def poll_once(self) -> None:
        if self._uncertain_error is not None:
            raise self._uncertain_error
        # 先消费已经存在的客户管家回复，避免把它们归入本轮刚转发的新请求。
        self.poll_kf()
        self.poll_wx()

    def run(self, stop_event=None) -> None:
        """响应停止事件；发送结果不确定时向外抛错，由界面提示人工核对。"""
        event = stop_event if stop_event is not None else threading.Event()
        self._stop_event = event
        interval = self.cfg["poll_interval"]
        groups_text = "、".join(self.cfg.get("wx_groups") or [self.cfg["wx_group"]])
        self.log(f"开始运行：微信[{groups_text}] ⇄ 客户管家[{self.cfg['kf_group']}]，每 {interval:g} 秒轮询")
        try:
            while not event.is_set():
                try:
                    self.poll_once()
                except _StopRequested:
                    break
                except SendUncertainError as exc:
                    event.set()
                    self.log(f"[已停止] {exc}；请核对目标群中的发送结果后重新启动")
                    raise
                except RetryableSendError as exc:
                    self.log(f"[待重试] {exc}；已保留未发送消息")
                except Exception as exc:
                    self.log(f"[异常] {exc!r}")
                if event.wait(interval):
                    break
        finally:
            self._stop_event = None
            self.log("搬运已停止")


# ---------------------------------------------------------------------------
# Mock 演示：python wechat_forwarder.py 直接运行，验证完整逻辑
# ---------------------------------------------------------------------------

class MockWxSource(WxSource):
    """内存演示：预先灌入消息，取一次即清空。"""
    def __init__(self):
        self.inbox: List[Message] = []

    def fetch_new(self) -> List[Message]:
        out, self.inbox = self.inbox, []
        return out


class MockKfSource(KfSource):
    """内存演示：只放出名称完全匹配机器人的新事件。"""
    def __init__(self):
        self.inbox: List[Message] = []

    def fetch_replies(self, bot_name: str) -> List[Message]:
        messages, self.inbox = self.inbox, []
        return [message for message in messages if message.sender.strip() == bot_name]


class MockWxSink(WxSink):
    def __init__(self, store: List[str]):
        self.store = store

    def send(self, text: str, group: str = "") -> None:
        self.store.append(text)

    def send_mention(self, staff: str, text: str, group: str = "") -> None:
        # 内存演示只记录可读文本，真实适配器需要选择群成员完成提及。
        self.store.append(f"@{staff} {text}")


class MockKfSink(KfSink):
    def __init__(self, store: List[str]):
        self.store = store

    def send(self, text: str) -> None:
        self.store.append(text)


def mock_demo() -> None:
    """模拟完整业务：四类客户消息 + 队列空时小圆消息不搬 + 搬回关联 + 30秒重置。"""
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "wx_group": "潼阳圆通出港群",
        "kf_group": "潼阳-张士祥客户服务群",
        "block_keywords": ["退回", "拦截"],
        "at_keywords": ["投诉", "赔偿"],
        "service_staff": "张士祥",
        "bot_name": "小圆-总公司",
        "reset_seconds": 30,
    })

    wx_src, kf_src = MockWxSource(), MockKfSource()
    sent_wx, sent_kf = [], []
    f = Forwarder(cfg, wx_src, MockWxSink(sent_wx), kf_src, MockKfSink(sent_kf))

    # -- 场景1：队列空时，客服群里小圆在回复别人 → 不搬 --------------------
    kf_src.inbox = [Message("小圆-总公司", "运单号：YT0711234567890 已签收（人工触发的回复）")]
    f.poll_kf()
    print("场景1 队列空时小圆回复别人 → 搬回数:", len(sent_wx), "（应为 0）")

    # -- 场景2：微信来四类消息，只转发正常 YT --------------------------------
    wx_src.inbox = [
        Message("客户A", "大家好，今天天气不错"),                                   # 闲聊 → 忽略
        Message("客户A", "[图片]"),                                                # 图片 → 忽略
        Message("客户A", "YT0719078698064改地址 王先生18951191215江苏省宿迁市沭阳县"),  # → 转发
        Message("客户B", "YT0711234567890 拒收退回"),                              # 屏蔽词 → 忽略
        Message("客户C", "YT0798765432109 我要投诉，三天了没动静"),                  # @客服词 → @客服
        Message("客户D", "改个地址 YT0711555566667 济南市"),                        # YT不在开头 → 忽略
    ]
    f.poll_wx()
    print("\n== 方向① 结果 ==")
    print("发往客户管家:", sent_kf)
    print("发往微信群(@客服):", [t for t in sent_wx if t.startswith('@')])
    print("待回复队列:", list(f.queue._items))

    # -- 场景3：小圆连发两条回复（带单号+不带单号）→ 两条都搬回并保持等待 --
    kf_src.inbox = [
        Message("小圆-总公司", "运单号：YT0719078698064，已修改成功，修改后信息为(收件人：王先生,联系电话：18951191215)"),
        Message("人工客服-李", "这条我人工跟进一下"),   # 人工消息 → 不搬
    ]
    f.poll_kf()
    kf_src.inbox = [Message("小圆-总公司", "已上报延误，正在积极处理中")]  # 无单号，仍属同一单 → 搬回
    f.poll_kf()
    print("\n== 方向② 结果 ==")
    print("搬回微信(应2条):", [t for t in sent_wx if not t.startswith('@')])
    print("队列仍在等待(静默刷新中):", list(f.queue._items))

    # -- 场景4：30 秒无新回复 → 等待结束；迟到的回复不再搬 ------------------
    wx_src.inbox = [Message("客户E", "YT0719078611111 查一下这单")]
    f.poll_wx()
    for wb in list(f.queue._items):
        f.queue._items[wb] -= 31          # 全部拨回 31 秒前，模拟静默超时
    f.sweep_timeout()
    print("\n== 超时结束结果 ==")
    print("待回复队列(应空):", list(f.queue._items))
    kf_src.inbox = [Message("小圆-总公司", "运单号：YT0719078611111 迟到的回复")]  # 结束后到达
    f.poll_kf()
    print("迟到回复搬回数(应为0):", len([t for t in sent_wx if '迟到' in t]))


if __name__ == "__main__":
    mock_demo()
