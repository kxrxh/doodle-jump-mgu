use super::{LIMIT, address, bounded};
use crate::{Result, machine::Machine};
use std::ffi::CString;

pub(super) fn format(
    m: &Machine,
    template: &[u8],
    start: usize,
    pointer: Option<u32>,
) -> Result<Vec<u8>> {
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

pub(super) fn scan(source: &[u8], template: &[u8]) -> Result<(i32, Vec<Vec<u8>>)> {
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
#[path = "../../tests/unit/libc/formatting.rs"]
mod tests;
