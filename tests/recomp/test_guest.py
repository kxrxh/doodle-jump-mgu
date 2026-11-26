"""Exercise the AOT guest bridge with generated C and the existing host dispatcher."""
from pathlib import Path as _Path
import sys as _sys
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / 'tools'))
from prepare_reference import activate as _activate
_REFERENCE = _activate()

import ctypes as C
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
RECOMP = ROOT / 'runtime/recomp'
R0, R1, LR, PC, CPSR, FPSCR, D0 = 66, 67, 10, 11, 3, 6, 14

FIXTURE = r'''
#include "guest.h"
static void resolve(Arm32Ctx* c, uint32_t target) {
    if (arc_enter(c, target, 8)) return;
    switch (target & ~1u) {
    case 0x1000:
        ARC_ST32(ARC_R(c, 0), ARC_R(c, 1));
        ARC_W(c, 0, ARC_LD32(ARC_R(c, 0)) + 3);
        arc_return(c, ARC_LR(c));
        return;
    case 0x2000: {
        uint32_t caller = ARC_LR(c);
        ARC_W(c, 0, 7);
        ARC_W(c, 14, 0x2011);
        arc_dispatch(c, 0x3000);
        ARC_W(c, 0, ARC_R(c, 0) * 2);
        arc_return(c, caller);
        return;
    }
    case 0x4000:
        ARC_W(c, 0, 0xdead);
        arc_return(c, ARC_LR(c));
        return;
    case 0x5000:
        ARC_W(c, 0, ARC_LD32(ARC_R(c, 0)));
        arc_return(c, ARC_LR(c));
        return;
    case 0x6000:
        ARC_ST32(ARC_R(c, 0), ARC_R(c, 1));
        arc_return(c, ARC_LR(c));
        return;
    case 0x7000:
        for (;;) arc_block(c, 0x7000, 2);
    case 0x8000:
        ARC_W(c, 15, 0x8000);
        return;
    case 0xa000:
        ARC_W(c, 4, ARC_LR(c));
        ARC_W(c, 1, 77);
        ARC_DU(c, 0) = 0x12345678abcdef01ull;
        ARC_W(c, 14, 0xa011);
        arc_dispatch(c, 0xb000);
        if (ARC_R(c, 15) != 0xa010) return;
        ARC_W(c, 1, 999);
        ARC_W(c, 13, ARC_SP(c) - 64);
        ARC_DU(c, 0) = 0;
        ARC_W(c, 14, 0xa021);
        arc_dispatch(c, 0xb004);
        if (ARC_R(c, 15) != 0xa020) return;
        arc_trap(c, "longjmp returned to its active caller");
        return;
    case 0xa010:
        if (ARC_R(c, 5)) {
            ARC_W(c, 14, 0xa011);
            arc_dispatch(c, 0xb004);
            return;
        }
        ARC_W(c, 0, ARC_R(c, 0) + 10);
        arc_return(c, ARC_R(c, 4));
        return;
    default:
        arc_trap(c, "unresolved fixture target");
    }
}
void* fixture_create(void) { return guest_create(resolve); }
void doodle_resolve(Arm32Ctx* c, uint32_t target) { resolve(c, target); }
const char* doodle_engine_sha256(void) { return "fixture"; }
'''

SNAPSHOT = r'''
#include "guest.h"
#include "dispatch.h"
#include "image_memory.h"
#include <cstring>
static std::shared_ptr<PixelSnapshot> saved;
static int capture(void*, NativeCall* call, NativeResult*) {
    saved = call->images->snapshot(call->argument(0), 4).pixels;
    return saved ? 0 : 1;
}
extern "C" int fixture_register_capture(void* dispatch) {
    return dispatch_register(dispatch, "capture", capture, nullptr);
}
extern "C" uint32_t fixture_snapshot(void) {
    uint32_t value = 0;
    if (saved) std::memcpy(&value, saved->data(), 4);
    return value;
}
extern "C" size_t fixture_snapshot_owned(void) { return saved ? saved->owned_bytes() : 0; }
extern "C" void fixture_snapshot_clear(void) { saved.reset(); }
'''


