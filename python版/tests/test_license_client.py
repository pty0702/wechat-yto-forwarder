# -*- coding: utf-8 -*-
"""license_client 单元测试：签名校验、激活缓存、离线容错与无效卡密分支。"""

import hmac
import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import license_client  # noqa: E402


def _signed(payload: dict):
    """按服务端算法生成带 X-Sig 的伪响应。"""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sig = hmac.new(license_client.HMAC_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, sig


class _Response:
    def __init__(self, raw, sig):
        self._raw = raw
        self.headers = {"X-Sig": sig}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class SignatureTests(unittest.TestCase):
    def test_verify_signature_accepts_valid(self):
        raw, sig = _signed({"ok": True})
        self.assertTrue(license_client._verify_signature(raw, sig))

    def test_verify_signature_rejects_tampered_or_missing(self):
        raw, sig = _signed({"ok": True})
        self.assertFalse(license_client._verify_signature(raw + b"x", sig))
        self.assertFalse(license_client._verify_signature(raw, ""))
        self.assertFalse(license_client._verify_signature(raw, "0" * 64))


class RequestTests(unittest.TestCase):
    def test_request_success_parses_expire(self):
        raw, sig = _signed({"ok": True, "expire_at": 1893456000})
        with mock.patch.object(license_client, "_post", return_value=(
                json.loads(raw), raw, sig)):
            status = license_client._request("/api/check", "AG-2D-TESTKEY1")
        self.assertTrue(status.ok)
        self.assertEqual(status.expire_at, 1893456000)

    def test_request_rejects_bad_signature(self):
        raw, _sig = _signed({"ok": True, "expire_at": 1})
        with mock.patch.object(license_client, "_post", return_value=(
                json.loads(raw), raw, "deadbeef")):
            status = license_client._request("/api/verify", "AG-2D-TESTKEY1")
        self.assertFalse(status.ok)
        self.assertIn("签名", status.message)

    def test_request_maps_card_error(self):
        raw, sig = _signed({"ok": False, "error": "卡密已绑定其他设备"})
        with mock.patch.object(license_client, "_post", return_value=(
                json.loads(raw), raw, sig)):
            status = license_client._request("/api/verify", "AG-2D-TESTKEY1")
        self.assertFalse(status.ok)
        self.assertEqual(status.message, "卡密已绑定其他设备")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(license_client, "cache_path",
                                    return_value=Path(self._tmp.name) / "license.json")
        self.cache_path = patcher.start()
        self.addCleanup(patcher.stop)

    def test_activate_success_writes_cache(self):
        raw, sig = _signed({"ok": True, "expire_at": 1893456000})
        with mock.patch.object(license_client, "_post", return_value=(
                json.loads(raw), raw, sig)), \
                mock.patch.object(license_client, "machine_id", return_value="win-test"):
            status = license_client.activate("AG-2D-TESTKEY1")
        self.assertTrue(status.ok)
        cached = license_client.load_cache()
        self.assertEqual(cached["key"], "AG-2D-TESTKEY1")
        self.assertEqual(cached["machine_id"], "win-test")
        self.assertEqual(cached["expire_at"], 1893456000)

    def test_activate_network_failure_keeps_cache_untouched(self):
        def boom(*_args, **_kwargs):
            raise OSError("refused")
        with mock.patch.object(license_client, "_post", side_effect=boom):
            status = license_client.activate("AG-2D-TESTKEY1")
        self.assertFalse(status.ok)
        self.assertIn("无法连接", status.message)
        self.assertEqual(license_client.load_cache(), {})

    def test_ensure_licensed_offline_grace_within_window(self):
        license_client.save_cache("AG-2D-TESTKEY1", time.time() + 3600)

        def offline(*_args, **_kwargs):
            raise OSError("timeout")
        with mock.patch.object(license_client, "_post", side_effect=offline):
            status = license_client.ensure_licensed()
        self.assertTrue(status.ok)
        self.assertTrue(status.offline)
        self.assertIn("离线", status.message)

    def test_ensure_licensed_offline_expired_rejected(self):
        license_client.save_cache("AG-2D-TESTKEY1", time.time() - 10)

        def offline(*_args, **_kwargs):
            raise OSError("timeout")
        with mock.patch.object(license_client, "_post", side_effect=offline):
            status = license_client.ensure_licensed()
        self.assertFalse(status.ok)

    def test_ensure_licensed_without_key_asks_activation(self):
        status = license_client.ensure_licensed()
        self.assertFalse(status.ok)
        self.assertEqual(status.message, "未找到已激活的卡密")


class MachineIdTests(unittest.TestCase):
    def test_machine_id_has_stable_prefix(self):
        value = license_client.machine_id()
        self.assertTrue(value.startswith(("win-", "h-")))
        self.assertEqual(license_client.machine_id(), value)


if __name__ == "__main__":
    unittest.main()
