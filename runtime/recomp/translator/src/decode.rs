use std::collections::BTreeMap;

use capstone::RegId;
use capstone::arch::arm::{ArchMode, ArmCC, ArmOperand, ArmOperandType, ArmReg, ArmShift};
use capstone::prelude::*;

use crate::BuildResult;

pub(crate) type Instructions = BTreeMap<u32, Instruction>;

pub(crate) struct Decoder([Capstone; 2]);

impl Decoder {
    pub(crate) fn new() -> BuildResult<Self> {
        let build = |mode| Capstone::new().arm().mode(mode).detail(true).build();
        Ok(Self([build(ArchMode::Arm)?, build(ArchMode::Thumb)?]))
    }

    pub(crate) fn disassemble(
        &self,
        body: &[u8],
        address: u32,
        thumb: bool,
        mut visit: impl FnMut(Instruction) -> bool,
    ) -> BuildResult<u32> {
        let cs = &self.0[usize::from(thumb)];
        let mut end = address;
        let instructions = cs.disasm_all(body, u64::from(address))?;
        for raw in instructions.iter() {
            let detail = cs.insn_detail(raw)?;
            let arch = detail.arch_detail();
            let arm = arch.arm().ok_or("Missing ARM instruction detail")?;
            let address = u32::try_from(raw.address())?;
            let size = u32::try_from(raw.len())?;
            let mut bytes = [0; 4];
            bytes
                .get_mut(..raw.len())
                .ok_or("Invalid ARM instruction size")?
                .copy_from_slice(raw.bytes());
            let ins = Instruction {
                address,
                size,
                bytes,
                mnemonic: raw.mnemonic().ok_or("Missing ARM mnemonic")?.to_owned(),
                operands_text: raw.op_str().unwrap_or_default().to_owned(),
                operands: arm.operands().collect(),
                cc: arm.cc(),
                update_flags: arm.update_flags(),
                writeback: arm.writeback(),
            };
            end = ins.next();
            if !visit(ins) {
                break;
            }
        }
        Ok(end)
    }

    pub(crate) fn decode(
        &self,
        body: &[u8],
        address: u32,
        thumb: bool,
    ) -> BuildResult<Instructions> {
        let mut insns = Instructions::new();
        let mut pos = 0;
        while pos < body.len() {
            let end = self.disassemble(&body[pos..], address + pos as u32, thumb, |ins| {
                insns.insert(ins.address, ins);
                true
            })?;
            let after = (end - address) as usize;
            // An undecodable literal pool can precede another reachable block.
            pos = if after == pos {
                pos + if thumb { 2 } else { 4 }
            } else {
                after
            };
        }
        Ok(insns)
    }

    pub(crate) fn reg_name(&self, reg: RegId) -> String {
        self.0[0].reg_name(reg).unwrap_or_else(|| "?".into())
    }
}

pub(crate) struct Instruction {
    pub(crate) address: u32,
    pub(crate) size: u32,
    pub(crate) bytes: [u8; 4],
    pub(crate) mnemonic: String,
    pub(crate) operands_text: String,
    pub(crate) operands: Vec<ArmOperand>,
    pub(crate) cc: ArmCC,
    update_flags: bool,
    pub(crate) writeback: bool,
}

impl Instruction {
    pub(crate) fn next(&self) -> u32 {
        self.address + self.size
    }

    pub(crate) fn conditional(&self) -> bool {
        !matches!(self.cc, ArmCC::ARM_CC_INVALID | ArmCC::ARM_CC_AL)
    }

