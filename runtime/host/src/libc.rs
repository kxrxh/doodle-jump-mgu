//! Android libc compatibility for the original game's native host.
use crate::Result;
use crate::machine::{Machine, Return};
use flate2::{Decompress, FlushDecompress, Status};
use std::cell::RefCell;
use std::cmp::Ordering;
use std::collections::HashMap;
use std::ffi::CString;

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

fn parse_float_prefix(bytes: &[u8]) -> (f64, usize) {
    let mut at = 0;
    while bytes.get(at).is_some_and(u8::is_ascii_whitespace) {
        at += 1;
    }
    let start = at;
    let negative = bytes.get(at) == Some(&b'-');
    if matches!(bytes.get(at), Some(b'+' | b'-')) {
        at += 1;
    }
    let tail = &bytes[at..];
    if tail.len() >= 3 && tail[..3].eq_ignore_ascii_case(b"inf") {
        let length = if tail.len() >= 8 && tail[..8].eq_ignore_ascii_case(b"infinity") {
            8
        } else {
            3
        };
        return (
            if negative {
                f64::NEG_INFINITY
            } else {
                f64::INFINITY
            },
            at + length,
        );
    }
    if tail.len() >= 3 && tail[..3].eq_ignore_ascii_case(b"nan") {
        return (if negative { -f64::NAN } else { f64::NAN }, at + 3);
    }
    let mut digits = 0;
    while bytes.get(at).is_some_and(u8::is_ascii_digit) {
        at += 1;
        digits += 1;
    }
    if bytes.get(at) == Some(&b'.') {
        at += 1;
        while bytes.get(at).is_some_and(u8::is_ascii_digit) {
            at += 1;
            digits += 1;
        }
    }
    if digits == 0 {
        return (0.0, 0);
    }
    let exponent = at;
    if matches!(bytes.get(at), Some(b'e' | b'E')) {
        at += 1;
        if matches!(bytes.get(at), Some(b'+' | b'-')) {
            at += 1;
        }
        let first = at;
        while bytes.get(at).is_some_and(u8::is_ascii_digit) {
            at += 1;
        }
        if first == at {
            at = exponent;
        }
    }
    let value = std::str::from_utf8(&bytes[start..at])
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(if negative {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        });
    (value, at)
}

fn change_case(value: u32, lower: bool) -> u32 {
    if lower && (65..=90).contains(&value) {
        value + 32
    } else if !lower && (97..=122).contains(&value) {
        value - 32
    } else {
        value
    }
}

fn character(name: &str, value: u32) -> u32 {
    if value > 127 {
        return 0;
    }
    let c = value as u8;
    u32::from(match name {
        "isalpha" => c.is_ascii_alphabetic(),
        "isdigit" => c.is_ascii_digit(),
        "isspace" => matches!(c, 9..=13 | 28..=32),
        "isupper" => c.is_ascii_uppercase(),
        "islower" => c.is_ascii_lowercase(),
        "isprint" => (32..=126).contains(&c),
        "iscntrl" => c < 32 || c == 127,
        "isxdigit" => c.is_ascii_hexdigit(),
        _ => (33..=126).contains(&c) && !c.is_ascii_alphanumeric(),
    })
}

const fn crc_table() -> [u32; 256] {
    let mut table = [0; 256];
    let mut index = 0;
    while index < 256 {
        let mut value = index as u32;
        let mut bit = 0;
        while bit < 8 {
            value = (value >> 1) ^ (0xedb8_8320u32.wrapping_mul(value & 1));
            bit += 1;
        }
        table[index] = value;
        index += 1;
    }
    table
}

fn crc32_bytes(bytes: &[u8], seed: u32) -> u32 {
    const TABLE: [u32; 256] = crc_table();
    let mut value = !seed;
    for &byte in bytes {
        value = TABLE[((value ^ u32::from(byte)) & 255) as usize] ^ (value >> 8);
    }
    !value
}

fn next_random(state: &mut u64) -> u32 {
    *state = state.wrapping_mul(0x5deece66d).wrapping_add(11) & ((1 << 48) - 1);
    (*state >> 17) as u32
}

fn float_arg(m: &Machine, index: usize, single: bool) -> Result<f64> {
    Ok(if single {
        f64::from(f32::from_bits(m.arg(index)?))
    } else {
        f64::from_bits(u64::from(m.arg(index)?) | (u64::from(m.arg(index + 1)?) << 32))
    })
}

