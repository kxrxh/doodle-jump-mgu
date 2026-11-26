"""Compare emitted ARM/Thumb C with Unicorn on assembled instruction fixtures."""
from pathlib import Path as _Path
import sys as _sys
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / 'tools'))
from prepare_reference import activate as _activate
_REFERENCE = _activate()

import ctypes
from pathlib import Path
import random
import subprocess
import tempfile
import unittest

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm_const as ucarm

from recomp.lifter import Lifter


ROOT = Path(__file__).resolve().parents[2]
ADDRESS, RETURN, MEMORY = 0x100000, 0x200000, 0x300000


def assemble(directory, body, thumb=True):
    source = directory / 'fixture.S'
    source.write_text('.syntax unified\n.arch armv7-a\n.fpu neon\n.text\n' +
                      ('.thumb\n.thumb_func\n' if thumb else '.arm\n') +
                      '.global fixture\n.type fixture,%function\nfixture:\n' + body +
                      '\n.size fixture, .-fixture\n')
    output = directory / 'fixture.o'
    subprocess.run(['clang', '-target', 'armv7-none-eabi', '-c', str(source), '-o', str(output)],
                   check=True, capture_output=True)
    with output.open('rb') as stream:
        return ELFFile(stream).get_section_by_name('.text').data()


