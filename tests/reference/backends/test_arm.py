"""Exercise current C++ backends through pinned external Python adapters."""
import _support

import importlib
from pathlib import Path
import struct
import unittest


LIBRARY = Path(__file__).resolve().parents[3] / 'original/extracted/lib/armeabi-v7a/libDoodleJump.so'


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











if __name__ == '__main__':
    unittest.main()
