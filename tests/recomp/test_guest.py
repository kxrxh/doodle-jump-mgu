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
    case 0xc000: ARC_W(c, 0, ARC_LD8(ARC_R(c, 0))); break;
    case 0xc004: ARC_W(c, 0, ARC_LD16(ARC_R(c, 0))); break;
    case 0xc008: ARC_W(c, 0, ARC_LD32(ARC_R(c, 0))); break;
    case 0xc00c: {
        uint64_t value = ARC_LD64(ARC_R(c, 0));
        ARC_W(c, 0, value);
        ARC_W(c, 1, value >> 32);
        break;
    }
    case 0xc100: ARC_ST8(ARC_R(c, 0), ARC_R(c, 1)); break;
    case 0xc104: ARC_ST16(ARC_R(c, 0), ARC_R(c, 1)); break;
    case 0xc108: ARC_ST32(ARC_R(c, 0), ARC_R(c, 1)); break;
    case 0xc10c: ARC_ST64(ARC_R(c, 0), ARC_R(c, 1) | (uint64_t)ARC_R(c, 2) << 32); break;
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
    arc_return(c, ARC_LR(c));
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
struct HookProbe { size_t next; unsigned seen = 0; bool removed = false; };
static void remove_next_hook(void *guest, uint64_t, uint32_t, void *user) {
    auto &probe = *static_cast<HookProbe *>(user);
    probe.seen = probe.seen * 10 + 1;
    if (!probe.removed) {
        probe.removed = true;
        if (guest_hook_del(guest, probe.next)) probe.seen = 999;
    }
}
static void deleted_hook(void *, uint64_t, uint32_t, void *user) {
    auto &probe = *static_cast<HookProbe *>(user);
    probe.seen = probe.seen * 10 + 2;
}
static void last_hook(void *, uint64_t, uint32_t, void *user) {
    auto &probe = *static_cast<HookProbe *>(user);
    probe.seen = probe.seen * 10 + 3;
}
extern "C" void *fixture_create(void);
extern "C" unsigned fixture_delete_future_hook(void) {
    void *guest = fixture_create();
    HookProbe probe{};
    size_t first, last;
    guest_hook_add(guest, &first, 4, reinterpret_cast<void *>(remove_next_hook), &probe, 0x4000, 0x4000);
    guest_hook_add(guest, &probe.next, 4, reinterpret_cast<void *>(deleted_hook), &probe, 0x4000, 0x4000);
    guest_hook_add(guest, &last, 4, reinterpret_cast<void *>(last_hook), &probe, 0x4000, 0x4000);
    guest_context(guest)->r[14] = 0x9000;
    int result = guest_run(guest, 0x4001, 0x9000);
    guest_free(guest);
    return result ? 999 : probe.seen;
}

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
            'guest_hook_add': (C.c_int, [C.c_void_p, C.POINTER(C.c_size_t), C.c_int,
                                       C.c_void_p, C.c_void_p, C.c_uint64, C.c_uint64]),
            'guest_hook_del': (C.c_int, [C.c_void_p, C.c_size_t]),
            'guest_stop': (C.c_int, [C.c_void_p]),
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
            'dispatch_clear_error': (None, [C.c_void_p]),
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

    def test_fixed_width_accesses_keep_unaligned_page_and_permission_checks(self):
        self.assertEqual(self.lib.guest_mem_map(self.guest, 0x10000, 8192, 3), 0)
        value = 0xfedcba9876543210
        for i, width in enumerate((1, 2, 4, 8)):
            for address in (0x10003, 0x10fff):
                with self.subTest(width=width, address=address):
                    self.put(R0, address)
                    self.put(R1, value & 0xffffffff)
                    self.put(R0 + 2, value >> 32)
                    self.execute(0xc101 + 4 * i)
                    expected = value.to_bytes(8, 'little')[:width]
                    self.assertEqual(self.read(address, width), expected)
                    self.put(R0, address)
                    self.execute(0xc001 + 4 * i)
                    loaded = self.get(R0) | (self.get(R1) << 32 if width == 8 else 0)
                    self.assertEqual(loaded, int.from_bytes(expected, 'little'))

        self.assertEqual(self.lib.guest_mem_unmap(self.guest, 0x11000, 4096), 0)
        for i, width in enumerate((1, 2, 4, 8)):
            address = 0x11000 if width == 1 else 0x10fff
            before = self.read(0x10ff8, 8)
            for target in (0xc001, 0xc101):
                self.put(R0, address)
                self.put(LR, 0x9000)
                self.assertNotEqual(self.lib.guest_run(self.guest, target + 4 * i, 0x9000), 0)
                self.assertEqual(self.read(0x10ff8, 8), before)

        self.assertEqual(self.lib.guest_mem_protect(self.guest, 0x10000, 4096, 1), 0)
        for i in range(4):
            self.put(R0, 0x10003)
            self.put(LR, 0x9000)
            before = self.read(0x10003, 8)
            self.assertNotEqual(self.lib.guest_run(self.guest, 0xc101 + 4 * i, 0x9000), 0)
            self.assertEqual(self.read(0x10003, 8), before)

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

    def memory_call(self, name, *arguments):
        self.assertEqual(self.lib.dispatch_add(self.dispatch, 0x3000, name.encode(), 0), 0)
        for index, value in enumerate(arguments):
            self.put(R0 + index, value)
        self.execute(0x3000)
        return self.get(R0)

    def test_native_bulk_copy_uses_checked_guest_memory_without_a_host_callback(self):
        self.host(lambda *_: 1)
        self.assertEqual(self.lib.guest_mem_map(self.guest, 0x10000, 16384, 3), 0)
        source = bytes(range(256)) * 16
        self.assertEqual(self.lib.guest_mem_write(self.guest, 0x10003, source, len(source)), 0)
        for name in ('memcpy', '__aeabi_memcpy'):
            for size in (7, len(source)):
                with self.subTest(name=name, size=size):
                    self.assertEqual(self.lib.guest_mem_write(self.guest, 0x12004, b'X', 1), 0)
                    self.assertEqual(self.lib.guest_mem_write(self.guest, 0x12005 + size, b'Y', 1), 0)
                    self.assertEqual(self.memory_call(name, 0x12005, 0x10003, size), 0x12005)
                    self.assertEqual(self.read(0x12004, size + 2), b'X' + source[:size] + b'Y')

    def test_native_memmove_reads_the_whole_source_before_overlapping_writes(self):
        self.host(lambda *_: 1)
        self.assertEqual(self.lib.guest_mem_map(self.guest, 0x10000, 4096, 3), 0)
        for name in ('memmove', '__aeabi_memmove'):
            for destination, source, expected in ((0x10002, 0x10000, b'ababcdefgh'),
                                                   (0x10000, 0x10002, b'cdefghijij')):
                with self.subTest(name=name, destination=destination):
                    self.assertEqual(self.lib.guest_mem_write(self.guest, 0x10000, b'abcdefghij', 10), 0)
                    self.assertEqual(self.memory_call(name, destination, source, 8), destination)
                    self.assertEqual(self.read(0x10000, 10), expected)

    def test_native_memset_keeps_the_standard_and_arm_argument_orders(self):
        self.host(lambda *_: 1)
        self.assertEqual(self.lib.guest_mem_map(self.guest, 0x10000, 8192, 3), 0)
        for name, arguments, size, value in (
                ('memset', (0x10003, 0x17f, 7), 7, b'\x7f'),
                ('__aeabi_memset', (0x10003, 7, 0xfffffffe), 7, b'\xfe'),
                ('memset', (0x10003, 0x17f, 4096), 4096, b'\x7f'),
                ('__aeabi_memset', (0x10003, 4096, 0xfffffffe), 4096, b'\xfe')):
            with self.subTest(name=name, size=size):
                self.assertEqual(self.memory_call(name, *arguments), 0x10003)
                self.assertEqual(self.read(0x10003, size), value * size)

    def test_native_arm_clear_aliases_zero_the_requested_length(self):
        self.host(lambda *_: 1)
        self.assertEqual(self.lib.guest_mem_map(self.guest, 0x10000, 8192, 3), 0)
        for name in ('__aeabi_memclr', '__aeabi_memclr4', '__aeabi_memclr8'):
            for size in (7, 4096):
                with self.subTest(name=name, size=size):
                    self.assertEqual(self.lib.guest_mem_write(self.guest, 0x10000, b'X' * (size + 2), size + 2), 0)
                    self.assertEqual(self.memory_call(name, 0x10001, size), 0x10001)
                    self.assertEqual(self.read(0x10000, size + 2), b'X' + b'\0' * size + b'X')

    def test_zero_length_bulk_operations_do_not_access_unmapped_pointers(self):
        self.host(lambda *_: 1)
        for name, arguments in (
                ('memcpy', (0xdeadbeef, 0xcafef00d, 0)),
                ('memmove', (0xdeadbeef, 0xcafef00d, 0)),
                ('__aeabi_memcpy', (0xdeadbeef, 0xcafef00d, 0)),
                ('__aeabi_memmove', (0xdeadbeef, 0xcafef00d, 0)),
                ('memset', (0xdeadbeef, 0x17f, 0)),
                ('__aeabi_memset', (0xdeadbeef, 0, 0x17f)),
                ('__aeabi_memclr', (0xdeadbeef, 0)),
                ('__aeabi_memclr4', (0xdeadbeef, 0)),
                ('__aeabi_memclr8', (0xdeadbeef, 0))):
            with self.subTest(name=name):
                self.assertEqual(self.memory_call(name, *arguments), 0xdeadbeef)

    def test_bulk_size_limit_is_checked_before_allocation_or_guest_access(self):
        self.host(lambda *_: 1)
        oversized = (64 << 20) + 1
        for name, arguments in (
                ('memcpy', (0xdeadbeef, 0xcafef00d, oversized)),
                ('memmove', (0xdeadbeef, 0xcafef00d, oversized)),
                ('__aeabi_memcpy', (0xdeadbeef, 0xcafef00d, oversized)),
                ('__aeabi_memmove', (0xdeadbeef, 0xcafef00d, oversized)),
                ('memset', (0xdeadbeef, 0x17f, oversized)),
                ('__aeabi_memset', (0xdeadbeef, oversized, 0x17f)),
                ('__aeabi_memclr', (0xdeadbeef, oversized)),
                ('__aeabi_memclr4', (0xdeadbeef, oversized)),
                ('__aeabi_memclr8', (0xdeadbeef, oversized))):
            with self.subTest(name=name):
                self.lib.dispatch_clear_error(self.dispatch)
                self.assertEqual(self.lib.dispatch_add(self.dispatch, 0x3000, name.encode(), 0), 0)
                for index, value in enumerate(arguments):
                    self.put(R0 + index, value)
                self.put(LR, 0x9000)
                self.assertNotEqual(self.lib.guest_run(self.guest, 0x3000, 0x9000), 0)
                self.assertIn(b'Bulk memory operation exceeds its size limit', self.lib.dispatch_error(self.dispatch))

    def test_failed_bulk_access_leaves_previously_valid_bytes_unchanged(self):
        self.host(lambda *_: 1)
        self.assertEqual(self.lib.guest_mem_map(self.guest, 0x10000, 4096, 3), 0)
        for name, arguments in (('memcpy', (0x10000, 0x10ffe, 4)),
                                ('memmove', (0x10000, 0x10ffe, 4)),
                                ('memset', (0x10ffe, 0xff, 4)),
                                ('__aeabi_memclr', (0x10ffe, 4))):
            with self.subTest(name=name):
                self.lib.dispatch_clear_error(self.dispatch)
                self.assertEqual(self.lib.guest_mem_write(self.guest, 0x10000, b'keep', 4), 0)
                self.assertEqual(self.lib.guest_mem_write(self.guest, 0x10ffe, b'XY', 2), 0)
                self.assertEqual(self.lib.dispatch_add(self.dispatch, 0x3000, name.encode(), 0), 0)
                for index, value in enumerate(arguments):
                    self.put(R0 + index, value)
                self.put(LR, 0x9000)
                self.assertNotEqual(self.lib.guest_run(self.guest, 0x3000, 0x9000), 0)
                self.assertIn(b'Guest access failed', self.lib.dispatch_error(self.dispatch))
                self.assertEqual(self.read(0x10000, 4), b'keep')
                self.assertEqual(self.read(0x10ffe, 2), b'XY')

    def test_native_bulk_writes_preserve_the_previous_png_snapshot(self):
        self.host(lambda *_: 1)
        self.assertEqual(self.lib.dispatch_add(self.dispatch, 0x4000, b'capture', 1), 0)
        self.assertEqual(self.lib.fixture_register_capture(self.dispatch), 0)
        pointer = self.lib.dispatch_allocate_image(self.dispatch, 0x50000000, 4096)
        self.assertTrue(pointer)
        C.c_uint32.from_address(pointer).value = 0x11223344
        self.put(R0, 0x50000000)
        self.execute(0x4000)
        self.assertEqual(self.lib.fixture_snapshot_owned(), 0)
        self.assertEqual(self.lib.guest_mem_map(self.guest, 0x10000, 4096, 3), 0)
        self.assertEqual(self.lib.guest_mem_write(self.guest, 0x10000, b'\x88\x77\x66\x55', 4), 0)
        self.assertEqual(self.memory_call('memcpy', 0x50000000, 0x10000, 4), 0x50000000)
        self.assertEqual(C.c_uint32.from_address(pointer).value, 0x55667788)
        self.assertEqual(self.lib.fixture_snapshot(), 0x11223344)
        self.assertGreater(self.lib.fixture_snapshot_owned(), 0)
        self.assertEqual(self.memory_call('__aeabi_memclr', 0x50000000, 4), 0x50000000)
        self.assertEqual(C.c_uint32.from_address(pointer).value, 0)
        self.assertEqual(self.lib.fixture_snapshot(), 0x11223344)
        self.assertEqual(self.lib.dispatch_release_image(self.dispatch, 0x50000000), 0)

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

    def hook(self, mask, callback):
        kind = C.CFUNCTYPE(None, C.c_void_p, C.c_uint64, C.c_uint32, C.c_void_p)
        function = kind(callback)
        self.callbacks.append(function)
        handle = C.c_size_t()
        self.assertEqual(self.lib.guest_hook_add(self.guest, C.byref(handle), mask,
                         C.cast(function, C.c_void_p), None, 0x4000, 0x4000), 0)
        return handle.value

    def test_a_single_block_hook_can_add_the_next_callback(self):
        seen = []
        def add(*_):
            seen.append(1)
            self.hook(8, lambda *_: seen.append(2))
        self.hook(8, add)
        self.execute(0x4001)
        self.assertEqual(seen, [1, 2])

    def test_hook_masks_keep_block_and_code_registration_order(self):
        seen = []
        self.hook(4 | 8, lambda *_: seen.append(1))
        self.hook(8, lambda *_: seen.append(2))
        self.hook(4, lambda *_: seen.append(3))
        self.execute(0x4001)
        # Dispatch checks code hooks before the translated entry emits block/code events.
        self.assertEqual(seen, [1, 3, 1, 2, 1, 3])

    def test_stopping_in_a_block_hook_prevents_remaining_callbacks(self):
        seen = []
        def stop(*_):
            seen.append(1)
            self.assertEqual(self.lib.guest_stop(self.guest), 0)
        self.hook(8, stop)
        self.hook(8 | 4, lambda *_: seen.append(2))
        self.put(LR, 0x9000)
        self.assertNotEqual(self.lib.guest_run(self.guest, 0x4001, 0x9000), 0)
        self.assertIn(b'stopped', self.lib.guest_error(self.guest))
        self.assertEqual(seen, [2, 1])

    def test_deleting_a_future_hook_keeps_later_callbacks_safe(self):
        program = """
import ctypes, sys
lib = ctypes.CDLL(sys.argv[1])
lib.fixture_delete_future_hook.restype = ctypes.c_uint
print(lib.fixture_delete_future_hook())
"""
        result = subprocess.run([_sys.executable, '-c', program, str(Path(self.directory.name) / 'guest.so')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '1313', 'a removed callback must not run or hide later callbacks')

    def test_budget_stops_generated_loop_and_reports_original_dispatch_error(self):
        self.host(lambda *_: 1)
        self.assertEqual(self.lib.dispatch_begin_call(self.dispatch, 20), 0)
        self.assertNotEqual(self.lib.guest_run(self.guest, 0x7001, 0x9000), 0)
        self.assertIn(b'instruction limit', self.lib.dispatch_error(self.dispatch))
        self.lib.dispatch_end_call(self.dispatch)


if __name__ == '__main__':
    unittest.main()
