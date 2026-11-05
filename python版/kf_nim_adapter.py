# -*- coding: utf-8 -*-
"""客户管家云信（NIM）SDK 通道：经 WebView2 CDP 直调页面内云信实例收发。

与 DOM 通道（kf_dom_adapter）同门槛：客户端须以调试口（CDP 9223）启动。
通道差异（2026-11-05 PoC 实测）：
- 发送 = 页面云信实例 sendText，带回执（idServer/status）——不再模拟输入框，
  不存在草稿残留、假阳性上屏、连发限流丢消息这一类 DOM 通道问题；
- 接收 = hook 页面实例 onmsg 回调推入页面队列，不再解析消息列表 DOM，
  新消息实时到达（优于 3 秒轮询）。

页面依赖（升级需跟进）：Vue 组件 imLayout 持有云信实例；宿主客户端注入的
原生桥（imNotifyHandler 等）在客户端内天然存在，无需 shim。
"""

from __future__ import annotations

import json

from wechat_forwarder import Message, RetryableSendError, SendUncertainError
from windows_adapter import AdapterConfigurationError
from kf_dom_adapter import _Cdp, _DEFAULT_CDP

# 定位云信实例 + hook onmsg（幂等：重复执行不会二次包裹）
_HOOK_JS = r"""
(() => {
  const root = document.getElementById('app');
  if (!root || !root.__vue__) return JSON.stringify({ok: false, why: '页面未就绪（未登录或非客户端环境）'});
  let imVm = null;
  const walk = (vm, depth) => {
    if (!vm || imVm || depth > 12) return;
    if (vm.$options.name === 'imLayout' && vm.im) { imVm = vm; return; }
    for (const c of vm.$children) walk(c, depth + 1);
  };
  walk(root.__vue__, 0);
  if (!imVm) return JSON.stringify({ok: false, why: '未找到云信实例（页面结构可能已升级）'});
  const im = imVm.im;
  window.__kfIm = im;
  let opts = null;
  if (im.options && typeof im.options === 'object') opts = im.options;
  else if (im.protocol && im.protocol.options) opts = im.protocol.options;
  if (!opts || typeof opts.onmsg !== 'function') {
    return JSON.stringify({ok: false, why: '未找到 onmsg 挂载点（云信版本可能已变）', account: im.account || ''});
  }
  if (!window.__kfHooked) {
    window.__kfInbox = [];
    const orig = opts.onmsg;
    opts.onmsg = function(msg) {
      try {
        window.__kfInbox.push({
          id: msg.idClient || '', from: msg.from || '',
          fromNick: msg.fromNick || msg.from || '',
          type: msg.type || 'text', scene: msg.scene || '',
          target: msg.target || msg.to || '',
          text: (typeof msg.text === 'string') ? msg.text : '', time: msg.time || 0,
        });
      } catch (e) {}
      try { return orig.apply(this, arguments); } catch (e) {}
    };
    window.__kfHooked = true;
  }
  return JSON.stringify({ok: true, account: im.account || ''});
})()
"""

# 群列表（localStorage 缓存的云信团队信息；精确名优先，Python 侧匹配）
_TEAMS_JS = r"""
(() => {
  const teams = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (!k || k.indexOf('imTeamInfoSession') !== 0) continue;
    try {
      const t = JSON.parse(localStorage.getItem(k));
      if (t && t.teamId && t.name) teams.push({teamId: String(t.teamId), name: t.name});
    } catch (e) {}
  }
  return JSON.stringify(teams);
})()
"""

# 读取并清空收件队列（仅本群 team 消息）
_INBOX_JS = r"""
(() => {
  const box = window.__kfInbox || [];
  window.__kfInbox = [];
  return JSON.stringify(box);
})()
"""


def _send_js(text: str, team_id: str) -> str:
    body = json.dumps(text, ensure_ascii=False)
    target = json.dumps(str(team_id))
    return (
        "new Promise(resolve => {"
        "  const im = window.__kfIm;"
        "  if (!im || typeof im.sendText !== 'function')"
        "    { resolve(JSON.stringify({err: '云信实例不可用'})); return; }"
        "  im.sendText({"
        "    scene: 'team', to: " + target + ", text: " + body + ","
        "    done: (err, msg) => resolve(JSON.stringify({"
        "      err: err ? String(err) : null,"
        "      idServer: msg && msg.idServer || null,"
        "      status: msg && msg.status || null"
        "    }))"
        "  });"
        "})"
    )


