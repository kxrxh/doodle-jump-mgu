// Adapted from iparecomp. See ../../LICENSE and ../../upstream.json.
use std::borrow::Cow;
use std::fmt::Write;

use capstone::arch::arm::{ArmOperand, ArmOperandType, ArmShift};

use crate::decode::{Instruction, immediate, register};
use crate::lifter::{Emission, Lifter, image_address, write_register};

impl Lifter<'_> {
    fn arithmetic_operands<'a>(
        &self,
        ins: &'a Instruction,
        count: usize,
    ) -> Emission<Cow<'a, [ArmOperand]>> {
        let ops = ins.operands.as_slice();
        if !self.thumb
            && ops.len() == count + 1
            && let (Some(value), Some(rotation)) =
                (immediate(&ops[count - 1]), immediate(&ops[count]))
        {
            if !(0..32).contains(&rotation) || rotation & 1 != 0 {
                return Err(ins.unsupported());
            }
            let mut ops = ops[..count].to_vec();
            ops[count - 1].op_type =
                ArmOperandType::Imm((value as u32).rotate_right(rotation as u32) as i32);
            Ok(Cow::Owned(ops))
        } else {
            Ok(Cow::Borrowed(ops))
        }
    }

    pub(crate) fn alu3(&self, ins: &Instruction, op: &str) -> Emission {
        let ops = self.arithmetic_operands(ins, 3)?;
        let (destination, left, right) = match ops.as_ref() {
            [d, b] => (d, d, b),
            [d, a, b] => (d, a, b),
            _ => return Err(ins.unsupported()),
        };
        let rd = self.dest(ins, destination)?;
        let mut a = self.read(ins, self.dest(ins, left)?);
        let (mut b, carry) = self.operand(ins, right)?;
        let arithmetic = match op {
            "add" => Some(("a + b", "arc_add_flags(c, a, b, 0)")),
            "adc" => Some(("a + b + ci", "arc_add_flags(c, a, b, ci)")),
            "sub" | "rsb" => Some(("a - b", "arc_sub_flags(c, a, b, 1)")),
            "sbc" | "rsc" => Some(("a - b - (1u - ci)", "arc_sub_flags(c, a, b, ci)")),
            _ => None,
        };
        if let Some((expr, flags)) = arithmetic {
            if matches!(op, "rsb" | "rsc") {
                std::mem::swap(&mut a, &mut b);
            }
            // Flag-setting carry instructions must latch the input carry first.
            let latch = if matches!(op, "adc" | "sbc" | "rsc") {
                ", ci = c->cf"
            } else {
                ""
            };
            let mut body = format!("{{ uint32_t a = {a}, b = {b}{latch}; ");
            if ins.sets_flags() {
                let _ = write!(body, "{flags}; ");
            }
            return Ok(format!("{body}{} }}", write_register(rd, expr)));
        }
        let expr = match op {
            "and" => "a & b",
            "orr" => "a | b",
            "eor" => "a ^ b",
            "bic" => "a & ~b",
            "orn" => "a | ~b",
            _ => return Err(ins.unsupported()),
        };
        let mut body = format!("{{ uint32_t a = {a}, b = {b}, r = {expr}; ");
        logical_flags(ins, &mut body, carry);
        Ok(format!("{body}{} }}", write_register(rd, "r")))
    }

    pub(crate) fn alu2(&self, ins: &Instruction, op: &str) -> Emission {
        let ops = self.arithmetic_operands(ins, 2)?;
        let [destination, source] = ops.as_ref() else {
            return Err(ins.unsupported());
        };
        let rd = self.dest(ins, destination)?;
        let (value, carry) = self.operand(ins, source)?;
        let mut body = if op == "movt" {
            let current = self.read(ins, rd);
            format!("{{ uint32_t r = ({current} & 0xffffu) | ((({value}) & 0xffffu) << 16); ")
        } else {
            let invert = if op == "mvn" { "~" } else { "" };
            format!("{{ uint32_t r = {invert}({value}); ")
        };
        if op != "movt" {
            logical_flags(ins, &mut body, carry);
        }
        Ok(format!("{body}{} }}", write_register(rd, "r")))
    }

    pub(crate) fn test(&self, ins: &Instruction, op: &str) -> Emission {
        let ops = self.arithmetic_operands(ins, 2)?;
        let [left, right] = ops.as_ref() else {
            return Err(ins.unsupported());
        };
        let a = self.read(ins, self.dest(ins, left)?);
        let (b, carry) = self.operand(ins, right)?;
        match op {
            "cmp" => Ok(format!(
                "{{ uint32_t a = {a}, b = {b}; arc_sub_flags(c, a, b, 1); }}"
            )),
            "cmn" => Ok(format!(
                "{{ uint32_t a = {a}, b = {b}; arc_add_flags(c, a, b, 0); }}"
            )),
            _ => {
                let expr = if op == "tst" { "a & b" } else { "a ^ b" };
                let mut body = format!("{{ uint32_t a = {a}, b = {b}, r = {expr}; ARC_NZ(c, r); ");
                if let Some(carry) = carry {
                    let _ = write!(body, "c->cf = {carry}; ");
                }
                body.push('}');
                Ok(body)
            }
        }
    }

    pub(crate) fn shift(&self, ins: &Instruction, op: &str) -> Emission {
        let ops = ins.operands.as_slice();
        let [destination, source, ..] = ops else {
            return Err(ins.unsupported());
        };
        let rd = self.dest(ins, destination)?;
        if ops.len() == 2 && source.shift != ArmShift::Invalid {
            let (value, carry) = self.shifted(ins, source)?;
            let mut body = format!("{{ uint32_t r = {value}; ");
            logical_flags(ins, &mut body, carry);
            return Ok(format!("{body}{} }}", write_register(rd, "r")));
        }
        let call = if op == "rrx" {
            let src = self.read(ins, self.dest(ins, source)?);
            format!("arc_rrx({src}, c->cf)")
        } else {
            let (src, shift) = match ops {
                [_, shift] => (rd, shift),
                [_, a, shift] => (self.dest(ins, a)?, shift),
                _ => return Err(ins.unsupported()),
            };
            let src = self.read(ins, src);
            let amount = if let Some(value) = immediate(shift) {
                format!("{}u", value & 0xff)
            } else {
                format!("({} & 0xFFu)", self.read(ins, self.dest(ins, shift)?))
            };
            format!("arc_{op}({src}, {amount}, c->cf)")
        };
        let flags = if ins.sets_flags() {
            "ARC_NZ(c, s.value); c->cf = s.carry; "
        } else {
            ""
        };
        Ok(format!(
            "{{ ArcShift s = {call}; {flags}{} }}",
            write_register(rd, "s.value")
        ))
    }

    pub(crate) fn mul(&self, ins: &Instruction, op: &str) -> Emission {
        let regs = ins
            .operands
            .iter()
            .filter_map(register)
            .map(|reg| self.reg(ins, reg))
            .collect::<Emission<Vec<_>>>()?;
        if matches!(op, "mul" | "mla" | "mls") {
            let (rd, expr) = match (op, regs.as_slice()) {
                ("mul", &[rd, n, m]) => (rd, format!("ARC_R(c, {n}) * ARC_R(c, {m})")),
                ("mla", &[rd, n, m, acc]) => (
                    rd,
                    format!("ARC_R(c, {n}) * ARC_R(c, {m}) + ARC_R(c, {acc})"),
                ),
                ("mls", &[rd, n, m, acc]) => (
                    rd,
                    format!("ARC_R(c, {acc}) - ARC_R(c, {n}) * ARC_R(c, {m})"),
                ),
                _ => return Err(ins.unsupported()),
            };
            let flags = if ins.sets_flags() {
                "ARC_NZ(c, r); "
            } else {
                ""
            };
            return Ok(format!(
                "{{ uint32_t r = {expr}; {flags}{} }}",
                write_register(rd, "r")
            ));
        }
        let [lo, hi, n, m] = regs[..] else {
            return Err(ins.unsupported());
        };
        let wide = if op.starts_with('u') {
            format!("(uint64_t)ARC_R(c, {n}) * (uint64_t)ARC_R(c, {m})")
        } else {
            format!("(uint64_t)((int64_t)(int32_t)ARC_R(c, {n}) * (int64_t)(int32_t)ARC_R(c, {m}))")
        };
        let mut body = format!("{{ uint64_t w = {wide}; ");
        if op.ends_with("lal") {
            let _ = write!(
                body,
                "w += ((uint64_t)ARC_R(c, {hi}) << 32) | ARC_R(c, {lo}); "
            );
        }
        if ins.sets_flags() {
            body.push_str("c->nf = (uint32_t)(w >> 63); c->zf = (w == 0); ");
        }
        Ok(format!(
            "{body}ARC_W(c, {lo}, (uint32_t)w); ARC_W(c, {hi}, (uint32_t)(w >> 32)); }}"
        ))
    }

    pub(crate) fn smulxy(&self, ins: &Instruction, op: &str) -> Emission {
        let [destination, left, right] = ins.operands.as_slice() else {
            return Err(ins.unsupported());
        };
        let part = |operand, half| -> Emission {
            let value = self.read(ins, self.dest(ins, operand)?);
            let high = if half == b't' { " >> 16" } else { "" };
            Ok(format!("(int32_t)(int16_t)({value}{high})"))
        };
        let left = part(left, op.as_bytes()[4])?;
        let right = part(right, op.as_bytes()[5])?;
        Ok(write_register(
            self.dest(ins, destination)?,
            &format!("(uint32_t)({left} * {right})"),
        ))
    }

    pub(crate) fn extend(&self, ins: &Instruction, op: &str) -> Emission {
        let (destination, acc, source) = match ins.operands.as_slice() {
            [d, s] => (d, None, s),
            [d, a, s] => (d, Some(a), s),
            _ => return Err(ins.unsupported()),
        };
        let mut src = self.read(ins, self.dest(ins, source)?);
        match source.shift {
            ArmShift::Invalid => {}
            ArmShift::Ror(n) => src = format!("arc_ror({src}, {n}u, c->cf).value"),
            _ => return Err(ins.unsupported()),
        }
        let cast = match (op.ends_with('h'), op.starts_with("sxt")) {
            (true, true) => "(uint32_t)(int32_t)(int16_t)",
            (true, false) => "(uint32_t)(uint16_t)",
            (false, true) => "(uint32_t)(int32_t)(int8_t)",
            (false, false) => "(uint32_t)(uint8_t)",
        };
        let expr = match acc {
            Some(a) => format!("{} + {cast}({src})", self.read(ins, self.dest(ins, a)?)),
            None => format!("{cast}({src})"),
        };
        Ok(write_register(self.dest(ins, destination)?, &expr))
    }

    pub(crate) fn misc(&self, ins: &Instruction, op: &str) -> Emission {
        let ops = ins.operands.as_slice();
        if let ("adr", [d, offset]) = (op, ops)
            && let Some(offset) = immediate(offset)
        {
            let pc = self.pc_value(ins) & if self.thumb { !3 } else { !0 };
            return Ok(write_register(
                self.dest(ins, d)?,
                &image_address(pc.wrapping_add_signed(offset)),
            ));
        }
        if self.thumb && ins.size == 2 && ins.bytes[1] == 0xbf && ins.bytes[0] & 15 != 0 {
            // Capstone has already assigned the IT predicates to the following instructions.
            return Ok("(void)0;".into());
        }
        match (op, ops) {
            ("sbfx" | "ubfx", [d, s, lsb, width]) => {
                let (lsb, width) = bitfield(ins, lsb, width)?;
                let value = self.read(ins, self.dest(ins, s)?);
                let expr = if op == "ubfx" {
                    format!("(({value} >> {lsb}) & {:#x}u)", u32::MAX >> (32 - width))
                } else {
                    format!(
                        "(uint32_t)((int32_t)({value} << {}) >> {})",
                        32 - lsb - width,
                        32 - width
                    )
                };
                Ok(write_register(self.dest(ins, d)?, &expr))
            }
            ("bfi", [d, s, lsb, width]) => self.bitfield_insert(ins, d, Some(s), lsb, width),
            ("bfc", [d, lsb, width]) => self.bitfield_insert(ins, d, None, lsb, width),
            ("usat", [d, width, value]) => {
                let width = immediate(width)
                    .filter(|n| (0..=31).contains(n))
                    .ok_or_else(|| ins.unsupported())?;
                let maximum = (1u32 << width) - 1;
                let (value, _) = self.shifted(ins, value)?;
                let store = write_register(self.dest(ins, d)?, "result");
                Ok(format!(
                    "{{ int32_t v = (int32_t){value}; uint32_t result; \
                    if (v < 0) {{ result = 0; c->qf = 1; }} \
                    else if ((uint32_t)v > {maximum:#x}u) {{ result = {maximum:#x}u; c->qf = 1; }} \
                    else result = (uint32_t)v; {store} }}"
                ))
            }
            ("nop" | "pld" | "pldw" | "pli" | "dmb" | "dsb" | "isb" | "yield", _) => {
                Ok("(void)0;".into())
            }
            ("clz", [d, s]) => {
                let src = self.read(ins, self.dest(ins, s)?);
                let store = write_register(self.dest(ins, d)?, "n");
                Ok(format!(
                    "{{ uint32_t v = {src}, n = 0; while (n < 32 && !(v & 0x80000000u)) {{ v <<= 1; ++n; }} {store} }}"
                ))
            }
            ("rev" | "rev16" | "revsh", [d, s]) => {
                let value = self.read(ins, self.dest(ins, s)?);
                let expr = match op {
                    "rev" => {
                        "((v >> 24) | ((v >> 8) & 0xFF00u) | ((v << 8) & 0xFF0000u) | (v << 24))"
                    }
                    "rev16" => "(((v >> 8) & 0x00FF00FFu) | ((v << 8) & 0xFF00FF00u))",
                    _ => "((uint32_t)(int32_t)(int16_t)(((v >> 8) & 0xFFu) | (v << 8)))",
                };
                Ok(format!(
                    "{{ uint32_t v = {value}; {} }}",
                    write_register(self.dest(ins, d)?, expr)
                ))
            }
            _ => Err(ins.unsupported()),
        }
    }

    fn bitfield_insert(
        &self,
        ins: &Instruction,
        dest: &ArmOperand,
        source: Option<&ArmOperand>,
        lsb: &ArmOperand,
        width: &ArmOperand,
    ) -> Emission {
        let rd = self.dest(ins, dest)?;
        let (lsb, width) = bitfield(ins, lsb, width)?;
        let mask = (u32::MAX >> (32 - width)) << lsb;
        let value = if let Some(source) = source {
            self.read(ins, self.dest(ins, source)?)
        } else {
            "0u".into()
        };
        Ok(write_register(
            rd,
            &format!(
                "((ARC_R(c, {rd}) & {:#x}u) | (({value} << {lsb}) & {mask:#x}u))",
                !mask
            ),
        ))
    }
}

fn logical_flags(ins: &Instruction, body: &mut String, carry: Option<String>) {
    if ins.sets_flags() {
        body.push_str("ARC_NZ(c, r); ");
        if let Some(carry) = carry {
            let _ = write!(body, "c->cf = {carry}; ");
        }
    }
}

fn bitfield(ins: &Instruction, lsb: &ArmOperand, width: &ArmOperand) -> Emission<(i32, i32)> {
    if let (Some(lsb), Some(width)) = (immediate(lsb), immediate(width))
        && (0..32).contains(&lsb)
        && (1..=32 - lsb).contains(&width)
    {
        Ok((lsb, width))
    } else {
        Err(ins.unsupported())
    }
}
