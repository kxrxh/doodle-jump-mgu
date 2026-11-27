use super::*;
use serde_json::{Value, json};
use std::{
    io::Read,
    os::{fd::AsRawFd, unix::net::UnixStream},
};
fn pair() -> (Output, UnixStream, UnixStream) {
    let (writer, reader) = UnixStream::pair().unwrap();
    reader.set_nonblocking(true).unwrap();
    let output = Output::new(writer.as_raw_fd()).unwrap();
    (output, writer, reader)
}
fn event(out: &mut Output, value: Value) {
    let body = serde_json::to_vec(&value).unwrap();
    out.write_all(b"EVT1").unwrap();
    out.write_all(&(body.len() as u32).to_le_bytes()).unwrap();
    out.write_all(&body).unwrap();
    out.flush().unwrap();
}
fn read_available(reader: &mut UnixStream, wire: &mut Vec<u8>) {
    let mut data = [0; 32768];
    loop {
        match reader.read(&mut data) {
            Ok(0) => break,
            Ok(n) => wire.extend_from_slice(&data[..n]),
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
            Err(e) => panic!("{e}"),
        }
    }
}
fn drain(out: &mut Output, reader: &mut UnixStream) -> Vec<u8> {
    let mut wire = Vec::new();
    for _ in 0..2000 {
        out.pump().unwrap();
        read_available(reader, &mut wire);
        if !out.wants_write() {
            return wire;
        }
    }
    panic!("output did not drain")
}
fn packets(wire: &[u8]) -> Vec<([u8; 4], Vec<u8>)> {
    let mut result = Vec::new();
    let mut offset = 0;
    while offset < wire.len() {
        assert!(wire.len() - offset >= 8);
        let kind = wire[offset..offset + 4].try_into().unwrap();
        let size = u32::from_le_bytes(wire[offset + 4..offset + 8].try_into().unwrap()) as usize;
        offset += 8;
        assert!(size <= wire.len() - offset);
        result.push((kind, wire[offset..offset + size].to_vec()));
        offset += size;
    }
    result
}
fn raw_frame() -> Vec<u8> {
    let mut body = Vec::with_capacity(8 + 408 * 720 * 4);
    body.extend(408u32.to_le_bytes());
    body.extend(720u32.to_le_bytes());
    body.resize(8 + 408 * 720 * 4, 0x57);
    body
}
fn delta(rgba: Option<[u8; 4]>) -> Vec<u8> {
    let mut body = Vec::new();
    for word in [408u32, 720, u32::from(rgba.is_some())] {
        body.extend(word.to_le_bytes());
    }
    if let Some(rgba) = rgba {
        for word in [0u32, 0, 1, 1] {
            body.extend(word.to_le_bytes());
        }
        body.extend(rgba);
    }
    body
}
fn small_send_buffer(fd: RawFd) {
    let capacity = 4096i32;
    assert_eq!(
        unsafe {
            libc::setsockopt(
                fd,
                libc::SOL_SOCKET,
                libc::SO_SNDBUF,
                (&capacity as *const i32).cast(),
                std::mem::size_of::<i32>() as libc::socklen_t,
            )
        },
        0
    );
}
#[test]
fn an_unread_store_keeps_latest_draws_after_critical_events() {
    let (mut out, _writer, mut reader) = pair();
    event(&mut out, json!({"type":"ready"}));
    for frame in 0..10000 {
        if frame == 5000 {
            event(&mut out, json!({"type":"lifecycle","running":false}));
        }
        if frame == 7500 {
            event(
                &mut out,
                json!({"type":"dialog","id":7,"payload":{"type":"draw"}}),
            );
        }
        event(&mut out, json!({"type":"draw","frame":frame}));
        event(&mut out, json!({"type":"presented","frame":frame}));
    }
    assert!(out.wants_write());
    assert!(out.can_accept_frame());
    let frames = packets(&drain(&mut out, &mut reader));
    let values = frames
        .iter()
        .map(|(kind, body)| {
            assert_eq!(kind, b"EVT1");
            serde_json::from_slice::<Value>(body).unwrap()
        })
        .collect::<Vec<_>>();
    assert_eq!(
        values,
        [
            json!({"type":"ready"}),
            json!({"type":"lifecycle","running":false}),
            json!({"type":"dialog","id":7,"payload":{"type":"draw"}}),
            json!({"type":"draw","frame":9999}),
            json!({"type":"presented","frame":9999})
        ]
    );
}
#[test]
fn partial_cpu_packets_hold_admission_and_keep_every_byte() {
    let (mut out, writer, mut reader) = pair();
    small_send_buffer(writer.as_raw_fd());
    assert_ne!(
        unsafe { libc::fcntl(writer.as_raw_fd(), libc::F_GETFL) } & libc::O_NONBLOCK,
        0
    );
    let body = raw_frame();
    let allocation = body.as_ptr();
    let expected = body.clone();
    out.packet(b"FRM1", body).unwrap();
    assert!(!out.can_accept_frame());
    let Data::Parts { body, .. } = &out.queue.front().unwrap().data else {
        panic!("owned frame expected")
    };
    assert_eq!(
        body.as_ptr(),
        allocation,
        "CPU frame allocation is moved, not copied"
    );
    out.pump().unwrap();
    assert!(out.wants_write());
    assert!(!out.can_accept_frame());
    let mut wire = Vec::new();
    let mut prefix = [0; 13];
    let size = reader.read(&mut prefix).unwrap();
    wire.extend_from_slice(&prefix[..size]);
    wire.extend(drain(&mut out, &mut reader));
    let frames = packets(&wire);
    assert_eq!(frames, vec![(*b"FRM1", expected)]);
    assert!(out.can_accept_frame());
}
#[test]
fn a_started_draw_and_incremental_cpu_frames_are_not_dropped() {
    let (mut out, writer, mut reader) = pair();
    small_send_buffer(writer.as_raw_fd());
    event(
        &mut out,
        json!({"type":"draw","frame":0,"padding":"x".repeat(1<<20)}),
    );
    out.pump().unwrap();
    event(&mut out, json!({"type":"lifecycle","running":false}));
    event(&mut out, json!({"type":"draw","frame":1}));
    event(&mut out, json!({"type":"draw","frame":2}));
    let first = delta(None);
    let second = delta(Some([1, 2, 3, 4]));
    out.packet(b"FRM2", first.clone()).unwrap();
    out.packet(b"FRM2", second.clone()).unwrap();
    assert!(!out.can_accept_frame());
    let frames = packets(&drain(&mut out, &mut reader));
    assert_eq!(frames.len(), 5);
    assert_eq!(
        serde_json::from_slice::<Value>(&frames[0].1).unwrap()["frame"],
        0
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&frames[1].1).unwrap()["type"],
        "lifecycle"
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&frames[2].1).unwrap()["frame"],
        2
    );
    assert_eq!(frames[3], (*b"FRM2", first));
    assert_eq!(frames[4], (*b"FRM2", second));
}
#[test]
fn rejects_bad_packets_without_losing_committed_events() {
    let (mut out, _writer, mut reader) = pair();
    event(&mut out, json!({"type":"ready"}));
    assert!(out.packet(b"GPF1", vec![]).is_err());
    assert!(out.packet(b"EVT1", b"not json".to_vec()).is_err());
    assert!(out.packet(b"TEX1", vec![0; 12]).is_err());
    assert!(out.packet(b"FRM1", vec![0; 8]).is_err());
    let mut malformed = delta(None);
    malformed[8..12].copy_from_slice(&65u32.to_le_bytes());
    assert!(out.packet(b"FRM2", malformed).is_err());
    out.write_all(b"EVT1").unwrap();
    out.write_all(&10u32.to_le_bytes()).unwrap();
    out.write_all(b"{}").unwrap();
    assert!(out.flush().is_err());
    let mut bad_header = b"EVT1".to_vec();
    bad_header.extend(u32::MAX.to_le_bytes());
    assert!(out.write_all(&bad_header).is_err());
    event(&mut out, json!({"type":"error","message":"controlled"}));
    let frames = packets(&drain(&mut out, &mut reader));
    assert_eq!(frames.len(), 2);
    assert_eq!(
        serde_json::from_slice::<Value>(&frames[0].1).unwrap()["type"],
        "ready"
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&frames[1].1).unwrap()["type"],
        "error"
    );
}
#[test]
fn critical_event_count_and_large_texture_queue_are_bounded() {
    let (mut out, _writer, _reader) = pair();
    for id in 0..128 {
        out.packet(
            b"EVT1",
            serde_json::to_vec(&json!({"type":"critical","id":id})).unwrap(),
        )
        .unwrap();
    }
    assert!(
        out.packet(b"EVT1", br#"{"type":"critical"}"#.to_vec())
            .is_err()
    );
    let (mut out, _writer, _reader) = pair();
    let mut texture = Vec::new();
    for word in [2u32, 2048, 2048] {
        texture.extend(word.to_le_bytes());
    }
    texture.resize(12 + 2048 * 2048 * 4, 0);
    for _ in 0..3 {
        out.packet(b"TEX1", texture.clone()).unwrap();
    }
    assert!(!out.can_accept_frame());
    assert!(out.packet(b"TEX1", texture).is_err());
}
#[test]
fn a_closed_store_reports_broken_pipe_instead_of_waiting() {
    let (mut out, _writer, reader) = pair();
    drop(reader);
    event(&mut out, json!({"type":"ready"}));
    assert_eq!(out.pump().unwrap_err().kind(), io::ErrorKind::BrokenPipe);
}
#[test]
fn one_pump_cannot_starve_the_actor_with_more_than_one_mebibyte() {
    let path = std::env::temp_dir().join(format!("dj-output-budget-{}", std::process::id()));
    let file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .unwrap();
    let mut out = Output::new(file.as_raw_fd()).unwrap();
    let body = raw_frame();
    let size = body.len() + 8;
    out.packet(b"FRM1", body).unwrap();
    out.pump().unwrap();
    assert_eq!(file.metadata().unwrap().len(), 1 << 20);
    assert!(out.wants_write());
    assert!(!out.can_accept_frame());
    out.pump().unwrap();
    assert_eq!(file.metadata().unwrap().len(), size as u64);
    assert!(!out.wants_write());
    assert!(out.can_accept_frame());
    std::fs::remove_file(path).unwrap();
}
