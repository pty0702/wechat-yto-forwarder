# -*- coding: utf-8 -*-
"""配置迁移到用户数据目录（C 盘）的单测：老位置自动搬迁，新位置优先。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wechat_forwarder  # noqa: E402


class ConfigMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        data_dir = root / "appdata"
        legacy_dir = root / "legacy"
        legacy_dir.mkdir()
        # profile 文件放老目录里
        self.profile = legacy_dir / "wizard.generated.json"
        self.profile.write_text(json.dumps(
            {"kf": {"parser": "yto_dom", "self_names": ["小圆"],
                    "cdp_url": "http://127.0.0.1:9223"},
             "wx": {"parser": "wechat_db", "self_names": ["昵称A"]}},
            ensure_ascii=False), encoding="utf-8")
        (legacy_dir / "config.json").write_text(json.dumps({
            "wx_group": "老群", "kf_group": "客服群", "bot_name": "小圆-总公司",
            "adapter_profile": str(self.profile)}, ensure_ascii=False),
            encoding="utf-8")
        patcher = mock.patch.multiple(
            wechat_forwarder, DATA_DIR=str(data_dir),
            CONFIG_FILE=str(data_dir / "config.json"),
            _LEGACY_CONFIG_FILE=str(legacy_dir / "config.json"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_migrate_moves_config_and_profile(self):
        wechat_forwarder._migrate_legacy_config()
        cfg = json.loads(Path(wechat_forwarder.CONFIG_FILE).read_text(encoding="utf-8"))
        self.assertEqual(cfg["wx_group"], "老群")
        # profile 复制到数据目录，config 里的路径改写指向新位置
        new_profile = Path(cfg["adapter_profile"])
        self.assertEqual(new_profile.parent.name, "profiles")
        self.assertTrue(new_profile.is_file())
        self.assertNotEqual(str(new_profile), str(self.profile))

    def test_no_migrate_when_target_exists(self):
        Path(wechat_forwarder.DATA_DIR).mkdir(parents=True, exist_ok=True)
        Path(wechat_forwarder.CONFIG_FILE).write_text(json.dumps(
            {"wx_group": "新配置"}), encoding="utf-8")
        wechat_forwarder._migrate_legacy_config()
        cfg = json.loads(Path(wechat_forwarder.CONFIG_FILE).read_text(encoding="utf-8"))
        self.assertEqual(cfg["wx_group"], "新配置")


if __name__ == "__main__":
    unittest.main()
