"""用合成 PE 镜像验证静态扫描边界；不读取或改动任何运行进程。"""

import hashlib
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from accessibility_gate import GateScanError, locate_accessibility_gate, parse_pe


class SyntheticPE:
    """三个真实节布局，文件偏移故意不同于 RVA，包含零填充数据尾部。"""

    pe_offset = 0x80
    optional_offset = pe_offset + 24
    section_table = optional_offset + 0xF0
    text_raw = 0x400
    text_rva = 0x1000
    rdata_raw = 0xC00
    data_raw = 0xE00

    def __init__(self, value=1, data_rva=0x3000, rdata_rva=0x2000):
        self.data_rva = data_rva
        self.rdata_rva = rdata_rva
        self.buffer = bytearray(0x1000)
        self.buffer[:2] = b"MZ"
        struct.pack_into("<I", self.buffer, 0x3C, self.pe_offset)
        self.buffer[self.pe_offset:self.pe_offset + 4] = b"PE\0\0"
        struct.pack_into("<HH", self.buffer, self.pe_offset + 4, 0x8664, 3)
        struct.pack_into("<H", self.buffer, self.pe_offset + 20, 0xF0)
        struct.pack_into("<H", self.buffer, self.optional_offset, 0x20B)
        struct.pack_into("<Q", self.buffer, self.optional_offset + 24, 0x180000000)
        struct.pack_into("<II", self.buffer, self.optional_offset + 56, 0x4000, 0x400)
        self.section(0, b".text", 0x800, self.text_rva, 0x800, self.text_raw, 0x60000020)
        self.section(1, b".rdata", 0x200, rdata_rva, 0x200, self.rdata_raw, 0x40000040)
        self.section(2, b".data", 0x400, data_rva, 0x200, self.data_raw, 0xC0000040)
        category = b"qt.accessibility.core\0"
        self.buffer[self.rdata_raw + 0x20:self.rdata_raw + 0x20 + len(category)] = category
        self.lea(0x20, rdata_rva + 0x20)
        self.gate(0x80, data_rva + 0x40)
        self.buffer[self.data_raw + 0x40] = value

    def section(self, index, name, virtual_size, rva, raw_size, raw_offset, flags):
        cursor = self.section_table + index * 40
        struct.pack_into("<8sIIII", self.buffer, cursor, name, virtual_size, rva, raw_size, raw_offset)
        struct.pack_into("<I", self.buffer, cursor + 36, flags)

    def lea(self, text_offset, target_rva, rex=True):
        instruction_rva = self.text_rva + text_offset
        prefix = b"\x48\x8d\x0d" if rex else b"\x8d\x0d"
        instruction = prefix + struct.pack("<i", target_rva - instruction_rva - len(prefix) - 4)
        offset = self.text_raw + text_offset
        self.buffer[offset:offset + len(instruction)] = instruction

    def gate(self, text_offset, target_rva):
        instruction_rva = self.text_rva + text_offset
        instruction = (b"\x48\x85\xc9\x0f\x84\x00\x00\x00\x00\x80\x3d"
                       + struct.pack("<i", target_rva - instruction_rva - 16)
                       + b"\x00\x0f\x84\x00\x00\x00\x00")
        offset = self.text_raw + text_offset
        self.buffer[offset:offset + len(instruction)] = instruction

    def bytes(self):
        return bytes(self.buffer)


