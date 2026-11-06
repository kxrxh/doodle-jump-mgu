"""Android's C ABI at the ARM/native host boundary; no Android code is replaced."""
import functools
import ctypes
import math
import re
import struct
import time
import zlib


def signed(value):
    return value - 0x100000000 if value & 0x80000000 else value


class LibC:
    def __init__(self, machine):
        self.m = m = machine
        self.elapsed = 0.0
        self.random = 0x1234ABCD330E
        self.keys = {}
        self.streams = {}
        self.errno = m.malloc(4)
        self.calendar = m.malloc(64)
        self.epoch = int(time.time())
        self.locale = self.cstring(b'C')
        self.exits = []
        self.jumps = {}
        self.calls = {}
        a = m.arg
        s = lambda i: m.string(a(i))
        handlers = {
            'strlen': lambda: len(s(0)),
            'strcmp': lambda: (s(0) > s(1)) - (s(0) < s(1)),
            'strncmp': lambda: (s(0)[:a(2)] > s(1)[:a(2)]) - (s(0)[:a(2)] < s(1)[:a(2)]),
            'strcpy': lambda: self.write_string(a(0), s(1)),
            'strncpy': self.strncpy,
            'strchr': lambda: self.find(a(0), s(0) + b'\0', bytes([a(1) & 255])),
            'strstr': lambda: self.find(a(0), s(0), s(1)),
            'memcmp': self.memcmp,
            'memchr': lambda: self.find(a(0), bytes(m.uc.mem_read(a(0), a(2))), bytes([a(1) & 255])),
            'calloc': self.calloc,
            'realloc': self.realloc,
            'memalign': self.memalign,
            'atoi': lambda: int((re.match(rb'\s*[+-]?\d+', s(0)) or [b'0'])[0]),
            'strtod': self.strtod,
            'sscanf': self.sscanf,
            'puts': lambda: self.log(s(0)),
            'printf': lambda: self.log(self.format(s(0), 1)),
            'fprintf': lambda: self.log(self.format(s(1), 2)),
            'fputc': lambda: self.log(bytes([a(0) & 255])) or a(0),
            'fputs': lambda: self.log(s(0)),
            'sprintf': lambda: self.write_string(a(0), self.format(s(1), 2), count=True),
            'snprintf': lambda: self.write_string(a(0), self.format(s(2), 3), a(1), count=True),
            'vsnprintf': lambda: self.write_string(a(0), self.format(s(2), 0, a(3)), a(1), count=True),
            '__android_log_print': lambda: self.log(self.format(s(2), 3)),
            '__android_log_write': lambda: self.log(s(2)),
            '__cxa_atexit': self.atexit,
            '__aeabi_atexit': self.atexit,
            '__cxa_finalize': lambda: 0,
            'pthread_once': self.once,
            'pthread_mutex_init': lambda: 0,
            'pthread_mutex_destroy': lambda: 0,
            'pthread_key_create': self.key_create,
            'pthread_key_delete': lambda: self.keys.pop(a(0), None) or 0,
            'pthread_getspecific': lambda: self.keys.get(a(0), 0),
            'pthread_setspecific': self.key_set,
            '__errno': lambda: self.errno,
            'setlocale': lambda: self.locale,
            'fcntl': lambda: int(a(0) in (1, 2)) if a(1) == 3 else 0,
            'fstat': self.fstat,
            'clock': lambda: int(self.elapsed * 1_000_000),
            'clock_gettime': self.clock_gettime,
            'time': self.time,
            'localtime': lambda: self.calendar_time(False),
            'gmtime': lambda: self.calendar_time(True),
            'mktime': self.mktime,
            'getpid': lambda: 1,
            'sysconf': lambda: 4096 if signed(a(0)) == 39 else 3,
            'srand48': self.seed,
            'lrand48': self.rand,
            'crc32': lambda: zlib.crc32(bytes(m.uc.mem_read(a(1), a(2))), a(0)) if a(1) else 0,
            'inflateInit_': self.inflate_init,
            'inflateReset': self.inflate_init,
            'inflate': self.inflate,
            'inflateEnd': lambda: self.streams.pop(a(0), None) and 0,
            'qsort': self.qsort,
            'setjmp': self.setjmp,
            'longjmp': self.longjmp,
        }
        for name in ('__aeabi_memclr', '__aeabi_memclr4', '__aeabi_memclr8'):
            handlers[name] = lambda: m.fill(a(0), 0, a(1))
        for name in ('abort', 'exit', '_exit', '__stack_chk_fail', '__assert2'):
            handlers[name] = lambda name=name: self.fatal(name)
        for name in ('sigaction', 'sigprocmask', 'bsd_signal', 'fflush', 'ferror'):
            handlers[name] = lambda: 0
        for name in ('sqrt', 'sin', 'cos', 'floor', 'ceil', 'atan', 'exp', 'acos', 'log', 'trunc', 'pow', 'atan2', 'fmax', 'round', 'rint'):
            for suffix in ('', 'f'):
                handlers[name + suffix] = lambda name=name, single=bool(suffix): self.math(name, single)
        for name in ('lrint', 'lround'):
            for suffix in ('', 'f'):
                handlers[name + suffix] = lambda single=bool(suffix): round(self.float_arg(0, single))
        for name in ('isalpha', 'iscntrl', 'isprint', 'ispunct', 'isspace', 'isxdigit', 'isupper', 'islower', 'isdigit'):
            handlers[name] = lambda name=name: self.character(name, a(0))
            handlers[name.replace('is', 'isw', 1)] = lambda name=name: self.character(name, a(0))
        for name in ('tolower', 'toupper', 'towlower', 'towupper'):
            handlers[name] = lambda name=name: self.case(a(0), 'lower' in name)
        handlers.update({
            'wcslen': lambda: len(self.wstring(a(0))),
            'wmemcpy': lambda: m.copy(a(0), a(1), a(2) * 4),
            'wmemmove': lambda: m.copy(a(0), a(1), a(2) * 4),
            'wmemset': self.wmemset,
        })
        m.handlers.update(handlers)
        self.data_tables()

    def data_tables(self):
        m = self.m
        m.put32(m.imports['__stack_chk_guard'], 0x6A09E667)
        for name, lower in (('_tolower_tab_', True), ('_toupper_tab_', False)):
            values = [-1] + [self.case(i, lower) for i in range(256)]
            pointer = m.malloc(514)
            m.uc.mem_write(pointer, struct.pack('<257h', *values))
            m.put32(m.imports[name], pointer)
        values = [0]
        for i in range(256):
            c = chr(i)
            mask = (1 if 65 <= i <= 90 else 0) | (2 if 97 <= i <= 122 else 0)
            mask |= 4 if 48 <= i <= 57 else 0
            mask |= 8 if i in (9, 10, 11, 12, 13, 32) else 0
            mask |= 16 if 33 <= i <= 126 and not c.isalnum() else 0
            mask |= 32 if i < 32 or i == 127 else 0
            mask |= 64 if c in 'abcdefABCDEF0123456789' else 0
            mask |= 128 if i == 32 else 0
            values.append(mask)
        pointer = m.malloc(257)
        m.uc.mem_write(pointer, bytes(values))
        m.put32(m.imports['_ctype_'], pointer)

    @staticmethod
    def case(value, lower):
        if lower and 65 <= value <= 90:
            return value + 32
        if not lower and 97 <= value <= 122:
            return value - 32
        return value

    @staticmethod
    def character(name, value):
        if value > 127:
            return 0
        c = chr(value)
        methods = {'isalpha': c.isalpha, 'isdigit': c.isdigit, 'isspace': c.isspace,
                   'isupper': c.isupper, 'islower': c.islower, 'isprint': c.isprintable}
        if name in methods:
            return int(methods[name]())
        if name == 'iscntrl':
            return int(value < 32 or value == 127)
        if name == 'isxdigit':
            return int(c in '0123456789abcdefABCDEF')
        return int(c.isprintable() and not c.isalnum() and not c.isspace())

    def wstring(self, pointer):
        values = []
        while (value := self.m.u32(pointer + len(values) * 4)):
            values.append(value)
        return values

    def wmemset(self):
        m = self.m
        m.uc.mem_write(m.arg(0), struct.pack('<I', m.arg(1)) * m.arg(2))
        return m.arg(0)

    def cstring(self, value):
        pointer = self.m.malloc(len(value) + 1)
        self.m.uc.mem_write(pointer, value + b'\0')
        return pointer

    def write_string(self, pointer, value, limit=None, count=False):
        output = value if limit is None else value[:max(0, limit - 1)]
        if limit is None or limit:
            self.m.uc.mem_write(pointer, output + b'\0')
        return len(value) if count else pointer

    def strncpy(self):
        m = self.m
        value = m.string(m.arg(1))[:m.arg(2)]
        m.uc.mem_write(m.arg(0), value.ljust(m.arg(2), b'\0'))
        return m.arg(0)

    @staticmethod
    def find(pointer, haystack, needle):
        index = haystack.find(needle)
        return pointer + index if index >= 0 else 0

    def memcmp(self):
        m = self.m
        left = bytes(m.uc.mem_read(m.arg(0), m.arg(2)))
        right = bytes(m.uc.mem_read(m.arg(1), m.arg(2)))
        return next((a - b for a, b in zip(left, right) if a != b), 0)

    def calloc(self):
        size = self.m.arg(0) * self.m.arg(1)
        pointer = self.m.malloc(size)
        self.m.fill(pointer, 0, size)
        return pointer

    def realloc(self):
        m = self.m
        old, size = m.arg(0), m.arg(1)
        pointer = m.malloc(size)
        if old:
            m.copy(pointer, old, min(m.allocation_size(old), size))
            m.free(old)
        return pointer

    def memalign(self):
        m = self.m
        alignment, size = m.arg(0), m.arg(1)
        m.heap = (m.heap + alignment - 1) & ~(alignment - 1)
        return m.malloc(size)

    def strtod(self):
        m = self.m
        value = m.string(m.arg(0))
        match = re.match(rb'\s*[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|inf(?:inity)?|nan)', value, re.I)
        if m.arg(1):
            m.put32(m.arg(1), m.arg(0) + (match.end() if match else 0))
        number = float(match[0]) if match else 0.0
        return struct.unpack('<II', struct.pack('<d', number))

    def sscanf(self):
        m = self.m
        source, template = m.string(m.arg(0)), m.string(m.arg(1))
        outputs = []
        pattern = re.compile(rb'%(?:%|(\*)?(\d*)(hh|ll|h|l|L|z)?([diuoxXfFeEgGcs]))')
        position = 0
        matches = []
        while True:
            position = template.find(b'%', position)
            if position < 0:
                break
            match = pattern.match(template, position)
            if not match:
                raise NotImplementedError(f'Unsupported scanf format {template!r}')
            position = match.end()
            if match[4]:
                length, kind = match[3] or b'', match[4]
                if (kind in b'sc' and length) or (kind in b'fFeEgG' and length not in (b'', b'l')):
                    raise NotImplementedError(f'Unsupported scanf length {template!r}')
                matches.append(match)
        for match in matches:
            if match[1]:
                continue
            length, kind = match[3] or b'', match[4]
            if kind in b'sc':
                buffer = ctypes.create_string_buffer(max(len(source) + 1, int(match[2] or b'1') + 1))
                size = None
            elif kind in b'fFeEgG':
                buffer = ctypes.c_double() if length == b'l' else ctypes.c_float()
                size = ctypes.sizeof(buffer)
            else:
                integer = {b'hh': ctypes.c_byte, b'h': ctypes.c_short, b'l': ctypes.c_long,
                           b'll': ctypes.c_longlong, b'z': ctypes.c_size_t}.get(length, ctypes.c_int)
                buffer = integer()
                size = 4 if length in (b'l', b'z') else ctypes.sizeof(buffer)
            outputs.append((buffer, size, kind, match[2]))
        scanner = ctypes.CDLL(None).sscanf
        scanner.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        scanner.restype = ctypes.c_int
        result = scanner(source, template, *[ctypes.byref(buffer) for buffer, _, _, _ in outputs])
        for i, (buffer, size, kind, width) in enumerate(outputs[:max(0, result)]):
            value = ctypes.string_at(ctypes.byref(buffer), size) if size else (buffer.value + b'\0' if kind == b's' else buffer.raw[:int(width or b'1')])
            m.uc.mem_write(m.arg(i + 2), value)
        return result

    def float_arg(self, index, single):
        if single:
            return struct.unpack('<f', struct.pack('<I', self.m.arg(index)))[0]
        return struct.unpack('<d', struct.pack('<II', self.m.arg(index), self.m.arg(index + 1)))[0]

    def math(self, name, single):
        x = self.float_arg(0, single)
        if name in ('pow', 'atan2', 'fmax'):
            y = self.float_arg(1 if single else 2, single)
            result = max(x, y) if name == 'fmax' else getattr(math, name)(x, y)
        elif name == 'round':
            result = math.floor(x + 0.5) if x >= 0 else math.ceil(x - 0.5)
        elif name == 'rint':
            result = round(x)
        else:
            result = getattr(math, name)(x)
        if single:
            return struct.unpack('<I', struct.pack('<f', result))[0]
        return struct.unpack('<II', struct.pack('<d', result))

    def atexit(self):
        self.exits.append(tuple(self.m.arg(i) for i in range(3)))
        return 0

    def once(self):
        m = self.m
        pointer, function = m.arg(0), m.arg(1)
        if not m.u32(pointer):
            m.put32(pointer, 1)
            m.call(function)
        return 0

    def key_create(self):
        key = len(self.keys) + 1
        self.keys[key] = 0
        self.m.put32(self.m.arg(0), key)
        return 0

    def key_set(self):
        self.keys[self.m.arg(0)] = self.m.arg(1)
        return 0

    def clock_gettime(self):
        seconds = (self.epoch if self.m.arg(0) == 0 else 0) + self.elapsed
        self.m.uc.mem_write(self.m.arg(1), struct.pack('<ii', int(seconds), int(seconds % 1 * 1e9)))
        return 0

    def fstat(self):
        m = self.m
        m.fill(m.arg(1), 0, 96)
        m.put32(m.arg(1) + 16, 0o20600)
        m.put32(m.arg(1) + 20, 1)
        m.put32(m.arg(1) + 52, 4096)
        return 0

    def time(self):
        seconds = int(self.epoch + self.elapsed)
        if self.m.arg(0):
            self.m.put32(self.m.arg(0), seconds)
        return seconds

    def calendar_time(self, utc):
        seconds = signed(self.m.u32(self.m.arg(0)))
        tm = time.gmtime(seconds) if utc else time.localtime(seconds)
        fields = (tm.tm_sec, tm.tm_min, tm.tm_hour, tm.tm_mday, tm.tm_mon-1,
                  tm.tm_year-1900, (tm.tm_wday+1)%7, tm.tm_yday-1, tm.tm_isdst)
        self.m.uc.mem_write(self.calendar, struct.pack('<9i', *fields))
        return self.calendar

    def mktime(self):
        second, minute, hour, day, month, year, weekday, yearday, dst = struct.unpack('<9i', self.m.uc.mem_read(self.m.arg(0), 36))
        return int(time.mktime((year+1900, month+1, day, hour, minute, second, (weekday+6)%7, yearday+1, dst)))

    def seed(self):
        self.random = (self.m.arg(0) << 16) | 0x330E
        return 0

    def rand(self):
        self.random = (self.random * 0x5DEECE66D + 11) & ((1 << 48) - 1)
        return self.random >> 17

    def inflate_init(self):
        self.streams[self.m.arg(0)] = zlib.decompressobj()
        return 0

    def inflate(self):
        m = self.m
        pointer = m.arg(0)
        inp, avail, total, out, capacity, total_out = struct.unpack('<6I', m.uc.mem_read(pointer, 24))
        stream = self.streams[pointer]
        data = bytes(m.uc.mem_read(inp, avail))
        output = stream.decompress(data, capacity)
        consumed = avail - len(stream.unconsumed_tail) - len(stream.unused_data)
        if output:
            m.uc.mem_write(out, output)
        m.uc.mem_write(pointer, struct.pack('<6I', inp + consumed, avail - consumed, total + consumed,
                                          out + len(output), capacity - len(output), total_out + len(output)))
        return 1 if stream.eof else 0

    def qsort(self):
        m = self.m
        base, count, size, compare = (m.arg(i) for i in range(4))
        order = sorted(range(count), key=functools.cmp_to_key(lambda a, b: signed(m.call(compare, base + a * size, base + b * size))))
        result = b''.join(bytes(m.uc.mem_read(base + i * size, size)) for i in order)
        m.uc.mem_write(base, result)
        return 0

    def setjmp(self):
        self.jumps[self.m.arg(0)] = self.m.uc.context_save()
        return 0

    def longjmp(self):
        context = self.jumps[self.m.arg(0)]
        value = self.m.arg(1) or 1
        self.m.uc.context_restore(context)
        return value

    def format(self, template, start, pointer=None):
        m = self.m
        index = start
        def word():
            nonlocal index
            value = m.u32(pointer + index * 4) if pointer is not None else m.arg(index)
            index += 1
            return value
        def replace(match):
            nonlocal index
            spec = match[0].decode('ascii')
            conversion = spec[-1]
            if conversion == '%':
                return b'%'
            if '*' in spec:
                spec = spec.replace('*', str(signed(word())), 1)
            if conversion in 'fFeEgGaA' or 'll' in spec:
                index += index & 1
                lo, hi = word(), word()
                value = struct.unpack('<d', struct.pack('<II', lo, hi))[0] if conversion in 'fFeEgGaA' else (lo | hi << 32)
            else:
                value = word()
            if conversion == 's':
                value = m.string(value).decode('utf-8', 'replace')
            elif conversion == 'p':
                return f'0x{value:x}'.encode()
            elif conversion in 'di':
                value = signed(value)
            spec = re.sub(r'[hlzjtL]', '', spec)
            try:
                return (spec % value).encode('utf-8')
            except (ValueError, TypeError):
                raise ValueError(f'Unsupported native format {spec}')
        return re.sub(rb'%(?:[-+ #0]*)(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlzjtL]*[diuoxXfFeEgGaAcsp%]', replace, template)

    @staticmethod
    def log(value):
        print('[engine]', value.decode('utf-8', 'replace').rstrip(), flush=True)
        return len(value)

    def fatal(self, name):
        raise RuntimeError(f'Original engine called {name}')
