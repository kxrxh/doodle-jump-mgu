"""Function ranges and mapped bytes for the pinned ARM Android library."""
from dataclasses import dataclass
from pathlib import Path
import struct

from elftools.elf.elffile import ELFFile


@dataclass
class Function:
    address: int
    size: int
    thumb: bool
    section: str


class ElfImage:
    def __init__(self, path, base=0x10000000):
        self.path = Path(path)
        self.base = base
        self.symbols = {}
        self.functions = {}
        self.segments = []
        self.sections = []
        self.noreturn = set()
        self.unwind_entries = 0
        starts = {}
        with self.path.open('rb') as stream:
            elf = ELFFile(stream)
            if elf.elfclass != 32 or not elf.little_endian or elf['e_machine'] != 'EM_ARM' or elf['e_type'] != 'ET_DYN':
                raise ValueError('Expected a little-endian ARM32 shared library')
            for segment in elf.iter_segments():
                if segment['p_type'] == 'PT_LOAD':
                    address = base + segment['p_vaddr']
                    size = segment['p_memsz']
                    if address < base or address + size > 0x100000000:
                        raise ValueError('ELF segment exceeds guest address space')
                    self.segments.append((address, segment.data() + bytes(size - segment['p_filesz'])))
            for section in elf.iter_sections():
                if section['sh_flags'] & 4:
                    self.sections.append((base + section['sh_addr'], section['sh_size'], section.name))
            dynamic = elf.get_section_by_name('.dynsym')
            if dynamic is None:
                raise ValueError('ELF dynamic symbols are missing')
            plt, relocations = elf.get_section_by_name('.plt'), elf.get_section_by_name('.rel.plt')
            if plt and relocations:
                fatal = {'abort', 'exit', '_exit', '__assert2', '__stack_chk_fail', '__cxa_throw'}
                for index, relocation in enumerate(relocations.iter_relocations()):
                    if dynamic.get_symbol(relocation['r_info_sym']).name in fatal:
                        self.noreturn.add(base + plt['sh_addr'] + 20 + 12 * index)
            for symbol in dynamic.iter_symbols():
                if symbol['st_shndx'] == 'SHN_UNDEF' or not symbol.name:
                    continue
                address = base + symbol['st_value']
                self.symbols[symbol.name] = address
                if symbol['st_info']['type'] == 'STT_FUNC':
                    starts[address & ~1] = bool(address & 1)
            for section in elf.iter_sections():
                if section['sh_type'] not in ('SHT_REL', 'SHT_RELA'):
                    continue
                for relocation in section.iter_relocations():
                    if relocation['r_info_type'] != 23:
                        continue
                    value = struct.unpack('<I', self.read(base + relocation['r_offset'], 4))[0]
                    target = base + value
                    if any(low <= (target & ~1) < low + size for low, size, _ in self.sections) and value & 1:
                        starts[target & ~1] = True
            constructors = elf.get_section_by_name('.init_array')
            if constructors:
                data = constructors.data()
                for offset in range(0, len(data), 4):
                    value = struct.unpack_from('<I', data, offset)[0]
                    if value not in (0, 0xffffffff):
                        starts[(base + value) & ~1] = bool(value & 1)
            self.entry_modes = dict(starts)
            unwind = elf.get_section_by_name('.ARM.exidx')
            if unwind:
                data = unwind.data()
                if len(data) % 8:
                    raise ValueError('Truncated ARM unwind index')
                self.unwind_entries = len(data) // 8
                for offset in range(0, len(data), 8):
                    relative = struct.unpack_from('<I', data, offset)[0] & 0x7fffffff
                    if relative & 0x40000000:
                        relative -= 0x80000000
                    address = base + unwind['sh_addr'] + offset + relative
                    # EHABI PREL31 addresses omit the mode bit. The pinned engine's
                    # executable exports are Thumb; explicit BLX targets refine this later.
                    starts.setdefault(address, True)
            for address, size, name in self.sections:
                if name == '.plt':
                    starts[address] = False
                    self.entry_modes[address] = False
                    for offset in range(20, size - 11, 12):
                        starts[address + offset] = False
                        self.entry_modes[address + offset] = False
                else:
                    starts.setdefault(address, True)
            self._ranges(starts)

    def _ranges(self, starts):
        self.functions.clear()
        for address, size, section in self.sections:
            boundaries = sorted(start for start in starts if address <= start < address + size)
            for start, end in zip(boundaries, boundaries[1:] + [address + size]):
                self.functions[start] = Function(start, end - start, starts[start], section)

    def read(self, address, size):
        if address < 0 or size < 0 or address + size > 0x100000000:
            raise ValueError('Invalid guest read')
        for start, data in self.segments:
            if start <= address and address + size <= start + len(data):
                return data[address - start:address - start + size]
        raise ValueError(f'ELF read outside mapped segments: {address:#x}+{size}')
