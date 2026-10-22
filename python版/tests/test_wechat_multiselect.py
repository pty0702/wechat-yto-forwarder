"""验证多选快照的严格对应，不连接或操作微信。"""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wechat_multiselect import SenderMappingError, WxRow, sender_mapping


class SenderMappingTests(unittest.TestCase):
    def test_maps_reordered_rows_by_complete_runtime_id(self):
        normal = [WxRow((7, 129), "消息甲"), WxRow((7, 131), "消息乙")]
        selected = [WxRow((7, 131), "Zlient 消息乙"), WxRow((7, 129), "nide 消息甲")]
        self.assertEqual(
            sender_mapping(normal, selected), {(7, 129): "nide", (7, 131): "Zlient"}
        )

    def test_preserves_same_body_with_distinct_full_ids(self):
        normal = [WxRow((7, 1, 99), "同文"), WxRow((7, 2, 99), "同文")]
        selected = [WxRow((7, 1, 99), "甲 同文"), WxRow((7, 2, 99), "乙 同文")]
        self.assertEqual(
            sender_mapping(normal, selected), {(7, 1, 99): "甲", (7, 2, 99): "乙"}
        )

    def test_preserves_nickname_spaces_emoji_and_multiline_body(self):
        sender = "A B👨‍👩‍👧‍👦"
        content = "第一行 😀\n第二行 A B"
        self.assertEqual(
            sender_mapping([WxRow((1,), content)], [WxRow((1,), sender + " " + content)]),
            {(1,): sender},
        )

    def test_removes_only_one_final_body_suffix(self):
        self.assertEqual(
            sender_mapping([WxRow((1,), "你好")], [WxRow((1,), "你好 用户 你好")]),
            {(1,): "你好 用户"},
        )

    def test_preserves_outer_nickname_whitespace(self):
        sender = " 甲 "
        self.assertEqual(
            sender_mapping([WxRow((1,), "正文")], [WxRow((1,), sender + " 正文")]),
            {(1,): sender},
        )

    def test_preserves_body_whitespace_without_normalizing(self):
        for content in (" 正文 ", "\n正文\n", "\t", " "):
            with self.subTest(content=repr(content)):
                self.assertEqual(
                    sender_mapping([WxRow((1,), content)], [WxRow((1,), "甲 " + content)]),
                    {(1,): "甲"},
                )

    def test_ignores_non_text_rows_on_both_sides(self):
        normal = [WxRow((1,), "正文"), WxRow((2,), "", "time")]
        selected = [WxRow((1,), "甲 正文"), WxRow((3,), "提示", "system")]
        self.assertEqual(sender_mapping(normal, selected), {(1,): "甲"})

    def test_empty_text_sets_produce_empty_mapping(self):
        self.assertEqual(sender_mapping([], []), {})
        self.assertEqual(sender_mapping([WxRow((1,), "", "time")], []), {})

    def test_accepts_iterators_without_modifying_rows(self):
        normal = (WxRow((1,), "正文"),)
        selected = (WxRow((1,), "甲 正文"),)
        self.assertEqual(sender_mapping(iter(normal), iter(selected)), {(1,): "甲"})
        self.assertEqual(normal[0].content, "正文")
        self.assertEqual(selected[0].content, "甲 正文")

    def test_rejects_duplicate_id_in_either_snapshot(self):
        normal = [WxRow((1,), "正文")]
        selected = [WxRow((1,), "甲 正文")]
        for rows_a, rows_b in ((normal * 2, selected), (normal, selected * 2)):
            with self.subTest(normal_count=len(rows_a), selected_count=len(rows_b)):
                with self.assertRaises(SenderMappingError):
                    sender_mapping(rows_a, rows_b)

    def test_rejects_duplicate_id_hidden_in_non_text_row(self):
        with self.assertRaises(SenderMappingError):
            sender_mapping(
                [WxRow((1,), "正文"), WxRow((1,), "", "time")],
                [WxRow((1,), "甲 正文")],
            )

    def test_rejects_missing_extra_or_replaced_text_id(self):
        normal = [WxRow((1,), "正文")]
        cases = ([], [WxRow((1,), "甲 正文"), WxRow((2,), "乙 正文")], [WxRow((2,), "甲 正文")])
        for selected in cases:
            with self.subTest(selected=selected):
                with self.assertRaises(SenderMappingError):
                    sender_mapping(normal, selected)

    def test_rejects_text_kind_changing_between_snapshots(self):
        for first_kind, second_kind in (("text", "system"), ("time", "text")):
            with self.subTest(first_kind=first_kind, second_kind=second_kind):
                with self.assertRaises(SenderMappingError):
                    sender_mapping(
                        [WxRow((1,), "正文", first_kind)],
                        [WxRow((1,), "甲 正文", second_kind)],
                    )

    def test_rejects_changed_truncated_or_extended_body_suffix(self):
        for selected_content in ("甲 原文变了", "甲 原", "甲 原文!", "甲 原文\n", "甲 原 文"):
            with self.subTest(selected_content=selected_content):
                with self.assertRaises(SenderMappingError):
                    sender_mapping([WxRow((1,), "原文")], [WxRow((1,), selected_content)])

    def test_rejects_missing_or_unobserved_separator(self):
        for selected_content in ("甲正文", "甲\t正文", "甲\u00a0正文", "甲\u2005正文"):
            with self.subTest(selected_content=selected_content):
                with self.assertRaises(SenderMappingError):
                    sender_mapping([WxRow((1,), "正文")], [WxRow((1,), selected_content)])

    def test_rejects_empty_or_whitespace_sender(self):
        for sender in ("", " ", "\t", "\n", "\u00a0"):
            with self.subTest(sender=repr(sender)):
                with self.assertRaises(SenderMappingError):
                    sender_mapping([WxRow((1,), "正文")], [WxRow((1,), sender + " 正文")])

    def test_rejects_invalid_runtime_id_values(self):
        for runtime_id in ((), [1], "1", None, (True,), (1, "2"), (1.0,)):
            with self.subTest(runtime_id=runtime_id):
                for first, second in (
                    ([WxRow(runtime_id, "正文")], [WxRow((1,), "甲 正文")]),
                    ([WxRow((1,), "正文")], [WxRow(runtime_id, "甲 正文")]),
                ):
                    with self.assertRaises(SenderMappingError):
                        sender_mapping(first, second)

    def test_rejects_empty_text_and_invalid_field_types(self):
        for row in (WxRow((1,), ""), WxRow((1,), None), WxRow((1,), "正文", None)):
            with self.subTest(row=row):
                with self.assertRaises(SenderMappingError):
                    sender_mapping([row], [WxRow((1,), "甲 正文")])
        with self.assertRaises(SenderMappingError):
            sender_mapping([WxRow((1,), "正文")], [WxRow((1,), "")])

    def test_rejects_wrong_row_and_container_types(self):
        for normal in (None, 1, [{"runtime_id": (1,), "content": "正文"}]):
            with self.subTest(normal=normal):
                with self.assertRaises(SenderMappingError):
                    sender_mapping(normal, [])

    def test_one_bad_row_rejects_the_entire_batch(self):
        normal = [WxRow((1,), "正文一"), WxRow((2,), "正文二")]
        selected = [WxRow((1,), "甲 正文一"), WxRow((2,), "乙 改动")]
        with self.assertRaises(ValueError):
            sender_mapping(normal, selected)

    def test_does_not_guess_boundary_inside_spaced_nickname(self):
        # 两份文本无法排除正文新增前缀的歧义，只保证原正文后缀完全保留。
        self.assertEqual(
            sender_mapping([WxRow((1,), "内容")], [WxRow((1,), "nide 新 内容")]),
            {(1,): "nide 新"},
        )


if __name__ == "__main__":
    unittest.main()
