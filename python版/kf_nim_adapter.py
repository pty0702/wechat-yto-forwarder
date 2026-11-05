# -*- coding: utf-8 -*-
"""客户管家混合通道：读取沿用 DOM 解析（稳定），发送走云信 SDK 直连。

背景（2026-11-05 实测）：DOM 通道的发送链路存在假阳性上屏、连发限流
丢消息、失败草稿卡死输入框三类问题；云信 SDK sendText 带服务器回执
（idServer/status），从机制上消除。读取侧 DOM 解析已稳定运行，且云信
本地查询接口（getLocalSessions/getLocalMsgs）在客户端后台时回调不稳，
故读取维持 DOM、仅替换发送。

- 读取：完整复用 KfDomAdapter（消息行 DOM 解析 + 基线去重）；
- 发送：页面云信实例 sendText，err 且无 idServer 才算成功；
- 页面依赖：Vue 组件 imLayout 持有云信实例（升级需跟进）。
"""

from __future__ import annotations

import json

from wechat_forwarder import RetryableSendError, SendUncertainError
from windows_adapter import AdapterConfigurationError
from kf_dom_adapter import KfDomAdapter

# 定位云信实例（幂等；只做定位与能力检查）
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
  if (typeof im.sendText !== 'function') {
    return JSON.stringify({ok: false, why: '云信实例缺少 sendText（版本可能已变）'});
  }
  window.__kfIm = im;
  return JSON.stringify({ok: true, account: im.account || ''});
})()
"""

# 群列表（localStorage 缓存的云信团队信息）
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


class KfNimAdapter(KfDomAdapter):
    """读取走 DOM（继承 KfDomAdapter），发送走云信 SDK 直连。

    2026-11-05 真机联调：发送回执 idServer/status 验证通过；接收监听
    收到真实业务消息，但 SDK 本地查询在客户端后台时回调不稳定，读取
    暂不切 SDK（云信查询通道保留在 git 历史里，稳定后可切）。
    """

    def prepare(self):
        super().prepare()  # DOM 读取基线（_known、标题校验）
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
        self.log(f"[适配] 客户管家云信发送通道已绑定 {self.group}"
                 f"（teamId={self._team_id}，账号 {self._account[:8]}…）")

    # ------------------------------------------------------------------
    # 发送（SDK 回执：err / idServer / status）
    # ------------------------------------------------------------------
    def send(self, text):
        if not isinstance(text, str) or not text:
            raise AdapterConfigurationError("发送文本不能为空")
        if not getattr(self, "_team_id", ""):
            raise AdapterConfigurationError("云信通道适配器尚未 prepare")
        receipt = json.loads(self._client().evaluate(_send_js(text, self._team_id)) or "{}")
        err = receipt.get("err")
        id_server = receipt.get("idServer")
        status = receipt.get("status")
        if not err and id_server:
            # 服务器落地凭证齐备：真成功（无需 DOM 回显确认）。
            self.log(f"[适配] 云信发送回执 idServer={id_server} status={status}")
            return
        if err:
            # SDK 明确报错：确定未发出（如被移出群、内容过长、参数错误）。
            raise RetryableSendError(f"云信发送失败：{err}")
        # err 为空但无 idServer：状态异常，无法确认是否送达。
        raise SendUncertainError(
            f"云信发送回执异常（status={status}，无服务器消息号）；请人工核对是否补发")
