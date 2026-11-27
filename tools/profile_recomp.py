"""Seed AOT discovery with entry points observed in the pinned Unicorn engine."""
import argparse
import contextlib
import ctypes as C
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from prepare_reference import activate
activate()


class Block(C.Structure):
    _fields_ = [('address', C.c_uint64), ('size', C.c_uint32),
                ('cpsr', C.c_uint32), ('lr', C.c_uint32)]


class Edge(C.Structure):
    _fields_ = [('source', C.c_uint64), ('target', C.c_uint64), ('size', C.c_uint32),
                ('from_cpsr', C.c_uint32), ('from_lr', C.c_uint32), ('to_cpsr', C.c_uint32)]


def helper():
    source = ROOT / 'tools/recomp_reference/profile.cpp'
    directory = ROOT / '.cache/recomp-profile'
    directory.mkdir(parents=True, exist_ok=True)
    library = directory / f'profile-{sys.platform}-{platform.machine()}.so'
    marker = library.with_suffix('.sha256')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if not library.exists() or not marker.exists() or marker.read_text().strip() != digest:
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            output = Path(temporary) / library.name
            subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++14', '-shared', '-fPIC', '-O3',
                            '-Wall', '-Wextra', '-Werror', str(source), '-o', str(output)], check=True)
            output.replace(library)
            marker.write_text(digest + '\n')
    result = C.CDLL(str(library))
    definitions = {
        'profile_create': (C.c_void_p, [C.c_uint64, C.c_uint64, C.c_void_p, C.c_size_t, C.c_size_t]),
        'profile_free': (None, [C.c_void_p]), 'profile_error': (C.c_char_p, [C.c_void_p]),
        'profile_blocks': (C.POINTER(Block), [C.c_void_p]),
        'profile_edges': (C.POINTER(Edge), [C.c_void_p]),
        'profile_block_count': (C.c_size_t, [C.c_void_p]),
        'profile_edge_count': (C.c_size_t, [C.c_void_p]),
    }
    for name, (restype, argtypes) in definitions.items():
        function = getattr(result, name)
        function.restype, function.argtypes = restype, argtypes
    return result


