// Adapted from iparecomp. See ../../LICENSE and ../../upstream.json.
use std::collections::{BTreeMap, BTreeSet};

use capstone::arch::arm::{ArmCC, ArmOperandType};

use crate::{
    BuildResult,
    decode::{
        Decoder, Instruction, Instructions, core_register, immediate, register, shift_immediate,
    },
    elf::{Function, Image},
    lifter::branch_target,
};

#[derive(Default)]
pub(crate) struct Flow {
    pub(crate) live: BTreeSet<u32>,
    pub(crate) tables: BTreeMap<u32, Vec<u32>>,
    pub(crate) missing: BTreeSet<u32>,
    pub(crate) fallthrough: bool,
}

pub(crate) fn discover(image: &mut Image<'_>, decoder: &Decoder) -> BuildResult<bool> {
    let mut starts: BTreeMap<_, _> = image
        .functions
        .values()
        .map(|f| (f.address, f.thumb))
        .collect();
    let mut known = image.entries.clone();
    for _ in 0..16 {
        let mut found = BTreeMap::new();
        let mut interiors = BTreeSet::new();
        for address in known.keys() {
            let function = *image
                .functions
                .get(address)
                .ok_or("Entry is outside executable ranges")?;
            let body = image.read(function.address, function.size)?;
            let mut insns = decoder.decode(&body, function.address, function.thumb)?;
            let flow = walk(decoder, &image.noreturn, &mut insns, function, &body)?;
            let end = function.address + function.size;
            for address in flow.live {
                let ins = &insns[&address];
                interiors.extend(address + 1..ins.next());
                let op = ins.op();
                if op == "bx"
                    && ins
                        .operands
                        .first()
                        .and_then(register)
                        .and_then(core_register)
                        == Some(15)
                {
                    let pc = address + if function.thumb { 4 } else { 8 };
                    found.insert(pc & !3, false);
                    continue;
                }
                if let Some(target) = call_target(ins)
                    .or_else(|| branch_target(ins).filter(|t| !(function.address..end).contains(t)))
                    && image.executable(target)
                {
                    found.insert(target, function.thumb != (op == "blx"));
                }
            }
            for target in flow.tables.values().flatten() {
                if !(function.address..end).contains(target) && image.executable(*target) {
                    found.insert(*target, function.thumb);
                }
            }
            if flow.fallthrough && image.executable(end) {
                found.insert(end, function.thumb);
            }
        }
        let old_count = known.len();
        // A false branch decoded from data cannot split a proven instruction.
        for (address, thumb) in found {
            if !interiors.contains(&address) {
                known.entry(address).or_insert(thumb);
            }
        }
        starts.extend(known.iter().map(|(&address, &thumb)| (address, thumb)));
        image.set_ranges(&starts);
        if known.len() == old_count {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(crate) fn walk(
    decoder: &Decoder,
    noreturn: &BTreeSet<u32>,
    insns: &mut Instructions,
    function: Function,
    body: &[u8],
) -> BuildResult<Flow> {
    let mut flow = Flow::default();
    let mut work = vec![function.address];
    let end = function.address + function.size;
    while let Some(address) = work.pop() {
        let Some(ins) = insns.get(&address) else {
            if (function.address..end).contains(&address) {
                flow.missing.insert(address);
            } else if address == end {
                flow.fallthrough = true;
            }
            continue;
        };
        if !flow.live.insert(address) {
            continue;
        }
        let op = ins.op();
        let ends =
            writes_pc(ins) || call_target(ins).is_some_and(|target| noreturn.contains(&target));
        if let Some(target) = branch_target(ins).filter(|target| insns.contains_key(target)) {
            work.push(target);
        }
        // Predicated terminators may still fall through.
        let next =
            (matches!(op, "cbz" | "cbnz") || !(ends && !ins.conditional())).then(|| ins.next());
        let table = if ends && function.thumb && matches!(op, "tbb" | "tbh") {
            thumb_table(decoder, insns, function, body, address)?
        } else if ends && op == "add" {
            arm_table(insns, ins)
        } else {
            None
        };
        if let Some(table) = table {
            work.extend(&table);
            flow.tables.insert(address, table);
        }
        work.extend(next);
    }
    Ok(flow)
}

fn call_target(ins: &Instruction) -> Option<u32> {
    if matches!(ins.op(), "bl" | "blx") {
        ins.operands
            .last()
            .and_then(immediate)
            .map(|target| target as u32 & !1)
    } else {
        None
    }
}

fn writes_pc(ins: &Instruction) -> bool {
    let is_pc = |operand| register(operand).and_then(core_register) == Some(15);
    match ins.op() {
        "b" | "bx" | "bxj" | "tbb" | "tbh" | "cbz" | "cbnz" => true,
        "pop" | "ldm" | "ldmia" | "ldmib" | "ldmda" | "ldmdb" => ins.operands.iter().any(is_pc),
        "mov" | "mvn" | "movw" | "movt" | "add" | "adc" | "sub" | "sbc" | "rsb" | "rsc" | "and"
        | "orr" | "orn" | "eor" | "bic" => ins.operands.first().is_some_and(is_pc),
        op if op.starts_with("ldr") => ins.operands.first().is_some_and(is_pc),
        _ => false,
    }
}

fn arm_table(insns: &Instructions, ins: &Instruction) -> Option<Vec<u32>> {
    // ADD PC, PC, rN, LSL #k has executable slots. LDR tables contain
    // relocatable addresses and are handled at run time.
    let [dest, pc, index] = ins.operands.as_slice() else {
        return None;
    };
    if !ins.conditional()
        || register(dest).and_then(core_register) != Some(15)
        || register(pc).and_then(core_register) != Some(15)
    {
        return None;
    }
    let index_reg = register(index).and_then(core_register)?;
    let stride = 1 << shift_immediate(index.shift).filter(|n| matches!(n, 2 | 3))?;
    let mut previous = ins.address.checked_sub(ins.size)?;
    let mut count = None;
    for _ in 0..4 {
        let Some(candidate) = insns.get(&previous) else {
            break;
        };
        if matches!(candidate.mnemonic_root(), "cmp" | "rsbs")
            && candidate.operands.len() >= 2
            && candidate
                .operands
                .first()
                .and_then(register)
                .and_then(core_register)
                == Some(index_reg)
            && let Some(limit) = candidate.operands.last().and_then(immediate)
        {
            count = limit.checked_add(1);
            break;
        }
        previous = previous.checked_sub(candidate.size)?;
    }
    let count = count.filter(|n| (1..=4096).contains(n))? as u32;
    (0..count)
        .map(|n| {
            let target = ins.address.checked_add(8 + n * stride)?;
            insns.contains_key(&target).then_some(target)
        })
        .collect()
}

fn thumb_targets(
    insns: &Instructions,
    function: Function,
    body: &[u8],
    ins: &Instruction,
) -> Option<Vec<u32>> {
    let [operand] = ins.operands.as_slice() else {
        return None;
    };
    let ArmOperandType::Mem(mem) = operand.op_type else {
        return None;
    };
    if core_register(mem.base()) != Some(15) {
        return None;
    }
    let index = core_register(mem.index())?;
    let mut previous = insns.range(..ins.address).rev();
    let (_, guard) = previous.next()?;
    let (_, compare) = previous.next()?;
    if guard.op() != "b" || compare.op() != "cmp" {
        return None;
    }
    let [reg, limit] = compare.operands.as_slice() else {
        return None;
    };
    if register(reg).and_then(core_register) != Some(index) {
        return None;
    }
    let limit = immediate(limit)?;
    let count = match guard.cc {
        ArmCC::ARM_CC_HI => limit.checked_add(1)?,
        ArmCC::ARM_CC_HS => limit,
        _ => return None,
    };
    if !(1..=4096).contains(&count) {
        return None;
    }
    let base = ins.address.checked_add(4)?;
    let offset = (base - function.address) as usize;
    let width = if ins.op() == "tbb" { 1 } else { 2 };
    let size = count as usize * width;
    body.get(offset..offset + size)?
        .chunks_exact(width)
        .map(|bytes| {
            let slot = if width == 1 {
                u32::from(bytes[0])
            } else {
                u32::from(u16::from_le_bytes([bytes[0], bytes[1]]))
            };
            let target = base.checked_add(2 * slot)?;
            (target >= base + size as u32 && target < function.address + function.size)
                .then_some(target)
        })
        .collect()
}

fn thumb_table(
    decoder: &Decoder,
    insns: &mut Instructions,
    function: Function,
    body: &[u8],
    address: u32,
) -> BuildResult<Option<Vec<u32>>> {
    let Some(targets) = thumb_targets(insns, function, body, &insns[&address]) else {
        return Ok(None);
    };
    // A linear decode may have consumed the first case as part of the table.
    for target in &targets {
        decoder.disassemble(
            &body[(target - function.address) as usize..],
            *target,
            true,
            |decoded| {
                let keep_going = !(writes_pc(&decoded) && !decoded.conditional());
                insns.insert(decoded.address, decoded);
                keep_going
            },
        )?;
        if !insns.contains_key(target) {
            return Ok(None);
        }
    }
    Ok(Some(targets))
}
