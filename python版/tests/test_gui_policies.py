# -*- coding: utf-8 -*-
"""GUI 按群覆盖解析的语义测试：空段=清空，缺段=继承全局。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import ConfigWindow  # noqa: E402


class ParsePoliciesTests(unittest.TestCase):
    def setUp(self):
        self.groups = {"A群", "B群"}

    def test_missing_segment_means_inherit(self):
        # 缺整段：B群只设屏蔽词，@词继承全局
        result = ConfigWindow._parse_policies(["B群|坏词"], self.groups)
        self.assertEqual(result, {"B群": {"block_keywords": ["坏词"]}})

    def test_empty_segment_means_clear(self):
        # 空段显式存在：清空屏蔽词（@词继承）
        result = ConfigWindow._parse_policies(["B群||催件"], self.groups)
        self.assertEqual(result, {"B群": {"block_keywords": [], "at_keywords": ["催件"]}})

    def test_roundtrip_preserves_explicit_clear(self):
        # 回显（join 空=空段）再解析：清空语义保留
        policy = {"B群": {"block_keywords": [], "at_keywords": ["催件"]}}
        lines = [f"{g}|{','.join(p.get('block_keywords', []))}|{','.join(p.get('at_keywords', []))}"
                 for g, p in policy.items()]
        result = ConfigWindow._parse_policies(lines, self.groups)
        self.assertEqual(result, policy)

    def test_unknown_group_rejected(self):
        with self.assertRaises(ValueError):
            ConfigWindow._parse_policies(["C群|词"], self.groups)


if __name__ == "__main__":
    unittest.main()
