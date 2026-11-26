"""Translate reachable ARM/Thumb instructions to C using Capstone operand detail.

Adapted from iparecomp; source revision and license are recorded alongside this
file. The ELF loader supplies function ranges, while control-flow discovery
separates instructions from literal pools. Unsupported reachable code traps.
"""
from __future__ import annotations

import collections
import struct

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, CS_MODE_LITTLE_ENDIAN
from capstone import arm as ca

# Capstone's condition enum in encoding order, as the ARC_COND_* macro suffix.
CC_MACRO = [None, "EQ", "NE", "CS", "CC", "MI", "PL", "VS", "VC",
            "HI", "LS", "GE", "LT", "GT", "LE", "AL"]
# What capstone actually spells into the mnemonic, which is not always the
# macro name: `hs` and `cs` are the same condition, as are `lo` and `cc`.
CC_SUFFIX = [(), ("eq",), ("ne",), ("cs", "hs"), ("cc", "lo"), ("mi",), ("pl",),
             ("vs",), ("vc",), ("hi",), ("ls",), ("ge",), ("lt",), ("gt",),
             ("le",), ("al",)]

REGS = {f"r{i}": i for i in range(16)}
REGS.update({"sb": 9, "sl": 10, "fp": 11, "ip": 12, "sp": 13, "lr": 14, "pc": 15})

# Shift kinds in capstone's enum order. The _REG forms take the amount from a
# register, so the amount is not known at lift time and the helper must be
# called rather than inlined.
SFT = {1: "asr", 2: "lsl", 3: "lsr", 4: "ror", 5: "rrx",
       6: "asr", 7: "lsl", 8: "lsr", 9: "ror", 10: "rrx"}
SFT_REG = {6, 7, 8, 9, 10}

ALU3 = {"add", "adc", "sub", "sbc", "rsb", "rsc", "and", "orr", "orn", "eor", "bic"}
ALU2 = {"mov", "mvn", "movw", "movt"}
TEST = {"cmp", "cmn", "tst", "teq"}
SHIFTOP = {"lsl", "lsr", "asr", "ror", "rrx"}
# (bytes, signed) for each load/store width.
WIDTH = {"": (4, False), "b": (1, False), "h": (2, False),
         "sb": (1, True), "sh": (2, True)}


class Unsupported(Exception):
    """Raised by an emitter that does not handle this instruction's shape."""


def form(ins) -> str:
    """A short key describing an instruction's shape, for the report.

    Mnemonic plus operand kinds. Enough to group the tail into things worth
    writing an emitter for, without listing every register combination
    separately and drowning the signal.
    """
    kinds = []
    for op in ins.operands:
        if op.type == ca.ARM_OP_REG:
            kinds.append(f"r<{SFT[op.shift.type]}>" if op.shift.type else "r")
        elif op.type == ca.ARM_OP_IMM:
            kinds.append("i")
        elif op.type == ca.ARM_OP_MEM:
            # The addressing mode is part of the shape, not an incidental
            # detail. Collapsing every `ldr` into one key meant the harvester
            # took whichever encodings came first -- all immediate-offset ones
            # -- and a scaled register index was never sampled at all. A whole
            # addressing mode went untested, and the lost `lsl #2` on every
            # array subscript in the binary lived there.
            mode = "[m"
            if op.mem.index:
                mode += "+r"
                if op.shift.type:
                    mode += f"<{SFT[op.shift.type]}>"
                elif op.mem.lshift:
                    mode += "<lsl>"
            elif op.mem.disp:
                mode += "+d"
            kinds.append(mode + "]")
        elif op.type == ca.ARM_OP_FP:
            kinds.append("f")
        else:
            kinds.append(f"?{op.type}")
    return f"{ins.mnemonic.split('.')[0]} {','.join(kinds)}"


