"""Guest32 memory and register adapter for the ahead-of-time game module."""
import ctypes as C
from functools import lru_cache
from pathlib import Path
import weakref


class State(C.Structure):
    _fields_ = [('r', C.c_uint32 * 16), ('nf', C.c_uint32), ('zf', C.c_uint32),
                ('cf', C.c_uint32), ('vf', C.c_uint32), ('qf', C.c_uint32),
                ('vectors', C.c_uint64 * 32), ('fpscr', C.c_uint32), ('image_base', C.c_uint32),
                ('itstate', C.c_uint32), ('cpsr', C.c_uint32), ('cpacr', C.c_uint32), ('fpexc', C.c_uint32)]


@lru_cache(maxsize=1)
def library():
    path = Path(__file__).resolve().parent / 'game.so'
    if not path.exists():
        raise RuntimeError('The recompiled game module is missing; run tools/recompile.py')
    lib = C.CDLL(str(path))
    signatures = {
        'guest_create': ([C.c_void_p], C.c_void_p), 'guest_free': ([C.c_void_p], None),
        'guest_context': ([C.c_void_p], C.POINTER(State)), 'guest_context_size': ([], C.c_size_t),
        'guest_context_save': ([C.c_void_p, C.c_void_p, C.c_size_t], C.c_int),
        'guest_context_restore': ([C.c_void_p, C.c_void_p, C.c_size_t], C.c_int),
        'guest_context_jump': ([C.c_void_p, C.c_void_p, C.c_size_t], C.c_int),
        'guest_get_api': ([C.c_void_p], None),
        'guest_run': ([C.c_void_p, C.c_uint32, C.c_uint32], C.c_int),
        'guest_error': ([C.c_void_p], C.c_char_p),
        'guest_reg_read': ([C.c_void_p, C.c_int, C.c_void_p], C.c_int),
        'guest_reg_write': ([C.c_void_p, C.c_int, C.c_void_p], C.c_int),
        'guest_mem_map': ([C.c_void_p, C.c_uint64, C.c_size_t, C.c_uint32], C.c_int),
        'guest_mem_read': ([C.c_void_p, C.c_uint64, C.c_void_p, C.c_size_t], C.c_int),
        'guest_mem_write': ([C.c_void_p, C.c_uint64, C.c_void_p, C.c_size_t], C.c_int),
        'doodle_engine_sha256': ([], C.c_char_p),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    if lib.guest_context_size() != C.sizeof(State):
        raise RuntimeError('Incompatible recompiled guest context')
    return lib


class Machine:
    def __init__(self, engine_sha256):
        self.lib = lib = library()
        if lib.doodle_engine_sha256().decode() != engine_sha256:
            raise ValueError('The recompiled module does not match the original engine')
        self._uch = lib.guest_create(C.cast(lib.doodle_resolve, C.c_void_p))
        if not self._uch:
            raise MemoryError('Cannot allocate the recompiled engine context')
        self._finalizer = weakref.finalize(self, lib.guest_free, self._uch)
        lib.guest_context(self._uch).contents.image_base = 0x10000000

    def api(self, kind):
        result = kind()
        self.lib.guest_get_api(C.byref(result))
        return result

    def check(self, status):
        if status:
            message = self.lib.guest_error(self._uch).decode()
            state = self.lib.guest_context(self._uch).contents
            raise RuntimeError(f'{message} (PC={state.r[15]:#x}, LR={state.r[14]:#x})')

    def mem_map(self, address, size, permissions=7):
        self.check(self.lib.guest_mem_map(self._uch, address, size, permissions))

    def mem_read(self, address, size):
        output = C.create_string_buffer(size)
        self.check(self.lib.guest_mem_read(self._uch, address, output, size))
        return bytearray(output.raw)

    def mem_write(self, address, data):
        data = bytes(data)
        self.check(self.lib.guest_mem_write(self._uch, address, data, len(data)))

    def reg_read(self, register):
        width = 16 if 50 <= register <= 65 else 8
        output = (C.c_ubyte * width)()
        self.check(self.lib.guest_reg_read(self._uch, register, output))
        return int.from_bytes(output, 'little')

    def reg_write(self, register, value):
        width = 16 if 50 <= register <= 65 else 8
        data = (value & ((1 << (width * 8)) - 1)).to_bytes(width, 'little')
        buffer = (C.c_ubyte * width).from_buffer_copy(data)
        self.check(self.lib.guest_reg_write(self._uch, register, buffer))

    def context_save(self):
        output = C.create_string_buffer(C.sizeof(State))
        self.check(self.lib.guest_context_save(self._uch, output, len(output)))
        return output

    def context_restore(self, context):
        self.check(self.lib.guest_context_restore(self._uch, context, len(context)))

    def context_jump(self, context):
        self.check(self.lib.guest_context_jump(self._uch, context, len(context)))

    def emu_start(self, address, until):
        self.check(self.lib.guest_run(self._uch, address, until))
