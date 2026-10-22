"""按完整运行 ID 对照微信普通态与多选态消息，不访问客户端。

多选名称必须以一个 ASCII 空格加原始完整正文结尾。仅凭这两份文本，
无法区分带空格的昵称与正文前新增的同类前缀；调用方仍须核验发送者身份。
"""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class WxRow:
    """普通态 content 是正文，多选态 content 是完整 UIA Name。"""

    runtime_id: tuple[int, ...]
    content: str
    kind: str = "text"


class SenderMappingError(ValueError):
    """两份消息快照无法完整、精确地对应。"""


def _text_rows(rows: Iterable[WxRow], label: str) -> dict[tuple[int, ...], WxRow]:
    """验证全部行的身份，再取出文本行，避免过滤掩盖重复身份。"""
    try:
        iterator = iter(rows)
    except TypeError as exc:
        raise SenderMappingError(f"{label}必须是 WxRow 可迭代对象") from exc

    result: dict[tuple[int, ...], WxRow] = {}
    seen: set[tuple[int, ...]] = set()
    for row in iterator:
        if not isinstance(row, WxRow):
            raise SenderMappingError(f"{label}包含非 WxRow 数据")
        runtime_id = row.runtime_id
        if (
            not isinstance(runtime_id, tuple)
            or not runtime_id
            or any(type(part) is not int for part in runtime_id)
        ):
            raise SenderMappingError(f"{label}的 RuntimeId 必须是非空整数元组")
        if runtime_id in seen:
            raise SenderMappingError(f"{label}包含重复 RuntimeId")
        seen.add(runtime_id)
        if not isinstance(row.content, str) or not isinstance(row.kind, str):
            raise SenderMappingError(f"{label}的 content 和 kind 必须是字符串")
        if row.kind != "text":
            continue
        if not row.content:
            raise SenderMappingError(f"{label}包含空文本行，无法精确拆分发送者")
        result[runtime_id] = row
    return result


def sender_mapping(
    normal_rows: Iterable[WxRow], selected_rows: Iterable[WxRow]
) -> dict[tuple[int, ...], str]:
    """返回完整 RuntimeId 到发送者名称的映射，任何错配都整批拒绝。

    两侧 kind != "text" 的行不参与映射，但其 RuntimeId 仍须合法且唯一。
    文本行的 ID 集合必须相同，行顺序可以不同；空的文本集合返回空字典。
    只剥除一次 ``" " + 原始正文`` 后缀，不拆分、清理或归一化昵称及正文。
    后缀不一致、无分隔空格、昵称为空或全为空白时抛 SenderMappingError。
    此函数不把名称当成唯一账号身份，也不判断该名称是否代表本人。
    """
    normal = _text_rows(normal_rows, "普通态快照")
    selected = _text_rows(selected_rows, "多选态快照")
    if normal.keys() != selected.keys():
        missing = len(normal.keys() - selected.keys())
        extra = len(selected.keys() - normal.keys())
        raise SenderMappingError(
            f"两态文本 RuntimeId 不一致：缺失 {missing} 条，多余 {extra} 条"
        )

    result: dict[tuple[int, ...], str] = {}
    for runtime_id, row in normal.items():
        selected_content = selected[runtime_id].content
        suffix = " " + row.content
        if not selected_content.endswith(suffix):
            raise SenderMappingError("多选名称未保留分隔空格和完整原正文后缀")
        sender = selected_content[:-len(suffix)]
        if not sender.strip():
            raise SenderMappingError("多选名称中的发送者为空或全为空白")
        result[runtime_id] = sender
    return result
