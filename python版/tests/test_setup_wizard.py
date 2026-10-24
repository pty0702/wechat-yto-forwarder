# -*- coding: utf-8 -*-
"""setup_wizard 单元测试：profile 生成、写盘与 setup_needed 判定。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import setup_wizard  # noqa: E402


class BuildProfileTests(unittest.TestCase):
    def test_profile_minimal_and_loadable(self):
        profile = setup_wizard.build_profile("微信昵称A", ["小圆"], "http://127.0.0.1:9223")
        self.assertEqual(profile["wx"]["parser"], "wechat_db")
        self.assertEqual(profile["wx"]["self_names"], ["微信昵称A"])
        self.assertEqual(profile["kf"]["parser"], "yto_dom")
        self.assertEqual(profile["kf"]["self_names"], ["小圆"])
        self.assertEqual(profile["kf"]["cdp_url"], "http://127.0.0.1:9223")


class WriteSetupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        patcher = mock.patch.multiple(
            setup_wizard, CONFIG_FILE=root / "config.json",
            PROFILE_DIR=root / "profiles")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_write_setup_creates_config_and_profile(self):
        path = setup_wizard.write_setup(
            "客户群", "客服群", "圆通快递", "小圆-总公司", "微信昵称A", ["小圆"])
        self.assertTrue(path.is_file())
        profile = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(profile["wx"]["self_names"], ["微信昵称A"])
        cfg = json.loads((root := setup_wizard.CONFIG_FILE).read_text(encoding="utf-8"))
        self.assertEqual(cfg["wx_group"], "客户群")
        self.assertEqual(cfg["kf_group"], "客服群")
        self.assertEqual(cfg["mode"], "live")
        self.assertEqual(cfg["adapter_profile"], str(path))
        self.assertFalse(setup_wizard.setup_needed())

    def test_setup_needed_true_when_missing_or_incomplete(self):
        self.assertTrue(setup_wizard.setup_needed())
        setup_wizard.CONFIG_FILE.write_text(
            json.dumps({"wx_group": "a", "kf_group": "b",
                        "adapter_profile": str(setup_wizard.PROFILE_DIR / "none.json")}),
            encoding="utf-8")
        self.assertTrue(setup_wizard.setup_needed())


if __name__ == "__main__":
    unittest.main()