class Recorder:
    def __init__(self, uc, begin, end, max_blocks=200000, max_edges=500000):
        from unicorn.unicorn_py3.unicorn import uclib
        from unicorn import UC_HOOK_BLOCK
        self.library, self.uc, self.uclib = helper(), uc, uclib
        self.handle = self.library.profile_create(begin, end, C.cast(uclib.uc_reg_read, C.c_void_p),
                                                   max_blocks, max_edges)
        if not self.handle:
            raise RuntimeError('Cannot create bounded Unicorn block profile')
        self.hook = C.c_size_t()
        status = uclib.uc_hook_add(uc._uch, C.byref(self.hook), UC_HOOK_BLOCK,
                                   C.cast(self.library.profile_block, C.c_void_p), self.handle, 1, 0)
        if status:
            self.library.profile_free(self.handle)
            self.handle = None
            raise RuntimeError(f'Cannot install native block recorder: {status}')

    def records(self):
        error = self.library.profile_error(self.handle)
        if error:
            raise RuntimeError(error.decode())
        blocks = self.library.profile_blocks(self.handle)
        edges = self.library.profile_edges(self.handle)
        return ([Block(b.address, b.size, b.cpsr, b.lr)
                 for b in (blocks[i] for i in range(self.library.profile_block_count(self.handle)))],
                [Edge(e.source, e.target, e.size, e.from_cpsr, e.from_lr, e.to_cpsr)
                 for e in (edges[i] for i in range(self.library.profile_edge_count(self.handle)))])

    def close(self):
        if self.handle:
            status = self.uclib.uc_hook_del(self.uc._uch, self.hook)
            if status:
                raise RuntimeError(f'Cannot remove native block recorder: {status}')
            self.library.profile_free(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def entries_from_trace(image, blocks, edges):
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB
    from capstone import arm as ca
    decoders = {False: Cs(CS_ARCH_ARM, CS_MODE_ARM), True: Cs(CS_ARCH_ARM, CS_MODE_THUMB)}
    for decoder in decoders.values():
        decoder.detail = True
    entries = {}
    metadata = set(getattr(image, 'entry_modes', {})) | set(getattr(image, 'functions', {}))
    def add(address, thumb):
        address &= 0xfffffffe
        if not any(start <= address < start + size for start, size, _ in image.sections):
            return
        if address in entries and entries[address] != thumb:
            raise ValueError(f'Profile observed both ARM and Thumb at {address:#x}')
        entries[address] = thumb
    for block in blocks:
        if block.address in metadata:
            add(block.address, bool(block.cpsr & 32))
    decoded = {}
    for edge in edges:
        thumb = bool(edge.from_cpsr & 32)
        key = (edge.source, edge.size, thumb)
        if key not in decoded:
            instructions = list(decoders[thumb].disasm(image.read(edge.source, edge.size), edge.source))
            if not instructions or instructions[-1].address + instructions[-1].size != edge.source + edge.size:
                raise ValueError(f'Cannot decode recorded block at {edge.source:#x}')
            decoded[key] = instructions[-1]
        instruction = decoded[key]
        operands = instruction.operands
        if instruction.id in (ca.ARM_INS_BL, ca.ARM_INS_BLX):
            if operands[-1].type == ca.ARM_OP_IMM:
                if (operands[-1].imm & ~1) != edge.target:
                    continue  # A conditional call fell through.
            add(edge.target, bool(edge.to_cpsr & 32))
        elif instruction.id == ca.ARM_INS_BX:
            if instruction.reg_name(operands[0].reg) not in ('lr', 'r14') and edge.target != (edge.from_lr & ~1):
                add(edge.target, bool(edge.to_cpsr & 32))
        elif instruction.id not in (ca.ARM_INS_POP, ca.ARM_INS_TBB, ca.ARM_INS_TBH) and operands:
            block_load = instruction.mnemonic.startswith('ldm')
            writes_pc = operands[0].type == ca.ARM_OP_REG and instruction.reg_name(operands[0].reg) == 'pc'
            writes_pc |= block_load and any(op.type == ca.ARM_OP_REG and instruction.reg_name(op.reg) == 'pc'
                                          for op in operands[1:])
            stack = any(op.type == ca.ARM_OP_MEM and instruction.reg_name(op.mem.base) == 'sp' for op in operands)
            stack |= block_load and operands[0].type == ca.ARM_OP_REG and instruction.reg_name(operands[0].reg) == 'sp'
            returns_lr = not block_load and any(op.type == ca.ARM_OP_REG and instruction.reg_name(op.reg) == 'lr'
                                              for op in operands[1:])
            if writes_pc and not stack and not returns_lr and edge.target != (edge.from_lr & ~1):
                add(edge.target, bool(edge.to_cpsr & 32))
    return entries


def profile(original, themes, frames, play, max_blocks, max_edges):
    from arm import Arm, BASE, ENGINE_SHA256
    from game import Game
    from audio import Audio
    from recomp_reference.elf import ElfImage
    library = original / 'extracted/lib/armeabi-v7a/libDoodleJump.so'
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    if digest != ENGINE_SHA256:
        raise ValueError('Expected the pinned original Doodle Jump ARM engine')
    image = ElfImage(library)
    end = max(start + size for start, size, _ in image.sections)
    all_blocks, all_edges = [], []
    initialize = Arm.__init__
    active = []
    def instrument(machine, *args, **kwargs):
        initialize(machine, *args, **kwargs)
        if machine.backend != 'unicorn':
            raise ValueError('Build-time tracing requires the Unicorn oracle')
        active.append(Recorder(machine.uc, BASE, end, max_blocks, max_edges))
    sessions = []
    for theme in themes:
        game = None
        try:
            with tempfile.TemporaryDirectory(prefix='doodle-profile-game-') as temporary, \
                 open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), \
                 patch.object(Arm, '__init__', instrument), patch('libc.time.time', return_value=1700000000):
                data = Path(temporary)
                (data / 'appdata.i3d').write_text(
                    '<DoodleJumpHScore value="10000" />\n'
                    f'<DoodleJumpActiveTheme value="{theme}" />\n'
                    f'<DoodleJumpDefaultTheme value="{theme}" />\n')
                game = Game(original, data, render_size=(408, 720), backend='unicorn')
                with contextlib.closing(Audio(game.jni.assets, playback=False)) as audio:
                    game.jni.audio = audio
                    def step(touches=(), reset=False):
                        game.graphics.begin_gpu_frame(True, reset=reset)
                        game.step(touches=touches, readback=False)
                        game.graphics.gpu_frame()
                        audio.mix(800)
                    if play:
                        for i in range(240):
                            step(reset=i == 0)
                        step([(0, 0, 180, 295)])
                        step([(0, 2, 180, 295)])
                    for i in range(frames):
                        step(reset=not play and i == 0)
                    for recorder in active:
                        blocks, edges = recorder.records()
                        all_blocks.extend(blocks)
                        all_edges.extend(edges)
                    sessions.append(dict(theme=theme, frames=frames, warmup=242 if play else 0,
                                         playing=game.jni.playing))
        finally:
            for recorder in active:
                recorder.close()
            active.clear()
            game = None
            gc.collect()
    entries = entries_from_trace(image, all_blocks, all_edges)
    return dict(engine_sha256=digest,
                entries={hex(address): entries[address] for address in sorted(entries)},
                coverage=dict(blocks=len(all_blocks), edges=len(all_edges), entrypoints=len(entries),
                              sessions=sessions, scope='Observed execution only; unvisited paths still require static discovery'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, default=ROOT / 'original')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--theme', default='1', help='theme ID, comma-separated IDs, or all')
    parser.add_argument('--frames', type=int, default=8)
    parser.add_argument('--play', action='store_true', help='warm up the menu and press Play before recording frames')
    parser.add_argument('--max-blocks', type=int, default=200000)
    parser.add_argument('--max-edges', type=int, default=500000)
    args = parser.parse_args()
    allowed = (1, 14, 12, 11, 9, 8, 5, 6, 7, 4, 2, 10)
    themes = allowed if args.theme == 'all' else tuple(int(value) for value in args.theme.split(','))
    if not themes or any(theme not in allowed for theme in themes) or not 0 <= args.frames <= 100000:
        parser.error('Invalid theme or frame count')
    if not 1 <= args.max_blocks <= 10000000 or not 1 <= args.max_edges <= 10000000:
        parser.error('Profile capacities must be between 1 and 10000000')
    result = profile(args.original, themes, args.frames, args.play, args.max_blocks, args.max_edges)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result['coverage'], indent=2))


if __name__ == '__main__':
    main()
