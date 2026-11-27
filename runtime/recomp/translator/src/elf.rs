use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};

use goblin::elf::{
    Elf, SectionHeader, header, program_header::PT_LOAD, section_header::SHF_EXECINSTR,
    sym::STT_FUNC,
};

use crate::BuildResult;

pub(crate) const IMAGE_BASE: u32 = 0x1000_0000;
pub(crate) const HELPER_BASE: u32 = 0x0110_0000;

#[derive(Clone, Copy)]
pub(crate) struct Function {
    pub(crate) address: u32,
    pub(crate) size: u32,
    pub(crate) thumb: bool,
}

pub(crate) struct Image<'a> {
    sections: Vec<(u32, u32)>,
    segments: Vec<(u64, u64, &'a [u8])>,
    pub(crate) entries: BTreeMap<u32, bool>,
    pub(crate) functions: BTreeMap<u32, Function>,
    pub(crate) noreturn: BTreeSet<u32>,
}

fn section<'e>(elf: &'e Elf<'_>, name: &str) -> Option<&'e SectionHeader> {
    elf.section_headers
        .iter()
        .find(|header| elf.shdr_strtab.get_at(header.sh_name) == Some(name))
}

fn section_data<'a>(bytes: &'a [u8], header: &SectionHeader) -> BuildResult<&'a [u8]> {
    let start = usize::try_from(header.sh_offset)?;
    let size = usize::try_from(header.sh_size)?;
    Ok(bytes
        .get(start..start.checked_add(size).ok_or("ELF section size overflow")?)
        .ok_or("ELF section exceeds input bytes")?)
}

fn little_word(bytes: &[u8]) -> BuildResult<u32> {
    Ok(u32::from_le_bytes(bytes.try_into()?))
}

impl<'a> Image<'a> {
    pub(crate) fn parse(bytes: &'a [u8]) -> BuildResult<Self> {
        let elf = Elf::parse(bytes)?;
        if elf.is_64
            || !elf.little_endian
            || elf.header.e_machine != header::EM_ARM
            || elf.header.e_type != header::ET_DYN
        {
            return Err("Expected a little-endian ARM32 shared library".into());
        }
        if section(&elf, ".dynsym").is_none() {
            return Err("ELF dynamic symbols are missing".into());
        }
        let mut image = Self {
            sections: Vec::new(),
            segments: Vec::new(),
            entries: BTreeMap::new(),
            functions: BTreeMap::new(),
            noreturn: BTreeSet::new(),
        };
        for ph in elf.program_headers.iter().filter(|ph| ph.p_type == PT_LOAD) {
            let low = u64::from(IMAGE_BASE) + ph.p_vaddr;
            let high = low + ph.p_memsz;
            if high > (1 << 32) || ph.p_filesz > ph.p_memsz {
                return Err("ELF segment exceeds guest address space".into());
            }
            let start = usize::try_from(ph.p_offset)?;
            let size = usize::try_from(ph.p_filesz)?;
            let data = bytes
                .get(start..start.checked_add(size).ok_or("ELF segment size overflow")?)
                .ok_or("ELF segment exceeds input bytes")?;
            image.segments.push((low, high, data));
        }
        for header in &elf.section_headers {
            if header.sh_flags & u64::from(SHF_EXECINSTR) != 0 {
                let start = guest_address(header.sh_addr)?;
                let end = start
                    .checked_add(u32::try_from(header.sh_size)?)
                    .ok_or("Executable section exceeds guest address space")?;
                image.sections.push((start, end));
            }
        }
        if let Some(plt) = section(&elf, ".plt") {
            let start = guest_address(plt.sh_addr)?;
            let fatal = [
                "abort",
                "exit",
                "_exit",
                "__assert2",
                "__stack_chk_fail",
                "__cxa_throw",
            ];
            if let Some((_, relocations)) = elf.shdr_relocs.iter().find(|(index, _)| {
                elf.shdr_strtab.get_at(elf.section_headers[*index].sh_name) == Some(".rel.plt")
            }) {
                for (index, relocation) in relocations.iter().enumerate() {
                    if let Some(symbol) = elf.dynsyms.get(relocation.r_sym)
                        && elf
                            .dynstrtab
                            .get_at(symbol.st_name)
                            .is_some_and(|name| fatal.contains(&name))
                    {
                        let offset = u32::try_from(20 + 12 * index)?;
                        image
                            .noreturn
                            .insert(start.checked_add(offset).ok_or("Invalid PLT entry")?);
                    }
                }
            }
        }
        let mut starts = BTreeMap::new();
        for symbol in elf.dynsyms.iter() {
            if symbol.st_shndx != 0
                && symbol.st_type() == STT_FUNC
                && elf
                    .dynstrtab
                    .get_at(symbol.st_name)
                    .is_some_and(|name| !name.is_empty())
            {
                let entry = guest_address(symbol.st_value)?;
                starts.insert(entry & !1, entry & 1 != 0);
            }
        }
        for (_, relocations) in &elf.shdr_relocs {
            for relocation in relocations.iter().filter(|r| r.r_type == 23) {
                let value = little_word(&image.read(guest_address(relocation.r_offset)?, 4)?)?;
                if value & 1 != 0
                    && let Ok(target) = guest_address(u64::from(value & !1))
                    && image.executable(target)
                {
                    starts.insert(target, true);
                }
            }
        }
        if let Some(header) = section(&elf, ".init_array") {
            let data = section_data(bytes, header)?;
            for word in data.chunks(4) {
                let value = little_word(word)?;
                if value != 0 && value != u32::MAX {
                    starts.insert(guest_address(u64::from(value & !1))?, value & 1 != 0);
                }
            }
        }
        image.entries.clone_from(&starts);
        if let Some(header) = section(&elf, ".ARM.exidx") {
            let data = section_data(bytes, header)?;
            let (records, remainder) = data.as_chunks::<8>();
            if !remainder.is_empty() {
                return Err("Truncated ARM unwind index".into());
            }
            let address = i64::from(guest_address(header.sh_addr)?);
            for (index, record) in records.iter().enumerate() {
                let prel31 = ((little_word(&record[..4])? << 1) as i32) >> 1;
                let entry = address + i64::try_from(index * 8)? + i64::from(prel31);
                if let Ok(entry) = u32::try_from(entry) {
                    // EHABI omits the instruction mode. Explicit entries refine Thumb.
                    starts.entry(entry).or_insert(true);
                }
            }
        }
        for (start, _) in &image.sections {
            starts.entry(*start).or_insert(true);
        }
        if let Some(plt) = section(&elf, ".plt") {
            let start = guest_address(plt.sh_addr)?;
            let size = u32::try_from(plt.sh_size)?;
            for offset in std::iter::once(0).chain((20..size.saturating_sub(11)).step_by(12)) {
                let entry = start.checked_add(offset).ok_or("Invalid PLT entry")?;
                starts.insert(entry, false);
                image.entries.insert(entry, false);
            }
        }
        image.set_ranges(&starts);
        Ok(image)
    }