class KfNimAdapter:
    """客户管家来源与发送端；收发都走页面内云信实例（CDP 注入）。"""

    def __init__(self, profile, group, log=print, cdp=None):
        self.profile = profile
        self.group = group
        self.log = log
        self.self_names = [str(n).strip() for n in profile.get("self_names", [])
                           if str(n).strip()]
        self._cdp = cdp
        self._known = set()
        self._account = ""
        self._team_id = ""
        self._baseline_done = False

    def _client(self):
        if self._cdp is None:
            self._cdp = _Cdp(self.profile.get("cdp_url") or _DEFAULT_CDP)
        return self._cdp

    def prepare(self):
        cdp = self._client()
        hook = json.loads(cdp.evaluate(_HOOK_JS) or "{}")
        if not hook.get("ok"):
            raise AdapterConfigurationError(
                f"云信通道初始化失败：{hook.get('why', '未知原因')}；"
                "请确认客户端已用调试脚本启动且已登录")
        self._account = str(hook.get("account") or "")
        teams = json.loads(cdp.evaluate(_TEAMS_JS) or "[]")
        exact = [t for t in teams if t.get("name") == self.group]
        fuzzy = [t for t in teams
                 if t.get("name") and (t["name"] in self.group or self.group in t["name"])]
        picked = exact[0] if exact else (fuzzy[0] if len(fuzzy) == 1 else None)
        if not picked:
            raise AdapterConfigurationError(
                f"云信通道在客户管家里找不到群「{self.group}」"
                f"（共 {len(teams)} 个群缓存）；请先在客户端打开该群后重试")
        self._team_id = str(picked["teamId"])
        # hook 挂载时刻起收新消息；prepare 前到达的消息不入队，天然基线。
        cdp.evaluate(_INBOX_JS)
        self._baseline_done = True
        self.log(f"[适配] 客户管家云信通道已绑定 {self.group}"
                 f"（teamId={self._team_id}，账号 {self._account[:8]}…）")

    # ------------------------------------------------------------------
    # 读取（onmsg hook 队列 → Message）
    # ------------------------------------------------------------------
    def _to_message(self, raw):
        if raw.get("scene") != "team" or str(raw.get("target")) != self._team_id:
            return None
        sender = str(raw.get("fromNick") or raw.get("from") or "").strip()
        if not sender:
            return None
        message_id = f"kfnim:{raw.get('id') or ''}:{raw.get('time')}"
        kind = "text" if raw.get("type") == "text" else "media"
        is_self = (raw.get("from") == self._account) or (sender in self.self_names)
        return Message(sender, raw.get("text") or "", message_id=message_id,
                       is_self=is_self, kind=kind, group=self.group)

    def fetch_new(self):
        if not self._baseline_done:
            raise AdapterConfigurationError("云信通道适配器尚未 prepare")
        raw_list = json.loads(self._client().evaluate(_INBOX_JS) or "[]")
        fresh = []
        for raw in raw_list:
            msg = self._to_message(raw)
            if msg is None or msg.message_id in self._known:
                continue
            self._known.add(msg.message_id)
            fresh.append(msg)
        return fresh

    def fetch_replies(self, bot_name):
        return [msg for msg in self.fetch_new() if msg.sender == bot_name]

    # ------------------------------------------------------------------
    # 发送（SDK 回执：err / idServer / status）
    # ------------------------------------------------------------------
    def send(self, text):
        if not isinstance(text, str) or not text:
            raise AdapterConfigurationError("发送文本不能为空")
        if not self._baseline_done:
            raise AdapterConfigurationError("云信通道适配器尚未 prepare")
        receipt = json.loads(self._client().evaluate(_send_js(text, self._team_id)) or "{}")
        err = receipt.get("err")
        id_server = receipt.get("idServer")
        status = receipt.get("status")
        if not err and id_server:
            # 服务器落地凭证齐备：真成功。
            self.log(f"[适配] 云信发送回执 idServer={id_server} status={status}")
            return
        if err:
            # SDK 明确报错：确定未发出（如被移出群、内容过长、参数错误）。
            raise RetryableSendError(f"云信发送失败：{err}")
        # err 为空但无 idServer：状态异常，无法确认是否送达。
        raise SendUncertainError(
            f"云信发送回执异常（status={status}，无服务器消息号）；请人工核对是否补发")

    def send_mention(self, staff, text):
        raise AdapterConfigurationError("云信通道暂不支持真实 @，请在微信端提醒客服")
