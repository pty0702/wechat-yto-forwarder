# -*- coding: utf-8 -*-
"""卡密验证客户端：激活、状态查询、离线容错与响应签名校验。

服务端为 cardserver（Python stdlib HTTP + SQLite + HMAC-SHA256）。
首次激活把卡密绑定到本机 machine_id；服务端不可达时，本地缓存仍在
有效期内容许离线运行。缓存写在用户 LOCALAPPDATA 下，避免绿色目录
被设为只读时无法容错。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

SERVER_URL = "http://82.157.64.196:8848"
HMAC_SECRET = "fcd83908ed4547412b6089f9044bef931de3cfe64fa170111bcec7c92accb8f4"
_CACHE_NAME = "license.json"
_TIMEOUT = 8


@dataclass
class LicenseStatus:
    """一次验证的结果；offline=True 表示走离线容错（缓存未过期）。"""
    ok: bool
    expire_at: float = 0.0
    message: str = ""
    offline: bool = False


def machine_id() -> str:
    """本机唯一标识：Windows 读注册表 MachineGuid，其余平台回退哈希。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        return "win-" + str(guid).replace("-", "")
    except OSError:
        pass
    except ImportError:
        pass
    try:
        out = subprocess.run(
            ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid"],
            capture_output=True, text=True, timeout=5).stdout
        for part in out.replace(" ", "").split():
            if part.count("-") == 4 and len(part) == 36:
                return "win-" + part.replace("-", "")
    except Exception:
        pass
    host = socket.gethostname()
    mac = ":".join(format(byte, "02x") for byte in uuid.getnode().to_bytes(6, "big"))
    return "h-" + hashlib.sha256((host + mac).encode()).hexdigest()[:16]


def _verify_signature(raw_body: bytes, sig_header: str) -> bool:
    """响应体 HMAC-SHA256 签名校验，防伪造响应。"""
    if not sig_header:
        return False
    calc = hmac.new(HMAC_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, sig_header.strip())


def _post(path: str, payload: dict):
    """POST JSON 并返回 (解析结果, 原始字节, 签名头)；网络异常向上抛 OSError。"""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        SERVER_URL + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        raw = response.read()
        return json.loads(raw.decode("utf-8")), raw, response.headers.get("X-Sig", "")


def _request(path: str, key: str) -> LicenseStatus:
    """带签名校验的一次请求；签名不符按失败处理，不给容错。"""
    result, raw, sig = _post(path, {"key": key, "machine_id": machine_id()})
    if not _verify_signature(raw, sig):
        return LicenseStatus(False, message="服务器签名校验失败")
    if not result.get("ok"):
        return LicenseStatus(False, message=str(result.get("error", "卡密无效")))
    try:
        expire = float(result.get("expire_at") or 0)
    except (TypeError, ValueError):
        expire = 0.0
    return LicenseStatus(True, expire_at=expire)


def cache_path() -> Path:
    """许可证缓存文件位置：用户 LOCALAPPDATA 下独立目录。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "YtoForwarder" / _CACHE_NAME


def load_cache() -> dict:
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(key: str, expire_at: float) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "key": key, "machine_id": machine_id(),
        "expire_at": expire_at, "saved_at": time.time(),
    }, ensure_ascii=False), encoding="utf-8")


def activate(key: str) -> LicenseStatus:
    """首次激活（绑定本机并开始计时）；成功后写本地缓存。"""
    try:
        status = _request("/api/verify", key)
    except OSError as exc:
        return LicenseStatus(False, message=f"无法连接验证服务器：{exc}")
    if status.ok:
        save_cache(key, status.expire_at)
    return status


def check(key: str) -> LicenseStatus:
    """查询卡密状态（不激活、不消耗时长）。"""
    try:
        return _request("/api/check", key)
    except OSError as exc:
        return LicenseStatus(False, message=f"无法连接验证服务器：{exc}")


def ensure_licensed(key: str | None = None) -> LicenseStatus:
    """入口判定：有缓存先走 check，服务器不可达时按缓存剩余时间离线容错。

    key 显式传入时等价于激活入口（首次使用）。
    """
    cached = load_cache()
    key = key or str(cached.get("key") or "")
    if not key:
        return LicenseStatus(False, message="未找到已激活的卡密")
    status = check(key)
    if status.ok:
        save_cache(key, status.expire_at)
        return status
    # 服务器不可达时不直接拒绝：本地缓存仍在有效期内容许离线运行。
    expire = float(cached.get("expire_at") or 0)
    if status.message.startswith("无法连接") and time.time() < expire:
        return LicenseStatus(True, expire_at=expire, offline=True,
                             message="验证服务器不可达，离线容错运行中")
    return status