fn guest_integer(value: f64) -> Result<u32> {
    if !value.is_finite() {
        return Err("Invalid guest integer conversion".into());
    }
    Ok(value.trunc().rem_euclid(4294967296.0) as u32)
}
fn single_bits(value: f64) -> Result<u32> {
    let result = value as f32;
    if value.is_finite() && result.is_infinite() {
        return Err("Single precision math overflow".into());
    }
    Ok(result.to_bits())
}

fn math_name(name: &str) -> Option<(&str, bool)> {
    const NAMES: &[&str] = &[
        "sqrt", "sin", "cos", "floor", "ceil", "atan", "exp", "acos", "log", "trunc", "pow",
        "atan2", "fmax", "round", "rint", "lrint", "lround",
    ];
    if NAMES.contains(&name) {
        Some((name, false))
    } else {
        name.strip_suffix('f')
            .filter(|name| NAMES.contains(name))
            .map(|name| (name, true))
    }
}

fn python_math(name: &str, x: f64, y: Option<f64>) -> Result<f64> {
    if matches!(name, "sin" | "cos") && x.is_infinite()
        || name == "sqrt" && x < 0.0
        || name == "log" && x <= 0.0
        || name == "acos" && !x.is_nan() && !(-1.0..=1.0).contains(&x)
        || matches!(name, "floor" | "ceil" | "trunc" | "round" | "rint") && !x.is_finite()
    {
        return Err(format!("Invalid {name} argument"));
    }
    let result = match name {
        "sqrt" => x.sqrt(),
        "sin" => x.sin(),
        "cos" => x.cos(),
        "floor" => x.floor(),
        "ceil" => x.ceil(),
        "atan" => x.atan(),
        "exp" => x.exp(),
        "acos" => x.acos(),
        "log" => x.ln(),
        "trunc" => x.trunc(),
        "round" => x.round(),
        "rint" => x.round_ties_even(),
        "pow" | "atan2" | "fmax" => {
            let y = y.ok_or("Missing binary math argument")?;
            match name {
                "atan2" => x.atan2(y),
                "fmax" => {
                    if y > x {
                        y
                    } else {
                        x
                    }
                }
                _ => {
                    if x == 0.0 && y < 0.0 || x < 0.0 && y.is_finite() && y.fract() != 0.0 {
                        return Err("Invalid pow argument".into());
                    }
                    x.powf(y)
                }
            }
        }
        _ => return Err(format!("Unknown math function {name}")),
    };
    if matches!(name, "exp" | "pow")
        && x.is_finite()
        && y.is_none_or(f64::is_finite)
        && result.is_infinite()
    {
        return Err(format!("{name} overflow"));
    }
    Ok(
        if result == 0.0 && matches!(name, "floor" | "ceil" | "trunc" | "round" | "rint") {
            0.0
        } else {
            result
        },
    )
}

fn format(m: &Machine, template: &[u8], start: usize, pointer: Option<u32>) -> Result<Vec<u8>> {
    format_bytes(
        template,
        start,
        &mut |index| match pointer {
            Some(base) => m.u32(address(base, index * 4)?),
            None => m.arg(index),
        },
        &mut |address| m.string(address),
    )
}

#[derive(Default)]
struct Spec {
    flags: String,
    width: Option<usize>,
    precision: Option<usize>,
    width_star: bool,
    precision_star: bool,
    length: String,
    kind: u8,
    end: usize,
}

fn specification(bytes: &[u8], start: usize) -> Result<Option<Spec>> {
    let mut at = start + 1;
    let mut result = Spec::default();
    while bytes.get(at).is_some_and(|c| b"-+ #0".contains(c)) {
        result.flags.push(bytes[at] as char);
        at += 1;
    }
    if bytes.get(at) == Some(&b'*') {
        result.width_star = true;
        at += 1;
    } else {
        result.width = decimal(bytes, &mut at)?;
    }
    if bytes.get(at) == Some(&b'.') {
        at += 1;
        if bytes.get(at) == Some(&b'*') {
            result.precision_star = true;
            at += 1;
        } else {
            result.precision = Some(decimal(bytes, &mut at)?.unwrap_or(0));
        }
    }
    while bytes.get(at).is_some_and(|c| b"hlzjtL".contains(c)) {
        result.length.push(bytes[at] as char);
        at += 1;
    }
    let Some(&kind) = bytes.get(at).filter(|c| b"diuoxXfFeEgGaAcsp%".contains(c)) else {
        return Ok(None);
    };
    result.kind = kind;
    result.end = at + 1;
    Ok(Some(result))
}