    pub(crate) fn condition_macro(&self) -> &'static str {
        [
            "", "EQ", "NE", "CS", "CC", "MI", "PL", "VS", "VC", "HI", "LS", "GE", "LT", "GT", "LE",
            "AL",
        ][self.cc as usize]
    }

    pub(crate) fn mnemonic_root(&self) -> &str {
        self.mnemonic.split('.').next().unwrap_or_default()
    }

    fn without_condition(&self) -> &str {
        let mnemonic = self.mnemonic_root();
        if !self.conditional() {
            return mnemonic;
        }
        let suffixes: &[&str] = match self.cc {
            ArmCC::ARM_CC_HS => &["cs", "hs"],
            ArmCC::ARM_CC_LO => &["cc", "lo"],
            _ => &[
                "", "eq", "ne", "cs", "cc", "mi", "pl", "vs", "vc", "hi", "ls", "ge", "lt", "gt",
                "le", "al",
            ][self.cc as usize..self.cc as usize + 1],
        };
        suffixes
            .iter()
            .find_map(|suffix| {
                mnemonic
                    .strip_suffix(suffix)
                    .filter(|rest| !rest.is_empty())
            })
            .unwrap_or(mnemonic)
    }

    pub(crate) fn op(&self) -> &str {
        let mnemonic = self.without_condition();
        if matches!(mnemonic, "addw" | "subw")
            || (self.update_flags
                && mnemonic.ends_with('s')
                && mnemonic.len() > 1
                && !mnemonic.starts_with('v')
                && !matches!(mnemonic, "mrs" | "msr"))
        {
            &mnemonic[..mnemonic.len() - 1]
        } else {
            mnemonic
        }
    }

    pub(crate) fn sets_flags(&self) -> bool {
        // Capstone also marks instructions that only read the carry as updating flags.
        self.update_flags
            && (!matches!(self.op(), "adc" | "sbc" | "rsc")
                || self.without_condition().ends_with('s'))
    }

    pub(crate) fn unsupported(&self) -> String {
        use ArmOperandType::*;
        let kinds = self
            .operands
            .iter()
            .map(|operand| match operand.op_type {
                Reg(_) if operand.shift != ArmShift::Invalid => {
                    format!("r<{}>", shift_name(operand.shift))
                }
                Reg(_) => "r".into(),
                Imm(_) => "i".into(),
                Fp(_) => "f".into(),
                Mem(mem) if mem.index().0 != 0 && operand.shift != ArmShift::Invalid => {
                    format!("[m+r<{}>]", shift_name(operand.shift))
                }
                Mem(mem) if mem.index().0 != 0 => "[m+r]".into(),
                Mem(mem) if mem.disp() != 0 => "[m+d]".into(),
                Mem(_) => "[m]".into(),
                Invalid => "?0".into(),
                Cimm(_) => "?64".into(),
                Pimm(_) => "?65".into(),
                Setend(_) => "?66".into(),
                SysReg(_) => "?67".into(),
            })
            .collect::<Vec<_>>();
        format!("{} {}", self.mnemonic_root(), kinds.join(","))
    }
}

pub(crate) fn core_register(reg: RegId) -> Option<u16> {
    let reg = u32::from(reg.0);
    match reg {
        ArmReg::ARM_REG_R0..=ArmReg::ARM_REG_R12 => Some((reg - ArmReg::ARM_REG_R0) as u16),
        ArmReg::ARM_REG_SP => Some(13),
        ArmReg::ARM_REG_LR => Some(14),
        ArmReg::ARM_REG_PC => Some(15),
        _ => None,
    }
}

pub(crate) fn shift_name(shift: ArmShift) -> &'static str {
    use ArmShift::*;
    match shift {
        Asr(_) | AsrReg(_) => "asr",
        Lsl(_) | LslReg(_) => "lsl",
        Lsr(_) | LsrReg(_) => "lsr",
        Ror(_) | RorReg(_) => "ror",
        Rrx(_) | RrxReg(_) => "rrx",
        Invalid => "",
    }
}

pub(crate) fn shift_immediate(shift: ArmShift) -> Option<u32> {
    use ArmShift::*;
    match shift {
        Asr(value) | Lsl(value) | Lsr(value) | Ror(value) | Rrx(value) => Some(value),
        _ => None,
    }
}

pub(crate) fn shift_register(shift: ArmShift) -> Option<RegId> {
    use ArmShift::*;
    match shift {
        AsrReg(reg) | LslReg(reg) | LsrReg(reg) | RorReg(reg) | RrxReg(reg) => Some(reg),
        _ => None,
    }
}

pub(crate) fn immediate(operand: &ArmOperand) -> Option<i32> {
    match operand.op_type {
        ArmOperandType::Imm(imm) => Some(imm),
        _ => None,
    }
}

pub(crate) fn register(operand: &ArmOperand) -> Option<RegId> {
    match operand.op_type {
        ArmOperandType::Reg(reg) => Some(reg),
        _ => None,
    }
}
