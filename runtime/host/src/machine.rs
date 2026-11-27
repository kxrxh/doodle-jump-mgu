use crate::{Result, ffi};
use sha2::{Digest, Sha256};
use std::{
    cell::{Cell, RefCell},
    collections::BTreeMap,
    ffi::CString,
    path::Path,
};
pub const BASE: u32 = 0x10000000;
const STUB: u32 = 0x01000000;
const RETURN: u32 = STUB + 0xff000;
const STACK: u32 = 0x70000000;
const SMALL: u32 = 0x40000000;
const IMAGE: u32 = 0x50000000;
const ENGINE: &str = "4f92175d8ca5fcab546c0170803f80f7e7b37873b7c51241ef5a12c703fd8158";
#[derive(Clone, Copy)]
pub enum Return {
    Word(u32),
    Wide(u64),
    Void,
}
#[derive(Default)]
struct Heap {
    at: u32,
    alloc: BTreeMap<u32, usize>,
    free: BTreeMap<usize, Vec<u32>>,
    image: u32,
    images: BTreeMap<u32, usize>,
    image_free: BTreeMap<usize, Vec<u32>>,
}
pub struct Machine {
    pub guest: ffi::Ptr,
    pub dispatch: Cell<ffi::Ptr>,
    imports: RefCell<BTreeMap<String, u32>>,
    pub hooks: RefCell<BTreeMap<u32, String>>,
    helpers: RefCell<BTreeMap<String, u32>>,
    pub symbols: RefCell<BTreeMap<String, u32>>,
    constructors: RefCell<Vec<u32>>,
    heap: RefCell<Heap>,
    depth: Cell<u32>,
    pub errors: RefCell<Vec<Option<String>>>,
}
fn word(data: &[u8], offset: usize) -> Result<u32> {
    let bytes = data.get(offset..offset + 4).ok_or("Truncated ELF data")?;
    Ok(u32::from_le_bytes(
        bytes.try_into().map_err(|_| "Invalid ELF word")?,
    ))
}
fn half(data: &[u8], offset: usize) -> Result<u16> {
    let bytes = data.get(offset..offset + 2).ok_or("Truncated ELF data")?;
    Ok(u16::from_le_bytes(
        bytes.try_into().map_err(|_| "Invalid ELF halfword")?,
    ))
}
fn name(data: &[u8], offset: usize) -> Result<String> {
    let tail = data.get(offset..).ok_or("Invalid ELF string")?;
    let end = tail
        .iter()
        .position(|x| *x == 0)
        .ok_or("Unterminated ELF name")?;
    String::from_utf8(tail[..end].to_vec()).map_err(|e| e.to_string())
}
impl Machine {
    pub fn new(root: &Path) -> Result<Self> {
        if unsafe { ffi::text(ffi::doodle_engine_sha256()) } != ENGINE {
            return Err("Native module does not match the game".into());
        }
        let guest = unsafe { ffi::guest_create(ffi::doodle_resolve as *const () as usize) };
        if guest.is_null() {
            return Err("Cannot allocate guest context".into());
        }
        let m = Self {
            guest,
            dispatch: Cell::new(std::ptr::null_mut()),
            imports: RefCell::new(BTreeMap::new()),
            hooks: RefCell::new(BTreeMap::new()),
            helpers: RefCell::new(BTreeMap::new()),
            symbols: RefCell::new(BTreeMap::new()),
            constructors: RefCell::new(vec![]),
            heap: RefCell::new(Heap {
                at: 0x20000000,
                image: IMAGE,
                ..Heap::default()
            }),
            depth: Cell::new(0),
            errors: RefCell::new(vec![]),
        };
        if unsafe { ffi::guest_context_size() } != std::mem::size_of::<ffi::GuestState>() {
            return Err("Incompatible native guest ABI".into());
        }
        // The checked C layout shares only architectural state, never engine-owned pointers.
        unsafe { (*ffi::guest_context(guest).cast::<ffi::GuestState>()).image_base = BASE };
        for (at, size) in [
            (STUB, 0x100000),
            (STUB + 0x100000, 8192),
            (SMALL, 0x1000000),
            (STACK, 0x800000),
            (0x20000000, 0x10000000),
        ] {
            m.map(at, size)?;
        }
        for (reg, value) in [(12, STACK + 0x7ff000), (111, 0xf00000), (4, 0x40000000)] {
            m.set_reg(reg, value)?;
        }
        m.put32(STUB + 0x101000, SMALL)?;
        let data = std::fs::read(root.join("arm_helpers.o")).map_err(|e| e.to_string())?;
        let sections = sections(&data)?;
        let strings = section_bytes(&data, &sections[half(&data, 50)? as usize])?;
        let text = sections
            .iter()
            .find(|s| name(strings, s[0] as usize).is_ok_and(|v| v == ".text"))
            .ok_or("Missing native memory helpers")?;
        m.write(STUB + 0x100000, section_bytes(&data, text)?)?;
        for section in &sections {
            if section[1] == 2 {
                let names = section_bytes(&data, &sections[section[6] as usize])?;
                for s in section_bytes(&data, section)?.as_chunks::<16>().0 {
                    let n = name(names, word(s, 0)? as usize)?;
                    if let Some(n) = n.strip_suffix("_native") {
                        m.helpers
                            .borrow_mut()
                            .insert(n.into(), STUB + 0x100000 + word(s, 4)?);
                    }
                }
            }
        }
        Ok(m)
    }
    pub fn bind(&self, fallback: ffi::Fallback, user: ffi::Ptr) -> Result<()> {
        let mut api = ffi::GuestApi::default();
        unsafe { ffi::guest_get_api(&mut api) };
        let dispatch = unsafe {
            ffi::dispatch_create(
                self.guest,
                &api,
                4,
                8,
                256,
                STUB as u64,
                (RETURN - 1) as u64,
                fallback,
                user,
            )
        };
        if dispatch.is_null() {
            return Err("Cannot create host dispatcher".into());
        }
        self.dispatch.set(dispatch);
        for (a, n) in [(STUB + 0xfe000, "malloc"), (STUB + 0xfe004, "free")] {
            self.hooks.borrow_mut().insert(a, n.into());
            self.map_import(a, n, false)?;
        }
        Ok(())
    }
    pub fn load(&self, path: &Path) -> Result<()> {
        let data = std::fs::read(path).map_err(|e| e.to_string())?;
        if format!("{:x}", Sha256::digest(&data)) != ENGINE {
            return Err("Expected the pinned original game engine".into());
        }
        if data.get(..7) != Some(b"\x7fELF\x01\x01\x01") {
            return Err("Invalid ARM ELF".into());
        }
        let offset = word(&data, 28)? as usize;
        let width = half(&data, 42)? as usize;
        let count = half(&data, 44)? as usize;
        let mut segments = vec![];
        let mut end = 0;
        for i in 0..count {
            let p = offset + i * width;
            if word(&data, p)? == 1 {
                let v = word(&data, p + 8)?;
                let size = word(&data, p + 20)?;
                end = end.max(v.checked_add(size).ok_or("ELF range overflow")?);
                segments.push((
                    v,
                    word(&data, p + 4)? as usize,
                    word(&data, p + 16)? as usize,
                ));
            }
        }
        self.map(BASE, ((end as usize) + 4095) & !4095)?;
        for (v, offset, size) in segments {
            self.write(
                BASE + v,
                data.get(offset..offset + size)
                    .ok_or("Truncated ELF segment")?,
            )?;
        }
        let sections = sections(&data)?;
        let symbol = sections
            .iter()
            .find(|s| s[1] == 11)
            .ok_or("Missing ELF symbols")?;
        let table = section_bytes(&data, symbol)?;
        let names = section_bytes(&data, &sections[symbol[6] as usize])?;
        let mut symbols = vec![];
        for s in table.as_chunks::<16>().0 {
            let n = name(names, word(s, 0)? as usize)?;
            let value = word(s, 4)?;
            let defined = half(s, 14)? != 0;
            if defined && !n.is_empty() {
                self.symbols.borrow_mut().insert(n.clone(), BASE + value);
            }
            symbols.push((n, value, defined));
        }
        for section in &sections {
            if section[1] == 9 || section[1] == 4 {
                let width = if section[1] == 9 { 8 } else { 12 };
                for r in section_bytes(&data, section)?.chunks_exact(width) {
                    let addr = BASE + word(r, 0)?;
                    let info = word(r, 4)?;
                    let addend = self.u32(addr)?;
                    let value = match info & 255 {
                        23 => BASE.wrapping_add(addend),
                        2 | 21 | 22 => {
                            let (n, _, _) = symbols
                                .get((info >> 8) as usize)
                                .ok_or("Invalid ELF symbol index")?;
                            let resolved = self.symbols.borrow().get(n).copied();
                            let v = match resolved {
                                Some(v) => v,
                                None => self.import_address(n)?,
                            };
                            if info & 255 == 2 {
                                v.wrapping_add(addend)
                            } else {
                                v
                            }
                        }
                        kind => return Err(format!("Unsupported ARM relocation {kind}")),
                    };
                    self.put32(addr, value)?;
                }
            }
        }
        let names = section_bytes(&data, &sections[half(&data, 50)? as usize])?;
        if let Some(s) = sections
            .iter()
            .find(|s| name(names, s[0] as usize).is_ok_and(|n| n == ".init_array"))
        {
            for i in (0..s[5]).step_by(4) {
                self.constructors
                    .borrow_mut()
                    .push(self.u32(BASE + s[3] + i)?);
            }
        }
        Ok(())
    }
    pub fn initialize(&self) -> Result<()> {
        let calls = self.constructors.borrow().clone();
        for address in calls {
            if address != 0 && address != u32::MAX {
                self.call(address, &[])?;
            }
        }
        Ok(())
    }
    pub fn symbol(&self, name: &str) -> Result<u32> {
        self.symbols
            .borrow()
            .get(name)
            .copied()
            .ok_or_else(|| format!("Missing game export {name}"))
    }
    pub fn import_address(&self, name: &str) -> Result<u32> {
        if let Some(v) = self.imports.borrow().get(name) {
            return Ok(*v);
        }
        // Keep bulk copies and fills out of translated byte loops during frame transitions.
        let helper = self.helpers.borrow().get(name).copied().filter(|_| {
            !matches!(
                name,
                "memcpy"
                    | "memmove"
                    | "__aeabi_memcpy"
                    | "__aeabi_memmove"
                    | "memset"
                    | "__aeabi_memset"
            )
        });
        if let Some(v) = helper {
            self.imports.borrow_mut().insert(name.into(), v);
            return Ok(v);
        }
        let address = if [
            "__stack_chk_guard",
            "_ctype_",
            "_tolower_tab_",
            "_toupper_tab_",
            "__sF",
        ]
        .contains(&name)
            || name.starts_with("SL_IID_")
        {
            self.malloc(4096)?
        } else {
            let a = STUB + (self.imports.borrow().len() as u32) * 4;
            self.write(a, &[0x1e, 0xff, 0x2f, 0xe1])?;
            self.hooks.borrow_mut().insert(a, name.into());
            self.map_import(a, name, false)?;
            a
        };
        self.imports.borrow_mut().insert(name.into(), address);
        Ok(address)
    }
    fn map_import(&self, address: u32, name: &str, intercept: bool) -> Result<()> {
        let n = CString::new(name).map_err(|e| e.to_string())?;
        self.dispatch_check(unsafe {
            ffi::dispatch_add(
                self.dispatch.get(),
                address as u64,
                n.as_ptr(),
                intercept as i32,
            )
        })
    }
    pub fn intercept(&self, address: u32, name: &str) -> Result<()> {
        self.hooks.borrow_mut().insert(address, name.into());
        self.map_import(address, name, true)
    }
    pub fn register_native(&self, name: &str, callback: usize, context: ffi::Ptr) -> Result<()> {
        let n = CString::new(name).map_err(|e| e.to_string())?;
        self.dispatch_check(unsafe {
            ffi::dispatch_register(self.dispatch.get(), n.as_ptr(), callback, context)
        })?;
        self.import_address(name)?;
        Ok(())
    }
    fn check(&self, status: i32) -> Result<()> {
        if status == 0 {
            Ok(())
        } else {
            Err(unsafe { ffi::text(ffi::guest_error(self.guest)) })
        }
    }
    fn dispatch_check(&self, status: i32) -> Result<()> {
        if status == 0 {
            Ok(())
        } else {
            Err(unsafe { ffi::text(ffi::dispatch_error(self.dispatch.get())) })
        }
    }
    pub fn map(&self, address: u32, size: usize) -> Result<()> {
        self.check(unsafe { ffi::guest_mem_map(self.guest, address as u64, size, 7) })
    }
    pub fn read(&self, address: u32, size: usize) -> Result<Vec<u8>> {
        let mut out = vec![0; size];
        self.check(unsafe {
            ffi::guest_mem_read(self.guest, address as u64, out.as_mut_ptr().cast(), size)
        })?;
        Ok(out)
    }
    pub fn write(&self, address: u32, data: &[u8]) -> Result<()> {
        if address < 0x60000000 && address as u64 + data.len() as u64 > IMAGE as u64 {
            self.dispatch_check(unsafe {
                ffi::dispatch_before_write(self.dispatch.get(), address as u64, data.len())
            })?;
        }
        self.check(unsafe {
            ffi::guest_mem_write(self.guest, address as u64, data.as_ptr().cast(), data.len())
        })
    }
    pub fn u32(&self, address: u32) -> Result<u32> {
        word(&self.read(address, 4)?, 0)
    }
    pub fn put32(&self, address: u32, value: u32) -> Result<()> {
        self.write(address, &value.to_le_bytes())
    }
    pub fn reg(&self, reg: i32) -> Result<u32> {
        let mut v = 0u32;
        self.check(unsafe { ffi::guest_reg_read(self.guest, reg, (&mut v as *mut u32).cast()) })?;
        Ok(v)
    }
    pub fn set_reg(&self, reg: i32, value: u32) -> Result<()> {
        self.check(unsafe { ffi::guest_reg_write(self.guest, reg, (&value as *const u32).cast()) })
    }
    pub fn arg(&self, index: usize) -> Result<u32> {
        if index < 4 {
            self.reg(66 + index as i32)
        } else {
            self.u32(self.reg(12)? + ((index - 4) * 4) as u32)
        }
    }
    pub fn string(&self, address: u32) -> Result<Vec<u8>> {
        if address == 0 {
            return Ok(vec![]);
        }
        let mut out = vec![];
        while out.len() < 1 << 20 {
            let bytes = self.read(address + out.len() as u32, 256)?;
            if let Some(i) = bytes.iter().position(|x| *x == 0) {
                out.extend_from_slice(&bytes[..i]);
                return Ok(out);
            }
            out.extend_from_slice(&bytes);
        }
        Err("Unterminated guest string".into())
    }
    pub fn malloc(&self, size: usize) -> Result<u32> {
        let mut size = size.max(16).checked_add(15).ok_or("Allocation overflow")? & !15;
        if size <= 65536 {
            size = size.next_power_of_two();
        }
        let mut h = self.heap.borrow_mut();
        let address = if let Some(a) = h.free.get_mut(&size).and_then(Vec::pop) {
            a
        } else {
            let a = h.at;
            h.at =
                h.at.checked_add(size as u32)
                    .filter(|x| *x <= 0x30000000)
                    .ok_or("Guest heap exhausted")?;
            a
        };
        h.alloc.insert(address, size);
        Ok(address)
    }
    pub fn memalign(&self, alignment: usize, size: usize) -> Result<u32> {
        if !alignment.is_power_of_two() {
            return Err("Invalid allocation alignment".into());
        }
        let a = alignment as u32;
        {
            let mut h = self.heap.borrow_mut();
            h.at = h.at.checked_add(a - 1).ok_or("Alignment overflow")? & !(a - 1);
        }
        self.malloc(size)
    }
    pub fn allocation_size(&self, address: u32) -> Result<usize> {
        if (SMALL..SMALL + 0x1000000).contains(&address) {
            let bucket = self.u32(address - 16)?;
            if !address.is_multiple_of(16)
                || !(4..=12).contains(&bucket)
                || self.u32(address - 12)? != 1
            {
                return Err("Invalid small allocation".into());
            }
            return Ok(1usize << bucket);
        }
        self.heap
            .borrow()
            .alloc
            .get(&address)
            .copied()
            .ok_or("Unknown allocation".into())
    }
    pub fn free(&self, address: u32) -> Result<()> {
        if address == 0 {
            return Ok(());
        }
        if (SMALL..SMALL + 0x1000000).contains(&address) {
            self.allocation_size(address)?;
            let a = *self
                .helpers
                .borrow()
                .get("free")
                .ok_or("Missing free helper")?;
            self.call(a, &[address])?;
            return Ok(());
        }
        let image = self.heap.borrow().images.get(&address).copied();
        if let Some(size) = image {
            self.dispatch_check(unsafe {
                ffi::dispatch_release_image(self.dispatch.get(), address)
            })?;
            let mut h = self.heap.borrow_mut();
            h.images.remove(&address);
            h.alloc.remove(&address);
            h.image_free.entry(size).or_default().push(address);
        } else {
            let mut h = self.heap.borrow_mut();
            let size = h.alloc.remove(&address).ok_or("Invalid guest free")?;
            h.free.entry(size).or_default().push(address);
        }
        Ok(())
    }
    pub fn image_malloc(&self, size: usize) -> Result<(u32, ffi::Ptr)> {
        if size == 0 || size > 64 << 20 {
            return Err("Invalid decoded image size".into());
        }
        let mapped = (size + 4095) & !4095;
        let mut h = self.heap.borrow_mut();
        let address = h
            .image_free
            .get(&mapped)
            .and_then(|v| v.last())
            .copied()
            .unwrap_or(h.image);
        if address as u64 + mapped as u64 > 0x60000000 {
            return Err("Image heap exhausted".into());
        }
        let p = unsafe { ffi::dispatch_allocate_image(self.dispatch.get(), address, size) };
        if p.is_null() {
            return Err(unsafe { ffi::text(ffi::dispatch_error(self.dispatch.get())) });
        }
        if let Some(v) = h.image_free.get_mut(&mapped).filter(|v| !v.is_empty()) {
            v.pop();
        } else {
            h.image += mapped as u32;
        }
        h.images.insert(address, mapped);
        h.alloc.insert(address, mapped);
        Ok((address, p))
    }
    pub fn copy(&self, destination: u32, source: u32, size: usize) -> Result<u32> {
        if size > 64 << 20 {
            return Err("Bulk memory operation exceeds its size limit".into());
        }
        self.write(destination, &self.read(source, size)?)?;
        Ok(destination)
    }
    pub fn fill(&self, address: u32, value: u8, size: usize) -> Result<u32> {
        if size > 64 << 20 {
            return Err("Bulk memory operation exceeds its size limit".into());
        }
        self.write(address, &vec![value; size])?;
        Ok(address)
    }
    pub fn snapshot(&self) -> Result<Vec<u8>> {
        let mut v = vec![0; unsafe { ffi::guest_context_size() }];
        self.check(unsafe { ffi::guest_context_save(self.guest, v.as_mut_ptr().cast(), v.len()) })?;
        Ok(v)
    }
    pub fn restore(&self, context: &[u8]) -> Result<()> {
        self.check(unsafe {
            ffi::guest_context_restore(self.guest, context.as_ptr().cast(), context.len())
        })
    }
    pub fn jump(&self, context: &[u8]) -> Result<()> {
        self.check(unsafe {
            ffi::guest_context_jump(self.guest, context.as_ptr().cast(), context.len())
        })
    }
    pub fn call(&self, address: u32, args: &[u32]) -> Result<u32> {
        let saved = self.snapshot()?;
        let depth = self.depth.get() + 1;
        self.depth.set(depth);
        self.errors.borrow_mut().push(None);
        unsafe { ffi::dispatch_clear_error(self.dispatch.get()) };
        let began = unsafe { ffi::dispatch_begin_call(self.dispatch.get(), 1_000_000_000) } == 0;
        let result = (|| {
            if !began {
                return Err(unsafe { ffi::text(ffi::dispatch_error(self.dispatch.get())) });
            }
            let stack = STACK + 0x7fe000 - depth * 0x10000;
            self.set_reg(12, stack)?;
            for (i, value) in args.iter().enumerate() {
                if i < 4 {
                    self.set_reg(66 + i as i32, *value)?
                } else {
                    self.put32(stack + ((i - 4) * 4) as u32, *value)?
                }
            }
            self.set_reg(10, RETURN)?;
            let status = unsafe { ffi::guest_run(self.guest, address, RETURN) };
            if let Some(e) = self.errors.borrow().last().and_then(Clone::clone) {
                return Err(e);
            }
            let e = unsafe { ffi::text(ffi::dispatch_error(self.dispatch.get())) };
            if !e.is_empty() {
                return Err(e);
            }
            self.check(status)?;
            if self.reg(11)? != RETURN {
                return Err("Game instruction limit exceeded".into());
            }
            self.reg(66)
        })();
        if began {
            unsafe { ffi::dispatch_end_call(self.dispatch.get()) }
        }
        unsafe { ffi::dispatch_clear_error(self.dispatch.get()) };
        self.errors.borrow_mut().pop();
        self.depth.set(depth - 1);
        let restore = self.restore(&saved);
        match (result, restore) {
            (Err(e), _) => Err(e),
            (_, Err(e)) => Err(e),
            (Ok(v), _) => Ok(v),
        }
    }
}
impl Drop for Machine {
    fn drop(&mut self) {
        unsafe {
            if !self.dispatch.get().is_null() {
                ffi::dispatch_destroy(self.dispatch.get())
            }
            ffi::guest_free(self.guest)
        }
    }
}
fn sections(data: &[u8]) -> Result<Vec<[u32; 10]>> {
    let offset = word(data, 32)? as usize;
    let width = half(data, 46)? as usize;
    let count = half(data, 48)? as usize;
    (0..count)
        .map(|i| {
            let mut s = [0; 10];
            for (j, v) in s.iter_mut().enumerate() {
                *v = word(data, offset + i * width + j * 4)?;
            }
            Ok(s)
        })
        .collect()
}
fn section_bytes<'a>(data: &'a [u8], section: &[u32; 10]) -> Result<&'a [u8]> {
    data.get(section[4] as usize..section[4] as usize + section[5] as usize)
        .ok_or("Truncated ELF section".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bulk_buffers_are_rejected_before_allocating_or_accessing_guest_memory() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../runtime/bundle");
        let m = Machine::new(&root).unwrap();
        assert!(
            m.copy(u32::MAX, u32::MAX, (64 << 20) + 1)
                .unwrap_err()
                .contains("Bulk memory")
        );
        assert!(
            m.fill(u32::MAX, 0, (64 << 20) + 1)
                .unwrap_err()
                .contains("Bulk memory")
        );
        assert_eq!(m.copy(0, 0, 0).unwrap(), 0);
        assert_eq!(m.fill(0, 0, 0).unwrap(), 0);
    }

    #[test]
    fn elf_reads_are_little_endian_and_reject_truncation() {
        assert_eq!(word(&[1, 2, 3, 4], 0).unwrap(), 0x04030201);
        assert!(word(&[1, 2, 3], 0).is_err());
        assert!(half(&[1], 0).is_err());
        assert!(name(b"no terminator", 0).is_err());
        assert_eq!(name(b"\0JNI\0", 1).unwrap(), "JNI");
    }
}
