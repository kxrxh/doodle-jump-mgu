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
fn formatting_retains_python_integer_and_character_conversions() {
    assert_eq!(
        formatted(b"%lld", &[u32::MAX, u32::MAX]).unwrap(),
        b"18446744069414584319"
    );
    assert_eq!(formatted(b"%+x", &[31]).unwrap(), b"+1f");
    assert_eq!(formatted(b"%.0c", &[65]).unwrap(), b"A");
}
#[test]
fn scanf_rejects_pointer_writes_and_unsupported_lengths() {
    assert!(scan(b"5", b"%n").is_err());
    assert!(scan(b"5", b"%ls").is_err());
    assert!(scan(b"5", b"%Lf").is_err());
}