class PEParsingTests(unittest.TestCase):
    def test_virtual_addresses_map_to_raw_offsets_and_zero_fill(self):
        pe = parse_pe(SyntheticPE().bytes())
        self.assertEqual(pe.image_base, 0x180000000)
        self.assertEqual(pe.image_size, 0x4000)
        self.assertEqual(pe.file_offset(0x1080), 0x480)
        self.assertEqual(pe.file_offset(0x3040), 0xE40)
        self.assertEqual(pe.file_offset(0x31FF), 0xFFF)
        self.assertIsNone(pe.file_offset(0x3200))
        self.assertIsNone(pe.file_offset(0x33FF))
        for rva in (0x400, 0x1800, 0x2800, 0x3400, 0x4000, -1):
            with self.subTest(rva=rva), self.assertRaises(GateScanError):
                pe.file_offset(rva)

    def test_accepts_initialized_data_raw_padding(self):
        image = SyntheticPE()
        struct.pack_into("<I", image.buffer, image.section_table + 80 + 8, 0x100)
        self.assertEqual(parse_pe(image.bytes()).file_offset(0x31FF), 0xFFF)

    def test_rejects_x86_and_arm64(self):
        for architecture in (0x14C, 0xAA64, 0):
            image = SyntheticPE()
            struct.pack_into("<H", image.buffer, image.pe_offset + 4, architecture)
            with self.subTest(machine=architecture), self.assertRaisesRegex(GateScanError, "AMD64"):
                parse_pe(image.bytes())

    def test_rejects_pe32_optional_header(self):
        image = SyntheticPE()
        struct.pack_into("<H", image.buffer, image.optional_offset, 0x10B)
        with self.assertRaisesRegex(GateScanError, "PE32"):
            parse_pe(image.bytes())

    def test_rejects_missing_signatures_and_invalid_header_pointer(self):
        for offset, value in ((0, b"XX"), (0x80, b"NOPE"), (0x3C, b"\xff\xff\xff\xff")):
            image = SyntheticPE()
            image.buffer[offset:offset + len(value)] = value
            with self.subTest(offset=offset), self.assertRaises(GateScanError):
                parse_pe(image.bytes())

    def test_rejects_truncated_images(self):
        image = SyntheticPE().bytes()
        for length in (0, 2, 0x3F, 0x84, 0x100, 0x200, 0xFFF):
            with self.subTest(length=length), self.assertRaises(GateScanError):
                parse_pe(image[:length])

    def test_rejects_bad_section_counts_and_short_optional_header(self):
        for offset, value in ((0x86, 0), (0x86, 97), (0x94, 111)):
            image = SyntheticPE()
            struct.pack_into("<H", image.buffer, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(GateScanError):
                parse_pe(image.bytes())

    def test_rejects_header_overlapping_section_table_or_exceeding_image(self):
        for headers_size, image_size in ((0x180, 0x4000), (0x2000, 0x4000), (0x400, 0x200)):
            image = SyntheticPE()
            struct.pack_into("<II", image.buffer, image.optional_offset + 56, image_size, headers_size)
            with self.subTest(headers=headers_size, image=image_size), self.assertRaises(GateScanError):
                parse_pe(image.bytes())

    def test_rejects_raw_and_virtual_section_overlap(self):
        for field_offset, replacement in ((20, 0xD00), (12, 0x2100)):
            image = SyntheticPE()
            struct.pack_into("<I", image.buffer, image.section_table + 80 + field_offset, replacement)
            with self.subTest(field=field_offset), self.assertRaisesRegex(GateScanError, "重叠"):
                parse_pe(image.bytes())

    def test_rejects_section_ranges_before_headers_or_past_limits(self):
        for field_offset, replacement in ((12, 0x200), (12, 0x3F00), (20, 0x200), (20, 0xF00)):
            image = SyntheticPE()
            struct.pack_into("<I", image.buffer, image.section_table + 80 + field_offset, replacement)
            with self.subTest(field=field_offset, replacement=replacement), self.assertRaisesRegex(GateScanError, "越界"):
                parse_pe(image.bytes())


class GateLocationTests(unittest.TestCase):
    def test_unique_target_returns_complete_evidence_and_file_hash(self):
        image = SyntheticPE()
        candidate = locate_accessibility_gate(image.bytes())
        self.assertEqual(candidate.target_rva, 0x3040)
        self.assertEqual(candidate.target_section, ".data")
        self.assertEqual(candidate.disk_byte, 1)
        self.assertEqual(candidate.sha256, hashlib.sha256(image.bytes()).hexdigest())
        self.assertEqual(candidate.semantic_status, "structural_candidate_only")
        self.assertTrue(any(item.instruction_rva == 0x1080 and item.string_rva == 0x2020
                            and item.string_reference_rva == 0x1020 for item in candidate.evidence))
        for evidence in candidate.evidence:
            self.assertEqual(bytes.fromhex(evidence.instruction_hex), image.bytes()[0x480:0x496])

    def test_accepts_zero_and_one_initial_values(self):
        for value in (0, 1):
            with self.subTest(value=value):
                self.assertEqual(locate_accessibility_gate(SyntheticPE(value).bytes()).disk_byte, value)

    def test_rejects_non_boolean_initial_value(self):
        for value in (2, 127, 255):
            with self.subTest(value=value), self.assertRaisesRegex(GateScanError, "布尔值"):
                locate_accessibility_gate(SyntheticPE(value).bytes())

    def test_accepts_virtual_zero_fill_without_claiming_disk_value(self):
        image = SyntheticPE()
        image.gate(0x80, 0x3280)
        candidate = locate_accessibility_gate(image.bytes())
        self.assertEqual(candidate.target_rva, 0x3280)
        self.assertIsNone(candidate.disk_byte)

    def test_multiple_evidence_for_same_target_is_allowed(self):
        image = SyntheticPE()
        image.gate(0x180, 0x3040)
        candidate = locate_accessibility_gate(image.bytes())
        self.assertEqual(candidate.target_rva, 0x3040)
        self.assertEqual({e.instruction_rva for e in candidate.evidence}, {0x1080, 0x1180})

    def test_rejects_multiple_distinct_targets_instead_of_choosing_nearest(self):
        image = SyntheticPE()
        image.gate(0x180, 0x3041)
        with self.assertRaisesRegex(GateScanError, "多个不同"):
            locate_accessibility_gate(image.bytes())

    def test_rejects_code_readonly_unmapped_and_executable_data_targets(self):
        for target in (0x1080, 0x2040, 0x2800, 0x4500):
            image = SyntheticPE()
            image.gate(0x80, target)
            with self.subTest(target=target), self.assertRaisesRegex(GateScanError, "候选"):
                locate_accessibility_gate(image.bytes())
        image = SyntheticPE()
        struct.pack_into("<I", image.buffer, image.section_table + 80 + 36, 0xE0000040)
        with self.assertRaisesRegex(GateScanError, "候选"):
            locate_accessibility_gate(image.bytes())

    def test_gate_and_string_rip_displacements_are_signed(self):
        for image in (SyntheticPE(data_rva=0x800), SyntheticPE(rdata_rva=0x800)):
            with self.subTest(data_rva=image.data_rva, rdata_rva=image.rdata_rva):
                self.assertEqual(locate_accessibility_gate(image.bytes()).target_rva, image.data_rva + 0x40)

    def test_supports_non_rex_rip_reference(self):
        image = SyntheticPE()
        image.buffer[0x420:0x427] = bytes(7)
        image.lea(0x20, 0x2020, rex=False)
        self.assertEqual(locate_accessibility_gate(image.bytes()).target_rva, 0x3040)

    def test_rejects_missing_string_reference_and_partial_gate(self):
        for offset, length in ((0xC20, 21), (0x420, 7), (0x480, 3), (0x490, 2)):
            image = SyntheticPE()
            image.buffer[offset:offset + length] = bytes(length)
            with self.subTest(offset=offset), self.assertRaises(GateScanError):
                locate_accessibility_gate(image.bytes())

    def test_ignores_gate_bytes_outside_executable_section(self):
        image = SyntheticPE()
        struct.pack_into("<I", image.buffer, image.section_table + 36, 0x40000040)
        with self.assertRaisesRegex(GateScanError, "RIP"):
            locate_accessibility_gate(image.bytes())

    def test_rejects_distant_evidence_and_invalid_distance(self):
        image = SyntheticPE().bytes()
        with self.assertRaisesRegex(GateScanError, "候选"):
            locate_accessibility_gate(image, max_distance=1)
        for distance in (-1, "10", None, 1.5):
            with self.subTest(distance=distance), self.assertRaisesRegex(GateScanError, "非负整数"):
                locate_accessibility_gate(image, max_distance=distance)


if __name__ == "__main__":
    unittest.main()
