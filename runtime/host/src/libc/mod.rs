//! Android libc compatibility for the original game's native host.
use crate::Result;
use crate::machine::{Machine, Return};
use flate2::{Decompress, FlushDecompress, Status};
use std::cell::RefCell;
use std::cmp::Ordering;
use std::collections::HashMap;

mod formatting;
mod scalars;

use formatting::{format, scan};
use scalars::{
    change_case, character, crc32_bytes, float_arg, guest_integer, math_name, next_random,
    parse_float_prefix, python_math, single_bits,
};

const LIMIT: usize = 64 * 1024 * 1024;

struct State {
    elapsed: f64,
    random: u64,
    keys: HashMap<u32, u32>,
    streams: HashMap<u32, Decompress>,
    jumps: HashMap<u32, Vec<u8>>,
    exits: Vec<[u32; 3]>,
}

pub struct LibC {
    state: RefCell<State>,
    errno: u32,
    calendar: u32,
    locale: u32,
    epoch: i64,
}

impl LibC {
    pub fn new(m: &Machine, epoch: i64) -> Result<Self> {
        let errno = m.malloc(4)?;
        let calendar = m.malloc(64)?;
        let locale = m.malloc(2)?;
        m.write(locale, b"C\0")?;
        let result = Self {
            state: RefCell::new(State {
                elapsed: 0.0,
                random: 0x1234_abcd_330e,
                keys: HashMap::new(),
                streams: HashMap::new(),
                jumps: HashMap::new(),
                exits: Vec::new(),
            }),
            errno,
            calendar,
            locale,
            epoch,
        };
        result.data_tables(m)?;
        Ok(result)
    }

    pub fn advance(&self, dt: f64) {
        self.state.borrow_mut().elapsed += dt;
    }

    fn data_tables(&self, m: &Machine) -> Result<()> {
        m.put32(m.import_address("__stack_chk_guard")?, 0x6a09_e667)?;
        for (name, lower) in [("_tolower_tab_", true), ("_toupper_tab_", false)] {
            let mut data = Vec::with_capacity(514);
            data.extend_from_slice(&(-1i16).to_le_bytes());
            for value in 0..256 {
                data.extend_from_slice(&(change_case(value, lower) as i16).to_le_bytes());
            }
            let pointer = m.malloc(data.len())?;
            m.write(pointer, &data)?;
            m.put32(m.import_address(name)?, pointer)?;
        }
        let mut data = vec![0];
        for value in 0u8..=255 {
            let mask = u8::from(value.is_ascii_uppercase())
                | (u8::from(value.is_ascii_lowercase()) << 1)
                | (u8::from(value.is_ascii_digit()) << 2)
                | (u8::from(matches!(value, 9..=13 | 32)) << 3)
                | (u8::from((33..=126).contains(&value) && !value.is_ascii_alphanumeric()) << 4)
                | (u8::from(value < 32 || value == 127) << 5)
                | (u8::from(value.is_ascii_hexdigit()) << 6)
                | (u8::from(value == 32) << 7);
            data.push(mask);
        }
        let pointer = m.malloc(data.len())?;
        m.write(pointer, &data)?;
        m.put32(m.import_address("_ctype_")?, pointer)
    }