class GuestApi(C.Structure):
    _fields_ = [(name, C.c_void_p) for name in (
        'reg_read', 'reg_write', 'mem_read', 'mem_write', 'hook_add', 'hook_del',
        'emu_stop', 'mem_map_ptr', 'mem_unmap', 'mem_protect')]
    _fields_.append(('registers', C.c_int * 7))


class Result(C.Structure):
    _fields_ = [('value', C.c_uint64), ('flags', C.c_uint32)]


Fallback = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint64, C.POINTER(Result))


class GuestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='doodle-aot-guest-')
        out = Path(cls.directory.name)
        (out / 'fixture.c').write_text(FIXTURE)
        (out / 'snapshot.cpp').write_text(SNAPSHOT)
        result = subprocess.run(['cc', '-std=c11', '-fexceptions', '-fPIC', '-O2',
                                 '-I', str(RECOMP), '-c', str(out / 'fixture.c'),
                                 '-o', str(out / 'fixture.o')], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        result = subprocess.run(['c++', '-std=c++14', '-shared', '-fPIC', '-O2',
                                 '-I', str(RECOMP), '-I', str(ROOT / 'runtime'),
                                 str(RECOMP / 'guest.cpp'), str(ROOT / 'runtime/dispatch.cpp'),
                                 str(ROOT / 'runtime/image_memory.cpp'), str(out / 'snapshot.cpp'),
                                 str(out / 'fixture.o'), '-o', str(out / 'guest.so')],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        cls.lib = C.CDLL(str(out / 'guest.so'))
        signatures = {
            'fixture_create': (C.c_void_p, []), 'guest_free': (None, [C.c_void_p]),
            'guest_create': (C.c_void_p, [C.c_void_p]),
            'doodle_engine_sha256': (C.c_char_p, []),
            'guest_reg_read': (C.c_int, [C.c_void_p, C.c_int, C.c_void_p]),
            'guest_reg_write': (C.c_int, [C.c_void_p, C.c_int, C.c_void_p]),
            'guest_mem_map': (C.c_int, [C.c_void_p, C.c_uint64, C.c_size_t, C.c_uint32]),
            'guest_mem_map_ptr': (C.c_int, [C.c_void_p, C.c_uint64, C.c_size_t, C.c_uint32, C.c_void_p]),
            'guest_mem_read': (C.c_int, [C.c_void_p, C.c_uint64, C.c_void_p, C.c_size_t]),
            'guest_mem_write': (C.c_int, [C.c_void_p, C.c_uint64, C.c_void_p, C.c_size_t]),
            'guest_mem_unmap': (C.c_int, [C.c_void_p, C.c_uint64, C.c_size_t]),
            'guest_mem_protect': (C.c_int, [C.c_void_p, C.c_uint64, C.c_size_t, C.c_uint32]),
            'guest_get_api': (None, [C.POINTER(GuestApi)]),
            'guest_run': (C.c_int, [C.c_void_p, C.c_uint32, C.c_uint32]),
            'guest_error': (C.c_char_p, [C.c_void_p]),
            'guest_context_size': (C.c_size_t, []),
            'guest_context_save': (C.c_int, [C.c_void_p, C.c_void_p, C.c_size_t]),
            'guest_context_restore': (C.c_int, [C.c_void_p, C.c_void_p, C.c_size_t]),
            'dispatch_create': (C.c_void_p, [C.c_void_p, C.POINTER(GuestApi), C.c_int, C.c_int,
                                            C.c_int, C.c_uint64, C.c_uint64, Fallback, C.c_void_p]),
            'dispatch_destroy': (None, [C.c_void_p]),
            'dispatch_add': (C.c_int, [C.c_void_p, C.c_uint64, C.c_char_p, C.c_int]),
            'dispatch_begin_call': (C.c_int, [C.c_void_p, C.c_uint64]),
            'dispatch_end_call': (None, [C.c_void_p]),
            'dispatch_error': (C.c_char_p, [C.c_void_p]),
            'dispatch_allocate_image': (C.c_void_p, [C.c_void_p, C.c_uint32, C.c_size_t]),
            'dispatch_release_image': (C.c_int, [C.c_void_p, C.c_uint32]),
            'fixture_register_capture': (C.c_int, [C.c_void_p]),
            'fixture_snapshot': (C.c_uint32, []),
            'fixture_snapshot_owned': (C.c_size_t, []),
            'fixture_snapshot_clear': (None, []),
        }
        for name, (result_type, arguments) in signatures.items():
            function = getattr(cls.lib, name)
            function.restype, function.argtypes = result_type, arguments
        if hasattr(cls.lib, 'guest_context_jump'):
            cls.lib.guest_context_jump.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
            cls.lib.guest_context_jump.restype = C.c_int

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.guest = self.lib.fixture_create()
        self.assertTrue(self.guest)
        self.dispatch = None
        self.callbacks = []

    def tearDown(self):
        self.lib.fixture_snapshot_clear()
        if self.dispatch:
            self.lib.dispatch_destroy(self.dispatch)
        self.lib.guest_free(self.guest)

    def put(self, reg, value, kind=C.c_uint32):
        value = kind(value)
        self.assertEqual(self.lib.guest_reg_write(self.guest, reg, C.byref(value)), 0)

    def get(self, reg, kind=C.c_uint32):
        value = kind()
        self.assertEqual(self.lib.guest_reg_read(self.guest, reg, C.byref(value)), 0)
        return value.value

    def read(self, address, size=4):
        output = C.create_string_buffer(size)
        self.assertEqual(self.lib.guest_mem_read(self.guest, address, output, size), 0)
        return output.raw

    def execute(self, target):
        self.put(LR, 0x9000)
        status = self.lib.guest_run(self.guest, target, 0x9000)
        self.assertEqual(status, 0, self.lib.guest_error(self.guest))

    def host(self, callback):
        api = GuestApi()
        self.lib.guest_get_api(C.byref(api))
        self.assertEqual(list(api.registers), [66, 67, 68, 69, 12, 10, 11])
        fallback = Fallback(callback)
        self.callbacks.append(fallback)
        self.dispatch = self.lib.dispatch_create(self.guest, C.byref(api), 4, 8, 256,
                                                 0x3000, 0x3000, fallback, None)
        self.assertTrue(self.dispatch)

    def test_generated_memory_aliases_host_pixels_without_copy(self):
        pixels = (C.c_uint32 * 1024)()
        self.assertEqual(self.lib.guest_mem_map_ptr(self.guest, 0x50000000, 4096, 3, pixels), 0)
        self.put(R0, 0x50000000)
        self.put(R1, 123)
        self.execute(0x1001)
        self.assertEqual(pixels[0], 123)
        self.assertEqual(self.get(R0), 126)
        pixels[0] = 456
        self.put(R0, 0x50000000)
        self.execute(0x5001)
        self.assertEqual(self.get(R0), 456)
        self.assertEqual(self.lib.guest_mem_unmap(self.guest, 0x50000000, 4096), 0)
        self.put(R0, 0x50000000)
        self.assertNotEqual(self.lib.guest_run(self.guest, 0x5001, 0x9000), 0)

    def test_cross_page_memory_and_overflow_are_checked_before_writing(self):
        self.assertEqual(self.lib.guest_mem_map(self.guest, 0x10000, 8192, 3), 0)
        self.put(R0, 0x10ffe)
        self.put(R1, 0x12345678)
        self.execute(0x6001)
        self.assertEqual(self.read(0x10ffe), b'\x78\x56\x34\x12')
        self.assertNotEqual(self.lib.guest_mem_map(self.guest, 0xfffff000, 8192, 3), 0)
        self.assertNotEqual(self.lib.guest_mem_map(self.guest, 0x10000, 4096, 3), 0)
        self.assertEqual(self.lib.guest_mem_unmap(self.guest, 0x11000, 4096), 0)
        self.put(R0, 0x10ffe)
        self.put(R1, 0xffffffff)
        self.assertNotEqual(self.lib.guest_run(self.guest, 0x6001, 0x9000), 0)
        self.assertEqual(self.read(0x10ffe, 2), b'\x78\x56')

    def test_architectural_context_restores_flags_and_aliased_fp_registers(self):
        self.put(R0, 123)
        self.put(CPSR, 0xa8000030)
        self.put(FPSCR, 0x01000000)
        self.put(D0, 0x12345678abcdef01, C.c_uint64)
        size = self.lib.guest_context_size()
        saved = C.create_string_buffer(size)
        self.assertEqual(self.lib.guest_context_save(self.guest, saved, size), 0)
        self.put(R0, 999)
        self.put(CPSR, 0x10)
        self.put(D0, 0, C.c_uint64)
        self.assertEqual(self.lib.guest_context_restore(self.guest, saved, size), 0)
        self.assertEqual(self.get(R0), 123)
        self.assertEqual(self.get(CPSR), 0xa8000030)
        self.assertEqual(self.get(FPSCR), 0x01000000)
        self.assertEqual(self.get(D0, C.c_uint64), 0x12345678abcdef01)
        self.assertEqual(self.get(79), 0xabcdef01)
        self.assertEqual(self.get(80), 0x12345678)

    def test_reset_status_matches_the_unicorn_arm_oracle(self):
        self.assertEqual(self.get(CPSR), 0x400001d3)
        self.assertEqual(self.get(FPSCR), 0)

    def test_run_rejects_a_generated_function_that_returns_to_the_wrong_pc(self):
        self.assertNotEqual(self.lib.guest_run(self.guest, 0x8001, 0x9000), 0)
        self.assertEqual(self.get(PC), 0x8000)

    def nonlocal_run(self, mode='jump'):
        saved = C.create_string_buffer(self.lib.guest_context_size())
        jump = getattr(self.lib, 'guest_context_jump', self.lib.guest_context_restore)
        callback_errors = []
        def respond(_, address, output):
            try:
                if address == 0xb000:
                    self.assertEqual(self.lib.guest_context_save(self.guest, saved, len(saved)), 0)
                    output.contents.value = 0
                else:
                    if mode == 'unknown':
                        C.c_uint32.from_buffer(saved, 14 * 4).value = 0xdead0001
                    if mode == 'restore':
                        status = self.lib.guest_context_restore(self.guest, saved, len(saved))
                    elif mode == 'invalid':
                        status = jump(self.guest, saved, len(saved) - 1)
                        self.assertNotEqual(status, 0)
                        self.put(LR, 0xa011)
                        status = 0
                    else:
                        status = jump(self.guest, saved, len(saved))
                    self.assertEqual(status, 0)
                    output.contents.value = 17
                output.contents.flags = 1
                return 0
            except BaseException as error:
                callback_errors.append(error)
                return 1
        self.host(respond)
        self.assertEqual(self.lib.dispatch_add(self.dispatch, 0xb000, b'setjmp', 1), 0)
        self.assertEqual(self.lib.dispatch_add(self.dispatch, 0xb004, b'longjmp', 1), 0)
        self.put(LR, 0x9000)
        self.put(12, 0x30000ff0)
        status = self.lib.guest_run(self.guest, 0xa001, 0x9000)
        if callback_errors:
            raise callback_errors[0]
        return status

    def test_nonlocal_jump_resumes_saved_continuation_with_restored_cpu_state(self):
        self.assertEqual(self.nonlocal_run(), 0, self.lib.guest_error(self.guest))
        self.assertEqual(self.get(R0), 27)
        self.assertEqual(self.get(R1), 77)
        self.assertEqual(self.get(12), 0x30000ff0)
        self.assertEqual(self.get(D0, C.c_uint64), 0x12345678abcdef01)

    def test_ordinary_context_restore_does_not_authorize_a_continuation(self):
        self.assertNotEqual(self.nonlocal_run('restore'), 0)

    def test_invalid_jump_context_does_not_arm_a_resume(self):
        self.assertNotEqual(self.nonlocal_run('invalid'), 0)

    def test_unresolved_nonlocal_continuation_fails_closed(self):
        self.assertNotEqual(self.nonlocal_run('unknown'), 0)
        self.assertIn(b'unresolved fixture', self.lib.guest_error(self.guest))

    def test_repeated_nonlocal_jumps_are_bounded_without_an_instruction_hook(self):
        self.put(71, 1)  # r5 keeps the continuation jumping to the same saved state.
        self.assertNotEqual(self.nonlocal_run(), 0)
        self.assertIn(b'continuation limit', self.lib.guest_error(self.guest))

    def test_jump_outside_execution_does_not_restore_or_arm_any_state(self):
        saved = C.create_string_buffer(self.lib.guest_context_size())
        self.put(R0, 123)
        self.assertEqual(self.lib.guest_context_save(self.guest, saved, len(saved)), 0)
        self.put(R0, 999)
        self.assertNotEqual(self.lib.guest_context_jump(self.guest, saved, len(saved)), 0)
        self.assertEqual(self.get(R0), 999)

    def test_machine_round_trips_full_neon_register_without_overrunning_ctypes_buffers(self):
        specification = importlib.util.spec_from_file_location('guest_machine_fixture', _REFERENCE / 'recomp/machine.py')
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        self.lib.guest_context.argtypes = [C.c_void_p]
        self.lib.guest_context.restype = C.POINTER(module.State)
        native, test = self.lib, self
        class CheckedLibrary:
            def __getattr__(self, name):
                return getattr(native, name)
            def guest_reg_read(self, guest, register, output):
                if 50 <= register <= 65:
                    test.assertGreaterEqual(C.sizeof(getattr(output, '_obj', output)), 16)
                return native.guest_reg_read(guest, register, output)
            def guest_reg_write(self, guest, register, value):
                if 50 <= register <= 65:
                    test.assertGreaterEqual(C.sizeof(getattr(value, '_obj', value)), 16)
                return native.guest_reg_write(guest, register, value)
        with patch.object(module, 'library', return_value=CheckedLibrary()):
            machine = module.Machine('fixture')
        try:
            value = 0xfedcba98765432100123456789abcdef
            machine.reg_write(50, value)
            self.assertEqual(machine.reg_read(50), value)
            self.assertEqual(machine.reg_read(D0), 0x0123456789abcdef)
            self.assertEqual(machine.reg_read(D0 + 1), 0xfedcba9876543210)
            machine.reg_write(D0 + 1, 0x1234)
            self.assertEqual(machine.reg_read(50), 0x12340123456789abcdef)
        finally:
            machine._finalizer()

    def test_existing_host_dispatch_returns_to_generated_c_and_intercepts_entries(self):
        def respond(_, address, output):
            output.contents.value = self.get(R0) + 10 if address == 0x3000 else 99
            output.contents.flags = 1
            return 0
        self.host(respond)
        self.assertEqual(self.lib.dispatch_add(self.dispatch, 0x3000, b'test', 0), 0)
        self.execute(0x2001)
        self.assertEqual(self.get(R0), 34)
        self.assertEqual(self.lib.dispatch_add(self.dispatch, 0x4000, b'intercept', 1), 0)
        self.execute(0x4001)
        self.assertEqual(self.get(R0), 99)

    def test_existing_png_snapshot_detaches_only_when_generated_code_writes(self):
        self.host(lambda *_: 1)
        self.assertEqual(self.lib.dispatch_add(self.dispatch, 0x3000, b'capture', 0), 0)
        self.assertEqual(self.lib.fixture_register_capture(self.dispatch), 0)
        pointer = self.lib.dispatch_allocate_image(self.dispatch, 0x50000000, 4096)
        self.assertTrue(pointer)
        C.c_uint32.from_address(pointer).value = 0x11223344
        self.put(R0, 0x50000000)
        self.execute(0x3000)
        self.assertEqual(self.lib.fixture_snapshot_owned(), 0)
        self.put(R0, 0x50000000)
        self.put(R1, 0x55667788)
        self.execute(0x6001)
        self.assertEqual(C.c_uint32.from_address(pointer).value, 0x55667788)
        self.assertEqual(self.lib.fixture_snapshot(), 0x11223344)
        self.assertGreater(self.lib.fixture_snapshot_owned(), 0)
        self.assertEqual(self.lib.dispatch_release_image(self.dispatch, 0x50000000), 0)

    def test_budget_stops_generated_loop_and_reports_original_dispatch_error(self):
        self.host(lambda *_: 1)
        self.assertEqual(self.lib.dispatch_begin_call(self.dispatch, 20), 0)
        self.assertNotEqual(self.lib.guest_run(self.guest, 0x7001, 0x9000), 0)
        self.assertIn(b'instruction limit', self.lib.dispatch_error(self.dispatch))
        self.lib.dispatch_end_call(self.dispatch)


if __name__ == '__main__':
    unittest.main()
