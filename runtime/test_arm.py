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

    def test_native_dispatch_handles_mutex_import_without_python_callback(self):
        machine = self.machine()
        def unexpected_callback():
            raise AssertionError('The native mutex import crossed into Python')
        machine.handlers['pthread_mutex_lock'] = unexpected_callback
        # Exercise the host dispatcher rather than the ARM assembly fast path.
        machine.native_imports.pop('pthread_mutex_lock')
        machine.imports.pop('pthread_mutex_lock', None)
        self.assertEqual(machine.call(machine.import_address('pthread_mutex_lock'), 123), 0)

    def test_host_results_preserve_arm_words_and_nested_call_context(self):
        machine = self.machine()
        pair = machine.register('host_pair', lambda: (0x89ABCDEF, 0x12345678))
        whole = machine.register('host_whole', lambda: 0x1234567889ABCDEF)
        shim = machine.malloc(16)
        # PUSH {lr}; BLX r3; MOV r0,r1; POP {pc} exposes the returned high word.
        machine.uc.mem_write(shim, struct.pack('<4I', 0xE52DE004, 0xE12FFF33, 0xE1A00001, 0xE49DF004))
        for target in (pair, whole):
            def outer(target=target):
                low = machine.call(target)
                return low ^ machine.arg(4)
            address = machine.register('host_outer', outer)
            self.assertEqual(machine.call(address, 1, 2, 3, 4, 0x76543210), 0xFFFFFFFF)
            self.assertEqual(machine.call(shim, 0, 0, 0, target), 0x12345678)

    def test_host_callback_errors_escape_emulation_and_next_call_still_works(self):
        machine = self.machine()
        def broken():
            raise ValueError('guest callback failed')
        address = machine.register('host_broken', broken)
        with self.assertRaisesRegex(ValueError, 'guest callback failed'):
            machine.call(address)
        with self.assertRaisesRegex(NotImplementedError, 'Host import missing_host'):
            machine.call(machine.import_address('missing_host'))
        self.assertEqual(machine.call(machine.register('host_ok', lambda: 123)), 123)

    def test_nested_host_error_can_be_caught_by_the_outer_handler(self):
        machine = self.machine()
        def broken():
            raise ValueError('nested callback failed')
        broken_address = machine.register('host_broken', broken)
        def recover():
            try:
                machine.call(broken_address)
            except ValueError:
                return 456
            raise AssertionError('The nested callback should fail')
        self.assertEqual(machine.call(machine.register('host_recover', recover)), 456)

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