fn decimal(bytes: &[u8], at: &mut usize) -> Result<Option<usize>> {
    let first = *at;
    let mut value = 0usize;
    while let Some(&c) = bytes.get(*at).filter(|c| c.is_ascii_digit()) {
        value = value
            .checked_mul(10)
            .and_then(|value| value.checked_add(usize::from(c - b'0')))
            .ok_or("Native format width overflow")?;
        if value > 1 << 20 {
            return Err("Native format exceeds its size limit".into());
        }
        *at += 1;
    }
    Ok((*at > first).then_some(value))
}

fn format_bytes(
    template: &[u8],
    start: usize,
    word: &mut dyn FnMut(usize) -> Result<u32>,
    string: &mut dyn FnMut(u32) -> Result<Vec<u8>>,
) -> Result<Vec<u8>> {
    let mut at = 0;
    let mut slot = start;
    let mut output = Vec::new();
    while at < template.len() {
        if template[at] != b'%' {
            output.push(template[at]);
            at += 1;
            continue;
        }
        let Some(mut spec) = specification(template, at)? else {
            output.push(b'%');
            at += 1;
            continue;
        };
        at = spec.end;
        if spec.kind == b'%' {
            output.push(b'%');
            continue;
        }
        if spec.width_star && spec.precision_star {
            return Err("Unsupported native format with two stars".into());
        }
        if spec.width_star || spec.precision_star {
            let value = word(slot)? as i32;
            slot += 1;
            if spec.width_star {
                if value < 0 {
                    spec.flags.push('-');
                }
                spec.width = Some(value.unsigned_abs() as usize);
            } else {
                spec.precision =
                    Some(usize::try_from(value).map_err(|_| "Invalid native format precision")?);
            }
            if spec.width.unwrap_or(0) > 1 << 20 || spec.precision.unwrap_or(0) > 1 << 20 {
                return Err("Native format exceeds its size limit".into());
            }
        }
        let wide = b"fFeEgGaA".contains(&spec.kind) || spec.length.contains("ll");
        if wide {
            slot += slot & 1;
        }
        let low = word(slot)?;
        slot += 1;
        let value = if wide {
            let high = word(slot)?;
            slot += 1;
            u64::from(low) | (u64::from(high) << 32)
        } else {
            u64::from(low)
        };
        let rendered = match spec.kind {
            b's' => format_text(&String::from_utf8_lossy(&string(low)?), &spec),
            b'c' => {
                spec.precision = None;
                format_text(
                    &char::from_u32(low)
                        .ok_or("Invalid native character")?
                        .to_string(),
                    &spec,
                )
            }
            b'p' => format!("0x{value:x}").into_bytes(),
            b'd' | b'i' | b'u' | b'o' | b'x' | b'X' => format_integer(value, &spec),
            b'a' => {
                let number = f64::from_bits(value);
                let mut text = format!("{number:?}").to_lowercase();
                if let Some((base, exponent)) = text.split_once('e') {
                    let exponent: i32 = exponent.parse().map_err(|_| "Invalid float exponent")?;
                    text = format!("{base}e{exponent:+03}");
                }
                format_text(&text, &spec)
            }
            b'A' => return Err("Unsupported native format %A".into()),
            _ => format_float(f64::from_bits(value), &spec)?,
        };
        if output
            .len()
            .checked_add(rendered.len())
            .is_none_or(|size| size > LIMIT)
        {
            return Err("Formatted output exceeds its size limit".into());
        }
        output.extend_from_slice(&rendered);
    }
    Ok(output)
}

fn format_text(text: &str, spec: &Spec) -> Vec<u8> {
    let value: String = text
        .chars()
        .take(spec.precision.unwrap_or(usize::MAX))
        .collect();
    let padding = spec
        .width
        .unwrap_or(0)
        .saturating_sub(value.chars().count());
    if spec.flags.contains('-') {
        format!("{value}{}", " ".repeat(padding)).into_bytes()
    } else {
        format!("{}{value}", " ".repeat(padding)).into_bytes()
    }
}

