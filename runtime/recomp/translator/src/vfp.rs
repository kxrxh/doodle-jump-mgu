// Adapted from iparecomp. See ../../LICENSE and ../../upstream.json.
use capstone::arch::arm::{ArmOperand, ArmOperandType, ArmReg};

use crate::decode::{Instruction, immediate, register};
use crate::lifter::{Emission, Lifter, write_register};
use crate::memory::transfer_address;

#[derive(Clone, Copy, Eq, Ord, PartialEq, PartialOrd)]
enum Precision {
    Single,
    Double,
}

#[derive(Clone, Copy, Eq, Ord, PartialEq, PartialOrd)]
struct VfpRegister(u32, Precision);

impl VfpRegister {
    fn from_operand(operand: &ArmOperand) -> Option<Self> {
        let reg = u32::from(register(operand)?.0);
        match reg {
            ArmReg::ARM_REG_S0..=ArmReg::ARM_REG_S31 => {
                Some(Self(reg - ArmReg::ARM_REG_S0, Precision::Single))
            }
            ArmReg::ARM_REG_D0..=ArmReg::ARM_REG_D31 => {
                Some(Self(reg - ArmReg::ARM_REG_D0, Precision::Double))
            }
            _ => None,
        }
    }

    fn floating(self) -> String {
        let view = if self.1 == Precision::Single {
            "S"
        } else {
            "D"
        };
        format!("ARC_{view}(c, {})", self.0)
    }

    fn bits(self) -> String {
        let view = if self.1 == Precision::Single {
            "SU"
        } else {
            "DU"
        };
        format!("ARC_{view}(c, {})", self.0)
    }

    fn low_word(self) -> u32 {
        self.0 * if self.1 == Precision::Single { 1 } else { 2 }
    }
}

fn vfp(ins: &Instruction, operand: &ArmOperand) -> Emission<VfpRegister> {
    VfpRegister::from_operand(operand).ok_or_else(|| ins.unsupported())
}

