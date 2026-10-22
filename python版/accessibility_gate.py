"""只读定位微信 DLL 中的无障碍门控候选，不包含进程读写操作。

独立实现 PE 解析及公开指令特征检查，参考来源：
https://learn.microsoft.com/windows/win32/debug/pe-format
https://github.com/scottfly189/WeChatAuto.SDK/blob/c98639483b7d5ef08b87e0c70a65e13c6123213c/WeChatAuto4_X/WeChatAuto/Utils/ForceOpenUITree.cs

唯一结构匹配不等于语义证明。Qt 官方的实例 m_active 与 Windows 后端
静态 m_accessibleActive 是不同字段，调用方不得仅凭扫描结果写进程内存。
"""

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
from typing import Optional, Tuple


class GateScanError(ValueError):
    """镜像不符合要求或候选证据不唯一。"""


@dataclass(frozen=True)
class Section:
    name: str
    rva: int
    virtual_size: int
    raw_offset: int
    raw_size: int
    flags: int

    @property
    def executable(self):
        return bool(self.flags & 0x20000000)

    @property
    def writable(self):
        return bool(self.flags & 0x80000000)

    def contains(self, rva):
        return self.rva <= rva < self.rva + max(self.virtual_size, self.raw_size)


@dataclass(frozen=True)
class PEImage:
    sections: Tuple[Section, ...]
    image_size: int
    image_base: int

    def section_at(self, rva):
        found = [section for section in self.sections if section.contains(rva)]
        if len(found) != 1:
            raise GateScanError("目标 RVA 不在唯一映像节中")
        return found[0]

    def file_offset(self, rva):
        section = self.section_at(rva)
        delta = rva - section.rva
        return section.raw_offset + delta if delta < section.raw_size else None


@dataclass(frozen=True)
class Evidence:
    instruction_rva: int
    string_reference_rva: int
    string_rva: int
    distance: int
    instruction_hex: str


@dataclass(frozen=True)
class GateCandidate:
    target_rva: int
    target_section: str
    disk_byte: Optional[int]
    image_size: int
    image_base: int
    sha256: str
    evidence: Tuple[Evidence, ...]
    semantic_status: str = "structural_candidate_only"


def _unpack(data, offset, fmt):
    size = struct.calcsize(fmt)
    if offset < 0 or offset + size > len(data):
        raise GateScanError("PE 文件截断，字段超出文件范围")
    return struct.unpack_from(fmt, data, offset)


def parse_pe(data):
    """严格接受 AMD64 PE32+；拒绝越界、重叠节与不合法镜像大小。"""
    if data[:2] != b"MZ":
        raise GateScanError("缺少 DOS MZ 标识")
    pe, = _unpack(data, 0x3C, "<I")
    if data[pe:pe + 4] != b"PE\0\0":
        raise GateScanError("缺少 PE 标识")
    machine, count = _unpack(data, pe + 4, "<HH")
    optional_size, = _unpack(data, pe + 20, "<H")
    if machine != 0x8664:
        raise GateScanError("只支持 AMD64 架构，不接受 x86 或 ARM64")
    if not 1 <= count <= 96 or optional_size < 112:
        raise GateScanError("PE 节数量或可选头长度不合法")
    optional = pe + 24
    magic, = _unpack(data, optional, "<H")
    if magic != 0x20B:
        raise GateScanError("只支持 PE32+ 可选头")
    image_base, = _unpack(data, optional + 24, "<Q")
    image_size, headers_size = _unpack(data, optional + 56, "<II")
    table = optional + optional_size
    if not table + 40 * count <= headers_size <= len(data) or headers_size > image_size:
        raise GateScanError("PE 头或映像大小不合法")
    sections = []
    raw_ranges = []
    virtual_ranges = []
    for index in range(count):
        cursor = table + index * 40
        raw_name, virtual_size, rva, raw_size, raw_offset = _unpack(data, cursor, "<8sIIII")
        flags, = _unpack(data, cursor + 36, "<I")
        name = raw_name.rstrip(b"\0").decode("ascii", errors="replace")
        span = max(virtual_size, raw_size)
        if span and (rva < headers_size or rva + span > image_size):
            raise GateScanError("PE 节的虚拟范围越界")
        if raw_size and (raw_offset < headers_size or raw_offset + raw_size > len(data)):
            raise GateScanError("PE 节的文件范围越界")
        if raw_size:
            raw_ranges.append((raw_offset, raw_offset + raw_size))
        if span:
            virtual_ranges.append((rva, rva + span))
        sections.append(Section(name, rva, virtual_size, raw_offset, raw_size, flags))
    for spans in (raw_ranges, virtual_ranges):
        spans.sort()
        if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
            raise GateScanError("PE 节范围重叠，无法可靠映射")
    return PEImage(tuple(sections), image_size, image_base)