fn format_integer(value: u64, spec: &Spec) -> Vec<u8> {
    let signed = matches!(spec.kind, b'd' | b'i');
    let number = if signed && value & 0x8000_0000 != 0 {
        i128::from(value) - (1i128 << 32)
    } else {
        i128::from(value)
    };
    let negative = signed && number < 0;
    let magnitude = if signed {
        number.unsigned_abs()
    } else {
        value as u128
    };
    let mut digits = match spec.kind {
        b'x' => format!("{magnitude:x}"),
        b'X' => format!("{magnitude:X}"),
        b'o' => format!("{magnitude:o}"),
        _ => magnitude.to_string(),
    };
    let precision = spec.precision.unwrap_or(0).saturating_sub(digits.len());
    digits = format!("{}{digits}", "0".repeat(precision));
    let sign = if negative {
        "-"
    } else if spec.flags.contains('+') {
        "+"
    } else if spec.flags.contains(' ') {
        " "
    } else {
        ""
    };
    let prefix = if spec.flags.contains('#') {
        match spec.kind {
            b'x' => "0x",
            b'X' => "0X",
            b'o' => "0o",
            _ => "",
        }
    } else {
        ""
    };
    let padding = spec
        .width
        .unwrap_or(0)
        .saturating_sub(sign.len() + prefix.len() + digits.len());
    if spec.flags.contains('-') {
        format!("{sign}{prefix}{digits}{}", " ".repeat(padding)).into_bytes()
    } else if spec.flags.contains('0') {
        format!("{sign}{prefix}{}{digits}", "0".repeat(padding)).into_bytes()
    } else {
        format!("{}{sign}{prefix}{digits}", " ".repeat(padding)).into_bytes()
    }
}

fn format_float(value: f64, spec: &Spec) -> Result<Vec<u8>> {
    let mut format = format!("%{}", spec.flags);
    if let Some(width) = spec.width {
        format.push_str(&width.to_string());
    }
    if let Some(precision) = spec.precision {
        format.push_str(&format!(".{precision}"));
    }
    format.push(spec.kind as char);
    let format = CString::new(format).map_err(|_| "Invalid native floating format")?;
    // The format is constructed from exactly one validated floating conversion.
    let size = unsafe { libc::snprintf(std::ptr::null_mut(), 0, format.as_ptr(), value) };
    if size < 0 || size as usize > 1 << 20 {
        return Err("Native floating format exceeds its size limit".into());
    }
    let mut result = vec![0u8; size as usize + 1];
    unsafe {
        libc::snprintf(
            result.as_mut_ptr().cast(),
            result.len(),
            format.as_ptr(),
            value,
        );
    }
    result.truncate(size as usize);
    Ok(result)
}

struct ScanField {
    size: Option<usize>,
    kind: u8,
    width: usize,
    capacity: usize,
}