    pub fn invoke(&self, name: &str, m: &Machine) -> Result<Option<Return>> {
        let a = |index| m.arg(index);
        let s = |index| m.string(a(index)?);
        let value = match name {
            "strlen" => s(0)?.len() as u32,
            "strcmp" | "strncmp" => {
                let mut left = s(0)?;
                let mut right = s(1)?;
                if name == "strncmp" {
                    let count = a(2)? as usize;
                    left.truncate(count);
                    right.truncate(count);
                }
                ordering(left.cmp(&right)) as u32
            }
            "strcpy" => write_string(m, a(0)?, &s(1)?, None, false)?,
            "strncpy" => {
                let count = bounded(a(2)? as usize)?;
                let mut data = s(1)?;
                data.resize(count, 0);
                m.write(a(0)?, &data)?;
                a(0)?
            }
            "strchr" => {
                let mut data = s(0)?;
                data.push(0);
                find(a(0)?, &data, &[a(1)? as u8])
            }
            "strstr" => find(a(0)?, &s(0)?, &s(1)?),
            "memchr" => find(
                a(0)?,
                &m.read(a(0)?, bounded(a(2)? as usize)?)?,
                &[a(1)? as u8],
            ),
            "memcmp" => {
                let count = bounded(a(2)? as usize)?;
                let left = m.read(a(0)?, count)?;
                let right = m.read(a(1)?, count)?;
                left.iter()
                    .zip(right)
                    .find_map(|(&x, y)| (x != y).then_some(i32::from(x) - i32::from(y)))
                    .unwrap_or(0) as u32
            }
            "calloc" => {
                let size = bounded(
                    (a(0)? as usize)
                        .checked_mul(a(1)? as usize)
                        .ok_or("Guest calloc overflow")?,
                )?;
                let pointer = m.malloc(size)?;
                m.fill(pointer, 0, size)?;
                pointer
            }
            "realloc" => {
                let old = a(0)?;
                let size = bounded(a(1)? as usize)?;
                let pointer = m.malloc(size)?;
                if old != 0 {
                    m.copy(pointer, old, m.allocation_size(old)?.min(size))?;
                    m.free(old)?;
                }
                pointer
            }
            "memalign" => m.memalign(a(0)? as usize, bounded(a(1)? as usize)?)?,
            "atoi" => {
                let bytes = s(0)?;
                let mut at = 0;
                while bytes.get(at).is_some_and(u8::is_ascii_whitespace) {
                    at += 1;
                }
                let negative = bytes.get(at) == Some(&b'-');
                if matches!(bytes.get(at), Some(b'+' | b'-')) {
                    at += 1;
                }
                let mut value = 0u32;
                while let Some(&digit) = bytes.get(at).filter(|value| value.is_ascii_digit()) {
                    value = value.wrapping_mul(10).wrapping_add(u32::from(digit - b'0'));
                    at += 1;
                }
                if negative {
                    value.wrapping_neg()
                } else {
                    value
                }
            }
            "strtod" => {
                let pointer = a(0)?;
                let (value, end) = parse_float_prefix(&s(0)?);
                if a(1)? != 0 {
                    m.put32(a(1)?, address(pointer, end)?)?;
                }
                return Ok(Some(Return::Wide(value.to_bits())));
            }
            "sscanf" => {
                let (count, values) = scan(&s(0)?, &s(1)?)?;
                for (index, bytes) in values.iter().take(count.max(0) as usize).enumerate() {
                    m.write(a(index + 2)?, bytes)?;
                }
                count as u32
            }
            "puts" | "fputs" | "__android_log_write" => {
                log(&s(if name == "__android_log_write" { 2 } else { 0 })?)
            }
            "printf" | "fprintf" | "__android_log_print" => {
                let first = match name {
                    "fprintf" => 1,
                    "__android_log_print" => 2,
                    _ => 0,
                };
                log(&format(m, &s(first)?, first + 1, None)?)
            }
            "fputc" => {
                log(&[a(0)? as u8]);
                a(0)?
            }
            "sprintf" => write_string(m, a(0)?, &format(m, &s(1)?, 2, None)?, None, true)?,
            "snprintf" | "vsnprintf" => {
                let pointer = if name == "vsnprintf" {
                    Some(a(3)?)
                } else {
                    None
                };
                let bytes = format(m, &s(2)?, if pointer.is_some() { 0 } else { 3 }, pointer)?;
                write_string(m, a(0)?, &bytes, Some(a(1)? as usize), true)?
            }
            "__cxa_atexit" | "__aeabi_atexit" => {
                self.state.borrow_mut().exits.push([a(0)?, a(1)?, a(2)?]);
                0
            }
            "pthread_once" => {
                let pointer = a(0)?;
                if m.u32(pointer)? == 0 {
                    m.put32(pointer, 1)?;
                    m.call(a(1)?, &[])?;
                }
                0
            }
            "pthread_key_create" => {
                let key = self.state.borrow().keys.len() as u32 + 1;
                self.state.borrow_mut().keys.insert(key, 0);
                m.put32(a(0)?, key)?;
                0
            }
            "pthread_key_delete" => self.state.borrow_mut().keys.remove(&a(0)?).unwrap_or(0),
            "pthread_getspecific" => self.state.borrow().keys.get(&a(0)?).copied().unwrap_or(0),
            "pthread_setspecific" => {
                self.state.borrow_mut().keys.insert(a(0)?, a(1)?);
                0
            }
            "__errno" => self.errno,
            "setlocale" => self.locale,
            "fcntl" => u32::from(a(1)? == 3 && matches!(a(0)?, 1 | 2)),
            "fstat" => {
                let pointer = a(1)?;
                m.fill(pointer, 0, 96)?;
                for (offset, value) in [(16, 0o20600), (20, 1), (52, 4096)] {
                    m.put32(address(pointer, offset)?, value)?;
                }
                0
            }
            "clock" => guest_integer(self.state.borrow().elapsed * 1_000_000.0)?,
            "clock_gettime" => {
                let seconds =
                    if a(0)? == 0 { self.epoch as f64 } else { 0.0 } + self.state.borrow().elapsed;
                let mut bytes = Vec::with_capacity(8);
                bytes.extend_from_slice(&(seconds as i32).to_le_bytes());
                bytes.extend_from_slice(&((seconds.rem_euclid(1.0) * 1e9) as i32).to_le_bytes());
                m.write(a(1)?, &bytes)?;
                0
            }
            "time" => {
                let value = guest_integer(self.epoch as f64 + self.state.borrow().elapsed)?;
                if a(0)? != 0 {
                    m.put32(a(0)?, value)?;
                }
                value
            }
            "localtime" | "gmtime" => {
                let seconds = i64::from(m.u32(a(0)?)? as i32) as libc::time_t;
                // These reentrant APIs fill caller-owned storage, not libc's static buffer.
                let mut value = unsafe { std::mem::zeroed::<libc::tm>() };
                let output = unsafe {
                    if name == "gmtime" {
                        libc::gmtime_r(&seconds, &mut value)
                    } else {
                        libc::localtime_r(&seconds, &mut value)
                    }
                };
                if output.is_null() {
                    return Err("Guest calendar conversion failed".into());
                }
                let fields = [
                    value.tm_sec,
                    value.tm_min,
                    value.tm_hour,
                    value.tm_mday,
                    value.tm_mon,
                    value.tm_year,
                    value.tm_wday,
                    value.tm_yday,
                    value.tm_isdst,
                ];
                let bytes: Vec<u8> = fields.into_iter().flat_map(i32::to_le_bytes).collect();
                m.write(self.calendar, &bytes)?;
                self.calendar
            }
            "mktime" => {
                let bytes = m.read(a(0)?, 36)?;
                let mut fields = [0i32; 9];
                for (index, field) in fields.iter_mut().enumerate() {
                    *field = i32::from_le_bytes(
                        bytes[index * 4..index * 4 + 4]
                            .try_into()
                            .map_err(|_| "Invalid guest calendar")?,
                    );
                }
                let mut value = unsafe { std::mem::zeroed::<libc::tm>() };
                value.tm_sec = fields[0];
                value.tm_min = fields[1];
                value.tm_hour = fields[2];
                value.tm_mday = fields[3];
                value.tm_mon = fields[4];
                value.tm_year = fields[5];
                value.tm_wday = fields[6];
                value.tm_yday = fields[7];
                value.tm_isdst = fields[8];
                unsafe { libc::mktime(&mut value) as u32 }
            }
            "getpid" => 1,
            "sysconf" => {
                if a(0)? as i32 == 39 {
                    4096
                } else {
                    3
                }
            }
            "srand48" => {
                self.state.borrow_mut().random = (u64::from(a(0)?) << 16) | 0x330e;
                0
            }
            "lrand48" => next_random(&mut self.state.borrow_mut().random),
            "crc32" => {
                if a(1)? == 0 {
                    0
                } else {
                    crc32_bytes(&m.read(a(1)?, bounded(a(2)? as usize)?)?, a(0)?)
                }
            }
            "inflateInit_" | "inflateReset" => {
                self.state
                    .borrow_mut()
                    .streams
                    .insert(a(0)?, Decompress::new(true));
                0
            }
            "inflateEnd" => {
                return Ok(Some(
                    if self.state.borrow_mut().streams.remove(&a(0)?).is_some() {
                        Return::Word(0)
                    } else {
                        Return::Void
                    },
                ));
            }
            "inflate" => self.inflate(m)?,
            "qsort" => {
                self.qsort(m)?;
                0
            }
            "setjmp" => {
                self.state.borrow_mut().jumps.insert(a(0)?, m.snapshot()?);
                0
            }
            "longjmp" => {
                let value = match a(1)? {
                    0 => 1,
                    value => value,
                };
                let snapshot = self
                    .state
                    .borrow()
                    .jumps
                    .get(&a(0)?)
                    .cloned()
                    .ok_or("Unknown guest jump buffer")?;
                m.jump(&snapshot)?;
                value
            }
            "wcslen" => {
                let pointer = a(0)?;
                let mut count = 0;
                while m.u32(address(pointer, count * 4)?)? != 0 {
                    count += 1;
                    if count > 1 << 20 {
                        return Err("Unterminated guest wide string".into());
                    }
                }
                count as u32
            }
            "wmemcpy" | "wmemmove" => m.copy(a(0)?, a(1)?, bounded(a(2)? as usize * 4)?)?,
            "wmemset" => {
                let count = bounded(a(2)? as usize * 4)?;
                let data: Vec<u8> = a(1)?
                    .to_le_bytes()
                    .into_iter()
                    .cycle()
                    .take(count)
                    .collect();
                m.write(a(0)?, &data)?;
                a(0)?
            }
            "__aeabi_memclr" | "__aeabi_memclr4" | "__aeabi_memclr8" => {
                m.fill(a(0)?, 0, bounded(a(1)? as usize)?)?
            }
            "__cxa_finalize"
            | "pthread_mutex_init"
            | "pthread_mutex_destroy"
            | "sigaction"
            | "sigprocmask"
            | "bsd_signal"
            | "fflush"
            | "ferror" => 0,
            "abort" | "exit" | "_exit" | "__stack_chk_fail" | "__assert2" => {
                return Err(format!("Original engine called {name}"));
            }
            "tolower" | "toupper" | "towlower" | "towupper" => {
                change_case(a(0)?, name.contains("lower"))
            }
            _ => {
                let normalized = name.strip_prefix("isw").map(|suffix| format!("is{suffix}"));
                let kind = normalized.as_deref().unwrap_or(name);
                if matches!(
                    kind,
                    "isalpha"
                        | "iscntrl"
                        | "isprint"
                        | "ispunct"
                        | "isspace"
                        | "isxdigit"
                        | "isupper"
                        | "islower"
                        | "isdigit"
                ) {
                    character(kind, a(0)?)
                } else if let Some((kind, single)) = math_name(name) {
                    let x = float_arg(m, 0, single)?;
                    if matches!(kind, "lrint" | "lround") {
                        if !x.is_finite() {
                            return Err("Invalid integer rounding argument".into());
                        }
                        x.round_ties_even() as i64 as u32
                    } else {
                        let y = if matches!(kind, "pow" | "atan2" | "fmax") {
                            Some(float_arg(m, if single { 1 } else { 2 }, single)?)
                        } else {
                            None
                        };
                        let value = python_math(kind, x, y)?;
                        return Ok(Some(if single {
                            Return::Word(single_bits(value)?)
                        } else {
                            Return::Wide(value.to_bits())
                        }));
                    }
                } else {
                    return Ok(None);
                }
            }
        };
        Ok(Some(Return::Word(value)))
    }

