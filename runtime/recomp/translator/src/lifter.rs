// Adapted from iparecomp. See ../../LICENSE and ../../upstream.json.
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write;

use capstone::RegId;
use capstone::arch::arm::{ArmOperand, ArmOperandType, ArmShift};

use crate::{
    BuildResult,
    decode::{
        Decoder, Instruction, core_register, immediate, register, shift_immediate, shift_name,
        shift_register,
    },
    elf::{Function, IMAGE_BASE},
    flow::{Flow, walk},
};

pub(crate) type Emission<T = String> = Result<T, String>;

pub(crate) struct Lifter<'a> {
    decoder: &'a Decoder,
    noreturn: &'a BTreeSet<u32>,
    pub(crate) thumb: bool,
    pub(crate) referenced: BTreeSet<u32>,
    pub(crate) continuations: BTreeMap<u32, (u32, bool)>,
    pub(crate) instructions: usize,
    pub(crate) supported: usize,
    pub(crate) unsupported: Vec<(String, usize)>,
}

impl<'a> Lifter<'a> {
    pub(crate) fn new(decoder: &'a Decoder, noreturn: &'a BTreeSet<u32>) -> Self {
        Self {
            decoder,
            noreturn,
            thumb: false,
            referenced: BTreeSet::new(),
            continuations: BTreeMap::new(),
            instructions: 0,
            supported: 0,
            unsupported: Vec::new(),
        }
    }

    pub(crate) fn lift(&mut self, function: Function, body: &[u8]) -> BuildResult<(String, bool)> {
        let Function {
            address,
            size,
            thumb,
        } = function;
        self.thumb = thumb;
        let mut insns = self.decoder.decode(body, address, thumb)?;
        let flow = walk(self.decoder, self.noreturn, &mut insns, function, body)?;
        let mut targets = BTreeSet::new();
        let mut returns = BTreeSet::new();
        for a in &flow.live {
            let ins = &insns[a];
            if matches!(ins.op(), "bl" | "blx") && flow.live.contains(&ins.next()) {
                returns.insert(ins.next());
                self.continuations.insert(ins.next(), (address, thumb));
            }
            if let Some(target) = branch_target(ins).filter(|t| flow.live.contains(t)) {
                targets.insert(target);
            }
            if let Some(table) = flow.tables.get(a) {
                targets.extend(table.iter().filter(|t| flow.live.contains(t)).copied());
            }
        }
        targets.extend(&returns);
        let mode = if thumb { "thumb" } else { "arm" };
        let mode_bit = if thumb { 32 } else { 0 };
        let name = name(address);
        let mut lines = vec![format!(
            "/* {name}: {} instructions, {mode} */",
            flow.live.len()
        )];
        if returns.is_empty() {
            lines.push(format!("void {name}(Arm32Ctx* c) {{"));
        } else {
            // A restored guest return can outlive its original native stack.
            lines.extend([
                format!("void {name}_resume(Arm32Ctx* c, uint32_t pc);"),
                format!("void {name}(Arm32Ctx* c) {{ {name}_resume(c, {address:#010x}u); }}"),
                format!("void {name}_resume(Arm32Ctx* c, uint32_t pc) {{"),
                format!("  if (pc != {address:#010x}u) {{"),
                format!("    if ((c->cpsr & 32u) != {mode_bit}u) {{ arc_trap(c, \"invalid continuation mode\"); return; }}"),
                "    switch (pc) {".into(),
            ]);
            lines.extend(
                returns
                    .iter()
                    .map(|pc| format!("    case {pc:#010x}u: goto L_{pc:08x};")),
            );
            lines.extend([
                "    default: arc_trap(c, \"invalid continuation address\"); return;".into(),
                "    }".into(),
                "  }".into(),
            ]);
        }
        let entry = address | u32::from(thumb);
        lines.push(format!(
            "  if (arc_enter(c, {entry:#010x}u, {size}u)) return;"
        ));
        let mut complete = flow.missing.is_empty();
        if !complete {
            self.count_unsupported(
                "undecodable reachable instruction".into(),
                flow.missing.len(),
            );
        }
        let mut order = flow.live.iter().copied().peekable();
        while let Some(a) = order.next() {
            let ins = &insns[&a];
            self.instructions += 1;
            if targets.contains(&a) {
                let pc = a | u32::from(thumb);
                lines.push(format!("L_{a:08x}:;"));
                lines.push(format!("  arc_block(c, {pc:#010x}u, {}u);", ins.size));
            }
            let text = format!("{} {}", ins.mnemonic, ins.operands_text);
            let text = text.trim_end();
            let code = match self.instruction(ins, &flow) {
                Ok(code) => {
                    self.supported += 1;
                    code
                }
                Err(why) => {
                    self.count_unsupported(why, 1);
                    complete = false;
                    format!("arc_trap(c, \"{name}: {text}\");")
                }
            };
            lines.push(format!("  /* {a:08x}  {text} */"));
            lines.push(format!("  {code}"));
            // An early literal pool can separate two reachable ranges.
            if let Some(&next) = order.peek().filter(|&&next| next != ins.next()) {
                if flow.missing.contains(&ins.next()) {
                    lines.push("  arc_trap(c, \"undecodable reachable instruction\");".into());
                }
                lines.push(format!("  goto L_{next:08x};"));
                targets.insert(next);
            }
        }
        if flow.fallthrough {
            let target = address + size;
            self.referenced.insert(target);
            lines.push(format!("  {}(c); return;", self::name(target)));
        } else {
            lines.push(format!(
                "  arc_trap(c, \"{name}: uncovered function fallthrough\");"
            ));
        }
        lines.push("}".into());
        Ok((lines.join("\n"), complete))
    }

