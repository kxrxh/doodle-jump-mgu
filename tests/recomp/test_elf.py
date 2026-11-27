"""Recover native entry points from the original Android ELF, without guessing data is code."""
from pathlib import Path as _Path
import sys as _sys
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / 'tools'))
from prepare_reference import activate as _activate
_REFERENCE = _activate()

from pathlib import Path
import sys
import hashlib
import json
import tempfile
from types import MethodType, SimpleNamespace
import unittest

try:
    from recomp_reference.elf import ElfImage
except ImportError:
    ElfImage = None


ROOT = Path(__file__).resolve().parents[2]
GAME = ROOT / 'original/extracted/lib/armeabi-v7a/libDoodleJump.so'


@unittest.skipUnless(GAME.exists(), 'requires the verified original game')
class ElfTests(unittest.TestCase):
    def image(self):
        self.assertIsNotNone(ElfImage, 'the Android ELF recompiler loader is missing')
        return ElfImage(GAME, base=0x10000000)

    def test_jni_entry_keeps_thumb_mode_and_exact_original_body(self):
        image = self.image()
        entry = image.symbols['Java_com_limasky_doodlejumpandroid_NotificationCenter_runGameLoopNative']
        self.assertEqual(entry, 0x100d2add)
        function = image.functions[entry & ~1]
        self.assertTrue(function.thumb)
        self.assertEqual(function.address, entry & ~1)
        self.assertEqual(image.read(function.address, 4), bytes.fromhex('2de9f04f'))

    def test_unwind_boundaries_recover_hidden_code_and_keep_import_thunks_arm(self):
        image = self.image()
        self.assertGreater(len(image.functions), 3000)
        self.assertGreater(image.unwind_entries, 3000)
        self.assertTrue(any(not fn.thumb and fn.section == '.plt' for fn in image.functions.values()))
        self.assertTrue(all(fn.size > 0 and len(image.read(fn.address, fn.size)) == fn.size
                            for fn in image.functions.values()))

    def test_fatal_import_thunks_have_no_returning_code_path(self):
        image = self.image()
        self.assertIn(0x1001d118, getattr(image, 'noreturn', ()), '__stack_chk_fail cannot return')
        self.assertIn(0x1001d28c, image.noreturn, 'abort cannot return')

    def test_nonmapped_and_wrapping_reads_are_rejected(self):
        image = self.image()
        for address, size in ((0, 4), (0xffffffff, 4), (0x10000000, -1)):
            with self.subTest(address=address, size=size), self.assertRaises(ValueError):
                image.read(address, size)

    def test_thumb_tail_branch_preserves_instruction_mode(self):
        from recomp_reference.elf import Function
        sys.path.insert(0, str(ROOT / 'tools'))
        from recomp_reference.emit import discover
        code = bytes.fromhex('00e07047704700bf')  # B 0x1004; BX LR; BX LR; NOP
        image = SimpleNamespace(
            sections=[(0x1000, len(code), '.text')],
            functions={0x1000: Function(0x1000, 4, True, '.text'),
                       0x1004: Function(0x1004, 4, True, '.text')},
            read=lambda address, size: code[address-0x1000:address-0x1000+size])
        image._ranges = MethodType(ElfImage._ranges, image)
        discover(image)
        self.assertTrue(image.functions[0x1004].thumb, 'B preserves Thumb; only BLX interworks')

    def test_bx_pc_veneer_creates_arm_entry_after_thumb_prefix(self):
        from recomp_reference.elf import Function
        sys.path.insert(0, str(ROOT / 'tools'))
        from recomp_reference.emit import discover
        code = bytes.fromhex('7847c0461eff2fe1')  # Thumb BX PC; padding; ARM BX LR
        image = SimpleNamespace(
            sections=[(0x1000, len(code), '.text')],
            functions={0x1000: Function(0x1000, len(code), True, '.text')},
            read=lambda address, size: code[address-0x1000:address-0x1000+size])
        image._ranges = MethodType(ElfImage._ranges, image)
        discover(image)
        self.assertIn(0x1004, image.functions)
        self.assertFalse(image.functions[0x1004].thumb)

    def test_compare_branch_discovers_target_across_unwind_boundary(self):
        from recomp_reference.elf import Function
        sys.path.insert(0, str(ROOT / 'tools'))
        from recomp_reference.emit import discover
        code = bytes.fromhex('08b1704700bf7047')  # CBZ R0, 0x1006; BX LR; NOP; BX LR
        image = SimpleNamespace(
            sections=[(0x1000, len(code), '.text')], entry_modes={0x1000: True},
            functions={0x1000: Function(0x1000, 4, True, '.text'),
                       0x1004: Function(0x1004, 4, True, '.text')},
            read=lambda address, size: code[address-0x1000:address-0x1000+size])
        image._ranges = MethodType(ElfImage._ranges, image)
        discover(image)
        self.assertIn(0x1006, image.functions)
        self.assertTrue(image.functions[0x1006].thumb)

    def test_discovered_target_cannot_split_an_existing_instruction(self):
        from recomp_reference.elf import Function
        sys.path.insert(0, str(ROOT / 'tools'))
        from recomp_reference.emit import discover
        code = bytes.fromhex('00f10100704700bffbe7')  # ADD.W; BX LR; padding; false B into ADD.W
        image = SimpleNamespace(
            sections=[(0x1000, len(code), '.text')], entry_modes={0x1000: True, 0x1008: True},
            functions={0x1000: Function(0x1000, 8, True, '.text'),
                       0x1008: Function(0x1008, 2, True, '.text')},
            read=lambda address, size: code[address-0x1000:address-0x1000+size])
        image._ranges = MethodType(ElfImage._ranges, image)
        discover(image)
        self.assertNotIn(0x1002, image.functions)

    def test_data_in_untrusted_ranges_cannot_split_a_real_thumb_instruction(self):
        sys.path.insert(0, str(ROOT / 'tools'))
        from recomp_reference.emit import apply_profile, discover
        image = self.image()
        apply_profile(image, ROOT / 'runtime/recomp/translator/entries.json')
        discover(image)
        # This is the second halfword of ADD.W at 0x1001e600 in the first ctor.
        self.assertTrue(0x1001e602 not in image.functions, 'A call target must not split ADD.W')
        self.assertTrue(0x100cc580 not in image.functions, 'Data after fatal calls cannot split LDR.W')
        self.assertTrue(0x10077e24 not in image.functions, 'A false branch cannot split ADDW in theme 7')
        self.assertTrue(image.discovery_converged)

    def test_profile_entries_are_bound_to_the_exact_engine(self):
        sys.path.insert(0, str(ROOT / 'tools'))
        from recomp_reference import emit as recompile
        self.assertTrue(hasattr(recompile, 'apply_profile'), 'verified trace entries are not supported')
        image = self.image()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.json'
            trace = {'engine_sha256': '0'*64, 'entries': {'0x10039450': True}}
            path.write_text(json.dumps(trace))
            with self.assertRaises(ValueError):
                recompile.apply_profile(image, path)
            trace['engine_sha256'] = hashlib.sha256(GAME.read_bytes()).hexdigest()
            path.write_text(json.dumps(trace))
            recompile.apply_profile(image, path)
            self.assertTrue(image.functions[0x10039450].thumb)
            trace['entries'] = {'0x40000000': True}
            path.write_text(json.dumps(trace))
            with self.assertRaises(ValueError):
                recompile.apply_profile(image, path)


if __name__ == '__main__':
    unittest.main()