    fn inflate(&self, m: &Machine) -> Result<u32> {
        let pointer = m.arg(0)?;
        let bytes = m.read(pointer, 24)?;
        let mut fields = [0u32; 6];
        for (index, field) in fields.iter_mut().enumerate() {
            *field = u32::from_le_bytes(
                bytes[index * 4..index * 4 + 4]
                    .try_into()
                    .map_err(|_| "Invalid guest zlib state")?,
            );
        }
        let input = m.read(fields[0], bounded(fields[1] as usize)?)?;
        let mut output = vec![0; bounded(fields[4] as usize)?];
        let (consumed, produced, done) = {
            let mut state = self.state.borrow_mut();
            let stream = state
                .streams
                .get_mut(&pointer)
                .ok_or("Unknown guest zlib stream")?;
            let before_in = stream.total_in();
            let before_out = stream.total_out();
            let status = stream
                .decompress(&input, &mut output, FlushDecompress::None)
                .map_err(|error| error.to_string())?;
            (
                (stream.total_in() - before_in) as u32,
                (stream.total_out() - before_out) as u32,
                status == Status::StreamEnd,
            )
        };
        m.write(fields[3], &output[..produced as usize])?;
        fields[0] = fields[0]
            .checked_add(consumed)
            .ok_or("Guest zlib input overflow")?;
        fields[1] -= consumed;
        fields[2] = fields[2]
            .checked_add(consumed)
            .ok_or("Guest zlib counter overflow")?;
        fields[3] = fields[3]
            .checked_add(produced)
            .ok_or("Guest zlib output overflow")?;
        fields[4] -= produced;
        fields[5] = fields[5]
            .checked_add(produced)
            .ok_or("Guest zlib counter overflow")?;
        let bytes: Vec<u8> = fields.into_iter().flat_map(u32::to_le_bytes).collect();
        m.write(pointer, &bytes)?;
        Ok(u32::from(done))
    }

