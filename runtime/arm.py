"""Execute the unchanged ARM Android library with explicit host imports."""
import hashlib
from pathlib import Path
import struct
import subprocess

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm_const import (
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_C1_C0_2,
    UC_ARM_REG_FPEXC, UC_ARM_REG_CPSR,
)


BASE = 0x10000000
STUB = 0x01000000
RETURN = STUB + 0xFF000
STACK = 0x70000000
SMALL_HEAP = 0x40000000
SMALL_LIMIT = SMALL_HEAP + 0x1000000
REGS = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
ENGINE_SHA256 = '4f92175d8ca5fcab546c0170803f80f7e7b37873b7c51241ef5a12c703fd8158'


class Arm:
    def __init__(self, library):
        if hashlib.sha256(library.read_bytes()).hexdigest() != ENGINE_SHA256:
            raise ValueError('Expected the unmodified Doodle Jump 3.10.0 ARMv7 engine')
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.uc.mem_map(STUB, 0x100000)
        self.native_imports = self._native_helpers()
        self.uc.mem_map(STACK, 0x800000)
        self.uc.mem_map(0x20000000, 0x10000000)
        self.uc.reg_write(UC_ARM_REG_SP, STACK + 0x7FF000)
        self.uc.reg_write(UC_ARM_REG_C1_C0_2, 0xF00000)
        self.uc.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
        self.heap = 0x20000000
        self.allocations = {}
        self.free_blocks = {}
        self.imports = {}
        self.handlers = {}
        self.hooks = {STUB+0xFE000:'malloc',STUB+0xFE004:'free'}
        self.depth = 0
        self.uc.hook_add(UC_HOOK_CODE, self._hook, begin=STUB, end=RETURN - 1)
        self._libc_memory()
        with library.open('rb') as source:
            elf = ELFFile(source)
            loads = [s for s in elf.iter_segments() if s['p_type'] == 'PT_LOAD']
            end = max(s['p_vaddr'] + s['p_memsz'] for s in loads)
            self.uc.mem_map(BASE, (end + 4095) & ~4095)
            for segment in loads:
                self.uc.mem_write(BASE + segment['p_vaddr'], segment.data())
            symbols = elf.get_section_by_name('.dynsym')
            self.symbols = {s.name: BASE + s['st_value'] for s in symbols.iter_symbols()
                            if s['st_shndx'] != 'SHN_UNDEF' and s.name}
            for section in elf.iter_sections():
                if section['sh_type'] not in ('SHT_REL', 'SHT_RELA'):
                    continue
                for relocation in section.iter_relocations():
                    address = BASE + relocation['r_offset']
                    kind = relocation['r_info_type']
                    addend = self.u32(address)
                    if kind == 23:  # R_ARM_RELATIVE
                        value = BASE + addend
                    elif kind in (2, 21, 22):
                        symbol = symbols.get_symbol(relocation['r_info_sym'])
                        value = self.symbols.get(symbol.name)
                        if value is None:
                            value = self.import_address(symbol.name)
                        if kind == 2:  # R_ARM_ABS32
                            value += addend
                    else:
                        raise ValueError(f'Unsupported ARM relocation {kind}')
                    self.put32(address, value)
            constructors = elf.get_section_by_name('.init_array')
            self.constructors = [self.u32(BASE + constructors['sh_addr'] + i)
                                 for i in range(0, constructors['sh_size'], 4)]

    def import_address(self, name):
        if name not in self.imports:
            if name in self.native_imports:
                self.imports[name] = self.native_imports[name]
                return self.imports[name]
            if name in ('__stack_chk_guard', '_ctype_', '_tolower_tab_', '_toupper_tab_', '__sF') or name.startswith('SL_IID_'):
                self.imports[name] = self.malloc(4096)
                return self.imports[name]
            address = STUB + len(self.imports) * 4
            self.uc.mem_write(address, b'\x1e\xff\x2f\xe1')  # ARM bx lr
            self.imports[name] = address
            self.hooks[address] = name
        return self.imports[name]

    def _native_helpers(self):
        directory = Path(__file__).resolve().parent
        source, artifact = directory/'arm_helpers.S', directory/'arm_helpers.o'
        if not artifact.exists() or source.stat().st_mtime > artifact.stat().st_mtime:
            subprocess.run(['clang','-target','armv7-none-eabi','-c',str(source),'-o',str(artifact)],check=True)
        address = STUB + 0x100000
        self.uc.mem_map(address, 8192)
        self.uc.mem_map(SMALL_HEAP,SMALL_LIMIT-SMALL_HEAP)
        self.put32(address+4096,SMALL_HEAP)
        with artifact.open('rb') as data:
            elf = ELFFile(data)
            code = elf.get_section_by_name('.text').data()
            if len(code)>4096 or any(s['sh_type']=='SHT_REL' and s['sh_size'] for s in elf.iter_sections()):
                raise ValueError('ARM helper code must be position independent and fit one page')
            self.uc.mem_write(address,code)
            # Tiny libc calls stay inside the ARM translator instead of crossing into Python.
            return {s.name[:-7]:address+s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()
                    if s.name.endswith('_native')}

    def register(self, name, callback):
        self.handlers[name] = callback
        return self.import_address(name)

    def _hook(self, uc, address, size, data):
        name = self.hooks[address]
        callback = self.handlers.get(name)
        if callback is None:
            raise NotImplementedError(f'Host import {name}, caller {uc.reg_read(UC_ARM_REG_LR):#x}')
        result = callback()
        if isinstance(result, tuple):
            self.uc.reg_write(UC_ARM_REG_R0, result[0] & 0xFFFFFFFF)
            self.uc.reg_write(UC_ARM_REG_R1, result[1] & 0xFFFFFFFF)
        elif result is not None:
            self.uc.reg_write(UC_ARM_REG_R0, result & 0xFFFFFFFF)
            if result > 0xFFFFFFFF:
                self.uc.reg_write(UC_ARM_REG_R1, result >> 32)
        self.uc.reg_write(UC_ARM_REG_PC, self.uc.reg_read(UC_ARM_REG_LR))

    def intercept(self, offset, name, callback):
        address = BASE + offset
        self.handlers[name] = callback
        self.hooks[address] = name
        self.uc.hook_add(UC_HOOK_CODE, self._hook, begin=address, end=address)

    def arg(self, index):
        if index < 4:
            return self.uc.reg_read(REGS[index])
        return self.u32(self.uc.reg_read(UC_ARM_REG_SP) + (index - 4) * 4)

    def u32(self, address):
        return struct.unpack('<I', self.uc.mem_read(address, 4))[0]

    def put32(self, address, value):
        self.uc.mem_write(address, struct.pack('<I', value & 0xFFFFFFFF))

    def string(self, address, limit=1 << 20):
        if not address:
            return b''
        out = bytearray()
        while len(out) < limit:
            part = bytes(self.uc.mem_read(address + len(out), min(256, limit - len(out))))
            end = part.find(0)
            if end >= 0:
                return bytes(out) + part[:end]
            out.extend(part)
        raise ValueError('Unterminated guest string')

    def malloc(self, size):
        size = max(16, (int(size) + 15) & ~15)
        if size <= 65536:
            size = 1 << (size - 1).bit_length()
        blocks = self.free_blocks.get(size)
        if blocks:
            address = blocks.pop()
        else:
            address = self.heap
            self.heap += size
            if self.heap > 0x30000000:
                raise MemoryError('Guest heap exhausted')
        self.allocations[address] = size
        return address

    def free(self, address):
        if SMALL_HEAP<=address<SMALL_LIMIT:
            self.allocation_size(address)
            return self.call(self.native_imports['free'],address)
        if address:
            size = self.allocations.pop(address)
            self.free_blocks.setdefault(size, []).append(address)
        return 0

    def allocation_size(self, address):
        if SMALL_HEAP<=address<SMALL_LIMIT:
            bucket,live=struct.unpack('<II',self.uc.mem_read(address-16,8))
            if address%16 or not 4<=bucket<=12 or live!=1:
                raise ValueError('Invalid small allocation')
            return 1<<bucket
        return self.allocations[address]

    def copy(self, destination, source, size):
        if size:
            self.uc.mem_write(destination, bytes(self.uc.mem_read(source, size)))
        return destination

    def fill(self, destination, value, size):
        if size:
            self.uc.mem_write(destination, bytes([value & 255]) * size)
        return destination

    def _libc_memory(self):
        self.handlers.update({
            'malloc': lambda: self.malloc(self.arg(0)),
            'free': lambda: self.free(self.arg(0)),
            'memcpy': lambda: self.copy(self.arg(0), self.arg(1), self.arg(2)),
            'memmove': lambda: self.copy(self.arg(0), self.arg(1), self.arg(2)),
            'memset': lambda: self.fill(self.arg(0), self.arg(1), self.arg(2)),
            '__aeabi_memcpy': lambda: self.copy(self.arg(0), self.arg(1), self.arg(2)),
            '__aeabi_memmove': lambda: self.copy(self.arg(0), self.arg(1), self.arg(2)),
            '__aeabi_memset': lambda: self.fill(self.arg(0), self.arg(2), self.arg(1)),
            'pthread_mutex_lock': lambda: 0,
            'pthread_mutex_unlock': lambda: 0,
        })

    def call(self, symbol, *arguments):
        address = self.symbols[symbol] if isinstance(symbol, str) else symbol
        context = self.uc.context_save()
        self.depth += 1
        try:
            stack = STACK + 0x7FE000 - self.depth * 0x10000
            self.uc.reg_write(UC_ARM_REG_SP, stack)
            for i, value in enumerate(arguments):
                if i < 4:
                    self.uc.reg_write(REGS[i], value & 0xFFFFFFFF)
                else:
                    self.put32(stack + (i - 4) * 4, value)
            self.uc.reg_write(UC_ARM_REG_LR, RETURN)
            self.uc.emu_start(address, RETURN, count=1_000_000_000)
            if self.uc.reg_read(UC_ARM_REG_PC) != RETURN:
                raise RuntimeError(f'Engine call instruction limit at {self.uc.reg_read(UC_ARM_REG_PC):#x}')
            return self.uc.reg_read(UC_ARM_REG_R0)
        except UcError as error:
            raise RuntimeError(f'ARM fault PC={self.uc.reg_read(UC_ARM_REG_PC):#x} '
                               f'LR={self.uc.reg_read(UC_ARM_REG_LR):#x}: {error}') from error
        finally:
            self.depth -= 1
            self.uc.context_restore(context)

    def initialize(self):
        for constructor in self.constructors:
            if constructor not in (0, 0xFFFFFFFF):
                self.call(constructor)
