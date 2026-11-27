use crate::{Result, machine::Machine};

pub(super) fn parse_float_prefix(bytes: &[u8]) -> (f64, usize) {
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

pub(super) fn change_case(value: u32, lower: bool) -> u32 {
    if lower && (65..=90).contains(&value) {
        value + 32
    } else if !lower && (97..=122).contains(&value) {
        value - 32
    } else {
        value
    }
}

pub(super) fn character(name: &str, value: u32) -> u32 {
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

pub(super) fn crc32_bytes(bytes: &[u8], seed: u32) -> u32 {
    const TABLE: [u32; 256] = crc_table();
    let mut value = !seed;
    for &byte in bytes {
        value = TABLE[((value ^ u32::from(byte)) & 255) as usize] ^ (value >> 8);
    }
    !value
}

pub(super) fn next_random(state: &mut u64) -> u32 {
    *state = state.wrapping_mul(0x5deece66d).wrapping_add(11) & ((1 << 48) - 1);
    (*state >> 17) as u32
}

pub(super) fn float_arg(m: &Machine, index: usize, single: bool) -> Result<f64> {
    Ok(if single {
        f64::from(f32::from_bits(m.arg(index)?))
    } else {
        f64::from_bits(u64::from(m.arg(index)?) | (u64::from(m.arg(index + 1)?) << 32))
    })
}

pub(super) fn guest_integer(value: f64) -> Result<u32> {
    if !value.is_finite() {
        return Err("Invalid guest integer conversion".into());
    }
    Ok(value.trunc().rem_euclid(4294967296.0) as u32)
}
pub(super) fn single_bits(value: f64) -> Result<u32> {
    let result = value as f32;
    if value.is_finite() && result.is_infinite() {
        return Err("Single precision math overflow".into());
    }
    Ok(result.to_bits())
}

pub(super) fn math_name(name: &str) -> Option<(&str, bool)> {
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

pub(super) fn python_math(name: &str, x: f64, y: Option<f64>) -> Result<f64> {
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

#[cfg(test)]
#[path = "../../tests/unit/libc/scalars.rs"]
mod tests;
