"""Record real Unicorn block transitions without a Python callback per block."""
from pathlib import Path as _Path
import sys as _sys
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / 'tools'))
from prepare_reference import activate as _activate
_REFERENCE = _activate()

import importlib.util
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_CPSR


ROOT = Path(__file__).resolve().parents[2]
ADDRESS, RETURN, STACK = 0x10000000, 0x20000000, 0x30000000


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        specification = importlib.util.spec_from_file_location('profile_recomp', ROOT / 'tools/profile_recomp.py')
        cls.module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(cls.module)
        cls.directory = tempfile.TemporaryDirectory(prefix='doodle-profile-test-')
        directory = Path(cls.directory.name)
        source = directory / 'calls.S'
        source.write_text('''.syntax unified
.arch armv7-a
.text
.arm
push {lr}
.word 0xeb00000d
adr r3, 2f
orr r3, r3, #1
blx r3
pop {pc}
.org 0x40
1: add r0, r0, #1
bx lr
.thumb
.org 0x80
2: adds r0, #2
bx lr
''')
        output = directory / 'calls.o'
        subprocess.run(['clang', '-target', 'armv7-none-eabi', '-c', str(source), '-o', str(output)],
                       check=True, capture_output=True)
        with output.open('rb') as stream:
            cls.code = ELFFile(stream).get_section_by_name('.text').data()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.uc.mem_map(ADDRESS, 4096)
        self.uc.mem_write(ADDRESS, self.code)
        self.uc.mem_map(RETURN, 4096)
        self.uc.mem_map(STACK, 4096)

    def execute(self):
        self.uc.reg_write(UC_ARM_REG_R0, 0)
        self.uc.reg_write(UC_ARM_REG_SP, STACK + 4080)
        self.uc.reg_write(UC_ARM_REG_LR, RETURN)
        self.uc.emu_start(ADDRESS, RETURN)
        self.assertEqual(self.uc.reg_read(UC_ARM_REG_R0), 3)

    def test_records_actual_indirect_thumb_call_and_skips_return_continuations(self):
        with self.module.Recorder(self.uc, ADDRESS, ADDRESS + len(self.code)) as trace:
            self.execute()
            blocks, edges = trace.records()
        modes = {(block.address, bool(block.cpsr & 32)) for block in blocks}
        self.assertIn((ADDRESS + 0x40, False), modes)
        self.assertIn((ADDRESS + 0x80, True), modes)
        code = self.code
        class Image:
            entry_modes = {ADDRESS: False}
            sections = [(ADDRESS, len(code), '.text')]
            def read(self, address, size):
                return code[address - ADDRESS:address - ADDRESS + size]
        entries = self.module.entries_from_trace(Image(), blocks, edges)
        self.assertEqual(entries[ADDRESS], False)
        self.assertEqual(entries[ADDRESS + 0x40], False)
        self.assertEqual(entries[ADDRESS + 0x80], True)
        self.assertNotIn(ADDRESS + 8, entries)
        self.assertNotIn(ADDRESS + 0x14, entries)

    def test_unique_records_stop_growing_when_the_same_blocks_repeat(self):
        with self.module.Recorder(self.uc, ADDRESS, ADDRESS + len(self.code)) as trace:
            self.execute()
            self.execute()
            before = tuple(map(len, trace.records()))
            self.execute()
            self.assertEqual(tuple(map(len, trace.records())), before)

    def test_untaken_conditional_call_does_not_seed_its_fallthrough_as_a_callee(self):
        code = bytearray(self.code)
        struct.pack_into('<I', code, 4, 0x1b00000d)  # BLNE with Z set must fall through.
        self.uc.mem_write(ADDRESS, bytes(code))
        self.uc.reg_write(UC_ARM_REG_R0, 0)
        self.uc.reg_write(UC_ARM_REG_SP, STACK + 4080)
        self.uc.reg_write(UC_ARM_REG_LR, RETURN)
        self.uc.reg_write(UC_ARM_REG_CPSR, 0x400001d3)
        with self.module.Recorder(self.uc, ADDRESS, ADDRESS + len(code)) as trace:
            self.uc.emu_start(ADDRESS, RETURN)
            blocks, edges = trace.records()
        self.assertEqual(self.uc.reg_read(UC_ARM_REG_R0), 2)
        class Image:
            entry_modes = {ADDRESS: False}
            sections = [(ADDRESS, len(code), '.text')]
            def read(self, address, size):
                return bytes(code[address - ADDRESS:address - ADDRESS + size])
        entries = self.module.entries_from_trace(Image(), blocks, edges)
        self.assertNotIn(ADDRESS + 0x40, entries)
        self.assertNotIn(ADDRESS + 8, entries)
        self.assertEqual(entries[ADDRESS + 0x80], True)

    def test_non_stack_ldm_pc_records_its_indirect_entry(self):
        directory = Path(self.directory.name)
        source, output = directory / 'ldm.S', directory / 'ldm.o'
        source.write_text('''.syntax unified
.arch armv7-a
.text
.arm
push {lr}
adr r3, 2f
adr lr, 1f
ldm r3, {r0, pc}
1: pop {pc}
.align 2
2: .word 5, 0x10000081
.thumb
.org 0x80
adds r0, #2
bx lr
''')
        subprocess.run(['clang', '-target', 'armv7-none-eabi', '-c', str(source), '-o', str(output)],
                       check=True, capture_output=True)
        with output.open('rb') as stream:
            code = ELFFile(stream).get_section_by_name('.text').data()
        self.uc.mem_write(ADDRESS, code)
        self.uc.reg_write(UC_ARM_REG_R0, 0)
        self.uc.reg_write(UC_ARM_REG_SP, STACK + 4080)
        self.uc.reg_write(UC_ARM_REG_LR, RETURN)
        with self.module.Recorder(self.uc, ADDRESS, ADDRESS + len(code)) as trace:
            self.uc.emu_start(ADDRESS, RETURN)
            blocks, edges = trace.records()
        self.assertEqual(self.uc.reg_read(UC_ARM_REG_R0), 7)
        class Image:
            entry_modes = {ADDRESS: False}
            sections = [(ADDRESS, len(code), '.text')]
            def read(self, address, size):
                return code[address - ADDRESS:address - ADDRESS + size]
        entries = self.module.entries_from_trace(Image(), blocks, edges)
        self.assertEqual(entries.get(ADDRESS + 0x80), True)

    def test_leaving_the_recorded_module_resets_the_predecessor(self):
        with self.module.Recorder(self.uc, ADDRESS, ADDRESS + 0x40) as trace:
            self.execute()
            blocks, edges = trace.records()
        self.assertTrue(blocks)
        self.assertEqual(edges, [])

    def test_record_capacity_fails_loudly_instead_of_emitting_partial_coverage(self):
        for capacity in ({'max_blocks': 1}, {'max_edges': 1}):
            with self.subTest(capacity=capacity), self.module.Recorder(
                    self.uc, ADDRESS, ADDRESS + len(self.code), **capacity) as trace:
                self.execute()
                with self.assertRaisesRegex(RuntimeError, 'capacity'):
                    trace.records()


if __name__ == '__main__':
    unittest.main()