fn scan(source: &[u8], template: &[u8]) -> Result<(i32, Vec<Vec<u8>>)> {
    let mut at = 0;
    let mut fields = Vec::new();
    while at < template.len() {
        if template[at] != b'%' {
            at += 1;
            continue;
        }
        at += 1;
        if template.get(at) == Some(&b'%') {
            at += 1;
            continue;
        }
        let suppressed = template.get(at) == Some(&b'*');
        if suppressed {
            at += 1;
        }
        let width = decimal(template, &mut at)?.unwrap_or(1);
        let first = at;
        if matches!(template.get(at), Some(b'h' | b'l')) {
            let letter = template[at];
            at += 1;
            if template.get(at) == Some(&letter) {
                at += 1;
            }
        } else if matches!(template.get(at), Some(b'L' | b'z')) {
            at += 1;
        }
        let length = &template[first..at];
        let kind = *template
            .get(at)
            .filter(|kind| b"diuoxXfFeEgGcs".contains(kind))
            .ok_or("Unsupported scanf format")?;
        at += 1;
        if b"sc".contains(&kind) && !length.is_empty()
            || b"fFeEgG".contains(&kind) && ![b"".as_slice(), b"l".as_slice()].contains(&length)
        {
            return Err("Unsupported scanf length".into());
        }
        if suppressed {
            continue;
        }
        let size = if b"sc".contains(&kind) {
            None
        } else if b"fFeEgG".contains(&kind) {
            Some(if length == b"l" { 8 } else { 4 })
        } else {
            Some(match length {
                b"hh" => 1,
                b"h" => 2,
                b"ll" => 8,
                _ => 4,
            })
        };
        let capacity = if size.is_none() {
            source.len().saturating_add(1).max(width)
        } else {
            8
        };
        bounded(capacity)?;
        fields.push(ScanField {
            size,
            kind,
            width,
            capacity,
        });
        if fields.len() > 16 {
            return Err("Too many scanf outputs".into());
        }
    }
    let source = CString::new(source).map_err(|_| "Invalid scanf source")?;
    let template = CString::new(template).map_err(|_| "Invalid scanf format")?;
    // u64 backing provides native alignment and room for host long and size_t.
    let mut buffers: Vec<Vec<u64>> = fields
        .iter()
        .map(|field| vec![0; field.capacity.div_ceil(8)])
        .collect();
    let mut pointers = [std::ptr::null_mut::<u64>(); 16];
    for (index, buffer) in buffers.iter_mut().enumerate() {
        pointers[index] = buffer.as_mut_ptr();
    }
    let p = pointers;
    let count = unsafe {
        libc::sscanf(
            source.as_ptr(),
            template.as_ptr(),
            p[0],
            p[1],
            p[2],
            p[3],
            p[4],
            p[5],
            p[6],
            p[7],
            p[8],
            p[9],
            p[10],
            p[11],
            p[12],
            p[13],
            p[14],
            p[15],
        )
    };
    let mut values = Vec::new();
    for (field, buffer) in fields.iter().zip(buffers).take(count.max(0) as usize) {
        let bytes: Vec<u8> = buffer.into_iter().flat_map(u64::to_ne_bytes).collect();
        let length = if let Some(size) = field.size {
            size
        } else if field.kind == b'c' {
            field.width
        } else {
            bytes
                .iter()
                .position(|&value| value == 0)
                .ok_or("Unterminated scanf output")?
                + 1
        };
        values.push(bytes[..length].to_vec());
    }
    Ok((count, values))
}
#[cfg(test)]
mod tests {
    use super::*;
    fn formatted(template: &[u8], words: &[u32]) -> Result<Vec<u8>> {
        format_bytes(
            template,
            0,
            &mut |i| {
                words
                    .get(i)
                    .copied()
                    .ok_or_else(|| "argument outside fixture".into())
            },
            &mut |address| {
                if address == 123 {
                    Ok("é".as_bytes().to_vec())
                } else {
                    Err("unexpected string address".into())
                }
            },
        )
    }
    #[test]
    fn decimal_float_prefix_updates_end_after_only_the_matched_number() {
        assert_eq!(parse_float_prefix(b"  -12.5tail"), (-12.5, 7));
        assert_eq!(parse_float_prefix(b"1e+oops"), (1.0, 1));
        assert_eq!(parse_float_prefix(b"0x1p4"), (0.0, 1));
        assert_eq!(parse_float_prefix(b"  words"), (0.0, 0));
    }
    #[test]
    fn infinity_and_nan_prefixes_match_the_guest_parser() {
        let (value, end) = parse_float_prefix(b"-INFINITYsuffix");
        assert!(value.is_infinite() && value.is_sign_negative());
        assert_eq!(end, 9);
        let (value, end) = parse_float_prefix(b" nan(payload)");
        assert!(value.is_nan());
        assert_eq!(end, 4);
    }
    #[test]
    fn ascii_character_helpers_preserve_python_classification() {
        assert_eq!(character("isalpha", b'Q' as u32), 1);
        assert_eq!(character("isspace", 28), 1);
        assert_eq!(character("isalpha", 233), 0);
        assert_eq!(character("ispunct", b'@' as u32), 1);
        assert_eq!(change_case(b'Q' as u32, true), b'q' as u32);
        assert_eq!(change_case(233, true), 233);
    }
    #[test]
    fn seeded_crc_and_lrand48_match_reference_sequences() {
        assert_eq!(crc32_bytes(b"123456789", 0), 0xcbf43926);
        assert_eq!(crc32_bytes(b"a", 0x12345678), 0xb67b731b);
        let mut state = 0x1234abcd330e;
        assert_eq!(
            [
                next_random(&mut state),
                next_random(&mut state),
                next_random(&mut state)
            ],
            [851401618, 1804928587, 758783491]
        );
    }
    #[test]
    fn formatting_preserves_signs_padding_percent_and_unicode_width() {
        assert_eq!(
            formatted(b"%04d %4s %%", &[(-7i32) as u32, 123]).unwrap(),
            "-007    é %".as_bytes()
        );
        assert_eq!(formatted(b"%#08x %#o", &[31, 8]).unwrap(), b"0x00001f 0o10");
        assert_eq!(formatted(b"%#.0x", &[0]).unwrap(), b"0x0");
    }
    #[test]
    fn formatting_aligns_promoted_doubles_and_accepts_unsigned_64_bits() {
        let bits = 1.5f64.to_bits();
        assert_eq!(
            formatted(
                b"%d %.1f",
                &[(-7i32) as u32, 0xdeadbeef, bits as u32, (bits >> 32) as u32]
            )
            .unwrap(),
            b"-7 1.5"
        );
        assert_eq!(
            formatted(b"%llu", &[u32::MAX, u32::MAX]).unwrap(),
            b"18446744073709551615"
        );
        assert_eq!(
            formatted(b"%*.2f", &[6, 0xdeadbeef, bits as u32, (bits >> 32) as u32]).unwrap(),
            b"  1.50"
        );
    }
    #[test]
    fn scanf_uses_real_native_conversion_and_returns_guest_sized_values() {
        let (count, values) = scan(b"42 -1.5 abcdef", b"%*d %lf %3s").unwrap();
        assert_eq!(count, 2);
        assert_eq!(
            values,
            vec![(-1.5f64).to_le_bytes().to_vec(), b"abc\0".to_vec()]
        );
        let (count, values) = scan(b"-12 65535", b"%ld %hu").unwrap();
        assert_eq!(count, 2);
        assert_eq!(
            values,
            vec![
                (-12i32).to_le_bytes().to_vec(),
                65535u16.to_le_bytes().to_vec()
            ]
        );
    }
    #[test]
    fn math_preserves_integer_rounding_and_python_domain_errors() {
        assert_eq!(python_math("round", -0.1, None).unwrap().to_bits(), 0);
        assert_eq!(python_math("rint", 2.5, None).unwrap(), 2.0);
        assert_eq!(python_math("rint", 3.5, None).unwrap(), 4.0);
        assert!(python_math("sqrt", -1.0, None).is_err());
        assert!(python_math("sin", f64::INFINITY, None).is_err());
        assert!(python_math("fmax", f64::NAN, Some(1.0)).unwrap().is_nan());
    }
    #[test]
    fn formatting_retains_python_integer_and_character_conversions() {
        assert_eq!(
            formatted(b"%lld", &[u32::MAX, u32::MAX]).unwrap(),
            b"18446744069414584319"
        );
        assert_eq!(formatted(b"%+x", &[31]).unwrap(), b"+1f");
        assert_eq!(formatted(b"%.0c", &[65]).unwrap(), b"A");
    }
    #[test]
    fn negative_nan_keeps_the_guest_sign_bit() {
        assert_eq!(
            parse_float_prefix(b"-NaN").0.to_bits(),
            0xfff8_0000_0000_0000
        );
    }
    #[test]
    fn guest_integer_conversion_wraps_clock_ticks_instead_of_saturating() {
        assert_eq!(guest_integer(5_000_000_000.0).unwrap(), 705032704);
        assert_eq!(guest_integer(-1.0).unwrap(), u32::MAX);
        assert_eq!(guest_integer(-0.75).unwrap(), 0);
        assert!(guest_integer(f64::INFINITY).is_err());
    }
    #[test]
    fn single_precision_conversion_refuses_finite_overflow() {
        assert!(single_bits(1e39).is_err());
        assert_eq!(single_bits(f64::INFINITY).unwrap(), f32::INFINITY.to_bits());
    }
    #[test]
    fn scanf_rejects_pointer_writes_and_unsupported_lengths() {
        assert!(scan(b"5", b"%n").is_err());
        assert!(scan(b"5", b"%ls").is_err());
        assert!(scan(b"5", b"%Lf").is_err());
    }
}
