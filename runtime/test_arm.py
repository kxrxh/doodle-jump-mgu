import importlib
from pathlib import Path
import struct
import unittest


LIBRARY = Path(__file__).resolve().parent.parent / 'original/extracted/lib/armeabi-v7a/libDoodleJump.so'


class ArmTests(unittest.TestCase):
    def machine(self):
        try:
            module = importlib.import_module('arm')
        except ModuleNotFoundError:
            self.fail('The original ARM engine execution host is missing')
        return module.Arm(LIBRARY)

    def test_original_engine_integer_and_double_calling_conventions(self):
        machine = self.machine()
        self.assertEqual(machine.call('__aeabi_idiv', -7, 2), 0xFFFFFFFD)
        self.assertEqual(machine.call('__aeabi_d2uiz', 0, 0x401E0000), 7)

    def test_original_cpp_allocator_uses_host_memory(self):
        machine = self.machine()
        pointer = machine.call('_Znwj', 64)
        machine.uc.mem_write(pointer, b'original engine')
        self.assertEqual(bytes(machine.uc.mem_read(pointer, 15)), b'original engine')
        machine.call('_ZdlPv', pointer)

    def test_small_native_allocations_reuse_memory_and_realloc_preserves_bytes(self):
        machine=self.machine()
        libc=importlib.import_module('libc').LibC(machine)
        self.assertIn('malloc',machine.native_imports)
        allocate=machine.import_address('malloc')
        release=machine.import_address('free')
        small=machine.call(allocate,31)
        self.assertEqual(small%16,0)
        machine.uc.mem_write(small,b'original pixels')
        larger=machine.call(machine.import_address('realloc'),small,5000)
        self.assertEqual(bytes(machine.uc.mem_read(larger,15)),b'original pixels')
        reused=machine.call(allocate,31)
        self.assertEqual(reused,small)
        machine.call(release,reused)
        machine.free(larger)
        machine.call(release,0)
        empty=machine.call(allocate,0)
        self.assertNotEqual(empty,0)
        machine.free(empty)
        machine.put32(0x01101000,0x41000000-16)
        fallback=machine.call(allocate,123)
        self.assertLess(fallback,0x30000000)
        self.assertGreaterEqual(machine.allocation_size(fallback),123)
        machine.call(release,fallback)

    def test_original_engine_static_initializers_complete(self):
        machine = self.machine()
        try:
            module = importlib.import_module('libc')
        except ModuleNotFoundError:
            self.fail('The Android C library compatibility layer is missing')
        module.LibC(machine)
        machine.initialize()
        pointer = machine.call('_Znwj', 1024)
        self.assertGreaterEqual(pointer, 0x20000000)

    def test_android_calendar_layout_used_by_highscores(self):
        machine = self.machine()
        libc = importlib.import_module('libc').LibC(machine)
        timestamp = machine.malloc(4)
        machine.put32(timestamp, 0)
        result = machine.call(machine.import_address('gmtime'), timestamp)
        self.assertEqual(struct.unpack('<9i', machine.uc.mem_read(result, 36)),
                         (0, 0, 0, 1, 0, 70, 4, 0, 0))

    def test_scanf_writes_android_width_and_rejects_unsupported_host_formats(self):
        machine = self.machine()
        libc = importlib.import_module('libc').LibC(machine)
        result = machine.malloc(16)
        machine.fill(result, 0xAA, 16)
        call = machine.import_address('sscanf')
        self.assertEqual(machine.call(call, libc.cstring(b'123'), libc.cstring(b'%ld'), result), 1)
        self.assertEqual(machine.u32(result), 123)
        self.assertEqual(machine.u32(result+4), 0xAAAAAAAA)
        for fmt in (b'%n', b'%Lf', b'%ls'):
            with self.assertRaises(NotImplementedError):
                machine.call(call, libc.cstring(b'123'), libc.cstring(fmt), result)

    def test_memory_and_string_imports_preserve_overlap_and_byte_semantics(self):
        machine = self.machine()
        libc = importlib.import_module('libc').LibC(machine)
        memory = machine.malloc(1024)
        for count in (0, 1, 3, 4, 31, 32, 255):
            for delta in (-7, 0, 7):
                source = bytes(i % 256 for i in range(512))
                expected = bytearray(source)
                expected[100+delta:100+delta+count] = source[100:100+count]
                machine.uc.mem_write(memory, source)
                destination = memory+100+delta
                self.assertEqual(machine.call(machine.import_address('memmove'), destination, memory+100, count), destination)
                self.assertEqual(bytes(machine.uc.mem_read(memory,512)), expected)
        for value in (b'', b'abc', b'abc\xff'):
            pointer = libc.cstring(value)
            self.assertEqual(machine.call(machine.import_address('strlen'), pointer), len(value))
            self.assertEqual(machine.call(machine.import_address('strcmp'), pointer, pointer), 0)
        self.assertEqual(machine.call(machine.import_address('strncmp'), 0, 0, 0), 0)


if __name__ == '__main__':
    unittest.main()
