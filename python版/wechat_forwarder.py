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
      先进先出对应最早一条等待）→ 搬回微信群并结束该条等待；
      队列为空时小圆回复别人的消息、群里任何其他信息一律不搬。
    超时：小圆正常两三秒回复；转发后 30 秒未等到对应回复即重置该条
      等待（打日志提醒），程序继续监听新消息，迟到的回复不再搬运。

窗口对接层（读取/发送微信与客户管家消息）在此定义接口，
当前提供 MockAdapter 演示实现；真实对接后续接入 uiautomation。
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "wx_group": "",       # 微信监控群名，如：潼阳圆通出港群
    "kf_group": "",       # 客户管家客服群名，如：潼阳-张士祥客户服务群
    "block_keywords": [],  # 屏蔽词：YT 消息命中即忽略
    "at_keywords": [],     # @客服词句：YT 消息命中即 @客服
    "service_staff": "",   # 客服名：@ 时 @ 的人，后期自己填
    "bot_name": "小圆",    # 客户管家里只搬这个机器人（模糊匹配发送者）
    "poll_interval": 3,    # 轮询间隔（秒）
    "reset_seconds": 30,   # 转发后 30 秒未等到小圆回复 → 重置该条等待
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass  # 配置损坏时按默认值启动，由 GUI 重新保存覆盖
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 消息模型与判定规则
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """一条窗口消息：发送者 + 内容。"""
    sender: str
    content: str
    ts: float = field(default_factory=time.time)

    @property
    def fingerprint(self) -> str:
        return f"{self.sender}|{self.content}"


class Verdict(Enum):
    IGNORE = "忽略"          # 装作没看见
    AT_SERVICE = "艾特客服"   # 微信群 @客服名 + 原话
    FORWARD = "转发"         # 转发到客户管家客服群


# 圆通运单号：YT + 10~15 位数字（常见 13 位）
WAYBILL_RE = re.compile(r"YT\d{10,15}", re.IGNORECASE)


def starts_with_waybill(text: str) -> bool:
    """消息是否以 YT 单号开头（允许前导空白）。"""
    m = WAYBILL_RE.search(text.lstrip()[:20])
    return bool(m and text.lstrip().upper().startswith(m.group().upper()))


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
    """方向① 的核心判定：按 屏蔽词 → @客服词 → 正常转发 的顺序。"""
    if not starts_with_waybill(msg.content):
        return Verdict.IGNORE          # 不带 YT 开头：图片、闲聊等一律忽略
    if hit_any(msg.content, cfg.get("block_keywords", [])):
        return Verdict.IGNORE          # 命中屏蔽词：装作没看见
    if hit_any(msg.content, cfg.get("at_keywords", [])):
        return Verdict.AT_SERVICE      # 命中 @客服词句：微信群 @客服
    return Verdict.FORWARD             # 正常转发客户管家


# ---------------------------------------------------------------------------
# 待回复队列（超时管理）
# ---------------------------------------------------------------------------

class PendingQueue:
    """转发到客户管家后等待小圆回复的记录；超过 reset_seconds 重置。"""

    def __init__(self, reset_seconds: int):
        self.reset_seconds = reset_seconds
        self._items: Dict[str, float] = {}   # 运单号 -> 转发时间戳（插入顺序即先进先出）

    def add(self, waybill: str) -> None:
        self._items[waybill] = time.time()

    def refresh(self, waybill: str) -> None:
        """搬回一条回复后刷新等待计时：小圆对同一单连发多条也能全部搬回，
        30 秒无新回复才算这轮对话结束。"""
        if waybill in self._items:
            self._items[waybill] = time.time()

    def peek_oldest(self) -> Optional[str]:
        """最早的等待运单号（不移除，供 FIFO 关联）。"""
        return next(iter(self._items), None)

    def is_empty(self) -> bool:
        return not self._items

    def sweep(self) -> List[str]:
        """返回超时应重置的运单号并移出队列。"""
        now = time.time()
        expired = [wb for wb, ts in self._items.items()
                   if now - ts >= self.reset_seconds]
        for wb in expired:
            del self._items[wb]
        return expired


# ---------------------------------------------------------------------------
# 窗口对接层接口（真实对接时替换为 uiautomation 实现）
# ---------------------------------------------------------------------------

class WxSource:
    """微信群消息来源：返回监控群里的新消息。"""
    def fetch_new(self) -> List[Message]:
        raise NotImplementedError


class WxSink:
    """微信群发送：粘贴到输入框并回车。"""
    def send(self, text: str) -> None:
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

