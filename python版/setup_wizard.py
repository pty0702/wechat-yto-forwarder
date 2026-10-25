# -*- coding: utf-8 -*-
"""首次运行向导：环境检测与开箱配置生成。

客户拿到绿色包后第一次启动，向导检测微信与客户管家调试口，自动读取
微信昵称，生成 config.json 与最小 adapter profile（wechat_db + yto_dom
解析器只需 self_names）。生成结果交给 GUI 的「双端检查」验证。
"""

from __future__ import annotations

import json
from pathlib import Path

from wechat_forwarder import CONFIG_FILE as _FORWARDER_CONFIG

# 与主程序共用同一份 config（打包后模块目录在 _internal 下，两边必须同源）
CONFIG_FILE = Path(_FORWARDER_CONFIG)
PROFILE_DIR = CONFIG_FILE.parent / "profiles"
DEFAULT_CDP = "http://127.0.0.1:9223"


def detect_wechat_running() -> bool:
    """微信主进程是否在本机运行（Weixin.exe / WeChat.exe）。"""
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
        return any(f'"{name}"' in out for name in ("Weixin.exe", "WeChat.exe"))
    except Exception:
        return False


def detect_cdp(cdp_url: str = DEFAULT_CDP) -> bool:
    """客户管家 WebView2 调试口是否可达（须用附带脚本启动客户管家）。"""
    try:
        import urllib.request
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def read_wechat_self_name() -> str:
    """从微信本地库读本账号群昵称；微信未运行/库不可读返回空串。"""
    try:
        import sys
        vendor = Path(__file__).resolve().parent / "vendor"
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        from wechatauto.db import WeChatDB
        db = WeChatDB()
        return str(db.get_nickname(db.wxid) or "").strip()
    except Exception:
        return ""


def build_profile(wx_self_name: str, kf_self_names: list[str],
                  cdp_url: str = DEFAULT_CDP) -> dict:
    """最小可用 adapter profile：微信库读 + 客户管家 DOM。"""
    return {
        "_说明": "首次运行向导自动生成，可手动补充字段。",
        "wx": {"parser": "wechat_db", "self_names": [wx_self_name]},
        "kf": {"parser": "yto_dom", "self_names": kf_self_names,
               "cdp_url": cdp_url},
    }


def write_setup(wx_groups, kf_group: str, service_staff: str, bot_name: str,
                wx_self_name: str, kf_self_names: list[str],
                cdp_url: str = DEFAULT_CDP) -> Path:
    """写入 profile 与 config；wx_groups 为群名列表（至少一个）。返回 profile 路径。"""
    if isinstance(wx_groups, str):
        wx_groups = [wx_groups]
    wx_groups = [g.strip() for g in wx_groups if str(g).strip()]
    profile = build_profile(wx_self_name, kf_self_names, cdp_url)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    profile_path = PROFILE_DIR / "wizard.generated.json"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    config = {
        "wx_group": wx_groups[0] if wx_groups else "",
        "wx_groups": wx_groups,
        "group_policies": {},
        "kf_group": kf_group,
        "block_keywords": [], "at_keywords": [],
        "service_staff": service_staff, "bot_name": bot_name,
        "poll_interval": 3.0, "reset_seconds": 30.0,
        "mode": "live", "wechat_accessibility": True,
        "wx_profile": {}, "kf_profile": {},
        "adapter_profile": str(profile_path),
    }
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return profile_path


def setup_needed() -> bool:
    """已有有效 config + profile 则不需要向导。"""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    profile = str(cfg.get("adapter_profile") or "")
    return not (profile and Path(profile).is_file()
                and cfg.get("wx_group") and cfg.get("kf_group"))