    fn count_unsupported(&mut self, why: String, count: usize) {
        if let Some((_, sum)) = self.unsupported.iter_mut().find(|(key, _)| key == &why) {
            *sum += count;
        } else {
            self.unsupported.push((why, count));
        }
    }

    fn instruction(&mut self, ins: &Instruction, flow: &Flow) -> Emission {
        let op = ins.op();
        let body = if let Some(table) = flow.tables.get(&ins.address) {
            let operands = &ins.operands;
            let idx = match operands.as_slice() {
                [
                    ArmOperand {
                        op_type: ArmOperandType::Mem(mem),
                        ..
                    },
                ] => self.reg(ins, mem.index())?,
                [
                    _,
                    ArmOperand {
                        op_type: ArmOperandType::Mem(mem),
                        ..
                    },
                ] => self.reg(ins, mem.index())?,
                [_, _, operand] => self.dest(ins, operand)?,
                _ => return Err(ins.unsupported()),
            };
            let mut arms = String::new();
            for (k, target) in table.iter().enumerate() {
                let _ = write!(arms, "case {k}: goto L_{target:08x}; ");
            }
            format!(
                "switch (ARC_R(c, {idx})) {{ {arms}default: arc_trap(c, \"invalid switch index\"); }}"
            )
        } else {
            match op {
                "add" | "adc" | "sub" | "sbc" | "rsb" | "rsc" | "and" | "orr" | "orn" | "eor"
                | "bic" => self.alu3(ins, op),
                "mov" | "mvn" | "movw" | "movt" => self.alu2(ins, op),
                "cmp" | "cmn" | "tst" | "teq" => self.test(ins, op),
                "lsl" | "lsr" | "asr" | "ror" | "rrx" => self.shift(ins, op),
                "mul" | "mla" | "mls" | "umull" | "smull" | "umlal" | "smlal" => self.mul(ins, op),
                "uxtb" | "uxth" | "sxtb" | "sxth" | "uxtab" | "uxtah" | "sxtab" | "sxtah" => {
                    self.extend(ins, op)
                }
                "b" | "bl" | "bx" | "blx" | "bxj" | "cbz" | "cbnz" => {
                    self.branch(ins, op, &flow.live)
                }
                op if op.starts_with("ldr") && !op.starts_with("ldrex") => self.load(ins, op),
                op if op.starts_with("str") && !op.starts_with("strex") => self.store(ins, op),
                "push" | "pop" => self.block(ins, op),
                op if op.starts_with("ldm") || op.starts_with("stm") => self.block(ins, op),
                "smulbb" | "smulbt" | "smultb" | "smultt" => self.smulxy(ins, op),
                "vmov" => self.vmov(ins),
                "vldr" | "vstr" => self.vldst(ins, op),
                "vpush" | "vpop" => self.vblock(ins, op),
                op if op.starts_with("vldm") || op.starts_with("vstm") => self.vblock(ins, op),
                "vadd" | "vsub" | "vmul" | "vdiv" | "vnmul" | "vmla" | "vmls" | "vnmla"
                | "vnmls" | "vneg" | "vabs" | "vsqrt" => self.vfp_arithmetic(ins, op),
                "vcmp" | "vcmpe" => self.vcmp(ins),
                "vcvt" => self.vcvt(ins),
                "vmrs" | "vmsr" => self.vmrs(ins, op),
                _ => self.misc(ins, op),
            }?
        };
        if ins.conditional() {
            Ok(format!(
                "if (ARC_COND_{}(c)) {{ {body} }}",
                ins.condition_macro()
            ))
        } else {
            Ok(body)
        }
    }

