use super::*;

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
fn math_preserves_integer_rounding_and_python_domain_errors() {
    assert_eq!(python_math("round", -0.1, None).unwrap().to_bits(), 0);
    assert_eq!(python_math("rint", 2.5, None).unwrap(), 2.0);
    assert_eq!(python_math("rint", 3.5, None).unwrap(), 4.0);
    assert!(python_math("sqrt", -1.0, None).is_err());
    assert!(python_math("sin", f64::INFINITY, None).is_err());
    assert!(python_math("fmax", f64::NAN, Some(1.0)).unwrap().is_nan());
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
