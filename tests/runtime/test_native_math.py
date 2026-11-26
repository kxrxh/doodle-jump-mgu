import importlib
import math
import random
import struct
import unittest

from test_arm import LIBRARY


class NativeMathTests(unittest.TestCase):
    def setUp(self):
        self.machine = importlib.import_module('arm').Arm(LIBRARY)
        self.libc = importlib.import_module('libc').LibC(self.machine)

    def reject_fallback(self, name):
        def unexpected_callback():
            raise AssertionError(f'{name} crossed into Python')
        self.machine.handlers[name] = unexpected_callback
        return self.machine.import_address(name)

    def test_float_imports_match_libc_without_python_callbacks(self):
        words = [0, 0x80000000, 1, 0x80000001, 0x007FFFFF, 0x00800000,
                 0x3EFFFFFF, 0x3F000000, 0x3F000001, 0x3FC00000, 0x40200000,
                 0xBEFFFFFF, 0xBF000000, 0xBF000001, 0xBFC00000, 0xC0200000,
                 0x4B000001, 0xCB000001, 0x7F7FFFFF, 0xFF7FFFFF]
        randomizer = random.Random(713)
        words += [word for word in (randomizer.getrandbits(32) for _ in range(128))
                  if word & 0x7F800000 != 0x7F800000]
        for name in ('round', 'sin', 'cos'):
            with self.subTest(name=name):
                reference = self.machine.register('reference_' + name,
                                                  lambda name=name: self.libc.math(name, True))
                native = self.reject_fallback(name + 'f')
                for word in words:
                    self.assertEqual(self.machine.call(native, word),
                                     self.machine.call(reference, word), (name, hex(word)))

    def test_nonfinite_math_errors_stop_and_allow_the_next_call(self):
        for name in ('roundf', 'sinf', 'cosf'):
            with self.subTest(name=name):
                native = self.reject_fallback(name)
                invalid = (0x7F800000, 0xFF800000)
                if name == 'roundf':
                    invalid += (0x7FC00000,)
                for word in invalid:
                    with self.assertRaises((RuntimeError, ValueError, OverflowError)):
                        self.machine.call(native, word)
                    self.assertEqual(self.machine.call(native, 0), 0x3F800000 if name == 'cosf' else 0)
        for name in ('sinf', 'cosf'):
            result = self.machine.call(self.reject_fallback(name), 0x7FC00000)
            self.assertTrue(math.isnan(struct.unpack('<f', struct.pack('<I', result))[0]))

    def test_wcslen_counts_four_byte_characters_without_python_callbacks(self):
        native = self.reject_fallback('wcslen')
        memory = self.machine.malloc(128)
        for offset in range(4):
            for values in ((), (0x41,), (0xE9, 0x1F600, 0x4E2D)):
                with self.subTest(offset=offset, values=values):
                    data = struct.pack('<' + 'I' * (len(values) + 1), *values, 0)
                    self.machine.uc.mem_write(memory + offset, data)
                    self.assertEqual(self.machine.call(native, memory + offset), len(values))


if __name__ == '__main__':
    unittest.main()
