use super::super::PREFIX;
use super::*;
use crate::{ffi, machine::Return};
use std::cell::RefCell;

#[test]
fn deferred_callbacks_observe_the_new_clock_and_drain_in_the_same_frame() {
    struct Probe {
        host: *const Host,
        clocks: RefCell<Vec<u32>>,
    }
    unsafe extern "C" fn observe(
        user: ffi::Ptr,
        _call: ffi::Ptr,
        out: *mut ffi::NativeResult,
    ) -> i32 {
        let probe = unsafe { &*user.cast::<Probe>() };
        let host = unsafe { &*probe.host };
        let value = host
            .libc
            .get()
            .unwrap()
            .invoke("clock", &host.machine)
            .unwrap()
            .unwrap();
        let Return::Word(clock) = value else {
            return 1;
        };
        let first = probe.clocks.borrow().is_empty();
        probe.clocks.borrow_mut().push(clock);
        if first {
            host.jni().unwrap().deferred.borrow_mut().push((1, 0, 0, 0));
        }
        unsafe {
            (*out).value = 0;
            (*out).flags = 1;
        }
        0
    }
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let data = std::env::temp_dir().join(format!("doodle-deferred-{}", std::process::id()));
    let host = Host::new(
        &root.join("original"),
        &data,
        &root.join("runtime/bundle"),
        1700000000,
        false,
        None,
    )
    .unwrap();
    let probe = Probe {
        host: &*host,
        clocks: RefCell::new(vec![]),
    };
    host.machine
        .register_native(
            "test.clock",
            observe as *const () as usize,
            (&probe as *const Probe).cast_mut().cast(),
        )
        .unwrap();
    let send = host
        .machine
        .symbol(&format!("{PREFIX}sendMessageNative"))
        .unwrap();
    host.machine.intercept(send & !1, "test.clock").unwrap();
    host.jni().unwrap().deferred.borrow_mut().push((1, 0, 0, 0));
    host.step(0., &[], 0.05).unwrap();
    assert_eq!(*probe.clocks.borrow(), vec![50000, 50000]);
    drop(host);
    std::fs::remove_dir_all(data).unwrap();
}

#[test]
fn bulk_memory_imports_do_not_execute_a_guest_loop_for_each_byte() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let data = std::env::temp_dir().join(format!("doodle-bulk-{}", std::process::id()));
    let host = Host::new(
        &root.join("original"),
        &data,
        &root.join("runtime/bundle"),
        1700000000,
        false,
        None,
    )
    .unwrap();
    let m = &host.machine;
    let buffer = m.malloc(32768).unwrap();
    let original: Vec<u8> = (0..8192).map(|i| (i % 251) as u8).collect();
    for name in [
        "memcpy",
        "memmove",
        "__aeabi_memcpy",
        "__aeabi_memmove",
        "memset",
        "__aeabi_memset",
    ] {
        m.write(buffer, &original).unwrap();
        let destination = buffer + if name.contains("memmove") { 1 } else { 16384 };
        let (args, expected) = if name == "memset" {
            ([destination, 0x19f, 8192], vec![0x9f; 8192])
        } else if name == "__aeabi_memset" {
            ([destination, 8192, 0x19f], vec![0x9f; 8192])
        } else {
            ([destination, buffer, 8192], original.clone())
        };
        let address = m.import_address(name).unwrap();
        assert_eq!(unsafe { ffi::dispatch_begin_call(m.dispatch.get(), 64) }, 0);
        let result = m.call(address, &args);
        unsafe { ffi::dispatch_end_call(m.dispatch.get()) };
        assert_eq!(
            result.unwrap_or_else(|e| panic!("{name}: {e}")),
            destination
        );
        assert_eq!(m.read(destination, 8192).unwrap(), expected, "{name}");
    }
    drop(host);
    std::fs::remove_dir_all(data).unwrap();
}

#[test]
fn input_numbers_reject_wrong_types_and_out_of_range_values() {
    for value in [
        json!(true),
        json!(null),
        json!("0.5"),
        json!(-1.01),
        json!(1.01),
    ] {
        assert!(number(&value, -1., 1.).is_err());
    }
    assert_eq!(number(&json!(0.123456789), -1., 1.).unwrap(), 0.123456789);
}

#[test]
fn frame_flags_do_not_coerce_json_values() {
    assert!(boolean(Some(&json!(1)), false).is_err());
    assert!(!boolean(None, false).unwrap());
    assert!(boolean(Some(&json!(true)), false).unwrap());
}

#[test]
fn packet_framing_uses_payload_bytes_without_text_conversion() {
    let mut out = vec![];
    packet(&mut out, b"GPF1", &[0, 128, 255]).unwrap();
    assert_eq!(
        out,
        [b"GPF1".as_slice(), &[3, 0, 0, 0, 0, 128, 255]].concat()
    );
}