    fn qsort(&self, m: &Machine) -> Result<()> {
        let base = m.arg(0)?;
        let count = m.arg(1)? as usize;
        let size = m.arg(2)? as usize;
        bounded(count.checked_mul(size).ok_or("Guest sort size overflow")?)?;
        let compare = m.arg(3)?;
        let mut order: Vec<usize> = (0..count).collect();
        let mut failure = None;
        order.sort_by(|&left, &right| {
            if failure.is_some() {
                return Ordering::Equal;
            }
            let result = address(base, left * size)
                .and_then(|a| address(base, right * size).and_then(|b| m.call(compare, &[a, b])));
            match result {
                Ok(value) => (value as i32).cmp(&0),
                Err(error) => {
                    failure = Some(error);
                    Ordering::Equal
                }
            }
        });
        if let Some(error) = failure {
            return Err(error);
        }
        let mut output = Vec::with_capacity(count * size);
        for index in order {
            output.extend_from_slice(&m.read(address(base, index * size)?, size)?);
        }
        m.write(base, &output)
    }
}

fn bounded(size: usize) -> Result<usize> {
    if size > LIMIT {
        Err("Guest libc buffer exceeds its size limit".into())
    } else {
        Ok(size)
    }
}

fn address(base: u32, offset: usize) -> Result<u32> {
    base.checked_add(u32::try_from(offset).map_err(|_| "Guest address overflow")?)
        .ok_or_else(|| "Guest address overflow".into())
}

fn ordering(value: Ordering) -> i32 {
    match value {
        Ordering::Less => -1,
        Ordering::Equal => 0,
        Ordering::Greater => 1,
    }
}

fn find(pointer: u32, haystack: &[u8], needle: &[u8]) -> u32 {
    if needle.is_empty() {
        return pointer;
    }
    haystack
        .windows(needle.len())
        .position(|value| value == needle)
        .and_then(|offset| pointer.checked_add(offset as u32))
        .unwrap_or(0)
}

fn write_string(
    m: &Machine,
    pointer: u32,
    bytes: &[u8],
    limit: Option<usize>,
    count: bool,
) -> Result<u32> {
    let length = limit.map_or(bytes.len(), |limit| {
        bytes.len().min(limit.saturating_sub(1))
    });
    if limit != Some(0) {
        let mut value = bytes[..length].to_vec();
        value.push(0);
        m.write(pointer, &value)?;
    }
    Ok(if count { bytes.len() as u32 } else { pointer })
}

fn log(bytes: &[u8]) -> u32 {
    eprintln!("[engine] {}", String::from_utf8_lossy(bytes).trim_end());
    bytes.len() as u32
}
