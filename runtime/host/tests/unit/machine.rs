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