class CompiledFixture:
    def __init__(self, body, thumb=True, expect_complete=True, coarse=False, restore_callbacks=False):
        self.temporary = tempfile.TemporaryDirectory(prefix='doodle-lift-')
        directory = Path(self.temporary.name)
        self.thumb = thumb
        self.code = assemble(directory, body, thumb)
        with (directory / 'fixture.o').open('rb') as stream:
            elf = ELFFile(stream)
            symbols = {s.name: ADDRESS + (s['st_value'] & ~1)
                       for s in elf.get_section_by_name('.symtab').iter_symbols() if s.name}
            modes = {s['st_value'] & ~1: bool(s['st_value'] & 1)
                     for s in elf.get_section_by_name('.symtab').iter_symbols()
                     if s['st_info']['type'] == 'STT_FUNC' and s['st_shndx'] != 'SHN_UNDEF'}
            starts = sorted(modes)
        self.lifter = lifter = Lifter()
        self.jump_stubs = (symbols['setjmp_stub'], symbols['longjmp_stub']) if restore_callbacks else (0,0)
        lifter.link_base = ADDRESS
        lifter.starts = {ADDRESS + start for start in starts}
        lifter.image = self.code
        lifter.ro_range = (ADDRESS, ADDRESS + len(self.code))
        parts = []
        complete = True
        for i,start in enumerate(starts):
            end = starts[i+1] if i+1<len(starts) else len(self.code)
            if coarse and i == 0:
                end = len(self.code)
            source, okay = lifter.lift_function(ADDRESS+start,end-start,self.code[start:end],modes[start])
            parts.append(source)
            complete &= okay
        declarations = '\n'.join('void '+lifter.name(ADDRESS+a)+'(Arm32Ctx*);' for a in starts)
        emitted = declarations + '\n' + '\n'.join(parts)
        if expect_complete and not complete:
            self.temporary.cleanup()
            raise AssertionError(dict(lifter.unsupported))
        if not expect_complete:
            self.complete = complete
            return
        source = directory / 'fixture.c'
        dispatch = '\nvoid arc_dispatch(Arm32Ctx* c,uint32_t target) { switch (target&~1u) {\n'
        dispatch += '\n'.join(f'case {ADDRESS+a:#x}u: {lifter.name(ADDRESS+a)}(c); return;' for a in starts)
        dispatch += '\ndefault: abort(); } }\n'
        resumes = 'static int resume_dispatch(Arm32Ctx* c) { switch(c->r[15]) {\n'
        for pc,(owner,mode) in getattr(lifter,'continuations',{}).items():
            resumes += f'case {pc:#x}u: {lifter.name(owner)}_resume(c,{pc:#x}u); return 1;\n'
        resumes += 'default: return 0; } }\n'
        source.write_text(f'#define SETJMP_ADDRESS {self.jump_stubs[0]:#x}u\n#define LONGJMP_ADDRESS {self.jump_stubs[1]:#x}u\n' + '''
#include "arm32_context.h"
#include <stdlib.h>
static unsigned char memory[4096];
static Arm32Ctx saved;
static int jumped;
static unsigned char* at(uint32_t a) {
    if (a < 0x300000u || a > 0x300ff8u) abort();
    return memory + a - 0x300000u;
}
#undef ARC_LD8
#undef ARC_LD8S
#undef ARC_LD16
#undef ARC_LD16S
#undef ARC_LD32
#undef ARC_ST8
#undef ARC_ST16
#undef ARC_ST32
#define ARC_LD8(a) (*(uint8_t*)at(a))
#define ARC_LD8S(a) ((uint32_t)(int32_t)*(int8_t*)at(a))
#define ARC_LD16(a) (*(uint16_t*)at(a))
#define ARC_LD16S(a) ((uint32_t)(int32_t)*(int16_t*)at(a))
#define ARC_LD32(a) (*(uint32_t*)at(a))
#define ARC_ST8(a,v) (*(uint8_t*)at(a)=(uint8_t)(v))
#define ARC_ST16(a,v) (*(uint16_t*)at(a)=(uint16_t)(v))
#define ARC_ST32(a,v) (*(uint32_t*)at(a)=(uint32_t)(v))
void arc_trap(Arm32Ctx* c, const char* what) { (void)c; (void)what; abort(); }
static void set_pc(Arm32Ctx* c,uint32_t a) {
    c->r[15]=a&~1u; c->cpsr=(c->cpsr&~32u)|((a&1u)<<5);
}
int arc_enter(Arm32Ctx* c, uint32_t a, uint32_t bytes) {
    (void)bytes; set_pc(c,a);
    if (SETJMP_ADDRESS && (a&~1u)==SETJMP_ADDRESS) {
        saved=*c; c->r[0]=0; set_pc(c,c->r[14]); return 1;
    }
    if (LONGJMP_ADDRESS && (a&~1u)==LONGJMP_ADDRESS) {
        uint32_t value=c->r[1]?c->r[1]:1;
        *c=saved; c->r[0]=value; jumped=1; set_pc(c,c->r[14]); return 1;
    }
    return 0;
}
void arc_block(Arm32Ctx* c, uint32_t a, uint32_t bytes) { (void)bytes; set_pc(c,a); }
void arc_return(Arm32Ctx* c,uint32_t a) { set_pc(c,a); }
''' + emitted + dispatch + resumes + '''
void run(uint32_t* words, uint8_t* bytes) {
    Arm32Ctx c; memset(&c,0,sizeof c);
    memcpy(c.r, words, 16*sizeof(uint32_t));
    c.nf=(words[16]>>31)&1; c.zf=(words[16]>>30)&1;
    c.cf=(words[16]>>29)&1; c.vf=(words[16]>>28)&1;
    c.qf=(words[16]>>27)&1;
    memcpy(c.v.u32,words+17,64*sizeof(uint32_t)); c.fpscr=words[81];
    c.cpsr=words[82];
    c.image_base=0x100000;
    memcpy(memory,bytes,sizeof memory);
    jumped=0;
    fn_00100000(&c);
    for (unsigned attempts=0;c.r[15]!=0x200000u && jumped && attempts<8;attempts++) {
        jumped=0;
        if(!resume_dispatch(&c)) break;
    }
    memcpy(words,c.r,16*sizeof(uint32_t));
    words[16]=c.nf<<31|c.zf<<30|c.cf<<29|c.vf<<28|c.qf<<27;
    memcpy(words+17,c.v.u32,64*sizeof(uint32_t)); words[81]=c.fpscr;
    words[82]=c.cpsr;
    memcpy(bytes,memory,sizeof memory);
}
''')
        output = directory / 'fixture.so'
        subprocess.run(['cc', '-std=c11', '-O2', '-fPIC', '-shared',
                        '-I', str(ROOT / 'runtime/recomp'), str(source), '-o', str(output)],
                       check=True, capture_output=True)
        self.library = ctypes.CDLL(str(output))
        self.library.run.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint8)]
        self.library.run.restype = None

    def close(self):
        self.temporary.cleanup()

    def compare(self, testcase, registers=None, flags=0, vfp=None, memory=None):
        registers = list(registers or range(16))
        registers += [0] * (16-len(registers))
        registers[13] = MEMORY + 3072
        registers[14] = RETURN
        vectors = list(vfp or [0] * 64)
        original = bytes(memory or bytes(range(256))*16)
        words = (ctypes.c_uint32 * 83)(*(registers + [flags] + vectors + [0,0x10 | (0x20 if self.thumb else 0)]))
        data = (ctypes.c_uint8 * 4096).from_buffer_copy(original)
        self.library.run(words, data)
        uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        uc.mem_map(ADDRESS, 4096)
        uc.mem_map(RETURN, 4096)
        uc.mem_map(MEMORY, 4096)
        uc.mem_write(ADDRESS, self.code)
        uc.mem_write(MEMORY, original)
        uc.reg_write(ucarm.UC_ARM_REG_C1_C0_2, 0xF00000)
        uc.reg_write(ucarm.UC_ARM_REG_FPEXC, 0x40000000)
        uc.reg_write(ucarm.UC_ARM_REG_CPSR, flags | 0x10 | (0x20 if self.thumb else 0))
        for i, value in enumerate(registers[:15]):
            uc.reg_write(getattr(ucarm, 'UC_ARM_REG_R'+str(i)) if i<13 else
                         ucarm.UC_ARM_REG_SP if i==13 else ucarm.UC_ARM_REG_LR,value)
        for i in range(32):
            uc.reg_write(getattr(ucarm,'UC_ARM_REG_S'+str(i)), vectors[i])
        if self.jump_stubs[0]:
            saved={}
            def host_call(machine,address,size,user):
                if address==self.jump_stubs[0]:
                    saved['context']=machine.context_save()
                    machine.reg_write(ucarm.UC_ARM_REG_R0,0)
                    machine.reg_write(ucarm.UC_ARM_REG_PC,machine.reg_read(ucarm.UC_ARM_REG_LR))
                elif address==self.jump_stubs[1]:
                    value=machine.reg_read(ucarm.UC_ARM_REG_R1) or 1
                    machine.context_restore(saved['context'])
                    machine.reg_write(ucarm.UC_ARM_REG_R0,value)
                    machine.reg_write(ucarm.UC_ARM_REG_PC,machine.reg_read(ucarm.UC_ARM_REG_LR))
            uc.hook_add(UC_HOOK_CODE,host_call)
        uc.emu_start(ADDRESS | self.thumb, RETURN, count=10000)
        testcase.assertEqual(uc.reg_read(ucarm.UC_ARM_REG_PC), RETURN, 'fixture must return')
        expected = [uc.reg_read(getattr(ucarm, 'UC_ARM_REG_R'+str(i))) for i in range(13)]
        expected += [uc.reg_read(ucarm.UC_ARM_REG_SP), uc.reg_read(ucarm.UC_ARM_REG_LR)]
        expected.append(RETURN)
        testcase.assertEqual(list(words[:16]), expected)
        testcase.assertEqual(words[82]&0x20,uc.reg_read(ucarm.UC_ARM_REG_CPSR)&0x20)
        testcase.assertEqual(words[16], uc.reg_read(ucarm.UC_ARM_REG_CPSR) & 0xf8000000)
        testcase.assertEqual(list(words[17:49]), [uc.reg_read(getattr(ucarm,'UC_ARM_REG_S'+str(i))) for i in range(32)])
        testcase.assertEqual(bytes(data), bytes(uc.mem_read(MEMORY,4096)))