impl Lifter<'_> {
    pub(crate) fn vmov(&self, ins: &Instruction) -> Emission {
        use Precision::{Double, Single};
        match ins.operands.as_slice() {
            [a, b] => {
                match (
                    VfpRegister::from_operand(a),
                    VfpRegister::from_operand(b),
                    &b.op_type,
                ) {
                    (Some(dest @ VfpRegister(_, Single)), _, ArmOperandType::Fp(imm)) => Ok(
                        format!("{} = {:#x}u;", dest.bits(), (*imm as f32).to_bits()),
                    ),
                    (Some(dest @ VfpRegister(_, Double)), _, ArmOperandType::Fp(imm)) => {
                        Ok(format!("{} = {:#x}ull;", dest.bits(), imm.to_bits()))
                    }
                    (Some(dest), Some(src), _) if dest.1 == src.1 =>
                    // Copy bits to preserve signalling NaNs.
                    {
                        Ok(format!("{} = {};", dest.bits(), src.bits()))
                    }
                    (Some(VfpRegister(dest, _)), None, _) => Ok(format!(
                        "ARC_SU(c, {dest}) = {};",
                        self.read(ins, self.dest(ins, b)?)
                    )),
                    (None, Some(VfpRegister(src, _)), _) => Ok(write_register(
                        self.dest(ins, a)?,
                        &format!("ARC_SU(c, {src})"),
                    )),
                    _ => Err(ins.unsupported()),
                }
            }
            [a, b, c] => match (
                VfpRegister::from_operand(a),
                VfpRegister::from_operand(b),
                VfpRegister::from_operand(c),
            ) {
                (Some(dest @ VfpRegister(_, Double)), None, None) => {
                    let low = dest.low_word();
                    Ok(format!(
                        "ARC_SU(c, {low}) = {}; ARC_SU(c, {}) = {};",
                        self.read(ins, self.dest(ins, b)?),
                        low + 1,
                        self.read(ins, self.dest(ins, c)?)
                    ))
                }
                (None, None, Some(source @ VfpRegister(_, Double))) => {
                    let low = source.low_word();
                    Ok(format!(
                        "{} {}",
                        write_register(self.dest(ins, a)?, &format!("ARC_SU(c, {low})")),
                        write_register(self.dest(ins, b)?, &format!("ARC_SU(c, {})", low + 1))
                    ))
                }
                _ => Err(ins.unsupported()),
            },
            _ => Err(ins.unsupported()),
        }
    }

    pub(crate) fn vldst(&self, ins: &Instruction, op: &str) -> Emission {
        let register = vfp(ins, ins.operands.first().ok_or_else(|| ins.unsupported())?)?;
        let (address, wb, _) = self.memory_address(ins, 1)?;
        let bits = register.bits();
        let width = if register.1 == Precision::Single {
            32
        } else {
            64
        };
        let access = if op == "vldr" {
            format!("{bits} = ARC_LD{width}(a);")
        } else {
            format!("ARC_ST{width}(a, {bits});")
        };
        Ok(format!("{{ uint32_t a = {address}; {access} {wb} }}"))
    }

    pub(crate) fn vblock(&self, ins: &Instruction, op: &str) -> Emission {
        let ops = ins.operands.as_slice();
        let (base, operands, mode, load, writeback) = match op {
            "vpush" => (13, ops, "db", false, true),
            "vpop" => (13, ops, "ia", true, true),
            _ => {
                let [base, operands @ ..] = ops else {
                    return Err(ins.unsupported());
                };
                let mode = if op.len() == 4 { "ia" } else { &op[4..] };
                if !matches!(mode, "ia" | "db") {
                    return Err(ins.unsupported());
                }
                (
                    self.dest(ins, base)?,
                    operands,
                    mode,
                    op.starts_with("vldm"),
                    ins.writeback,
                )
            }
        };
        let mut regs = operands
            .iter()
            .map(|operand| vfp(ins, operand))
            .collect::<Emission<Vec<_>>>()?;
        let VfpRegister(_, precision) = *regs.first().ok_or_else(|| ins.unsupported())?;
        if regs.iter().any(|reg| reg.1 != precision) {
            return Err(ins.unsupported());
        }
        let step = if precision == Precision::Single { 4 } else { 8 };
        let width = step * 8;
        let n = regs.len() * step;
        let source = self.read(ins, base);
        let mut parts = Vec::new();
        if mode == "db" {
            parts.push(format!("uint32_t a = {source} - {n:#x}u;"));
            if writeback {
                parts.push(format!("ARC_W(c, {base}, a);"));
            }
        } else {
            parts.push(format!("uint32_t a = {source};"));
            if writeback {
                parts.push(format!("ARC_W(c, {base}, a + {n:#x}u);"));
            }
        }
        regs.sort_unstable();
        for (index, reg) in regs.into_iter().enumerate() {
            let at = transfer_address(index * step);
            let bits = reg.bits();
            if load {
                parts.push(format!("{bits} = ARC_LD{width}({at});"));
            } else {
                parts.push(format!("ARC_ST{width}({at}, {bits});"));
            }
        }
        Ok(format!("{{ {} }}", parts.join(" ")))
    }

    pub(crate) fn vfp_arithmetic(&self, ins: &Instruction, op: &str) -> Emission {
        let ty = fp_type(ins)?;
        match (op, ins.operands.as_slice()) {
            ("vneg" | "vabs" | "vsqrt", [dest, source]) => {
                let d = vfp(ins, dest)?;
                let a = vfp(ins, source)?;
                if op == "vabs" {
                    if d.1 != a.1 {
                        return Err(ins.unsupported());
                    }
                    let mask = if d.1 == Precision::Single {
                        "0x7fffffffu"
                    } else {
                        "0x7fffffffffffffffull"
                    };
                    return Ok(format!("{} = {} & {mask};", d.bits(), a.bits()));
                }
                let a = a.floating();
                let expr = if op == "vneg" {
                    format!("-({a})")
                } else {
                    format!("arc_sqrt{ty}({a})")
                };
                Ok(format!("{} = {expr};", d.floating()))
            }
            (_, [dest, left, right]) => {
                let d = vfp(ins, dest)?.floating();
                let a = vfp(ins, left)?.floating();
                let b = vfp(ins, right)?.floating();
                let product = format!("arc_mul{ty}({a}, {b})");
                let expr = match op {
                    "vadd" => format!("arc_add{ty}({a}, {b})"),
                    "vsub" => format!("arc_sub{ty}({a}, {b})"),
                    "vmul" => product,
                    "vdiv" => format!("arc_div{ty}({a}, {b})"),
                    "vnmul" => format!("-{product}"),
                    "vmla" => format!("arc_add{ty}({d}, {product})"),
                    "vmls" => format!("arc_sub{ty}({d}, {product})"),
                    "vnmla" => format!("arc_sub{ty}(-({d}), {product})"),
                    "vnmls" => format!("arc_add{ty}(-({d}), {product})"),
                    _ => return Err(ins.unsupported()),
                };
                Ok(format!("{d} = {expr};"))
            }
            _ => Err(ins.unsupported()),
        }
    }

    pub(crate) fn vcmp(&self, ins: &Instruction) -> Emission {
        let ty = fp_type(ins)?;
        let [left, right] = ins.operands.as_slice() else {
            return Err(ins.unsupported());
        };
        let a = vfp(ins, left)?.floating();
        let b = if immediate(right).is_some() {
            // This encoding admits only zero as the immediate.
            if ty == "f32" {
                "0.0f".into()
            } else {
                "0.0".into()
            }
        } else {
            vfp(ins, right)?.floating()
        };
        Ok(format!("arc_vcmp_{ty}(c, {a}, {b});"))
    }

    pub(crate) fn vcvt(&self, ins: &Instruction) -> Emission {
        let mut types = ins.mnemonic.split('.');
        let (Some(to), Some(from), None) = (types.nth(1), types.next(), types.next()) else {
            return Err(ins.unsupported());
        };
        let (dest, source, fixed) = match ins.operands.as_slice() {
            [d, s] => (d, s, None),
            [d, s, bits] => (
                d,
                s,
                Some(
                    immediate(bits)
                        .filter(|n| (1..=32).contains(n))
                        .ok_or_else(|| ins.unsupported())?,
                ),
            ),
            _ => return Err(ins.unsupported()),
        };
        let dest = vfp(ins, dest)?;
        let source = vfp(ins, source)?;
        let scale = |direction, ty| {
            fixed.map_or_else(String::new, |bits| {
                let suffix = if ty == "f32" { "f" } else { "" };
                format!(" * 0x1p{direction}{bits}{suffix}")
            })
        };
        match (to, from) {
            ("f32" | "f64", "s32" | "u32") => {
                let raw = format!("ARC_SU(c, {})", source.low_word());
                let src = if from == "s32" {
                    format!("(int32_t){raw}")
                } else {
                    raw
                };
                let cast = if to == "f32" { "float" } else { "double" };
                Ok(format!(
                    "{} = ({cast})({src}){};",
                    dest.floating(),
                    scale("-", to)
                ))
            }
            ("s32" | "u32", "f32" | "f64") => {
                let extend = if dest.1 == Precision::Double && to == "s32" {
                    "(uint64_t)(int64_t)(int32_t)"
                } else {
                    ""
                };
                Ok(format!(
                    "{} = {extend}arc_{from}_to_{to}({}{});",
                    dest.bits(),
                    source.floating(),
                    scale("+", from)
                ))
            }
            ("f32", "f64") if fixed.is_none() => Ok(format!(
                "ARC_S(c, {}) = (float)({});",
                dest.0,
                source.floating()
            )),
            ("f64", "f32") if fixed.is_none() => Ok(format!(
                "ARC_D(c, {}) = (double)({});",
                dest.0,
                source.floating()
            )),
            _ => Err(ins.unsupported()),
        }
    }

    pub(crate) fn vmrs(&self, ins: &Instruction, op: &str) -> Emission {
        if ins.operands_text.contains("fpscr") {
            if ins.operands_text.contains("apsr_nzcv") {
                return Ok("arc_vmrs_nzcv(c);".into());
            }
            if let [a, b] = ins.operands.as_slice() {
                if op == "vmsr" {
                    if let Some(reg) = register(b) {
                        let rn = self.reg(ins, reg)?;
                        if rn != 15 {
                            return Ok(format!("c->fpscr = ARC_R(c, {rn});"));
                        }
                    }
                } else if let Some(reg) = register(a) {
                    let rd = self.reg(ins, reg)?;
                    if rd != 15 {
                        return Ok(format!("ARC_W(c, {rd}, c->fpscr);"));
                    }
                }
            }
        }
        Err(ins.unsupported())
    }
}

fn fp_type(ins: &Instruction) -> Emission<&str> {
    match ins.mnemonic.split('.').nth(1) {
        Some(ty @ ("f32" | "f64")) => Ok(ty),
        _ => Err(ins.unsupported()),
    }
}
