# -*- coding: utf-8 -*-
"""客户管家 DOM 通道：经 WebView2 CDP 读写页面，不依赖 UIA 树。

需要以「启动客户管家-调试.ps1」启动客户端（CDP 端口 9223）。
DOM 选择器来自本机 2.0.0.2 页面结构核对：消息行 div[id=数字]、
发送者在 news-box 头部、正文 .text-content、输入框 #edit-content、
发送按钮 button.send-btn。
"""

from __future__ import annotations

import json
import time
import urllib.request

from wechat_forwarder import Message, RetryableSendError, SendUncertainError
from windows_adapter import AdapterConfigurationError, _normalized, _TIME

_DEFAULT_CDP = "http://127.0.0.1:9223"


class _Cdp:
    """极简 CDP 客户端：HTTP 列页 + WebSocket 执行 JS。"""

    def __init__(self, base_url=_DEFAULT_CDP, timeout=8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._ws = None
        self._msg_id = 0

    def close(self):
        ws, loop = self._ws, getattr(self, "_loop", None)
        self._ws = None
        if ws is None:
            return
        import asyncio
        # close() 是协程且库内有 keepalive 常驻任务：不排干净会留
        # "Task was destroyed but it is pending" 告警噪音。
        try:
            loop.run_until_complete(ws.close())
        except Exception:
            pass
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass

    def _connect(self):
        if self._ws is not None:
            return
        import websockets
        import asyncio
        pages = json.loads(urllib.request.urlopen(
            self.base_url + "/json/list", timeout=self.timeout).read())
        page = next((p for p in pages if p.get("type") == "page"), None)
        if not page or not page.get("webSocketDebuggerUrl"):
            raise AdapterConfigurationError("CDP 未找到客户管家页面，请用调试脚本重启客户端")
        self._loop = asyncio.new_event_loop()
        self._ws = self._loop.run_until_complete(
            websockets.connect(page["webSocketDebuggerUrl"], max_size=20_000_000))

    def evaluate(self, expression):
        self._connect()
        self._msg_id += 1
        payload = json.dumps({
            "id": self._msg_id,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        })
        self._loop.run_until_complete(self._ws.send(payload))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            raw = self._loop.run_until_complete(self._ws.recv())
            message = json.loads(raw)
            if message.get("id") != self._msg_id:
                continue
            result = message.get("result", {}).get("result", {})
            if "value" in result:
                return result["value"]
            return result.get("description")
        raise AdapterConfigurationError("CDP 执行超时")

    def insert_text(self, text):
        self._connect()
        self._msg_id += 1
        self._loop.run_until_complete(self._ws.send(json.dumps({
            "id": self._msg_id,
            "method": "Input.insertText",
            "params": {"text": text},
        })))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            message = json.loads(self._loop.run_until_complete(self._ws.recv()))
            if message.get("id") == self._msg_id:
                return
        raise AdapterConfigurationError("CDP 输入超时")


def _parse_row(raw, self_names):
    """把一条消息行的 DOM 摘要转成 Message；结构同 yto 解析器。"""
    sender = (raw.get("sender") or "").strip()
    content = raw.get("content") or ""
    event_id = str(raw.get("id") or "").strip()
    if not sender or not event_id:
        return None
    kind = "media" if raw.get("media") else "text"
    is_self = sender in self_names
    message_id = f"客户管家:{event_id}"
    return Message(sender, content, message_id=message_id, is_self=is_self, kind=kind)


# 在页面内执行：抽取全部消息行摘要（不回传原始 HTML）。
_ROWS_JS = r"""
(() => {
  const out = [];
  for (const el of document.querySelectorAll('div[id]')) {
    if (!/^[0-9]{15,22}$/.test(el.id)) continue;
    const box = el.querySelector('.news-box');
    const body = el.querySelector('.text-content');
    if (!box) continue;
    let sender = '';
    for (const node of box.children) {
      if (node.classList.contains('text-content') || node.classList.contains('news-box')) continue;
      const texts = [];
      for (const t of node.querySelectorAll('*')) {
        if (t.children.length === 0 && (t.textContent || '').trim()) texts.push(t.textContent.trim());
      }
      for (const t of texts) {
        if (!/^(?:今天|昨天|\d{1,2}月\d{1,2}日)?\s*\d{1,2}:\d{2}(?::\d{2})?$/.test(t) && t.trim()) {
          sender = t.trim();
          break;
        }
      }
      if (sender) break;
    }
    const content = body ? (body.innerText || '') : '';
    const media = !!(body && body.querySelector('img,button,video'));
    out.push({id: el.id, sender, content, media});
  }
  return out;
})()
"""

_TITLE_JS = r"""
(() => {
  const spans = document.querySelectorAll('p.p-t-14 span.cursor-default');
  for (const s of spans) {
    if (s.offsetParent !== null) return (s.innerText || '').trim();
  }
  return '';
})()
"""


class KfDomAdapter:
    """客户管家来源与发送端；读写都走 CDP DOM。"""

    def __init__(self, profile, group, log=print, cdp=None):
        self.profile = profile
        self.group = group
        self.log = log
        self.self_names = [str(name).strip() for name in profile.get("self_names", [])
                           if str(name).strip()]
        self._cdp = cdp
        self._known = set()
        self._baseline_done = False

    def _client(self):
        if self._cdp is None:
            self._cdp = _Cdp(self.profile.get("cdp_url") or _DEFAULT_CDP)
        return self._cdp

    def _title(self):
        return str(self._client().evaluate(_TITLE_JS) or "").strip()

    def prepare(self):
        title = self._title()
        if title != self.group:
            raise RetryableSendError(
                f"客户管家当前会话不是配置的群，请手动打开目标群：{self.group}（读到：{title or '无'}）")
        rows = self._rows()
        self._known = {msg.message_id for msg in rows}
        self._baseline_done = True
        self.log(f"[适配] 客户管家 DOM 已绑定 {self.group}，历史 {len(rows)} 条")

    def _rows(self):
        raw = self._client().evaluate(_ROWS_JS)
        messages = []
        for item in raw or []:
            msg = _parse_row(item, self.self_names)
            if msg is not None:
                messages.append(msg)
        return messages

    def fetch_new(self):
        if not self._baseline_done:
            raise AdapterConfigurationError("客户管家 DOM 适配器尚未 prepare")
        fresh = []
        for msg in self._rows():
            if msg.message_id not in self._known:
                self._known.add(msg.message_id)
                fresh.append(msg)
        return fresh

    def fetch_replies(self, bot_name):
        return [msg for msg in self.fetch_new() if msg.sender == bot_name]

    def send(self, text):
        if not isinstance(text, str) or not text:
            raise AdapterConfigurationError("发送文本不能为空")
        title = self._title()
        if title != self.group:
            raise RetryableSendError(f"客户管家当前会话不是配置的群，请手动打开目标群：{self.group}")
        cdp = self._client()
        empty = cdp.evaluate(
            "(() => { const el = document.getElementById('edit-content');"
            " return !el || !(el.innerText || '').trim(); })()")
        if not empty:
            raise RetryableSendError("目标输入框已有草稿，请先处理草稿；程序不会覆盖")
        before = {msg.message_id for msg in self._rows()}
        focus_ok = cdp.evaluate(
            "(() => { const el = document.getElementById('edit-content');"
            " if (!el) return false; el.focus(); return document.activeElement === el; })()")
        if not focus_ok:
            raise AdapterConfigurationError("客户管家输入框无法获得焦点")
        cdp.insert_text(text)
        # 本机实测（2026-10-24）：该 Vue 版输入框靠 keyup 同步发送按钮状态，
        # 只派 input 事件按钮不启用；两个事件都派发才稳定。
        cdp.evaluate(
            "(() => { const el = document.getElementById('edit-content');"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('keyup', {bubbles: true})); return true; })()")
        draft = str(cdp.evaluate(
            "(() => { const el = document.getElementById('edit-content');"
            " return el ? (el.innerText || '') : ''; })()") or "")
        if _normalized(draft).strip() != _normalized(text).strip():
            raise AdapterConfigurationError(
                f"输入结果与原文不同（原文 {len(text)} 字，回读 {len(draft)} 字"
                f"：{draft[:40]!r}），已保留草稿，未提交发送；"
                "常见原因：中文输入法处于组合状态，请切英文输入法后重试")
        if cdp.evaluate(
                "(() => { const b = document.querySelector('button.send-btn');"
                " return !b || b.disabled; })()"):
            raise AdapterConfigurationError("发送按钮未启用，输入未被客户端接受，已保留草稿")
        clicked = cdp.evaluate(
            "(() => { const b = document.querySelector('button.send-btn');"
            " if (!b) return false; b.click(); return true; })()")
        if not clicked:
            raise AdapterConfigurationError("未找到发送按钮，已保留草稿")
        expected = _normalized(text).strip()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if cdp.evaluate(
                    "(() => { const el = document.getElementById('edit-content');"
                    " return !el || !(el.innerText || '').trim(); })()"):
                for msg in self._rows():
                    if msg.message_id not in before and msg.is_self \
                            and _normalized(msg.content).strip() == expected:
                        return
            time.sleep(0.2)
        raise SendUncertainError(
            "发送后未找到本账号的完整消息回显，请人工核对；已停止，避免重复发送")

    def send_mention(self, staff, text):
        raise AdapterConfigurationError("客户管家 DOM 通道不支持真实 @，请在微信端提醒客服")
