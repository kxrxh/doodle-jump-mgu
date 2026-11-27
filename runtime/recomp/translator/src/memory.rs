// Adapted from iparecomp. See ../../LICENSE and ../../upstream.json.
use capstone::arch::arm::{ArmOperandType, ArmShift};

use crate::decode::{Instruction, immediate, shift_immediate, shift_name};
use crate::lifter::{Emission, Lifter, image_address};

impl Lifter<'_> {
    pub(crate) fn memory_address(
        &self,
        ins: &Instruction,
        index: usize,
    ) -> Emission<(String, String, u16)> {
        let ops = &ins.operands;
        let operand = ops.get(index).ok_or_else(|| ins.unsupported())?;
        let ArmOperandType::Mem(mem) = operand.op_type else {
            return Err(ins.unsupported());
        };
        let base = self.reg(ins, mem.base())?;
        let base_expr = if self.thumb && base == 15 {
            image_address(self.pc_value(ins) & !3)
        } else {
            self.read(ins, base)
        };
        // Post-indexed offsets have their own operand. Access uses the original
        // base, while writeback uses the offset.
        if let Some(post) = ops.get(index + 1) {
            let offset = if let Some(imm) = immediate(post) {
                format!("{:#x}u", imm as u32)
            } else {
                format!("({})", self.shifted(ins, post)?.0)
            };
            let sign = if post.subtracted { "-" } else { "+" };
            let writeback = format!("ARC_W(c, {base}, {base_expr} {sign} {offset});");
            return Ok((base_expr, writeback, base));
        }
        let (offset, sub) = if mem.index().0 != 0 {
            let reg = self.read(ins, self.reg(ins, mem.index())?);
            let shift = operand.shift;
            let offset = match shift {
                ArmShift::Invalid | ArmShift::Lsl(0) => reg,
                ArmShift::Lsl(n) => format!("({reg} << {n})"),
                ArmShift::Rrx(_) => format!("arc_rrx({reg}, c->cf).value"),
                _ => {
                    let name = shift_name(shift);
                    let value = shift_immediate(shift).ok_or_else(|| ins.unsupported())?;
                    format!("arc_{name}({reg}, {value}u, c->cf).value")
                }
            };
            (offset, operand.subtracted)
        } else {
            let disp = mem.disp();
            (format!("{:#x}u", disp.unsigned_abs()), disp < 0)
        };
        let sign = if sub { "-" } else { "+" };
        let writeback = if ins.writeback {
            format!("ARC_W(c, {base}, a);")
        } else {
            String::new()
        };
        Ok((format!("({base_expr} {sign} {offset})"), writeback, base))
    }

    pub(crate) fn load(&self, ins: &Instruction, op: &str) -> Emission {
        let macro_name = match &op[3..] {
            "" | "d" => "ARC_LD32",
            "b" => "ARC_LD8",
            "h" => "ARC_LD16",
            "sb" => "ARC_LD8S",
            "sh" => "ARC_LD16S",
            _ => return Err(ins.unsupported()),
        };
        let index = memory_index(ins)?;
        let registers = ins.operands[..index]
            .iter()
            .map(|operand| self.dest(ins, operand))
            .collect::<Emission<Vec<_>>>()?;
        let (address, wb, base) = self.memory_address(ins, index)?;
        let body = format!("{{ uint32_t a = {address}; ");
        // Do not fold PC-relative data. The loader applies ELF relocations.
        match registers.as_slice() {
            [15] if base == 13 && ins.writeback => Ok(format!(
                "{body} uint32_t target = {macro_name}(a); {wb} arc_return(c, target); return; }}"
            )),
            [15] => Ok(format!(
                "{body}{wb} arc_dispatch(c, {macro_name}(a)); return; }}"
            )),
            [reg] => Ok(format!("{body}{wb} ARC_W(c, {reg}, {macro_name}(a)); }}")),
            [first, second] if op == "ldrd" => Ok(format!(
                "{body}{wb} ARC_W(c, {first}, ARC_LD32(a)); ARC_W(c, {second}, ARC_LD32(a + 4)); }}"
            )),
            _ => Err(ins.unsupported()),
        }
    }

    pub(crate) fn store(&self, ins: &Instruction, op: &str) -> Emission {
        let macro_name = match &op[3..] {
            "" | "d" => "ARC_ST32",
            "b" => "ARC_ST8",
            "h" => "ARC_ST16",
            _ => return Err(ins.unsupported()),
        };
        let index = memory_index(ins)?;
        let registers = ins.operands[..index]
            .iter()
            .map(|operand| self.dest(ins, operand))
            .collect::<Emission<Vec<_>>>()?;
        let (address, wb, _) = self.memory_address(ins, index)?;
        let body = format!("{{ uint32_t a = {address}; ");
        match registers.as_slice() {
            [reg] => Ok(format!(
                "{body}{macro_name}(a, {}); {wb} }}",
                self.read(ins, *reg)
            )),
            [first, second] if op == "strd" => Ok(format!(
                "{body}ARC_ST32(a, {}); ARC_ST32(a + 4, {}); {wb} }}",
                self.read(ins, *first),
                self.read(ins, *second)
            )),
            _ => Err(ins.unsupported()),
        }
    }

    pub(crate) fn block(&self, ins: &Instruction, op: &str) -> Emission {
        let ops = ins.operands.as_slice();
        let (base, operands, mode, load, writeback) = match op {
            "push" => (13, ops, "db", false, true),
            "pop" => (13, ops, "ia", true, true),
            _ => {
                let [base, regs @ ..] = ops else {
                    return Err(ins.unsupported());
                };
                let mode = if op.len() == 3 { "ia" } else { &op[3..] };
                (
                    self.dest(ins, base)?,
                    regs,
                    mode,
                    op.starts_with("ldm"),
                    ins.writeback,
                )
            }
        };
        if operands.is_empty() {
            return Err(ins.unsupported());
        }
        let mut regs = operands
            .iter()
            .map(|operand| self.dest(ins, operand))
            .collect::<Emission<Vec<_>>>()?;
        regs.sort_unstable();
        let n = regs.len() * 4;
        let source = self.read(ins, base);
        let (start, end) = match mode {
            "ia" => (source.clone(), format!("{source} + {n:#x}u")),
            "ib" => (format!("{source} + 4u"), format!("{source} + {n:#x}u")),
            "da" => (
                format!("{source} - {:#x}u", n - 4),
                format!("{source} - {n:#x}u"),
            ),
            "db" => (format!("{source} - {n:#x}u"), format!("{source} - {n:#x}u")),
            _ => return Err(ins.unsupported()),
        };
        let mut parts = vec![format!("uint32_t a = {start};")];
        // Compute writeback from the old base even if the register list loads it.
        if writeback {
            parts.push(format!("ARC_W(c, {base}, {end});"));
        }
        for (index, reg) in regs.into_iter().enumerate() {
            let at = transfer_address(index * 4);
            if load && reg == 15 {
                if base == 13 {
                    parts.push(format!("arc_return(c, ARC_LD32({at}));"));
                    parts.push("return;".into());
                } else {
                    parts.push(format!("arc_dispatch(c, ARC_LD32({at})); return;"));
                }
            } else if load {
                parts.push(format!("ARC_W(c, {reg}, ARC_LD32({at}));"));
            } else {
                parts.push(format!("ARC_ST32({at}, {});", self.read(ins, reg)));
            }
        }
        Ok(format!("{{ {} }}", parts.join(" ")))
    }
}

fn memory_index(ins: &Instruction) -> Emission<usize> {
    ins.operands
        .iter()
        .position(|op| matches!(op.op_type, ArmOperandType::Mem(_)))
        .ok_or_else(|| ins.unsupported())
}

pub(crate) fn transfer_address(offset: usize) -> String {
    if offset == 0 {
        "a".into()
    } else {
        format!("a + {offset:#x}u")
    }
}
