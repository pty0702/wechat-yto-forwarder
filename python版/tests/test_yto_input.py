"""使用合成控件验证客户管家 news-box 消息包装布局。"""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import windows_adapter as adapter
from test_adapter import Node, profile


def wrapped_message():
    header = Node(children=[Node("客户 张三", kind="TextControl"),
                            Node("今天 10:35:12", kind="TextControl")])
    body = Node(children=[Node("YT0719078698064 第一行", kind="TextControl"),
                          Node("第二行", kind="TextControl")])
    body.ClassName = "text-content w-s-pre-line"
    box = Node(children=[header, body])
    box.ClassName = "d-flex news-box flex-column"
    avatar = Node(children=[Node("头像", kind="ImageControl")])
    row = Node(automation_id="123456", children=[
        Node("2026年10月23日", kind="TextControl"), avatar, box,
    ])
    return row, box, header, body


class YtoWrappedMessageTests(unittest.TestCase):
    def parse(self, row):
        return adapter.parse_message(row, dict(profile(), parser="yto"), "客户管家")

    def test_sender_and_body_exclude_header_time_and_outer_date(self):
        row, _, _, _ = wrapped_message()
        parsed = self.parse(row)
        self.assertEqual(parsed.sender, "客户 张三")
        self.assertEqual(parsed.content, "YT0719078698064 第一行\n第二行")
        self.assertTrue(parsed.message_id.startswith("客户管家:123456:"))

    def test_avatar_image_does_not_turn_plain_body_into_media(self):
        row, box, _, _ = wrapped_message()
        box.children.insert(0, Node(children=[Node("内侧头像", kind="ImageControl")]))
        self.assertEqual(self.parse(row).kind, "text")

    def test_card_button_inside_body_marks_message_as_media(self):
        row, _, _, body = wrapped_message()
        body.children.append(Node(children=[Node("查看详情", kind="ButtonControl")]))
        parsed = self.parse(row)
        self.assertEqual(parsed.sender, "客户 张三")
        self.assertEqual(parsed.kind, "media")

    def test_multiple_headers_or_news_boxes_are_rejected(self):
        for ambiguity in ("headers", "boxes"):
            row, box, _, _ = wrapped_message()
            if ambiguity == "headers":
                box.children.append(Node(children=[Node("另一个发送者", kind="TextControl")]))
            else:
                _, other_box, _, _ = wrapped_message()
                row.children.append(other_box)
            with self.subTest(ambiguity=ambiguity), self.assertRaises(adapter.AdapterConfigurationError):
                self.parse(row)


if __name__ == "__main__":
    unittest.main()
