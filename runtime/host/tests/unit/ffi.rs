use super::*;

#[test]
fn guest_api_registers_match_the_native_bridge() {
    let mut api = GuestApi::default();
    unsafe { guest_get_api(&mut api) };
    assert_eq!(api.registers, [66, 67, 68, 69, 12, 10, 11]);
}