# 仅匹配完整的两条条件跳转形态，不把任意字节比较当成候选。
_GATE = re.compile(rb"\x48\x85\xc9\x0f\x84.{4}\x80\x3d.{4}\x00\x0f\x84.{4}", re.DOTALL)
_LEA = re.compile(rb"[\x40-\x4f]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}", re.DOTALL)
_LEA_NO_REX = re.compile(rb"\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}", re.DOTALL)
_CATEGORY = b"qt.accessibility.core\0"


def locate_accessibility_gate(data, max_distance=0x20000):
    """返回唯一结构候选；多个不同地址一律拒绝，不采用最近优先策略。"""
    if not isinstance(max_distance, int) or max_distance < 0:
        raise GateScanError("最大证据距离必须是非负整数")
    pe = parse_pe(data)
    string_rvas = set()
    for section in pe.sections:
        cursor = section.raw_offset
        end = cursor + section.raw_size
        while cursor < end:
            offset = data.find(_CATEGORY, cursor, end)
            if offset < 0:
                break
            string_rvas.add(section.rva + offset - section.raw_offset)
            cursor = offset + 1
    if not string_rvas:
        raise GateScanError("未找到 Qt 无障碍日志类别字符串")
    references = set()
    code = []
    for section in pe.sections:
        if not section.executable:
            continue
        block = data[section.raw_offset:section.raw_offset + section.raw_size]
        code.append((section, block))
        for expression, displacement_offset, size in ((_LEA, 3, 7), (_LEA_NO_REX, 2, 6)):
            for match in expression.finditer(block):
                offset = match.start()
                displacement, = struct.unpack_from("<i", block, offset + displacement_offset)
                instruction_rva = section.rva + offset
                target = instruction_rva + size + displacement
                if target in string_rvas:
                    references.add((instruction_rva, target))
    if not references:
        raise GateScanError("执行节中没有指向无障碍日志类别的 RIP 引用")
    candidates = {}
    invalid_targets = []
    for section, block in code:
        for match in _GATE.finditer(block):
            instruction_rva = section.rva + match.start()
            nearby = [(location, target) for location, target in references
                      if abs(location - instruction_rva) <= max_distance]
            if not nearby:
                continue
            displacement, = struct.unpack_from("<i", block, match.start() + 11)
            target_rva = instruction_rva + 16 + displacement
            try:
                target_section = pe.section_at(target_rva)
            except GateScanError:
                invalid_targets.append(target_rva)
                continue
            if not target_section.writable or target_section.executable:
                invalid_targets.append(target_rva)
                continue
            evidence = candidates.setdefault(target_rva, [])
            for location, string_rva in nearby:
                evidence.append(Evidence(instruction_rva, location, string_rva,
                                         abs(location - instruction_rva), match.group().hex()))
    if not candidates:
        detail = "；发现代码节、只读节或越界目标" if invalid_targets else ""
        raise GateScanError("未找到可写且不可执行节中的门控候选" + detail)
    if len(candidates) != 1:
        addresses = ", ".join(f"0x{rva:X}" for rva in sorted(candidates))
        raise GateScanError(f"存在多个不同门控候选，拒绝按距离猜测：{addresses}")
    target_rva, evidence = next(iter(candidates.items()))
    target_section = pe.section_at(target_rva)
    file_offset = pe.file_offset(target_rva)
    disk_byte = data[file_offset] if file_offset is not None else None
    if disk_byte not in (None, 0, 1):
        raise GateScanError("候选字段的磁盘初值不是布尔值 0/1")
    return GateCandidate(target_rva, target_section.name, disk_byte, pe.image_size,
                         pe.image_base, hashlib.sha256(data).hexdigest(),
                         tuple(sorted(evidence, key=lambda item: (item.instruction_rva, item.string_reference_rva))))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读扫描 PE 文件中的无障碍门控候选，不读写运行进程")
    parser.add_argument("dll", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(asdict(locate_accessibility_gate(args.dll.read_bytes())), ensure_ascii=False, indent=2))
    except (OSError, GateScanError) as exc:
        parser.exit(1, f"静态扫描未通过：{exc}\n")