class Forwarder:
    def __init__(self, cfg: dict, wx_src: WxSource, wx_snk: WxSink,
                 kf_src: KfSource, kf_snk: KfSink,
                 log: Callable[[str], None] = print):
        self.cfg = cfg
        self.wx_src = wx_src
        self.wx_snk = wx_snk
        self.kf_src = kf_src
        self.kf_snk = kf_snk
        self.log = log
        self.queue = PendingQueue(cfg.get("reset_seconds", 30))
        self._seen_wx: set[str] = set()      # 已处理的微信消息指纹
        self._seen_kf: set[str] = set()      # 已搬运的小圆回复指纹
        self._seen_wx_snap: set[str] = set() # 启动后第一条快照，不回溯历史

    # -- 方向① 微信 -> 客户管家 --------------------------------------------
    def poll_wx(self) -> None:
        for msg in self.wx_src.fetch_new():
            fp = msg.fingerprint
            if fp in self._seen_wx:
                continue
            self._seen_wx.add(fp)

            verdict = classify(msg, self.cfg)
            if verdict is Verdict.IGNORE:
                continue  # 装作没看见，不留痕

            if verdict is Verdict.AT_SERVICE:
                staff = self.cfg.get("service_staff", "").strip()
                if not staff:
                    self.log(f"[警告] 命中@客服词但客服名未配置：{msg.content[:30]}…")
                    continue
                self.wx_snk.send(f"@{staff} {msg.content}")
                self.log(f"[艾特客服] 已在微信群 @{staff}：{msg.content[:40]}")
            else:  # FORWARD
                self.kf_snk.send(msg.content)
                wb = extract_waybill(msg.content) or msg.fingerprint[:20]
                self.queue.add(wb)
                self.log(f"[转发] → 客户管家 {self.cfg.get('kf_group', '')}：{msg.content[:40]}")

    # -- 方向② 客户管家 -> 微信 --------------------------------------------
    def poll_kf(self) -> None:
        bot = self.cfg.get("bot_name", "小圆")
        for rep in self.kf_src.fetch_replies(bot):
            if _is_media(rep.content):
                continue  # 图片等非文本回复：当没看见
            fp = rep.fingerprint
            if fp in self._seen_kf:
                continue
            self._seen_kf.add(fp)

            # 队列为空：这条小圆回复不是我转发的消息引来的，不搬
            if self.queue.is_empty():
                continue

            # 关联并搬回：
            #   回复带单号且在等待队列 → 搬回并刷新计时（同单连发多条全部搬）
            #   回复不带单号且队列非空 → 对应最早一条等待，同样刷新
            #   回复带单号但不在队列 → 已结束的旧对话，不搬
            wb = extract_waybill(rep.content)
            if wb:
                if wb not in self.queue._items:
                    continue
                self.queue.refresh(wb)
            else:
                oldest = self.queue.peek_oldest()
                if oldest is None:
                    continue
                self.queue.refresh(oldest)

            self.wx_snk.send(rep.content)
            self.log(f"[搬回] ← {rep.sender}：{rep.content[:40]}")

    # -- 超时重置 -----------------------------------------------------------
    def sweep_timeout(self) -> None:
        for wb in self.queue.sweep():
            self.log(f"[结束] {wb} 已 {self.cfg.get('reset_seconds', 30)} 秒无小圆新回复，"
                     f"本轮等待结束；后续回复不再搬运，如需结果请到客户管家查看")

    def poll_once(self) -> None:
        self.poll_wx()
        self.poll_kf()
        self.sweep_timeout()

    def run(self) -> None:
        interval = max(1, int(self.cfg.get("poll_interval", 3)))
        self.log(f"开始运行：微信[{self.cfg.get('wx_group', '')}] ⇄ 客户管家[{self.cfg.get('kf_group', '')}]，每 {interval} 秒轮询")
        while True:
            try:
                self.poll_once()
            except Exception as e:  # 单轮异常不中断整体运行
                self.log(f"[异常] {e!r}")
            time.sleep(interval)


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
    """内存演示：只放出机器人（小圆）的新回复，人工消息留在箱里不搬。"""
    def __init__(self):
        self.inbox: List[Message] = []

    def fetch_replies(self, bot_name: str) -> List[Message]:
        out = [m for m in self.inbox if bot_name in m.sender]
        self.inbox = [m for m in self.inbox if bot_name not in m.sender]
        return out


class MockWxSink(WxSink):
    def __init__(self, store: List[str]):
        self.store = store

    def send(self, text: str) -> None:
        self.store.append(text)


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
