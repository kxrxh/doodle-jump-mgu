"""Python reference for the generated C and instruction coverage."""
import hashlib
import json
from pathlib import Path

from recompile import assemble_helpers
from .elf import ElfImage, Function
from .lifter import Lifter


def apply_profile(image, path):
    profile = json.loads(Path(path).read_text())
    digest = hashlib.sha256(image.path.read_bytes()).hexdigest()
    if profile.get('engine_sha256') != digest or not isinstance(profile.get('entries'), dict):
        raise ValueError('Trace does not match the original engine')
    modes = dict(image.entry_modes)
    for address, thumb in profile['entries'].items():
        value = int(address, 0)
        if type(thumb) is not bool or value & 1 or not any(low <= value < low + size for low, size, _ in image.sections):
            raise ValueError('Invalid trace entry point')
        modes[value] = thumb
    image.entry_modes = modes
    starts = {a: function.thumb for a, function in image.functions.items()}
    image._ranges({**starts, **modes})


def discover(image):
    """Refine EHABI ranges with direct call targets and ARM/Thumb interworking."""
    starts = {a: f.thumb for a, f in image.functions.items()}
    known = dict(getattr(image, 'entry_modes', starts))
    lifter = Lifter()
    lifter.noreturn = getattr(image, 'noreturn', set())
    for _ in range(16):
        found = {}
        interiors = set()
        for address in sorted(known):
            function = image.functions[address]
            lifter.thumb = function.thumb
            lifter._body = image.read(function.address, function.size)
            lifter._body_address = function.address
            instructions = lifter.decode(lifter._body, function.address, function.thumb)
            for address in lifter.reachable(instructions, function.address):
                instruction = instructions[address]
                interiors.update(range(address + 1, address + instruction.size))
                operation = lifter.root(instruction)
                operands = instruction.operands
                if operation == 'bx' and operands and operands[0].type == 1 and instruction.reg_name(operands[0].reg) == 'pc':
                    found[(address + (4 if function.thumb else 8)) & ~3] = False
                    continue
                if operation not in ('b', 'bl', 'blx', 'cbz', 'cbnz') or not operands or operands[-1].type != 2:
                    continue
                target = operands[-1].imm & 0xfffffffe
                if operation in ('b', 'cbz', 'cbnz') and function.address <= target < function.address + function.size:
                    continue
                if any(low <= target < low + size for low, size, _ in image.sections):
                    found[target] = not function.thumb if operation == 'blx' else function.thumb
            for table in lifter.tables.values():
                for target in table:
                    if not function.address <= target < function.address + function.size and any(
                            low <= target < low + size for low, size, _ in image.sections):
                        found[target] = function.thumb
            if lifter._fallthrough:
                target = function.address + function.size
                if any(low <= target < low + size for low, size, _ in image.sections):
                    found[target] = function.thumb
        # A false branch decoded from a literal pool must not truncate real code.
        found = {address: mode for address, mode in found.items() if address not in interiors}
        changed = any(a not in known for a in found)
        for address, mode in found.items():
            if address not in known:
                known[address] = mode
        starts.update(known)
        image._ranges(starts)
        if not changed:
            break
    image.discovery_converged = not changed


def helpers():
    from elftools.elf.elffile import ELFFile
    path = assemble_helpers()
    with path.open('rb') as stream:
        elf = ELFFile(stream)
        code = elf.get_section_by_name('.text').data()
        starts = sorted({s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()
                         if s.name.endswith('_native')})
    functions = [Function(0x01100000 + start, end - start, False, 'helpers')
                 for start, end in zip(starts, starts[1:] + [len(code)])]
    return code, functions


def generate(original, output, profile=None):
    image = ElfImage(original)
    if profile is not None:
        apply_profile(image, profile)
    discover(image)
    helper_code, helper_functions = helpers()
    functions = sorted([*image.functions.values(), *helper_functions], key=lambda function: function.address)
    lifter = Lifter()
    lifter.noreturn = image.noreturn
    lifter.link_base = image.base
    bodies = []
    for function in functions:
        if function.section == 'helpers':
            body = helper_code[function.address - 0x01100000:function.address - 0x01100000 + function.size]
        else:
            body = image.read(function.address, function.size)
        # ELF relocations are applied at run time; do not fold unrelocated pointers.
        source, complete = lifter.lift_function(function.address, function.size, body, function.thumb)
        bodies.append((function, source, complete))
    output.mkdir(parents=True, exist_ok=True)
    defined = {function.address for function in functions}
    missing = lifter.referenced - defined
    header = '#pragma once\n#include "arm32_context.h"\n'
    header += ''.join(f'void {lifter.name(address)}(Arm32Ctx* c);\n' for address in sorted(defined | missing))
    continuations = {address: owner for address, owner in getattr(lifter, 'continuations', {}).items()
                     if address not in defined}
    header += ''.join(f'void {lifter.name(address)}_resume(Arm32Ctx* c, uint32_t pc);\n'
                      for address in sorted({owner for owner, _ in continuations.values()}))
    (output / 'lifted.h').write_text(header)
    shards = ['#include "lifted.h"\n' for _ in range(32)]
    for index, (_, source, _) in enumerate(bodies):
        shards[index % len(shards)] += source + '\n'
    for index, shard in enumerate(shards):
        (output / f'lifted_{index:02}.c').write_text(shard)
    dispatch = '#include "lifted.h"\n'
    for address in sorted(missing):
        dispatch += f'void {lifter.name(address)}(Arm32Ctx* c) {{ arc_dispatch(c, {address:#x}u); }}\n'
    for address, (owner, _) in sorted(continuations.items()):
        dispatch += f'static void resume_{address:08x}(Arm32Ctx* c) {{ {lifter.name(owner)}_resume(c, {address:#x}u); }}\n'
    entries = {function.address: (lifter.name(function.address), function.thumb) for function in functions}
    entries.update({address: (f'resume_{address:08x}', thumb)
                    for address, (_, thumb) in continuations.items()})
    dispatch += 'struct Entry { uint32_t address; void (*call)(Arm32Ctx*); unsigned thumb; };\nstatic const struct Entry entries[] = {\n'
    dispatch += ''.join(f'{{{address:#x}u,{name},{int(thumb)}u}},\n'
                        for address, (name, thumb) in sorted(entries.items()))
    dispatch += '''};
void doodle_resolve(Arm32Ctx* c, uint32_t target) {
    uint32_t address = target & ~1u;
    size_t lo = 0, hi = sizeof(entries)/sizeof(entries[0]);
    while(lo < hi) { size_t mid = lo + (hi-lo)/2;
        if(entries[mid].address < address) lo=mid+1; else hi=mid; }
    if(lo == sizeof(entries)/sizeof(entries[0]) || entries[lo].address != address) {
        arc_dispatch_miss(c,target); return;
    }
    if ((target & 1u) != entries[lo].thumb) { arc_trap(c,"AOT instruction mode mismatch"); return; }
    entries[lo].call(c);
}
'''
    digest = hashlib.sha256(Path(original).read_bytes()).hexdigest()
    dispatch += f'const char* doodle_engine_sha256(void) {{ return "{digest}"; }}\n'
    (output / 'module.c').write_text(dispatch)
    report = dict(engine_sha256=digest, functions=len(bodies), complete_functions=sum(ok for _, _, ok in bodies),
                  discovery_converged=image.discovery_converged,
                  instructions=lifter.insns_total, supported_instructions=lifter.insns_lifted,
                  continuations=len(continuations),
                  unresolved_calls=[hex(address) for address in sorted(missing)],
                  unsupported=lifter.unsupported.most_common())
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2), flush=True)
    return report