class LifterTests(unittest.TestCase):
    def fixture(self, body, thumb=True, coarse=False, restore_callbacks=False):
        fixture = CompiledFixture(body, thumb, coarse=coarse,restore_callbacks=restore_callbacks)
        self.addCleanup(fixture.close)
        return fixture

    def test_it_predicates_and_suppresses_implicit_flags(self):
        f = self.fixture('cmp r0,#0\nitet eq\naddeq r1,#1\nsubne r2,#1\nmoveq r3,#0\nbx lr')
        for n in (0,1,0xffffffff):
            f.compare(self,[n,0x7fffffff,0x80000000,7],flags=0x30000000)

    def test_cbz_cbnz_follow_both_paths_without_changing_flags(self):
        f = self.fixture('cbz r0,1f\ncbnz r1,2f\nmovs r2,#7\nb 3f\n1: movs r2,#8\nb 3f\n2: movs r2,#9\n3: bx lr')
        for a,b in ((0,0),(1,0),(1,2)):
            f.compare(self,[a,b],flags=0xa0000000)

    def test_wide_add_sub_and_bitfields(self):
        f = self.fixture('addw r0,r0,#1234\nsubw r1,r1,#2345\nsbfx r2,r2,#5,#12\nubfx r3,r3,#7,#17\nbx lr')
        for n in (0,0x80000000,0xffffffff,0x12345678):
            f.compare(self,[n,n,n,n],flags=0xa0000000)

    def test_fixed_point_vcvt_scales_signed_and_unsigned_integers(self):
        f = self.fixture('vmov s15,r0\nvcvt.f32.s32 s15,s15,#2\n'
                         'vmov s14,r1\nvcvt.f32.u32 s14,s14,#16\nbx lr')
        for signed,unsigned in ((0,0),(1,1),(4,65536),(0xffffffff,0xffffffff),
                                (0x80000000,0x80000000),(0x7fffffff,0x7fffffff)):
            f.compare(self,[signed,unsigned])

    def test_fixed_point_vcvt_roundtrip_saturates_float_inputs(self):
        f = self.fixture('vmov s15,r0\nvcvt.s32.f32 s15,s15,#8\nvmov r2,s15\n'
                         'vcvt.f32.s32 s15,s15,#8\nvmov s14,r1\n'
                         'vcvt.u32.f32 s14,s14,#16\nvmov r3,s14\n'
                         'vcvt.f32.u32 s14,s14,#16\nbx lr')
        for value in (0,0x3dcccccd,0x3f800000,0xbf800000,0x4affffff,
                      0x4b000000,0xcb000001,0x477fffff,0x47800000,0x7fc00000,
                      0x7f800000,0xff800000):
            f.compare(self,[value,value])

    def test_fixed_point_double_vcvt_uses_low_integer_word(self):
        f = self.fixture('vmov d4,r0,r1\nvcvt.f64.s32 d4,d4,#8\n'
                         'vmov r2,r3,d4\nvcvt.s32.f64 d4,d4,#8\n'
                         'vmov d6,r6,r7\nvcvt.f64.u32 d6,d6,#16\n'
                         'vmov r8,r9,d6\nvcvt.u32.f64 d6,d6,#16\nbx lr')
        for value in (0,1,65536,0x7fffffff,0x80000000,0xffffffff):
            f.compare(self,[value,0x12345678,0,0,0,0,value,0x87654321])

    def test_two_operand_register_shifts_and_rotates(self):
        f = self.fixture('lsls r0,r1\nlsrs r2,r3\nasrs r4,r5\nrors r6,r7\nbx lr')
        for shift in (0,1,31,32,33,255,256):
            f.compare(self,[0x92345678,shift,0x92345678,shift,0x92345678,shift,0x92345678,shift],flags=0x30000000)

    def test_thumb_replicated_immediate_preserves_carry(self):
        f = self.fixture('movs.w r0,#0x00ff00ff\nbx lr')
        for flags in (0,0x20000000):
            f.compare(self,flags=flags)

    def test_thumb_rotated_immediate_replaces_carry(self):
        f = self.fixture('movs.w r0,#0x80000000\nbx lr')
        for flags in (0,0x20000000):
            f.compare(self,flags=flags)

    def test_pair_memory_writeback_uses_original_base_and_values(self):
        f = self.fixture('ldrd r0,r1,[r2],#8\nstrd r0,r1,[r3,#8]!\nbx lr')
        f.compare(self,[0,0,MEMORY+64,MEMORY+128])

    def test_thumb_pc_literal_is_word_aligned(self):
        f = self.fixture('nop\nldr r0,1f\nbx lr\n.align 2\n1: .word 0x12345678')
        f.compare(self)

    def test_tbb_recovers_case_targets(self):
        f = self.fixture('cmp r0,#2\nbhi 4f\ntbb [pc,r0]\n1: .byte (2f-1b)/2,(3f-1b)/2,(4f-1b)/2\n.align 1\n2: movs r1,#20\nb 5f\n3: movs r1,#30\nb 5f\n4: movs r1,#40\n5: bx lr')
        for n in (0,1,2,3,0xffffffff):
            f.compare(self,[n])

    def test_tbh_recovers_case_targets(self):
        f = self.fixture('cmp r0,#1\nbhi 4f\ntbh [pc,r0,lsl #1]\n1: .hword (2f-1b)/2,(3f-1b)/2\n2: movs r1,#20\nb 5f\n3: movs r1,#30\nb 5f\n4: movs r1,#40\n5: bx lr')
        for n in (0,1,2):
            f.compare(self,[n])

    def test_arm_arithmetic_still_matches_unicorn(self):
        f = self.fixture('adds r0,r0,r1\nadcs r2,r2,r3\nsubs r4,r4,r5\nsbcs r6,r6,r7\nbx lr',False)
        rng=random.Random(37)
        for _ in range(40):
            f.compare(self,[rng.getrandbits(32) for _ in range(8)],flags=rng.randrange(16)<<28)

    def test_arm_plt_style_rotated_immediate(self):
        f = self.fixture('add r0,r0,#220,#20\nmovs r1,#255,#8\ncmp r2,#255,#8\nbx lr',False)
        for n in (0,0xffffffff,0x12345678):
            f.compare(self,[n,n,n],flags=0x30000000)

    def test_thumb_adr_uses_aligned_pc(self):
        f = self.fixture('nop\nadr r0,1f\nbx lr\n.align 2\n1: .word 5')
        f.compare(self,flags=0xa0000000)

    def test_bitfield_insert_clear_and_orn(self):
        f = self.fixture('bfi r0,r1,#7,#9\nbfc r2,#2,#5\norn r3,r4,r5\nbx lr')
        for n in (0,0xffffffff,0x92345678):
            f.compare(self,[n,0x12345678,n,0,n,0x98abcdef],flags=0xa8000000)

    def test_unsigned_saturation_sets_sticky_q(self):
        f = self.fixture('usat r0,#8,r1\nusat r2,#15,r3,asr #4\nbx lr')
        for n in (0,255,256,0x7fffffff,0xffffffff,0x80000000):
            f.compare(self,[0,n,0,n],flags=0xa0000000)
            f.compare(self,[0,n,0,n],flags=0xa8000000)

    def test_vldmia_vstmia_preserve_vectors_and_base_writeback(self):
        f = self.fixture('vldmia r0!,{s0-s3}\nvstmia r1!,{s0-s3}\nbx lr')
        f.compare(self,[MEMORY+32,MEMORY+64],flags=0x30000000)

    def test_return_records_program_counter_and_mode(self):
        f = self.fixture('movs r0,#7\nbx lr')
        f.compare(self)

    def test_direct_call_and_pop_return_preserve_callers_pc(self):
        f = self.fixture('push {lr}\nbl helper\nadds r0,#1\npop {pc}\n.type helper,%function\n.thumb_func\nhelper:\nadds r0,#2\nbx lr')
        f.compare(self,[10])

    def test_arm_stack_load_to_pc_returns_without_indirect_call(self):
        f = self.fixture('push {lr}\nadds r0,r0,#3\nldr pc,[sp],#4',False)
        f.compare(self,[10])

    def test_undecodable_reachable_bytes_are_not_reported_complete(self):
        f = CompiledFixture('movs r0,#1\n.hword 0xf000',expect_complete=False)
        self.addCleanup(f.close)
        self.assertFalse(f.complete)

    def test_adjacent_function_range_fallthrough_keeps_executing(self):
        f = self.fixture('adds r0,#1\n.type helper,%function\n.thumb_func\nhelper:\nadds r0,#2\nbx lr')
        f.compare(self,[10])

    def test_blx_lr_captures_target_before_writing_return_address(self):
        f = self.fixture('push {lr}\nadr r1,helper\nadds r1,#1\nmov lr,r1\nblx lr\nadds r0,#1\npop {pc}\n.align 2\n.type helper,%function\n.thumb_func\nhelper:\nadds r0,#2\nbx lr')
        f.compare(self,[10])

    def test_nonlocal_stack_return_propagates_through_direct_calls(self):
        f = self.fixture('push {lr}\nbl helper1\nadds r0,#1\npop {pc}\n.type helper1,%function\n.thumb_func\nhelper1:\npush {lr}\nbl helper2\nadds r0,#2\npop {pc}\n.type helper2,%function\n.thumb_func\nhelper2:\nadds r0,#8\npop {pc}')
        f.compare(self,[10])

    def test_vfp_immediate_moves_preserve_encoded_value(self):
        f = self.fixture('vmov.f32 s0,#1.5\nvmov.f64 d1,#-2.0\nbx lr')
        f.compare(self,flags=0xa0000000)

    def test_vabs_clears_sign_on_zero_and_nan_without_numeric_conversion(self):
        f = self.fixture('vabs.f32 s0,s1\nvabs.f64 d1,d2\nbx lr')
        for bits in (0x80000000,0xffc01234,0xff801234,0xbf800000):
            vectors=[0]*64
            vectors[1]=bits
            vectors[4]=0x1234
            vectors[5]=0xfff80000
            f.compare(self,vfp=vectors)

    def test_vfp_binary_operations_and_compare_match_unicorn(self):
        f = self.fixture('vadd.f32 s0,s1,s2\nvsub.f32 s3,s4,s5\nvmul.f32 s6,s7,s8\nvdiv.f32 s9,s10,s11\nvcmp.f32 s0,s3\nvmrs apsr_nzcv,fpscr\nbx lr')
        import struct
        for values in ((1.5,2.5,5,1,2,3,8,2),(-1.25,2,-7,1,0,-3,1,-2)):
            vectors=[0]*64
            for i,value in zip((1,2,4,5,7,8,10,11),values):
                vectors[i]=struct.unpack('<I',struct.pack('<f',value))[0]
            f.compare(self,vfp=vectors)

    def test_reachability_does_not_decode_callee_in_callers_mode(self):
        # BLX at +2 targets the ARM helper at +12; the literal encoding avoids
        # an unresolved R_ARM_THM_CALL in an unlinked assembler fixture.
        body = 'push {lr}\n.inst.w 0xf000e804\nadds r0,#1\npop {pc}\n.align 2\n.arm\n.type helper,%function\nhelper:\nadd r0,r0,#2\nbx lr'
        with tempfile.TemporaryDirectory() as temporary:
            code = assemble(Path(temporary),body)
        lifter = Lifter()
        lifter.thumb = True
        lifter._body, lifter._body_address = code, ADDRESS
        instructions = lifter.decode(code,ADDRESS,True)
        self.assertEqual(lifter.reachable(instructions,ADDRESS),
                         {ADDRESS,ADDRESS+2,ADDRESS+6,ADDRESS+8})

    def test_coarse_thumb_range_can_call_arm_helper_and_return(self):
        body = 'push {lr}\n.inst.w 0xf000e804\nadds r0,#1\npop {pc}\n.align 2\n.arm\n.type helper,%function\nhelper:\nadd r0,r0,#2\nbx lr'
        f = self.fixture(body,coarse=True)
        self.assertIn(ADDRESS+12,f.lifter.referenced)
        f.compare(self,[10])

    def test_noreturn_call_has_no_fallthrough_but_keeps_callee_reference(self):
        body='bl abort_stub\nmovs r0,#99\nbx lr\n.thumb_func\nabort_stub:\nbx lr'
        with tempfile.TemporaryDirectory() as temporary:
            code=assemble(Path(temporary),body)
            with (Path(temporary)/'fixture.o').open('rb') as stream:
                symbols=ELFFile(stream).get_section_by_name('.symtab')
                target=next(ADDRESS+(s['st_value']&~1) for s in symbols.iter_symbols() if s.name=='abort_stub')
        lifter=Lifter()
        lifter.thumb=True
        lifter.link_base=ADDRESS
        lifter._body,lifter._body_address=code,ADDRESS
        lifter.noreturn={target}
        instructions=lifter.decode(code,ADDRESS,True)
        self.assertEqual(lifter.reachable(instructions,ADDRESS),{ADDRESS})
        lifter.lift_function(ADDRESS,len(code),code,True)
        self.assertIn(target,lifter.referenced)

    def test_saved_context_jump_resumes_earlier_callsite_without_prologue(self):
        body = ('push {lr}\nmovs r4,#5\nvmov s0,r4\nbl setjmp_stub\n'
                'mov.w r2,#0\nadc.w r6,r6,r2\ncmp r0,#0\nbne finish\n'
                'movs r4,#99\nbl deep\nfinish:\nadds r4,#1\nmov r0,r4\npop {pc}\n'
                '.type deep,%function\n.thumb_func\ndeep:\npush {lr}\nmovs r5,#88\n'
                'vmov s0,r5\ncmp r5,#255\nmovs r1,#7\nbl longjmp_stub\npop {pc}\n'
                '.type setjmp_stub,%function\n.thumb_func\nsetjmp_stub:\nbx lr\n'
                '.type longjmp_stub,%function\n.thumb_func\nlongjmp_stub:\nbx lr')
        f=self.fixture(body,restore_callbacks=True)
        f.compare(self,[10],flags=0x30000000)


if __name__ == '__main__':
    unittest.main()