class Lifter:
    def __init__(self) -> None:
        self.md = {}
        for thumb, mode in ((False, CS_MODE_ARM), (True, CS_MODE_THUMB)):
            md = Cs(CS_ARCH_ARM, mode | CS_MODE_LITTLE_ENDIAN)
            md.detail = True
            self.md[thumb] = md
        self.unsupported: collections.Counter = collections.Counter()
        # Which form was responsible for breaking how many *functions*. Late in
        # the tail, how widely a form is spread matters far more than how often
        # it occurs: on the ARM64 side, PC-relative literal loads were a tenth
        # of a percent of instructions and seven points of function
        # completeness, because they appear about once per function.
        self.blamed: collections.Counter = collections.Counter()
        self.starts: set[int] = set()
        self.referenced: set[int] = set()
        self.noreturn: set[int] = set()
        self.continuations: dict[int, tuple[int, bool]] = {}
        self.insns_total = 0
        self.insns_lifted = 0
        self.link_base = 0
        self.thumb = False
        # The image itself, so a literal-pool load can be resolved while
        # lifting rather than at run time. Set by load(); without it the fold
        # simply does not happen and the load is emitted as a load.
        self.image: bytes = b""
        self.ro_range = (0, 0)   # the read-only span a fold is allowed inside
        # Resolved switch tables for the function being lifted, keyed by
        # the address of the load that reads them. Filled by reachable(),
        # which is the pass that has to follow them anyway.
        self.tables: dict[int, list[int]] = {}
        self.folded = 0
        self._body = b""
        self._body_address = 0
        self._missing = set()
        self._fallthrough = False

    # --- naming ------------------------------------------------------------

    @staticmethod
    def name(addr: int) -> str:
        return f"fn_{addr:08x}"

    def strip_cc(self, ins) -> str:
        """The mnemonic with only its condition suffix removed."""
        m = ins.mnemonic.split(".")[0]
        if ins.cc and ins.cc != ca.ARM_CC_AL:
            for suffix in CC_SUFFIX[ins.cc]:
                if m.endswith(suffix) and len(m) > len(suffix):
                    return m[:-len(suffix)]
        return m

    def sets_flags(self, ins) -> bool:
        """Whether this instruction *writes* the flags.

        capstone reports `update_flags` for adc, sbc and rsc even when S is
        clear, because those three *read* the carry -- so the field means
        "touches CPSR", not "writes it". Trusting it makes `adc` clobber the
        flags of the instruction after it, which is a silent wrong answer of
        exactly the kind this emitter exists to avoid. For those three the
        mnemonic is authoritative: only the S form carries the trailing `s`.
        """
        if not ins.update_flags:
            return False
        if self.root(ins) not in ("adc", "sbc", "rsc"):
            return True
        return self.strip_cc(ins).endswith("s")

    def root(self, ins) -> str:
        """The mnemonic with its condition and flag suffixes removed.

        One emitter then serves all sixteen predicated forms of an instruction
        rather than sixteen near-copies.
        """
        m = self.strip_cc(ins)
        if m in ("addw", "subw"):
            return m[:-1]
        # The trailing `s` is the flag suffix only where the architecture has
        # one. VFP does not, and `vmrs`/`mrs` end in an `s` that is part of the
        # name -- stripping it there invents a mnemonic no emitter answers to.
        if ins.update_flags and m.endswith("s") and len(m) > 1                 and not m.startswith("v") and m not in ("mrs", "msr"):
            m = m[:-1]
        return m

    # --- operands ----------------------------------------------------------

    def reg(self, ins, r) -> int:
        n = ins.reg_name(r)
        if n not in REGS:
            raise Unsupported(f"{form(ins)}: register {n}")
        return REGS[n]

    def pc_value(self, ins) -> int:
        """What r15 reads as: the pipeline offset of the machine that defined
        the encoding. Plus 8 in ARM, plus 4 in Thumb, and a Thumb literal load
        aligns the base down to a word first."""
        if not self.thumb:
            return ins.address + 8
        return ins.address + 4

    def img(self, absolute: int) -> str:
        """An address the instruction stream derived from the PC.

        These binaries were linked to load at a fixed address, so a literal-pool
        constant is a real address in the original layout. Emitted relative to
        where the image actually landed, which keeps the output valid if the
        loader ever has to slide it.
        """
        return f"(c->image_base + {absolute - self.link_base:#x}u)"

    def read(self, ins, n: int) -> str:
        """Read register `n`, folding a PC read to a constant. The emitter
        knows where the instruction is, so r15 is never a runtime value."""
        return self.img(self.pc_value(ins)) if n == 15 else f"ARC_R(c, {n})"

    def shifted(self, ins, op) -> tuple[str, str | None]:
        """(value, shifter carry-out) for a register run through the shifter.

        The carry comes back separately because it is *not* the ALU carry: a
        flag-setting logical instruction writes C from here and the ALU never
        produced one. `None` means the shifter did not touch C, which is what a
        plain register operand -- `LSL #0` -- must do, and getting that wrong
        clobbers the carry between a compare and the instruction consuming it.
        """
        base = self.read(ins, self.reg(ins, op.reg))
        kind = op.shift.type
        if not kind:
            return base, None
        name = SFT[kind]
        if name == "rrx":
            call = f"arc_rrx({base}, c->cf)"
            return f"{call}.value", f"{call}.carry"
        if kind in SFT_REG:
            amount = self.read(ins, self.reg(ins, op.shift.value))
        else:
            amount = f"{op.shift.value}u"
        call = f"arc_{name}({base}, {amount}, c->cf)"
        if kind not in SFT_REG and name != "ror" and 1 <= op.shift.value <= 31:
            # A constant amount in range is a plain C shift the host compiler
            # sees straight through. Out of range it is not: `LSR #0` encodes
            # LSR #32, and shifting a uint32_t by 32 in C is undefined.
            if name == "asr":
                value = f"(uint32_t)((int32_t){base} >> {op.shift.value})"
            else:
                value = f"({base} {'<<' if name == 'lsl' else '>>'} {op.shift.value})"
            return value, f"{call}.carry"
        return f"{call}.value", f"{call}.carry"

    def operand(self, ins, op) -> tuple[str, str | None]:
        if op.type == ca.ARM_OP_REG:
            return self.shifted(ins, op)
        if op.type == ca.ARM_OP_IMM:
            v = op.imm & 0xFFFFFFFF
            carry = None
            raw = bytes(ins.bytes)
            if self.thumb:
                if len(raw) == 4:
                    first = int.from_bytes(raw[:2], "little")
                    second = int.from_bytes(raw[2:], "little")
                    if first & 0xfa00 == 0xf000:
                        imm12 = ((first >> 10) & 1) << 11 | ((second >> 12) & 7) << 8 | (second & 255)
                        # ThumbExpandImm replication forms retain carry; only
                        # the rotated form replaces it with the expanded MSB.
                        if imm12 & 0xc00:
                            carry = f"{v >> 31}u"
            elif len(raw) == 4 and (int.from_bytes(raw, "little") >> 8) & 15:
                carry = f"{v >> 31}u"
            return f"{v:#x}u", carry
        raise Unsupported(f"operand type {op.type}")

    def dest(self, ins, op) -> int:
        return self.reg(ins, op.reg)

    # --- writing a destination that might be the PC ------------------------

    def write(self, rd: int, value: str) -> str:
        """Assign to a destination register. Writing r15 is a branch, so it is
        emitted as one rather than as a store nothing would ever read."""
        if rd == 15:
            return f"arc_dispatch(c, {value}); return;"
        return f"ARC_W(c, {rd}, {value});"

    # --- emitters ----------------------------------------------------------

    def arithmetic_operands(self, ins, count):
        ops = list(ins.operands)
        if not self.thumb and len(ops) == count + 1 and ops[-2].type == ca.ARM_OP_IMM \
                and ops[-1].type == ca.ARM_OP_IMM:
            rotation = ops[-1].imm
            if not 0 <= rotation < 32 or rotation & 1:
                raise Unsupported(form(ins))
            immediate = type(ops[-2]).from_buffer_copy(ops[-2])
            value = immediate.imm & 0xffffffff
            immediate.value.imm = ((value >> rotation) | (value << ((32-rotation) & 31))) & 0xffffffff
            ops = ops[:-2] + [immediate]
        return ops

    def _alu3(self, ins, op: str) -> str:
        ops = self.arithmetic_operands(ins, 3)
        if len(ops) == 2:
            ops = [ops[0], ops[0], ops[1]]
        if len(ops) != 3:
            raise Unsupported(form(ins))
        rd = self.dest(ins, ops[0])
        a = self.read(ins, self.reg(ins, ops[1].reg))
        b, carry = self.operand(ins, ops[2])
        # adc/sbc/rsc take the carry as an *input*, and the flag helpers
        # write c->cf as an output. Reading c->cf in the result expression
        # after calling one of them reads the carry the instruction just
        # produced instead of the one it was given -- wrong by exactly one,
        # and only when S is set. Latch it first.
        carry_in = op in ("adc", "sbc", "rsc")
        latch = ", ci = c->cf" if carry_in else ""
        arith = {"add": ("+", "arc_add_flags(c, a, b, 0)"),
                 "adc": ("+", "arc_add_flags(c, a, b, ci)"),
                 "sub": ("-", "arc_sub_flags(c, a, b, 1)"),
                 "sbc": ("-", "arc_sub_flags(c, a, b, ci)")}
        logic = {"and": "&", "orr": "|", "eor": "^"}
        if op in ("rsb", "rsc"):
            a, b = b, a
            flags = "arc_sub_flags(c, a, b, 1)" if op == "rsb" \
                else "arc_sub_flags(c, a, b, ci)"
            expr = "a - b - (1u - ci)" if op == "rsc" else "a - b"
            body = f"uint32_t a = {a}, b = {b}{latch}; "
            if self.sets_flags(ins):
                body += f"{flags}; "
            return "{ " + body + self.write(rd, expr) + " }"
        if op in arith:
            sign, flags = arith[op]
            expr = f"a {sign} b"
            if op == "adc":
                expr = "a + b + ci"
            elif op == "sbc":
                expr = "a - b - (1u - ci)"
            body = f"uint32_t a = {a}, b = {b}{latch}; "
            if self.sets_flags(ins):
                body += f"{flags}; "
            return "{ " + body + self.write(rd, expr) + " }"
        if op in logic or op in ("bic", "orn"):
            expr = f"a {logic[op]} b" if op in logic else "a & ~b" if op == "bic" else "a | ~b"
            body = f"uint32_t a = {a}, b = {b}, r = {expr}; "
            if self.sets_flags(ins):
                body += "ARC_NZ(c, r); "
                if carry is not None:
                    body += f"c->cf = {carry}; "
            return "{ " + body + self.write(rd, "r") + " }"
        raise Unsupported(form(ins))

    def _alu2(self, ins, op: str) -> str:
        ops = self.arithmetic_operands(ins, 2)
        if len(ops) != 2:
            raise Unsupported(form(ins))
        rd = self.dest(ins, ops[0])
        v, carry = self.operand(ins, ops[1])
        # movw is a plain move of a 16-bit immediate -- the width is a fact
        # about the encoding, not about the semantics. movt is the one form
        # here that reads its own destination: it replaces the top halfword
        # and keeps the bottom, which is how a 32-bit constant is built out of
        # a movw/movt pair when there is no literal pool to load it from.
        if op == "movt":
            cur = self.read(ins, self.reg(ins, ops[0].reg))
            body = f"uint32_t r = ({cur} & 0xffffu) | ((({v}) & 0xffffu) << 16); "
            return "{ " + body + self.write(rd, "r") + " }"
        body = f"uint32_t r = {'~' if op == 'mvn' else ''}({v}); "
        if self.sets_flags(ins):
            body += "ARC_NZ(c, r); "
            if carry is not None:
                body += f"c->cf = {carry}; "
        return "{ " + body + self.write(rd, "r") + " }"

    def _test(self, ins, op: str) -> str:
        ops = self.arithmetic_operands(ins, 2)
        if len(ops) != 2:
            raise Unsupported(form(ins))
        a = self.read(ins, self.reg(ins, ops[0].reg))
        b, carry = self.operand(ins, ops[1])
        if op == "cmp":
            return "{ uint32_t a = %s, b = %s; arc_sub_flags(c, a, b, 1); }" % (a, b)
        if op == "cmn":
            return "{ uint32_t a = %s, b = %s; arc_add_flags(c, a, b, 0); }" % (a, b)
        expr = "a & b" if op == "tst" else "a ^ b"
        body = f"uint32_t a = {a}, b = {b}, r = {expr}; ARC_NZ(c, r); "
        if carry is not None:
            body += f"c->cf = {carry}; "
        return "{ " + body + "}"

    def _shift(self, ins, op: str) -> str:
        ops = ins.operands
        rd = self.dest(ins, ops[0])
        src = self.read(ins, self.reg(ins, ops[1].reg))
        if len(ops) == 2 and ops[1].shift.type:
            # `lsl r0, r1, r2` comes back as two operands with the shift folded
            # onto the source, not as three with the amount separate. Same
            # instruction, different shape, and the shifter helper already
            # knows how to read it.
            value, carry = self.shifted(ins, ops[1])
            body = f"uint32_t r = {value}; "
            if self.sets_flags(ins):
                body += "ARC_NZ(c, r); "
                if carry is not None:
                    body += f"c->cf = {carry}; "
            return "{ " + body + self.write(rd, "r") + " }"
        if op == "rrx":
            call = f"arc_rrx({src}, c->cf)"
        else:
            if len(ops) == 2:
                src = self.read(ins, rd)
                shift = ops[1]
            elif len(ops) == 3:
                shift = ops[2]
            else:
                raise Unsupported(form(ins))
            if shift.type == ca.ARM_OP_IMM:
                amount = f"{shift.imm & 0xFF}u"
            else:
                amount = f"({self.read(ins, self.reg(ins, shift.reg))} & 0xFFu)"
            call = f"arc_{op}({src}, {amount}, c->cf)"
        body = f"ArcShift s = {call}; "
        if self.sets_flags(ins):
            body += "ARC_NZ(c, s.value); c->cf = s.carry; "
        return "{ " + body + self.write(rd, "s.value") + " }"

    def _mul(self, ins, op: str) -> str:
        ops = ins.operands
        r = [self.reg(ins, o.reg) for o in ops if o.type == ca.ARM_OP_REG]
        if op in ("mul", "mla", "mls"):
            if op == "mul" and len(r) == 3:
                expr = f"ARC_R(c, {r[1]}) * ARC_R(c, {r[2]})"
            elif op == "mla" and len(r) == 4:
                expr = f"ARC_R(c, {r[1]}) * ARC_R(c, {r[2]}) + ARC_R(c, {r[3]})"
            elif op == "mls" and len(r) == 4:
                expr = f"ARC_R(c, {r[3]}) - ARC_R(c, {r[1]}) * ARC_R(c, {r[2]})"
            else:
                raise Unsupported(form(ins))
            body = f"uint32_t r = {expr}; "
            if self.sets_flags(ins):
                body += "ARC_NZ(c, r); "
            return "{ " + body + self.write(r[0], "r") + " }"
        if op in ("umull", "smull", "umlal", "smlal") and len(r) == 4:
            lo, hi, n, m = r
            if op[0] == "u":
                wide = f"(uint64_t)ARC_R(c, {n}) * (uint64_t)ARC_R(c, {m})"
            else:
                wide = (f"(uint64_t)((int64_t)(int32_t)ARC_R(c, {n}) * "
                        f"(int64_t)(int32_t)ARC_R(c, {m}))")
            body = f"uint64_t w = {wide}; "
            if op.endswith("lal"):
                body += (f"w += ((uint64_t)ARC_R(c, {hi}) << 32) | "
                         f"ARC_R(c, {lo}); ")
            if self.sets_flags(ins):
                body += "c->nf = (uint32_t)(w >> 63); c->zf = (w == 0); "
            body += f"ARC_W(c, {lo}, (uint32_t)w); ARC_W(c, {hi}, (uint32_t)(w >> 32)); "
            return "{ " + body + "}"
        raise Unsupported(form(ins))

    def _smulxy(self, ins, op: str) -> str:
        """smulbb/bt/tb/tt: a signed 16x16 product picked out of the halves of
        two registers. `b` is bits 15:0, `t` is bits 31:16."""
        ops = ins.operands
        rd = self.dest(ins, ops[0])
        half = op[-2:]
        def part(reg_op, which):
            v = self.read(ins, self.reg(ins, reg_op.reg))
            return f"(int32_t)(int16_t)({v}{' >> 16' if which == 't' else ''})"
        expr = f"(uint32_t)({part(ops[1], half[0])} * {part(ops[2], half[1])})"
        return self.write(rd, expr)

    def _extend(self, ins, op: str) -> str:
        """uxtb/uxth/sxtb/sxth and their accumulating forms. The optional
        `ror #n` is applied before the extension, not after."""
        ops = ins.operands
        rd = self.dest(ins, ops[0])
        acc = len(ops) == 3 and op.endswith(("ab", "ah", "ab16"))
        src_op = ops[2] if acc else ops[1]
        src = self.read(ins, self.reg(ins, src_op.reg))
        if src_op.shift.type:
            if SFT[src_op.shift.type] != "ror":
                raise Unsupported(form(ins))
            src = f"arc_ror({src}, {src_op.shift.value}u, c->cf).value"
        wide = op.endswith("h") or op.endswith("ah")
        signed = op.startswith("sxt")
        cast = ("(uint32_t)(int32_t)(int16_t)" if signed else "(uint32_t)(uint16_t)") \
            if wide else \
            ("(uint32_t)(int32_t)(int8_t)" if signed else "(uint32_t)(uint8_t)")
        expr = f"{cast}({src})"
        if acc:
            expr = f"{self.read(ins, self.reg(ins, ops[1].reg))} + {expr}"
        return self.write(rd, expr)

    def _misc(self, ins, op: str) -> str:
        ops = ins.operands
        if op == "adr" and len(ops) == 2 and ops[1].type == ca.ARM_OP_IMM:
            address = self.pc_value(ins)
            if self.thumb:
                address &= ~3
            return self.write(self.dest(ins, ops[0]), self.img(address + ops[1].imm))
        if self.thumb and len(ins.bytes) == 2 and int.from_bytes(ins.bytes, "little") & 0xff00 == 0xbf00 \
                and int.from_bytes(ins.bytes, "little") & 15:
            # Capstone has already attached each IT predicate to its following
            # instruction and suppressed the implicit Thumb flag writes.
            return "(void)0;"
        if op in ("sbfx", "ubfx") and len(ops) == 4:
            rd = self.dest(ins, ops[0])
            value = self.read(ins, self.reg(ins, ops[1].reg))
            lsb, width = ops[2].imm, ops[3].imm
            if not 0 <= lsb < 32 or not 1 <= width <= 32 - lsb:
                raise Unsupported(form(ins))
            if op == "ubfx":
                expression = f"(({value} >> {lsb}) & {0xffffffff >> (32-width):#x}u)"
            else:
                expression = f"(uint32_t)((int32_t)({value} << {32-lsb-width}) >> {32-width})"
            return self.write(rd, expression)
        if op in ("bfi", "bfc") and len(ops) == (4 if op == "bfi" else 3):
            rd = self.dest(ins, ops[0])
            lsb, width = ops[-2].imm, ops[-1].imm
            if not 0 <= lsb < 32 or not 1 <= width <= 32-lsb:
                raise Unsupported(form(ins))
            mask = ((1 << width)-1) << lsb
            value = self.read(ins, self.reg(ins, ops[1].reg)) if op == "bfi" else "0u"
            return self.write(rd, f"((ARC_R(c, {rd}) & {~mask & 0xffffffff:#x}u) | (({value} << {lsb}) & {mask:#x}u))")
        if op == "usat" and len(ops) == 3 and ops[1].type == ca.ARM_OP_IMM:
            width = ops[1].imm
            if not 0 <= width <= 31:
                raise Unsupported(form(ins))
            value, _ = self.shifted(ins, ops[2])
            maximum = (1 << width)-1
            return (f"{{ int32_t v = (int32_t){value}; uint32_t result; "
                    f"if (v < 0) {{ result = 0; c->qf = 1; }} "
                    f"else if ((uint32_t)v > {maximum:#x}u) {{ result = {maximum:#x}u; c->qf = 1; }} "
                    f"else result = (uint32_t)v; {self.write(self.dest(ins, ops[0]), 'result')} }}")
        if op in ("nop", "pld", "pldw", "pli", "dmb", "dsb", "isb", "yield"):
            return "(void)0;"
        if op == "clz":
            rd = self.dest(ins, ops[0])
            src = self.read(ins, self.reg(ins, ops[1].reg))
            return "{ uint32_t v = %s, n = 0; while (n < 32 && !(v & 0x80000000u)) " \
                   "{ v <<= 1; ++n; } %s }" % (src, self.write(rd, "n"))
        if op in ("rev", "rev16", "revsh"):
            rd = self.dest(ins, ops[0])
            v = self.read(ins, self.reg(ins, ops[1].reg))
            if op == "rev":
                expr = ("((v >> 24) | ((v >> 8) & 0xFF00u) | "
                        "((v << 8) & 0xFF0000u) | (v << 24))")
            elif op == "rev16":
                expr = ("(((v >> 8) & 0x00FF00FFu) | ((v << 8) & 0xFF00FF00u))")
            else:
                expr = "((uint32_t)(int32_t)(int16_t)(((v >> 8) & 0xFFu) | (v << 8)))"
            return "{ uint32_t v = %s; %s }" % (v, self.write(rd, expr))
        raise Unsupported(form(ins))

    # --- memory ------------------------------------------------------------

    def _mem_address(self, ins, mem_index: int) -> tuple[str, str, int]:
        """(address expression, writeback statement, base register).

        Three addressing forms, told apart by where the offset sits. If there is
        an operand *after* the memory operand the instruction is post-indexed
        and that operand is the offset; otherwise the offset is inside the
        memory operand, applied before the access, and written back only if the
        instruction says so.
        """
        ops = ins.operands
        mem = ops[mem_index].mem
        base = self.reg(ins, mem.base)
        base_expr = self.read(ins, base)
        if self.thumb and base == 15:
            base_expr = self.img(self.pc_value(ins) & ~3)

        def offset_of(op) -> str:
            if op.type == ca.ARM_OP_IMM:
                return f"{op.imm & 0xFFFFFFFF:#x}u"
            v, _ = self.shifted(ins, op)
            return f"({v})"

        post = mem_index + 1 < len(ops)
        if post:
            off = offset_of(ops[mem_index + 1])
            sub = ops[mem_index + 1].subtracted
        elif mem.index:
            idx = self.read(ins, self.reg(ins, mem.index))
            # The scale of a register index lives in the operand's shift, and
            # `mem.lshift` is zero even for `[r3, r6, lsl #2]`. Testing only
            # lshift silently drops the scale on every scaled index in the
            # binary -- which is to say on array subscripting -- and the result
            # reads one byte into an element instead of one element along.
            shift = ops[mem_index].shift
            name = SFT.get(shift.type) if shift.type else None
            if name == "rrx":
                idx = f"arc_rrx({idx}, c->cf).value"
            elif name and name != "lsl":
                idx = f"arc_{name}({idx}, {shift.value}u, c->cf).value"
            elif name == "lsl" and shift.value:
                idx = f"({idx} << {shift.value})"
            elif mem.lshift:
                idx = f"({idx} << {mem.lshift})"
            off = idx
            sub = ops[mem_index].subtracted
        else:
            off = f"{abs(mem.disp):#x}u"
            sub = mem.disp < 0

        sign = "-" if sub else "+"
        if post:
            # The access uses the base unmodified; the write-back happens after.
            return base_expr, f"ARC_W(c, {base}, {base_expr} {sign} {off});", base
        addr = f"({base_expr} {sign} {off})"
        wb = f"ARC_W(c, {base}, a);" if ins.writeback else ""
        return addr, wb, base

    def literal(self, addr: int, size: int):
        """The value at `addr`, if it is fixed for the life of the program.

        A literal-pool load reads read-only memory at an address the emitter
        already knows, so the value is a constant and can be emitted as one.
        This is exact, not a heuristic: `__TEXT` is mapped r-x and there is no
        instruction in the image that could write it.

        It matters for more than speed. `__TEXT,__text` is the only section of
        these binaries that lies below the 64 KB floor every desktop OS puts on
        low mappings, and its only run-time readers are these loads. Fold them
        and the image never needs a byte mapped below `0x10000`, which is what
        lets it load at its link address with no slide -- and a zero slide is
        what makes every unrebased absolute pointer in the file correct.
        """
        lo, hi = self.ro_range
        if not self.image or addr < lo or addr + size > hi:
            return None
        off = addr - self.link_base
        if off < 0 or off + size > len(self.image):
            return None
        chunk = self.image[off:off + size]
        return int.from_bytes(chunk, "little")

    def pc_literal(self, ins, mem_index: int, size: int):
        """The constant a PC-relative load reads, or None if it is not one."""
        ops = ins.operands
        mem = ops[mem_index].mem
        if self.reg(ins, mem.base) != 15 or mem.index or ins.writeback:
            return None
        if mem_index + 1 < len(ops):   # post-indexed: the base moves
            return None
        pc = self.pc_value(ins)
        if self.thumb:
            pc &= ~3
        return self.literal(pc + mem.disp, size)

    def _load(self, ins, op: str) -> str:
        suffix = "" if op == "ldrd" else op[3:]
        if suffix not in WIDTH:
            raise Unsupported(form(ins))
        size, signed = WIDTH[suffix]
        ops = ins.operands
        mem_index = next(i for i, o in enumerate(ops) if o.type == ca.ARM_OP_MEM)
        rt = [self.reg(ins, o.reg) for o in ops[:mem_index]]
        addr, wb, base = self._mem_address(ins, mem_index)
        macro = {1: "ARC_LD8", 2: "ARC_LD16", 4: "ARC_LD32"}[size]
        if signed:
            macro += "S"
        if len(rt) == 1 and rt[0] != 15:
            value = self.pc_literal(ins, mem_index, size)
            if value is not None:
                if signed and value >> (size * 8 - 1):
                    value -= 1 << (size * 8)
                self.folded += 1
                return self.write(rt[0], f"{value & 0xFFFFFFFF:#x}u")
        # The address goes into a temporary before anything is written back or
        # loaded. A load pair whose base register is also its first destination
        # would otherwise read its second element through the value the first
        # had just loaded -- a bug the per-instruction oracle cannot find,
        # because a form keyed on operand shapes does not record the aliasing.
        body = f"uint32_t a = {addr}; "
        if len(rt) == 1:
            if rt[0] == 15:
                # Loading the PC. Whatever it is, it is not a value: dispatch.
                if base == 13 and ins.writeback:
                    return "{ " + body + f" uint32_t target = {macro}(a); " + wb + " arc_return(c, target); return; }"
                return "{ " + body + wb + " arc_dispatch(c, " + macro + "(a)); return; }"
            body += wb + f" ARC_W(c, {rt[0]}, {macro}(a));"
            return "{ " + body + " }"
        if len(rt) == 2 and op == "ldrd":
            body += wb + (f" ARC_W(c, {rt[0]}, ARC_LD32(a));"
                          f" ARC_W(c, {rt[1]}, ARC_LD32(a + 4));")
            return "{ " + body + " }"
        raise Unsupported(form(ins))

    def _store(self, ins, op: str) -> str:
        suffix = "" if op == "strd" else op[3:]
        if suffix not in WIDTH or WIDTH[suffix][1]:
            raise Unsupported(form(ins))
        size = WIDTH[suffix][0]
        ops = ins.operands
        mem_index = next(i for i, o in enumerate(ops) if o.type == ca.ARM_OP_MEM)
        rt = [self.reg(ins, o.reg) for o in ops[:mem_index]]
        addr, wb, _ = self._mem_address(ins, mem_index)
        macro = {1: "ARC_ST8", 2: "ARC_ST16", 4: "ARC_ST32"}[size]
        body = f"uint32_t a = {addr}; "
        if len(rt) == 1:
            body += f"{macro}(a, {self.read(ins, rt[0])}); " + wb
        elif len(rt) == 2 and op == "strd":
            body += (f"ARC_ST32(a, {self.read(ins, rt[0])});"
                     f" ARC_ST32(a + 4, {self.read(ins, rt[1])}); " + wb)
        else:
            raise Unsupported(form(ins))
        return "{ " + body + " }"

    def _block(self, ins, op: str) -> str:
        """push/pop/ldm/stm.

        Registers always transfer in increasing register order at increasing
        addresses; the mode only decides where the run starts. A PC in the list
        ends the function -- which is how most functions of this era return,
        so `bx lr` cannot be treated as the terminator.
        """
        ops = ins.operands
        if op in ("push", "pop"):
            base, regs, writeback = 13, [self.reg(ins, o.reg) for o in ops], True
            mode = "db" if op == "push" else "ia"
            load = op == "pop"
        else:
            base = self.reg(ins, ops[0].reg)
            regs = [self.reg(ins, o.reg) for o in ops[1:]]
            writeback = ins.writeback
            mode = op[3:] or "ia"
            load = op.startswith("ldm")
        if not regs:
            raise Unsupported(form(ins))
        regs = sorted(regs)
        n = len(regs) * 4
        base_expr = self.read(ins, base)
        start = {"ia": f"{base_expr}", "ib": f"{base_expr} + 4u",
                 "da": f"{base_expr} - {n - 4:#x}u", "db": f"{base_expr} - {n:#x}u"}
        final = {"ia": f"{base_expr} + {n:#x}u", "ib": f"{base_expr} + {n:#x}u",
                 "da": f"{base_expr} - {n:#x}u", "db": f"{base_expr} - {n:#x}u"}
        if mode not in start:
            raise Unsupported(form(ins))
        parts = [f"uint32_t a = {start[mode]};"]
        # The write-back is computed from the original base and applied before
        # the transfers, so a base that is also in the register list ends up
        # holding the loaded value rather than the pointer.
        if writeback:
            parts.append(f"ARC_W(c, {base}, {final[mode]});")
        returns = False
        for i, r in enumerate(regs):
            at = "a" if i == 0 else f"a + {i * 4:#x}u"
            if load and r == 15:
                if base != 13:
                    parts.append(f"arc_dispatch(c, ARC_LD32({at})); return;")
                else:
                    parts.append(f"arc_return(c, ARC_LD32({at}));")
                returns = True
            elif load:
                parts.append(f"ARC_W(c, {r}, ARC_LD32({at}));")
            else:
                parts.append(f"ARC_ST32({at}, {self.read(ins, r)});")
        if returns and base == 13:
            parts.append("return;")
        return "{ " + " ".join(parts) + " }"

    # --- VFP ---------------------------------------------------------------
    # armv6 means VFPv2: s0-s31 and d0-d15, no NEON. These binaries are
    # soft-float at the ABI boundary and hard-float inside, which is why the
    # file is full of `vmov s13, r0` moving bits rather than converting them.

    @staticmethod
    def vreg(ins, r):
        """(index, 's'|'d') for a VFP register, or None for a core register.

        The name is the only thing that distinguishes them, and it has a trap
        in it: `sl`, `sb` and `sp` are core registers whose names start with
        the same letter as `s0`. The digits are what decide.
        """
        n = ins.reg_name(r)
        if len(n) > 1 and n[0] in "sd" and n[1:].isdigit():
            return int(n[1:]), n[0]
        return None

    def vread(self, ins, op) -> str:
        v = self.vreg(ins, op.reg)
        if v is None:
            raise Unsupported(form(ins))
        return f"ARC_{'S' if v[1] == 's' else 'D'}(c, {v[0]})"

    def _vmov(self, ins, op: str) -> str:
        ops = ins.operands
        v = [self.vreg(ins, o.reg) if o.type == ca.ARM_OP_REG else None
             for o in ops]
        if len(ops) == 2:
            if v[0] and ops[1].type == ca.ARM_OP_FP:
                index, kind = v[0]
                if kind == "s":
                    bits = struct.unpack('<I', struct.pack('<f', ops[1].fp))[0]
                    return f"ARC_SU(c, {index}) = {bits:#x}u;"
                bits = struct.unpack('<Q', struct.pack('<d', ops[1].fp))[0]
                return f"ARC_DU(c, {index}) = {bits:#x}ull;"
            if v[0] and v[1]:
                if v[0][1] != v[1][1]:
                    raise Unsupported(form(ins))
                w = "SU" if v[0][1] == "s" else "DU"
                # A bit copy rather than an assignment through the float view:
                # `vmov.f32` must not quieten a signalling NaN in passing.
                return f"ARC_{w}(c, {v[0][0]}) = ARC_{w}(c, {v[1][0]});"
            if v[0] and not v[1]:
                return (f"ARC_SU(c, {v[0][0]}) = "
                        f"{self.read(ins, self.reg(ins, ops[1].reg))};")
            if v[1] and not v[0]:
                return self.write(self.reg(ins, ops[0].reg),
                                  f"ARC_SU(c, {v[1][0]})")
        if len(ops) == 3:
            # A double moved through two core registers, low half first.
            if v[0] and v[0][1] == "d" and not v[1] and not v[2]:
                n = v[0][0]
                return (f"ARC_SU(c, {2 * n}) = "
                        f"{self.read(ins, self.reg(ins, ops[1].reg))}; "
                        f"ARC_SU(c, {2 * n + 1}) = "
                        f"{self.read(ins, self.reg(ins, ops[2].reg))};")
            if v[2] and v[2][1] == "d" and not v[0] and not v[1]:
                n = v[2][0]
                return (self.write(self.reg(ins, ops[0].reg),
                                   f"ARC_SU(c, {2 * n})") + " " +
                        self.write(self.reg(ins, ops[1].reg),
                                   f"ARC_SU(c, {2 * n + 1})"))
        raise Unsupported(form(ins))

    def _vldst(self, ins, op: str) -> str:
        ops = ins.operands
        v = self.vreg(ins, ops[0].reg)
        if v is None:
            raise Unsupported(form(ins))
        idx, kind = v
        if op == "vldr":
            value = self.pc_literal(ins, 1, 4 if kind == "s" else 8)
            if value is not None:
                self.folded += 1
                view = "SU" if kind == "s" else "DU"
                suffix = "u" if kind == "s" else "ull"
                return f"ARC_{view}(c, {idx}) = {value:#x}{suffix};"
        addr, wb, _ = self._mem_address(ins, 1)
        if op == "vldr":
            access = (f"ARC_SU(c, {idx}) = ARC_LD32(a);" if kind == "s"
                      else f"ARC_DU(c, {idx}) = ARC_LD64(a);")
        else:
            access = (f"ARC_ST32(a, ARC_SU(c, {idx}));" if kind == "s"
                      else f"ARC_ST64(a, ARC_DU(c, {idx}));")
        return "{ uint32_t a = " + addr + "; " + access + " " + wb + " }"

    def _vblock(self, ins, op: str) -> str:
        """VFP register lists use the same increasing-address transfer order."""
        if op in ("vpush", "vpop"):
            operands = ins.operands
            base, load, mode, writeback = 13, op == "vpop", "ia" if op == "vpop" else "db", True
        else:
            operands = ins.operands[1:]
            base = self.reg(ins, ins.operands[0].reg)
            load = op.startswith("vldm")
            mode = op[4:] or "ia"
            writeback = ins.writeback
            if mode not in ("ia", "db"):
                raise Unsupported(form(ins))
        regs = []
        for o in operands:
            v = self.vreg(ins, o.reg)
            if v is None:
                raise Unsupported(form(ins))
            regs.append(v)
        kinds = {k for _, k in regs}
        if len(kinds) != 1:
            raise Unsupported(form(ins))
        kind = kinds.pop()
        step = 8 if kind == "d" else 4
        n = len(regs) * step
        view = "SU" if kind == "s" else "DU"
        ld = "ARC_LD32" if kind == "s" else "ARC_LD64"
        st = "ARC_ST32" if kind == "s" else "ARC_ST64"
        # The new stack pointer is computed and written before the transfers,
        # so a fault part-way through leaves sp somewhere describable.
        source = self.read(ins, base)
        if mode == "db":
            parts = [f"uint32_t a = {source} - {n:#x}u;"]
            if writeback:
                parts.append(f"ARC_W(c, {base}, a);")
        else:
            parts = [f"uint32_t a = {source};"]
            if writeback:
                parts.append(f"ARC_W(c, {base}, a + {n:#x}u);")
        for i, (idx, _) in enumerate(sorted(regs)):
            at = "a" if i == 0 else f"a + {i * step:#x}u"
            if load:
                parts.append(f"ARC_{view}(c, {idx}) = {ld}({at});")
            else:
                parts.append(f"{st}({at}, ARC_{view}(c, {idx}));")
        return "{ " + " ".join(parts) + " }"

    def _vfp_arith(self, ins, op: str) -> str:
        parts = ins.mnemonic.split(".")
        if len(parts) < 2 or parts[1] not in ("f32", "f64"):
            raise Unsupported(form(ins))
        ty = parts[1]
        ops = ins.operands
        d = self.vread(ins, ops[0])
        if op in ("vneg", "vabs", "vsqrt"):
            if op == "vabs":
                destination = self.vreg(ins, ops[0].reg)
                source = self.vreg(ins, ops[1].reg)
                if destination is None or source is None or destination[1] != source[1]:
                    raise Unsupported(form(ins))
                view = "SU" if destination[1] == "s" else "DU"
                mask = "0x7fffffffu" if destination[1] == "s" else "0x7fffffffffffffffull"
                return f"ARC_{view}(c, {destination[0]}) = ARC_{view}(c, {source[0]}) & {mask};"
            a = self.vread(ins, ops[1])
            expr = {"vneg": f"-({a})",
                    "vabs": f"(({a}) < 0 ? -({a}) : ({a}))",
                    "vsqrt": f"arc_sqrt{ty}({a})"}[op]
            return f"{d} = {expr};"
        if len(ops) != 3:
            raise Unsupported(form(ins))
        a, b = self.vread(ins, ops[1]), self.vread(ins, ops[2])
        binop = {"vadd": "add", "vsub": "sub", "vmul": "mul", "vdiv": "div"}
        if op in binop:
            return f"{d} = arc_{binop[op]}{ty}({a}, {b});"
        # The accumulating forms. VNMLS negates the accumulator and not the
        # product, which is the opposite of what the name suggests: it is
        # -Sd + Sn*Sm, not -(Sd + Sn*Sm).
        product = f"arc_mul{ty}({a}, {b})"
        # VNMUL is the plain one of the negated family: the product itself,
        # negated. It is not an accumulate and does not read the destination.
        if op == "vnmul":
            return f"{d} = -{product};"
        if op == "vmla":
            return f"{d} = arc_add{ty}({d}, {product});"
        if op == "vmls":
            return f"{d} = arc_sub{ty}({d}, {product});"
        if op == "vnmla":
            return f"{d} = arc_sub{ty}(-({d}), {product});"
        if op == "vnmls":
            return f"{d} = arc_add{ty}(-({d}), {product});"
        raise Unsupported(form(ins))

    def _vcmp(self, ins, op: str) -> str:
        parts = ins.mnemonic.split(".")
        if len(parts) < 2 or parts[1] not in ("f32", "f64"):
            raise Unsupported(form(ins))
        ops = ins.operands
        a = self.vread(ins, ops[0])
        if ops[1].type == ca.ARM_OP_IMM:
            # The only immediate this encoding admits is zero.
            b = "0.0f" if parts[1] == "f32" else "0.0"
        else:
            b = self.vread(ins, ops[1])
        # vcmpe raises an exception on a quiet NaN and vcmp does not, which
        # moves a bit of FPSCR nothing here reads. The flags are identical.
        return f"arc_vcmp_{parts[1]}(c, {a}, {b});"

    def _vcvt(self, ins, op: str) -> str:
        parts = ins.mnemonic.split(".")
        if len(parts) != 3:
            raise Unsupported(form(ins))
        to, frm = parts[1], parts[2]
        ops = ins.operands
        dv = self.vreg(ins, ops[0].reg)
        sv = self.vreg(ins, ops[1].reg)
        if dv is None or sv is None:
            raise Unsupported(form(ins))
        fixed = len(ops) == 3
        if len(ops) not in (2, 3) or fixed and (ops[2].type != ca.ARM_OP_IMM or
                                               not 1 <= ops[2].imm <= 32):
            raise Unsupported(form(ins))
        if frm in ("s32", "u32"):
            if to not in ("f32", "f64"):
                raise Unsupported(form(ins))
            # Fixed-point doubles carry their integer in the low word.
            raw = f"ARC_SU(c, {sv[0] * (2 if sv[1] == 'd' else 1)})"
            src = f"(int32_t){raw}" if frm == "s32" else raw
            cast = "(float)" if to == "f32" else "(double)"
            scale = f" * 0x1p-{ops[2].imm}{'f' if to == 'f32' else ''}" if fixed else ""
            return f"{self.vread(ins, ops[0])} = {cast}({src}){scale};"
        if to in ("s32", "u32"):
            if frm not in ("f32", "f64"):
                raise Unsupported(form(ins))
            scale = f" * 0x1p+{ops[2].imm}{'f' if frm == 'f32' else ''}" if fixed else ""
            view = "DU" if dv[1] == "d" else "SU"
            extend = "(uint64_t)(int64_t)(int32_t)" if view == "DU" and to == "s32" else ""
            return (f"ARC_{view}(c, {dv[0]}) = "
                    f"{extend}arc_{frm}_to_{to}({self.vread(ins, ops[1])}{scale});")
        if fixed:
            raise Unsupported(form(ins))
        if to == "f32" and frm == "f64":
            return f"ARC_S(c, {dv[0]}) = (float)({self.vread(ins, ops[1])});"
        if to == "f64" and frm == "f32":
            return f"ARC_D(c, {dv[0]}) = (double)({self.vread(ins, ops[1])});"
        raise Unsupported(form(ins))

    def _vmrs(self, ins, op: str) -> str:
        # VFP comparisons publish their flags through FPSCR.
        if "apsr_nzcv" in ins.op_str and "fpscr" in ins.op_str:
            return "arc_vmrs_nzcv(c);"
        ops = ins.operands
        if len(ops) == 2 and "fpscr" in ins.op_str:
            # `vmsr fpscr, rN` is the other half of the same idiom: read it,
            # change the rounding or exception bits, put it back. The flag
            # bits are the emitter's own, so only the rest is stored.
            if op == "vmsr" and ops[1].type == ca.ARM_OP_REG:
                rn = self.reg(ins, ops[1].reg)
                if rn != 15:
                    return f"c->fpscr = ARC_R(c, {rn});"
            elif ops[0].type == ca.ARM_OP_REG:
                rd = self.reg(ins, ops[0].reg)
                if rd != 15:
                    return f"ARC_W(c, {rd}, c->fpscr);"
        raise Unsupported(form(ins))

    # --- control flow ------------------------------------------------------

    def _branch(self, ins, op: str, local: set[int]) -> str:
        ops = ins.operands
        target_op = ops[-1]
        if op in ("cbz", "cbnz") and len(ops) == 2 and target_op.type == ca.ARM_OP_IMM:
            target = target_op.imm & 0xfffffffe
            value = self.read(ins, self.reg(ins, ops[0].reg))
            if target in local:
                branch = f"goto L_{target:08x};"
            else:
                self.referenced.add(target)
                branch = f"{self.name(target)}(c); return;"
            return f"if ({value} {'==' if op == 'cbz' else '!='} 0) {{ {branch} }}"
        if op in ("b", "bl", "blx") and target_op.type == ca.ARM_OP_IMM:
            target = target_op.imm & 0xFFFFFFFF
            if op == "b":
                if (target & ~1) in local:
                    return f"goto L_{target & ~1:08x};"
                self.referenced.add(target & ~1)
                # A branch out of the function is a tail call: the callee
                # returns to our caller, which in C is exactly a call and a
                # return.
                return f"{self.name(target & ~1)}(c); return;"
            self.referenced.add(target & ~1)
            ret = (ins.address + ins.size) | self.thumb
            return (f"ARC_W(c, 14, {self.img(ret)}); "
                    f"{self.name(target & ~1)}(c); "
                    f"if (ARC_R(c, 15) != {self.img(ret & ~1)} || "
                    f"(c->cpsr & 32u) != {32 if self.thumb else 0}u) return;")
        if op in ("bx", "blx", "bxj") and target_op.type == ca.ARM_OP_REG:
            rm = self.reg(ins, target_op.reg)
            if op == "bx" and rm == 14:
                return "arc_return(c, ARC_LR(c)); return;"
            value = self.read(ins, rm)
            if op == "blx":
                ret = (ins.address + ins.size) | self.thumb
                return (f"{{ uint32_t target = {value}; ARC_W(c, 14, {self.img(ret)}); "
                        f"arc_dispatch(c, target); if (ARC_R(c, 15) != {self.img(ret & ~1)} || "
                        f"(c->cpsr & 32u) != {32 if self.thumb else 0}u) return; }}")
            return f"arc_dispatch(c, {value}); return;"
        raise Unsupported(form(ins))

    # --- one instruction ---------------------------------------------------

    def instruction(self, ins, local: set[int]) -> str:
        op = self.root(ins)
        table = self.tables.get(ins.address)
        if table:
            # A resolved switch becomes the branch it always was. The guest's
            # own `cmp` is the bounds check, and the condition this load
            # carries is what lets an out-of-range selector fall through to
            # the default -- so the conditional wrapper at the end of this
            # function is exactly the right thing to leave it to.
            ops_t = ins.operands
            if op in ("tbb", "tbh"):
                idx = self.reg(ins, ops_t[0].mem.index)
            else:
                idx = self.reg(ins, ops_t[1].mem.index) \
                    if ops_t[1].type == ca.ARM_OP_MEM \
                    else self.reg(ins, ops_t[2].reg)
            arms = " ".join(f"case {k}: goto L_{t:08x};"
                            for k, t in enumerate(table))
            body = f'switch (ARC_R(c, {idx})) {{ {arms} default: arc_trap(c, "invalid switch index"); }}'
        elif op in ALU3:
            body = self._alu3(ins, op)
        elif op in ALU2:
            body = self._alu2(ins, op)
        elif op in TEST:
            body = self._test(ins, op)
        elif op in SHIFTOP:
            body = self._shift(ins, op)
        elif op in ("mul", "mla", "mls", "umull", "smull", "umlal", "smlal"):
            body = self._mul(ins, op)
        elif op in ("uxtb", "uxth", "sxtb", "sxth", "uxtab", "uxtah",
                    "sxtab", "sxtah"):
            body = self._extend(ins, op)
        elif op in ("b", "bl", "bx", "blx", "bxj", "cbz", "cbnz"):
            body = self._branch(ins, op, local)
        elif op.startswith("ldr") and not op.startswith("ldrex"):
            body = self._load(ins, op)
        elif op.startswith("str") and not op.startswith("strex"):
            body = self._store(ins, op)
        elif op in ("push", "pop") or op.startswith(("ldm", "stm")):
            body = self._block(ins, op)
        elif op in ("smulbb", "smulbt", "smultb", "smultt"):
            body = self._smulxy(ins, op)
        elif op == "vmov":
            body = self._vmov(ins, op)
        elif op in ("vldr", "vstr"):
            body = self._vldst(ins, op)
        elif op in ("vpush", "vpop") or op.startswith(("vldm", "vstm")):
            body = self._vblock(ins, op)
        elif op in ("vadd", "vsub", "vmul", "vdiv", "vnmul", "vmla", "vmls",
                    "vnmla",
                    "vnmls", "vneg", "vabs", "vsqrt"):
            body = self._vfp_arith(ins, op)
        elif op in ("vcmp", "vcmpe"):
            body = self._vcmp(ins, op)
        elif op == "vcvt":
            body = self._vcvt(ins, op)
        elif op in ("vmrs", "vmsr"):
            body = self._vmrs(ins, op)
        else:
            body = self._misc(ins, op)
        if ins.cc and ins.cc != ca.ARM_CC_AL:
            return f"if (ARC_COND_{CC_MACRO[ins.cc]}(c)) {{ {body} }}"
        return body

    # --- decode and reachability -------------------------------------------

    def decode(self, body: bytes, addr: int, thumb: bool) -> dict:
        """Every instruction in the range, by address.

        Capstone stops at the first byte it cannot decode, which in a
        symbol-delimited function is the literal pool at the end. Step past it
        and resume, so a pool in the middle does not hide the code after it.
        """
        md = self.md[thumb]
        out, pos, step = {}, 0, 2 if thumb else 4
        while pos < len(body):
            before = pos
            for ins in md.disasm(body[pos:], addr + pos):
                out[ins.address] = ins
                pos = ins.address - addr + ins.size
            if pos == before:
                pos += step
        return out

    def writes_pc(self, ins, op: str) -> bool:
        if op in ("b", "bx", "bxj", "tbb", "tbh", "cbz", "cbnz"):
            return True
        if op in ("pop", "ldm", "ldmia", "ldmib", "ldmda", "ldmdb"):
            return any(o.type == ca.ARM_OP_REG and self.reg(ins, o.reg) == 15
                       for o in ins.operands)
        if op in ALU2 or op in ALU3 or op.startswith("ldr"):
            ops = ins.operands
            return bool(ops) and ops[0].type == ca.ARM_OP_REG \
                and self.reg(ins, ops[0].reg) == 15
        return False

    def jump_table(self, insns: dict, a: int):
        """Targets of `ldr<cond> pc, [pc, rN, lsl #2]` -- an ARM switch.

        A dense switch compiles to a bounds check, a load of the PC from a
        table indexed by the selector, and a fall-through to the default:

            cmp   r3, #4                  <- the bound, and the only record
            ldrls pc, [pc, r3, lsl #2]       of how long the table is
            b     <default>
            <5 words of absolute addresses>

        The table is read at lift time for exactly the reason a literal-pool
        load is folded at lift time: it lives in `__TEXT`, which is mapped r-x
        and cannot be written, and the image is never slid, so those words are
        already the addresses they will have at run time.

        Without this the walk stops at the load. The table is data, so nothing
        branches to the case bodies, they are never reached, and they lift to
        nothing -- then the switch becomes an indirect branch to an address
        that is inside a function but is not its entry, which the dispatcher
        cannot answer. Two thirds of `-[Shard init]` was invisible this way,
        and it is the function `PlayState` faults in.

        Returns None rather than guessing whenever anything does not fit.
        """
        ins = insns[a]
        ops = ins.operands
        op = self.root(ins)
        if self.thumb and op in ("tbb", "tbh"):
            return self.thumb_jump_table(insns, a)
        idx = None
        indirect = False
        stride = 4
        if op == "ldr" and len(ops) == 2 and ops[0].type == ca.ARM_OP_REG \
                and self.reg(ins, ops[0].reg) == 15 \
                and ops[1].type == ca.ARM_OP_MEM:
            # `ldr pc, [pc, rN, lsl #2]` -- the table holds addresses.
            mem = ops[1].mem
            if mem.base and self.reg(ins, mem.base) == 15 and mem.index \
                    and ops[1].shift.value == 2:
                idx = self.reg(ins, mem.index)
                indirect = True
        elif op == "add" and len(ops) == 3 \
                and ops[0].type == ca.ARM_OP_REG \
                and self.reg(ins, ops[0].reg) == 15 \
                and ops[1].type == ca.ARM_OP_REG \
                and self.reg(ins, ops[1].reg) == 15 \
                and ops[2].type == ca.ARM_OP_REG \
                and ops[2].shift.value in (2, 3):
            # `add pc, pc, rN, lsl #k` -- the table *is* the code, so the
            # targets are computed and there is nothing to read. The shift is
            # the slot size: lsl #2 is one branch per case, lsl #3 is two
            # instructions per case, and a compiler picks whichever the case
            # bodies fit in. Assuming the first is how the second lands every
            # case after the zeroth on the wrong instruction.
            idx = self.reg(ins, ops[2].reg)
            stride = 1 << ops[2].shift.value
        if idx is None:
            return None

        # The bound lives in the compare that set the condition this load is
        # predicated on. An unconditional load of the PC has no bound at all
        # and there is nothing here to find.
        if not ins.cc or ins.cc == ca.ARM_CC_AL:
            return None
        count, b = None, a - ins.size
        for _ in range(4):
            if b not in insns:
                break
            prev = insns[b]
            pops = prev.operands
            root = prev.mnemonic.split(".")[0]
            if root in ("cmp", "rsbs") and len(pops) >= 2 \
                    and pops[0].type == ca.ARM_OP_REG \
                    and self.reg(prev, pops[0].reg) == idx \
                    and pops[-1].type == ca.ARM_OP_IMM:
                # `cmp rN, #K` states the bound outright. `rsbs rN, rN, #K`
                # does not -- it computes K - rN, and the range is a property
                # of what fed it, which here is a difference of two clz
                # results. Taking K + 1 is an inference, and it is allowed
                # only because every slot is checked below against being a
                # real instruction inside this same function: a wrong bound
                # cannot quietly produce targets, it produces a refusal.
                count = pops[-1].imm + 1
                break
            b -= prev.size
        if not count or count < 1 or count > 4096:
            return None

        base = a + 8                       # the ARM PC bias, not a guess
        out = []
        for k in range(count):
            if indirect:
                word = self.literal(base + k * 4, 4)
                if word is None:
                    return None
                target = word & 0xFFFFFFFE
            else:
                target = base + k * stride
            # Every entry has to be an instruction in this same function. One
            # that is not says the bound was wrong, and a wrong bound means
            # treating whatever follows the table as branch targets.
            if target not in insns:
                return None
            out.append(target)
        return out

    def thumb_jump_table(self, insns: dict, address: int):
        ins = insns[address]
        if len(ins.operands) != 1 or ins.operands[0].type != ca.ARM_OP_MEM:
            return None
        mem = ins.operands[0].mem
        if not mem.base or self.reg(ins, mem.base) != 15 or not mem.index:
            return None
        index = self.reg(ins, mem.index)
        previous = [insns[a] for a in sorted(insns) if a < address][-6:]
        count = None
        # The compiler's unsigned range guard gives the exact table length.
        # Without that guard, do not guess where data ends and code starts.
        for i in range(len(previous) - 2, -1, -1):
            compare = previous[i]
            if self.root(compare) != "cmp" or len(compare.operands) != 2 or \
                    compare.operands[0].type != ca.ARM_OP_REG or \
                    self.reg(compare, compare.operands[0].reg) != index or \
                    compare.operands[1].type != ca.ARM_OP_IMM:
                continue
            guards = previous[i + 1:]
            if len(guards) != 1 or self.root(guards[0]) != "b":
                continue
            if guards[0].cc == ca.ARM_CC_HI:
                count = compare.operands[1].imm + 1
            elif guards[0].cc == ca.ARM_CC_HS:
                count = compare.operands[1].imm
            break
        if count is None or not 1 <= count <= 4096:
            return None
        base = address + 4
        width = 1 if self.root(ins) == "tbb" else 2
        offset = base - self._body_address
        if offset < 0 or offset + count * width > len(self._body):
            return None
        targets = []
        for n in range(count):
            value = int.from_bytes(self._body[offset + n * width:offset + (n + 1) * width], "little")
            target = base + 2 * value
            if target < base + count * width or target >= self._body_address + len(self._body):
                return None
            targets.append(target)
        # A literal table can decode as a 32-bit instruction and swallow the
        # first case in a linear decode. Restart at each proven case target.
        for target in targets:
            for decoded in self.md[True].disasm(self._body[target-self._body_address:], target):
                insns[decoded.address] = decoded
                if self.writes_pc(decoded, self.root(decoded)) and decoded.cc in (0, ca.ARM_CC_AL):
                    break
            if target not in insns:
                return None
        return targets

    def reachable(self, insns: dict, start: int) -> set[int]:
        """Walk from the entry point.

        This is what separates code from the literal pool. A pool sits past an
        unconditional terminator with nothing branching into it, so it is simply
        never reached -- no heuristic about what a word of zeroes means.
        """
        seen: set[int] = set()
        self.tables = {}
        self._missing = set()
        self._fallthrough = False
        work = [start]
        while work:
            a = work.pop()
            if a in seen:
                continue
            if a not in insns:
                if self._body_address <= a < self._body_address + len(self._body):
                    self._missing.add(a)
                elif a == self._body_address + len(self._body):
                    self._fallthrough = True
                continue
            seen.add(a)
            ins = insns[a]
            try:
                op = self.root(ins)
                ends = self.writes_pc(ins, op)
                if op in ("bl", "blx") and ins.operands and ins.operands[-1].type == ca.ARM_OP_IMM:
                    ends |= (ins.operands[-1].imm & 0xfffffffe) in self.noreturn
            except Unsupported:
                op, ends = "", False
            # Calls can target another instruction set even when coarse
            # unwind metadata puts the callee inside this byte range.
            if op in ("b", "cbz", "cbnz") and ins.operands \
                    and ins.operands[-1].type == ca.ARM_OP_IMM:
                t = ins.operands[-1].imm & 0xFFFFFFFE
                if t in insns:
                    work.append(t)
            # A switch reaches its case bodies only through the table.
            if ends and op in ("ldr", "add", "tbb", "tbh") and a not in self.tables:
                table = self.jump_table(insns, a)
                if table:
                    self.tables[a] = table
                    work.extend(table)
            # A conditional terminator still falls through to the next
            # instruction; only an unconditional one ends the run.
            if op in ("cbz", "cbnz") or not (ends and (not ins.cc or ins.cc == ca.ARM_CC_AL)):
                work.append(a + ins.size)
        return seen

    # --- one function ------------------------------------------------------

    def lift_function(self, addr: int, size: int, body: bytes,
                      thumb: bool) -> tuple[str, bool]:
        self.thumb = thumb
        self._body = body
        self._body_address = addr
        insns = self.decode(body, addr, thumb)
        live = self.reachable(insns, addr)
        order = sorted(live)

        targets: set[int] = set()
        returns: set[int] = set()
        for a in order:
            ins = insns[a]
            try:
                op = self.root(ins)
            except Unsupported:
                continue
            if op in ("bl", "blx") and a + ins.size in live:
                returns.add(a + ins.size)
                self.continuations[a + ins.size] = (addr, thumb)
            if op in ("b", "cbz", "cbnz") and ins.operands and ins.operands[-1].type == ca.ARM_OP_IMM:
                t = ins.operands[-1].imm & 0xFFFFFFFE
                if t in live:
                    targets.add(t)
            # Every case body of a resolved switch is jumped to by label.
            for t in self.tables.get(a, ()):
                if t in live:
                    targets.add(t)
        targets.update(returns)

        lines = [f"/* {self.name(addr)}: {len(order)} instructions, "
                 f"{'thumb' if thumb else 'arm'} */"]
        if returns:
            # A restored setjmp context can outlive the native call stack.
            # Re-enter at its guest return site without replaying the prologue.
            lines.extend([f"void {self.name(addr)}_resume(Arm32Ctx* c, uint32_t pc);",
                          f"void {self.name(addr)}(Arm32Ctx* c) {{ {self.name(addr)}_resume(c, {addr:#010x}u); }}",
                          f"void {self.name(addr)}_resume(Arm32Ctx* c, uint32_t pc) {{",
                          f"  if (pc != {addr:#010x}u) {{",
                          f'    if ((c->cpsr & 32u) != {32 if thumb else 0}u) {{ arc_trap(c, "invalid continuation mode"); return; }}',
                          "    switch (pc) {"])
            lines.extend(f"    case {pc:#010x}u: goto L_{pc:08x};" for pc in sorted(returns))
            lines.extend(['    default: arc_trap(c, "invalid continuation address"); return;',
                          "    }", "  }"])
        else:
            lines.append(f"void {self.name(addr)}(Arm32Ctx* c) {{")
        lines.extend([
                 f"  if (arc_enter(c, {addr | thumb:#010x}u, {size}u)) return;"])
        complete = not self._missing
        if self._missing:
            self.unsupported['undecodable reachable instruction'] += len(self._missing)
            self.blamed['undecodable reachable instruction'] += 1
        for i, a in enumerate(order):
            ins = insns[a]
            self.insns_total += 1
            if a in targets:
                lines.append(f"L_{a:08x}:;")
                lines.append(f"  arc_block(c, {a | thumb:#010x}u, {ins.size}u);")
            text = f"{ins.mnemonic} {ins.op_str}".strip()
            try:
                code = self.instruction(ins, live)
                self.insns_lifted += 1
            except Unsupported as why:
                key = str(why)
                self.unsupported[key] += 1
                if complete:
                    self.blamed[key] += 1
                complete = False
                code = f'arc_trap(c, "{self.name(addr)}: {text}");'
            lines.append(f"  /* {a:08x}  {text} */")
            lines.append(f"  {code}")
            # Reachable code is not always contiguous: a pool can sit between
            # two live runs. Falling off the end of one into the other would be
            # silently wrong, so the gap is jumped explicitly.
            nxt = order[i + 1] if i + 1 < len(order) else None
            if nxt is not None and nxt != a + ins.size:
                if a + ins.size in self._missing:
                    lines.append('  arc_trap(c, "undecodable reachable instruction");')
                lines.append(f"  goto L_{nxt:08x};")
                targets.add(nxt)
        if self._fallthrough:
            target = addr + size
            self.referenced.add(target)
            lines.append(f"  {self.name(target)}(c); return;")
        else:
            lines.append(f'  arc_trap(c, "{self.name(addr)}: uncovered function fallthrough");')
        lines.append("}")
        # A label the gap jump introduced late still has to exist.
        out = "\n".join(lines)
        for t in targets:
            if f"L_{t:08x}:;" not in out:
                out = out.replace(f"  /* {t:08x}  ", f"L_{t:08x}:;\n  /* {t:08x}  ", 1)
        return out, complete