    pub(crate) fn reg(&self, ins: &Instruction, reg: RegId) -> Emission<u16> {
        core_register(reg).ok_or_else(|| {
            format!(
                "{}: register {}",
                ins.unsupported(),
                self.decoder.reg_name(reg)
            )
        })
    }

    pub(crate) fn dest(&self, ins: &Instruction, operand: &ArmOperand) -> Emission<u16> {
        self.reg(ins, register(operand).ok_or_else(|| ins.unsupported())?)
    }

    pub(crate) fn pc_value(&self, ins: &Instruction) -> u32 {
        ins.address + if self.thumb { 4 } else { 8 }
    }

    pub(crate) fn read(&self, ins: &Instruction, reg: u16) -> String {
        if reg == 15 {
            image_address(self.pc_value(ins))
        } else {
            format!("ARC_R(c, {reg})")
        }
    }

    pub(crate) fn shifted(
        &self,
        ins: &Instruction,
        operand: &ArmOperand,
    ) -> Emission<(String, Option<String>)> {
        let base = self.read(ins, self.dest(ins, operand)?);
        let shift = operand.shift;
        if shift == ArmShift::Invalid {
            return Ok((base, None));
        }
        let kind = shift_name(shift);
        if kind == "rrx" {
            let call = format!("arc_rrx({base}, c->cf)");
            return Ok((format!("{call}.value"), Some(format!("{call}.carry"))));
        }
        let value = shift_immediate(shift);
        let amount = if let Some(reg) = shift_register(shift) {
            self.read(ins, self.reg(ins, reg)?)
        } else {
            format!("{}u", value.ok_or_else(|| ins.unsupported())?)
        };
        let call = format!("arc_{kind}({base}, {amount}, c->cf)");
        // C shifts by the full width are undefined. Inline only known safe counts.
        let result = match value.filter(|v| (1..=31).contains(v)) {
            Some(n) if kind == "asr" => format!("(uint32_t)((int32_t){base} >> {n})"),
            Some(n) if kind == "lsl" => format!("({base} << {n})"),
            Some(n) if kind == "lsr" => format!("({base} >> {n})"),
            _ => format!("{call}.value"),
        };
        Ok((result, Some(format!("{call}.carry"))))
    }

    pub(crate) fn operand(
        &self,
        ins: &Instruction,
        operand: &ArmOperand,
    ) -> Emission<(String, Option<String>)> {
        match operand.op_type {
            ArmOperandType::Reg(_) => self.shifted(ins, operand),
            ArmOperandType::Imm(value) => {
                let value = value as u32;
                let replaces_carry = if ins.size != 4 {
                    false
                } else if self.thumb {
                    // ThumbExpandImm replication forms keep carry. Rotated forms replace it.
                    let first = u16::from_le_bytes([ins.bytes[0], ins.bytes[1]]);
                    let second = u16::from_le_bytes([ins.bytes[2], ins.bytes[3]]);
                    first & 0xfa00 == 0xf000 && ((first >> 10) & 1 != 0 || (second >> 14) & 1 != 0)
                } else {
                    (u32::from_le_bytes(ins.bytes) >> 8) & 15 != 0
                };
                Ok((
                    format!("{value:#x}u"),
                    replaces_carry.then(|| format!("{}u", value >> 31)),
                ))
            }
            _ => Err(format!("operand type {}", operand_kind(operand))),
        }
    }