    pub(crate) fn set_ranges(&mut self, starts: &BTreeMap<u32, bool>) {
        self.functions.clear();
        for &(low, high) in &self.sections {
            let mut boundaries = starts.range(low..high).peekable();
            while let Some((&address, &thumb)) = boundaries.next() {
                let end = boundaries.peek().map_or(high, |&(&next, _)| next);
                self.functions.insert(
                    address,
                    Function {
                        address,
                        size: end - address,
                        thumb,
                    },
                );
            }
        }
    }

    pub(crate) fn executable(&self, address: u32) -> bool {
        self.sections
            .iter()
            .any(|&(low, high)| (low..high).contains(&address))
    }

    pub(crate) fn read(&self, address: u32, size: u32) -> BuildResult<Cow<'a, [u8]>> {
        let start = u64::from(address);
        let end = start + u64::from(size);
        for &(low, high, data) in &self.segments {
            if start >= low && end <= high {
                let offset = usize::try_from(start - low)?;
                let size = usize::try_from(size)?;
                if let Some(bytes) = data.get(offset..offset + size) {
                    return Ok(Cow::Borrowed(bytes));
                }
                // PT_LOAD memory past p_filesz is initialized to zero.
                let mut bytes = vec![0; size];
                if let Some(available) = data.get(offset..) {
                    bytes[..available.len()].copy_from_slice(available);
                }
                return Ok(Cow::Owned(bytes));
            }
        }
        Err(format!("ELF read outside mapped segments: {address:#x}+{size}").into())
    }
}

fn guest_address(offset: u64) -> BuildResult<u32> {
    IMAGE_BASE
        .checked_add(u32::try_from(offset)?)
        .ok_or_else(|| "ELF address exceeds guest address space".into())
}

pub(crate) fn helpers(bytes: &[u8]) -> BuildResult<(&[u8], Vec<Function>)> {
    let elf = Elf::parse(bytes)?;
    if !elf.is_object_file()
        || elf.is_64
        || !elf.little_endian
        || elf.header.e_machine != header::EM_ARM
    {
        return Err("Expected a little-endian ARM32 helpers object".into());
    }
    let code = section_data(bytes, section(&elf, ".text").ok_or("Missing helper .text")?)?;
    let size = u32::try_from(code.len())?;
    let starts = elf
        .syms
        .iter()
        .filter(|symbol| {
            elf.strtab
                .get_at(symbol.st_name)
                .is_some_and(|name| name.ends_with("_native"))
        })
        .map(|symbol| u32::try_from(symbol.st_value))
        .collect::<Result<BTreeSet<_>, _>>()?;
    let mut starts = starts.into_iter().peekable();
    let mut functions = Vec::new();
    while let Some(start) = starts.next() {
        if start >= size || HELPER_BASE.checked_add(size).is_none() {
            return Err("Helper symbol exceeds .text".into());
        }
        functions.push(Function {
            address: HELPER_BASE + start,
            size: starts.peek().copied().unwrap_or(size) - start,
            thumb: false,
        });
    }
    Ok((code, functions))
}
