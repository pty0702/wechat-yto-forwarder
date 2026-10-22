# -*- coding: utf-8 -*-
"""把演示或真实适配器装配到业务核心；不依赖图形界面。"""

import copy
import threading
import time
from typing import Callable

from wechat_forwarder import (
    DEFAULT_CONFIG, Forwarder, KfSource, Message, MockKfSink, MockWxSink,
    WxSource, validate_config,
)


def _demo_config() -> dict:
    """每次演示新建配置，避免把样例群、关键词写入用户配置。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update({
        "mode": "demo", "wx_group": "演示微信群", "kf_group": "演示客户管家群",
        "service_staff": "演示客服", "bot_name": "小圆-总公司",
        "block_keywords": ["退回", "拦截"], "at_keywords": ["投诉", "赔偿"],
        "poll_interval": 0.25, "reset_seconds": 4,
    })
    return validate_config(cfg)


class _DemoTimeline:
    """轮询时投递分阶段样例，无额外定时线程。"""

    def __init__(self, entries, started: float, log: Callable[[str], None]):
        self.entries = list(entries)
        self.started = started
        self.log = log

    def take_due(self):
        due = []
        elapsed = time.monotonic() - self.started
        while self.entries and elapsed >= self.entries[0][0]:
            _, label, message = self.entries.pop(0)
            self.log(f"[演示输入] {label}：{message.content}")
            due.append(message)
        return due


class _DemoWxSource(WxSource):
    def __init__(self, timeline: _DemoTimeline):
        self.timeline = timeline

    def fetch_new(self):
        return self.timeline.take_due()


class _DemoKfSource(KfSource):
    def __init__(self, timeline: _DemoTimeline):
        self.timeline = timeline

    def fetch_replies(self, bot_name: str):
        return [msg for msg in self.timeline.take_due() if bot_name == msg.sender]


def _run_demo(stop_event: threading.Event, log: Callable[[str], None]) -> None:
    cfg = _demo_config()
    started = time.monotonic()
    wx_timeline = _DemoTimeline([
        (0.75, "正常运单，应转发客户管家", Message(
            "演示客户甲", "YT0719078698064 请查询物流进度", message_id="demo-wx-1")),
        (1.5, "命中屏蔽词，应忽略", Message(
            "演示客户乙", "YT0711234567890 拒收退回", message_id="demo-wx-2")),
        (2.25, "命中客服词，应在微信提醒客服", Message(
            "演示客户丙", "YT0798765432109 我要投诉", message_id="demo-wx-3")),
        (3.0, "非运单消息，应忽略", Message(
            "演示客户丁", "大家好", message_id="demo-wx-4")),
        (8.0, "无回复运单，四秒后结束等待", Message(
            "演示客户戊", "YT0719078611111 请查询签收状态", message_id="demo-wx-5")),
    ], started, log)
    kf_timeline = _DemoTimeline([
        (0.0, "无等待记录的机器人回复，应忽略", Message(
            "小圆-总公司", "YT0711111111111 已签收", message_id="demo-kf-1")),
        (1.25, "带运单号回复，应搬回微信", Message(
            "小圆-总公司", "YT0719078698064 正在派送", message_id="demo-kf-2")),
        (2.0, "同一运单的后续回复，应搬回微信", Message(
            "小圆-总公司", "已联系网点跟进", message_id="demo-kf-3")),
        (2.5, "人工客服回复，应忽略", Message(
            "人工客服", "这单我会跟进", message_id="demo-kf-4")),
        (7.0, "超过等待时间的回复，应忽略", Message(
            "小圆-总公司", "YT0719078698064 迟到的补充回复", message_id="demo-kf-5")),
    ], started, log)
    log("[演示] 使用内置样例群、关键词和消息；不会连接或操作真实客户端，也不会改写配置。")
    log("[演示] 样例将在约十二秒内依次展示过滤、转发、提醒客服、回复搬回和超时；完成后可点击停止。")
    forwarder = Forwarder(
        cfg, _DemoWxSource(wx_timeline), MockWxSink([]),
        _DemoKfSource(kf_timeline), MockKfSink([]), log=log,
    )
    forwarder.run(stop_event)


def run_session(cfg: dict, stop_event: threading.Event,
                log: Callable[[str], None] = print) -> None:
    """在调用线程运行会话；真实适配器在同一线程内创建、使用并释放。"""
    mode = cfg.get("mode", "demo")
    if mode not in ("demo", "live"):
        raise ValueError("运行模式必须是 demo（演示）或 live（真实客户端）")
    if stop_event.is_set():
        return
    if mode == "demo":
        _run_demo(stop_event, log)
        return
    live_cfg = validate_config(copy.deepcopy(cfg), live=True)
    # 延迟导入，让未安装 Windows 适配依赖的环境也能运行演示。
    from windows_adapter import create_live_adapters

    with create_live_adapters(live_cfg, log) as adapters:
        if not stop_event.is_set():
            Forwarder(live_cfg, *adapters, log=log).run(stop_event)