    fn branch(&mut self, ins: &Instruction, op: &str, local: &BTreeSet<u32>) -> Emission {
        let operands = &ins.operands;
        let target = operands.last().ok_or_else(|| ins.unsupported())?;
        if matches!(op, "cbz" | "cbnz")
            && operands.len() == 2
            && let Some(address) = immediate(target)
        {
            let value = self.read(ins, self.dest(ins, &operands[0])?);
            let branch = self.static_branch(address as u32 & !1, local);
            let comparison = if op == "cbz" { "==" } else { "!=" };
            return Ok(format!("if ({value} {comparison} 0) {{ {branch} }}"));
        }
        let ret = ins.next() | u32::from(self.thumb);
        let mode = if self.thumb { 32 } else { 0 };
        let verify_return = || {
            format!(
                "if (ARC_R(c, 15) != {} || (c->cpsr & 32u) != {mode}u) return;",
                image_address(ret & !1),
            )
        };
        if matches!(op, "b" | "bl" | "blx")
            && let Some(address) = immediate(target)
        {
            let address = address as u32 & !1;
            if op == "b" {
                return Ok(self.static_branch(address, local));
            }
            self.referenced.insert(address);
            return Ok(format!(
                "ARC_W(c, 14, {}); {}(c); {}",
                image_address(ret),
                name(address),
                verify_return()
            ));
        }
        if matches!(op, "bx" | "blx" | "bxj")
            && let Some(reg) = register(target)
        {
            let reg = self.reg(ins, reg)?;
            if op == "bx" && reg == 14 {
                return Ok("arc_return(c, ARC_LR(c)); return;".into());
            }
            let value = self.read(ins, reg);
            if op == "blx" {
                // Preserve the target before overwriting LR, which might hold it.
                return Ok(format!(
                    "{{ uint32_t target = {value}; ARC_W(c, 14, {}); arc_dispatch(c, target); {} }}",
                    image_address(ret),
                    verify_return()
                ));
            }
            return Ok(format!("arc_dispatch(c, {value}); return;"));
        }
        Err(ins.unsupported())
    }

    fn static_branch(&mut self, target: u32, local: &BTreeSet<u32>) -> String {
        if local.contains(&target) {
            format!("goto L_{target:08x};")
        } else {
            self.referenced.insert(target);
            format!("{}(c); return;", name(target))
        }
    }
}

fn operand_kind(operand: &ArmOperand) -> u8 {
    use ArmOperandType::*;
    match operand.op_type {
        Invalid => 0,
        Reg(_) => 1,
        Imm(_) => 2,
        Mem(_) => 3,
        Fp(_) => 4,
        Cimm(_) => 64,
        Pimm(_) => 65,
        Setend(_) => 66,
        SysReg(_) => 67,
    }
}

pub(crate) fn branch_target(ins: &Instruction) -> Option<u32> {
    if matches!(ins.op(), "b" | "cbz" | "cbnz") {
        ins.operands
            .last()
            .and_then(immediate)
            .map(|address| address as u32 & !1)
    } else {
        None
    }
}

pub(crate) fn name(address: u32) -> String {
    format!("fn_{address:08x}")
}

pub(crate) fn image_address(address: u32) -> String {
    let offset = i64::from(address) - i64::from(IMAGE_BASE);
    let sign = if offset < 0 { "-" } else { "" };
    format!("(c->image_base + {sign}{:#x}u)", offset.unsigned_abs())
}

pub(crate) fn write_register(reg: u16, value: &str) -> String {
    if reg == 15 {
        format!("arc_dispatch(c, {value}); return;")
    } else {
        format!("ARC_W(c, {reg}, {value});")
    }
}
