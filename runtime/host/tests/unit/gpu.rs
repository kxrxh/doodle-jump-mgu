use super::*;
use serde_json::json;
use std::{
    io::{Read, Write},
    os::unix::net::{UnixListener, UnixStream},
    sync::atomic::{AtomicUsize, Ordering},
    time::Duration,
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
const BIND: u32 = 0x444a0002;
const VIEW: u32 = 0x444a0003;
const IMAGE: u32 = 0x444a0004;
const CURSOR: u32 = 0x444a0005;
struct Harness {
    path: PathBuf,
    listener: UnixListener,
    worker: Worker,
    now: Instant,
}
impl Harness {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "dj-gpu-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let listener = UnixListener::bind(&path).unwrap();
        listener.set_nonblocking(true).unwrap();
        let worker = Worker::new(path.clone());
        Self {
            path,
            listener,
            worker,
            now: Instant::now(),
        }
    }
    fn connect(&mut self) -> UnixStream {
        assert!(
            self.worker
                .poll(self.now)
                .iter()
                .any(|event| matches!(event, Event::Connected))
        );
        assert!(self.worker.ready());
        let (peer, _) = self.listener.accept().unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
        peer
    }
    fn bind(&mut self, peer: &mut UnixStream, native: bool) {
        assert!(
            self.worker
                .bind(
                    "/run/mgu-render/render.sock",
                    [-9000, -10000, 1920, 720],
                    self.now
                )
                .unwrap()
        );
        self.worker.poll(self.now);
        loop {
            let (kind, body) = message(peer);
            if kind == BIND {
                let mut expected = Vec::new();
                for n in [-9000i32, -10000, 1920, 720] {
                    expected.extend(n.to_le_bytes());
                }
                expected.extend(b"/run/mgu-render/render.sock\0");
                assert_eq!(body, expected);
                break;
            }
        }
        let events = self.reply(peer, json!({"event":"bind","native":native}));
        assert!(
            events
                .iter()
                .any(|event| matches!(event,Event::Bound(value) if *value==native))
        );
    }
    fn reply(&mut self, peer: &mut UnixStream, value: Value) -> Vec<Event> {
        peer.write_all(format!("{value}\n").as_bytes()).unwrap();
        self.worker.poll(self.now)
    }
    fn send(&mut self, peer: &mut UnixStream) {
        assert!(self.worker.submit_frame(scene(), self.now).unwrap());
        assert!(self.worker.pending());
        assert!(!self.worker.submit_frame(scene(), self.now).unwrap());
        self.worker.poll(self.now);
        let mut data = vec![0; scene().len() + 8];
        peer.read_exact(&mut data).unwrap();
        assert_eq!(&data[..scene().len()], scene());
        assert_eq!(&data[scene().len()..], &[1, 0, 74, 68, 0, 0, 0, 0]);
    }
}
impl Drop for Harness {
    fn drop(&mut self) {
        self.worker.close();
        let _ = std::fs::remove_file(&self.path);
    }
}
fn message(peer: &mut UnixStream) -> (u32, Vec<u8>) {
    let mut header = [0; 8];
    peer.read_exact(&mut header).unwrap();
    let kind = u32::from_le_bytes(header[..4].try_into().unwrap());
    let mut body = vec![0; u32::from_le_bytes(header[4..].try_into().unwrap()) as usize];
    peer.read_exact(&mut body).unwrap();
    (kind, body)
}
fn scene() -> Vec<u8> {
    let mut bytes = Vec::new();
    for value in [0x4d520008u32, 28, 408, 720, 1] {
        bytes.extend(value.to_le_bytes());
    }
    for value in [0.1f32, 0.2, 0.3, 1.] {
        bytes.extend(value.to_le_bytes());
    }
    bytes
}
fn video(sequence: u32, path: &str) -> Value {
    json!({"path":path,"width":408,"height":720,"sequence":sequence})
}
fn frame(events: &[Event]) -> &Value {
    events
        .iter()
        .find_map(|event| {
            if let Event::Frame(value) = event {
                Some(value)
            } else {
                None
            }
        })
        .expect("frame completion")
}
fn no_bytes(peer: &mut UnixStream) {
    peer.set_nonblocking(true).unwrap();
    let mut byte = [0];
    assert_eq!(
        peer.read(&mut byte).unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
    peer.set_nonblocking(false).unwrap();
}
#[test]
fn sends_a_scene_then_end_and_waits_for_fragmented_completion() {
    let mut h = Harness::new();
    let mut peer = h.connect();
    assert_eq!(h.worker.generation(), 1);
    h.send(&mut peer);
    let response = format!("{}\n", video(1, "/dev/shm/mgu-video-doodle-test"));
    peer.write_all(&response.as_bytes()[..13]).unwrap();
    assert!(h.worker.poll(h.now).is_empty());
    assert!(h.worker.pending());
    peer.write_all(&response.as_bytes()[13..]).unwrap();
    let events = h.worker.poll(h.now);
    assert_eq!(frame(&events)["sequence"], 1);
    assert!(!h.worker.pending());
    assert!(h.worker.ready());
}
#[test]
fn replays_cursor_and_view_and_preserves_sequence_across_presentation_modes() {
    let mut h = Harness::new();
    h.worker
        .set_cursor_image(1, 1, vec![255, 0, 0, 255])
        .unwrap();
    h.worker.cursor([180., 295., 1., 2.], true).unwrap();
    let mut peer = h.connect();
    h.bind(&mut peer, true);
    assert!(h.worker.native());
    h.worker.poll(h.now);
    let packets = [message(&mut peer), message(&mut peer), message(&mut peer)];
    assert_eq!(
        packets.iter().map(|p| p.0).collect::<Vec<_>>(),
        [VIEW, IMAGE, CURSOR]
    );
    assert_eq!(&packets[1].1[8..], &[255, 0, 0, 255]);
    assert_eq!(&packets[0].1[16..], &0i32.to_le_bytes());
    h.worker.view([756, 0, 408, 720], true).unwrap();
    h.worker.view([756, 0, 408, 720], true).unwrap();
    h.worker.poll(h.now);
    assert_eq!(message(&mut peer).0, VIEW);
    no_bytes(&mut peer);
    h.send(&mut peer);
    let events=h.reply(&mut peer,json!({"mode":"native","width":408,"height":720,"sequence":1,"presented":false,"timestamp":1234}));
    assert_eq!(frame(&events)["presented"], false);
    h.bind(&mut peer, false);
    assert!(!h.worker.native());
    assert!(h.worker.ready());
    h.send(&mut peer);
    let events = h.reply(&mut peer, video(2, "/dev/shm/mgu-video-doodle-new"));
    assert_eq!(frame(&events)["sequence"], 2);
}
#[test]
fn rejects_unsafe_or_replaced_video_paths_and_bad_sequences() {
    for reply in [
        video(1, "/etc/passwd"),
        video(2, "/dev/shm/mgu-video-doodle-test"),
        json!({"mode":"native","width":408,"height":720,"sequence":1,"presented":true,"timestamp":1}),
    ] {
        let mut h = Harness::new();
        let mut peer = h.connect();
        h.send(&mut peer);
        let events = h.reply(&mut peer, reply);
        assert!(
            events
                .iter()
                .any(|event| matches!(event, Event::Disconnected(_)))
        );
        assert!(!h.worker.ready());
        assert!(!h.worker.pending());
    }
    let mut h = Harness::new();
    let mut peer = h.connect();
    h.send(&mut peer);
    h.reply(&mut peer, video(1, "/dev/shm/mgu-video-doodle-first"));
    h.send(&mut peer);
    let events = h.reply(&mut peer, video(2, "/dev/shm/mgu-video-doodle-other"));
    assert!(
        events
            .iter()
            .any(|event| matches!(event, Event::Disconnected(_)))
    );
}
#[test]
fn validates_native_completion_fields_and_stacked_replies() {
    let mut h = Harness::new();
    let mut peer = h.connect();
    h.bind(&mut peer, true);
    h.worker.poll(h.now);
    message(&mut peer);
    h.send(&mut peer);
    let events = h.reply(
        &mut peer,
        json!({"mode":"native","width":408,"height":720,"sequence":1,"timestamp":1}),
    );
    assert!(
        events
            .iter()
            .any(|event| matches!(event, Event::Disconnected(_)))
    );
    let mut h = Harness::new();
    let mut peer = h.connect();
    h.send(&mut peer);
    let reply = format!(
        "{}\n{}\n",
        video(1, "/dev/shm/mgu-video-doodle-a"),
        video(2, "/dev/shm/mgu-video-doodle-a")
    );
    peer.write_all(reply.as_bytes()).unwrap();
    assert!(
        h.worker
            .poll(h.now)
            .iter()
            .any(|event| matches!(event, Event::Disconnected(_)))
    );
}
#[test]
fn bounds_acknowledgements_and_reconnects_after_disconnect() {
    let mut h = Harness::new();
    let mut peer = h.connect();
    peer.write_all(&vec![b'A'; 4097]).unwrap();
    assert!(
        h.worker
            .poll(h.now)
            .iter()
            .any(|event| matches!(event, Event::Disconnected(_)))
    );
    assert!(h.worker.last_error().unwrap().contains("large"));
    assert!(
        h.worker
            .poll(h.now + Duration::from_millis(2999))
            .is_empty()
    );
    assert!(
        h.worker
            .poll(h.now + Duration::from_secs(3))
            .iter()
            .any(|event| matches!(event, Event::Connected))
    );
    assert_eq!(h.worker.generation(), 2);
    let (mut peer, _) = h.listener.accept().unwrap();
    h.now += Duration::from_secs(3);
    h.send(&mut peer);
    let events = h.reply(&mut peer, video(1, "/dev/shm/mgu-video-doodle-reconnected"));
    assert_eq!(frame(&events)["sequence"], 1);
}
#[test]
fn times_out_pending_operations_and_stop_cancels_reconnect() {
    for bind in [false, true] {
        let mut h = Harness::new();
        let mut peer = h.connect();
        if bind {
            assert!(
                h.worker
                    .bind(
                        "/run/mgu-render/render.sock",
                        [-9000, -10000, 1920, 720],
                        h.now
                    )
                    .unwrap()
            );
            h.worker.poll(h.now);
            message(&mut peer);
        } else {
            h.send(&mut peer);
        }
        let limit = if bind { 3000 } else { 5000 };
        assert!(
            h.worker
                .poll(h.now + Duration::from_millis(limit - 1))
                .is_empty()
        );
        assert!(
            h.worker
                .poll(h.now + Duration::from_millis(limit))
                .iter()
                .any(|event| matches!(event, Event::Disconnected(_)))
        );
        assert!(!h.worker.pending());
        h.worker.close();
        assert!(h.worker.fd().is_none());
        assert!(h.worker.poll(h.now + Duration::from_secs(60)).is_empty());
    }
}
#[test]
fn invalid_commands_fail_before_socket_io() {
    let mut worker = Worker::new(PathBuf::from("/missing"));
    assert!(
        worker
            .bind("relative", [0, 0, 1920, 720], Instant::now())
            .is_err()
    );
    assert!(
        worker
            .bind("/run/render.sock", [0, 0, 0, 720], Instant::now())
            .is_err()
    );
    assert!(worker.view([-1, 0, 408, 720], true).is_err());
    assert!(worker.cursor([f32::NAN, 0., 0., 0.], false).is_err());
    assert!(worker.set_cursor_image(1, 1, vec![0; 3]).is_err());
    assert!(worker.submit_frame(vec![0; 8], Instant::now()).is_err());
    let mut bad = scene();
    bad[20..24].copy_from_slice(&f32::NAN.to_le_bytes());
    assert!(worker.submit_frame(bad, Instant::now()).is_err());
    let rect = challenge(1920, 720).unwrap();
    assert!((-73727..=-8192).contains(&rect[0]));
    assert!((-73727..=-8192).contains(&rect[1]));
    assert_eq!(&rect[2..], &[1920, 720]);
}
#[test]
fn backpressure_keeps_the_scene_allocation_and_coalesces_control_changes() {
    let mut h = Harness::new();
    let mut peer = h.connect();
    h.bind(&mut peer, true);
    h.worker.poll(h.now);
    message(&mut peer);
    let capacity = 4096i32;
    assert_eq!(
        unsafe {
            libc::setsockopt(
                h.worker.fd().unwrap(),
                libc::SOL_SOCKET,
                libc::SO_SNDBUF,
                (&capacity as *const i32).cast(),
                std::mem::size_of::<i32>() as libc::socklen_t,
            )
        },
        0
    );
    let mut body = Vec::new();
    for value in [7u32, 512, 512] {
        body.extend(value.to_le_bytes());
    }
    body.resize(12 + 512 * 512 * 4, 255);
    let mut data = packet(0x4d520007, body);
    data.extend(scene());
    let allocation = data.as_ptr();
    assert!(h.worker.submit_frame(data, h.now).unwrap());
    assert_eq!(
        h.worker.output.front().unwrap().bytes.slice().as_ptr(),
        allocation,
        "native scene allocation is moved, not copied"
    );
    h.worker.poll(h.now);
    assert!(h.worker.wants_write());
    for index in 0..10000 {
        h.worker
            .cursor([(index % 1000) as f32, 295., 1., 2.], true)
            .unwrap();
        h.worker.view([index % 1000, 0, 408, 720], true).unwrap();
    }
    assert!(
        h.worker.output.len() <= 4,
        "a slow GPU must not build up stale controls"
    );
    assert!(h.worker.queued_bytes < 2 << 20);
}
#[test]
fn reset_drops_the_bound_peer_and_replays_cached_cursor_after_reconnect() {
    let mut h = Harness::new();
    h.worker
        .set_cursor_image(1, 1, vec![255, 0, 0, 255])
        .unwrap();
    h.worker.cursor([180., 295., 1., 2.], true).unwrap();
    let mut peer = h.connect();
    h.bind(&mut peer, true);
    for _ in 0..3 {
        message(&mut peer);
    }
    assert!(h.worker.submit_frame(scene(), h.now).unwrap());
    h.worker.reset(h.now, "renderer restore failed");
    assert!(!h.worker.ready());
    assert!(!h.worker.native());
    assert!(!h.worker.pending());
    assert!(h.worker.fd().is_none());
    assert!(!h.worker.wants_write());
    assert_eq!(h.worker.last_error(), Some("renderer restore failed"));
    let mut byte = [0];
    assert_eq!(peer.read(&mut byte).unwrap(), 0);
    assert!(
        h.worker
            .poll(h.now + Duration::from_millis(2999))
            .is_empty()
    );
    assert!(
        h.worker
            .poll(h.now + Duration::from_secs(3))
            .iter()
            .any(|event| matches!(event, Event::Connected))
    );
    assert_eq!(h.worker.generation(), 2);
    assert!(h.worker.last_error().is_none());
    let (mut peer, _) = h.listener.accept().unwrap();
    peer.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
    h.now += Duration::from_secs(3);
    h.bind(&mut peer, true);
    let packets = [message(&mut peer), message(&mut peer), message(&mut peer)];
    assert_eq!(
        packets.iter().map(|packet| packet.0).collect::<Vec<_>>(),
        [VIEW, IMAGE, CURSOR]
    );
    assert_eq!(&packets[1].1[8..], &[255, 0, 0, 255]);
    assert_eq!(&packets[2].1[16..], &1u32.to_le_bytes());
    h.worker.close();
    h.worker.reset(h.now, "late reset");
    assert!(h.worker.poll(h.now + Duration::from_secs(60)).is_empty());
}
#[test]
fn a_premature_acknowledgement_cannot_release_an_incomplete_frame() {
    let mut h = Harness::new();
    let mut peer = h.connect();
    let capacity = 4096i32;
    assert_eq!(
        unsafe {
            libc::setsockopt(
                h.worker.fd().unwrap(),
                libc::SOL_SOCKET,
                libc::SO_SNDBUF,
                (&capacity as *const i32).cast(),
                std::mem::size_of::<i32>() as libc::socklen_t,
            )
        },
        0
    );
    let mut texture = Vec::new();
    for value in [7u32, 512, 512] {
        texture.extend(value.to_le_bytes());
    }
    texture.resize(12 + 512 * 512 * 4, 255);
    let mut data = packet(0x4d520007, texture);
    data.extend(scene());
    assert!(h.worker.submit_frame(data, h.now).unwrap());
    h.worker.poll(h.now);
    assert!(h.worker.wants_write());
    let events = h.reply(&mut peer, video(1, "/dev/shm/mgu-video-doodle-early"));
    assert!(
        events
            .iter()
            .any(|event| matches!(event, Event::Disconnected(_)))
    );
    assert!(!events.iter().any(|event| matches!(event, Event::Frame(_))));
}
#[test]
fn sequence_wrap_and_late_worker_start_preserve_completion_order() {
    let path = std::env::temp_dir().join(format!(
        "dj-gpu-late-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    let now = Instant::now();
    let mut worker = Worker::new(path.clone());
    assert!(
        worker
            .poll(now)
            .iter()
            .any(|event| matches!(event, Event::Disconnected(_)))
    );
    let listener = UnixListener::bind(&path).unwrap();
    assert!(worker.poll(now + Duration::from_millis(2999)).is_empty());
    assert!(
        worker
            .poll(now + Duration::from_secs(3))
            .iter()
            .any(|event| matches!(event, Event::Connected))
    );
    let (mut peer, _) = listener.accept().unwrap();
    worker.sequence = u32::MAX;
    assert!(
        worker
            .submit_frame(scene(), now + Duration::from_secs(3))
            .unwrap()
    );
    worker.poll(now + Duration::from_secs(3));
    let mut wire = vec![0; scene().len() + 8];
    peer.read_exact(&mut wire).unwrap();
    peer.write_all(format!("{}\n", video(1, "/dev/shm/mgu-video-doodle-wrap")).as_bytes())
        .unwrap();
    assert_eq!(
        frame(&worker.poll(now + Duration::from_secs(3)))["sequence"],
        1
    );
    worker.close();
    std::fs::remove_file(path).unwrap();
}
#[test]
fn gpu_stream_validation_preserves_texture_and_triangle_boundaries() {
    let mut texture = Vec::new();
    for value in [7u32, 2, 2] {
        texture.extend(value.to_le_bytes());
    }
    texture.extend([255u8; 16]);
    let mut data = packet(0x4d520007, texture);
    data.extend(scene());
    let mut removed = Vec::new();
    for value in [7u32, 0, 0] {
        removed.extend(value.to_le_bytes());
    }
    data.extend(packet(0x4d520007, removed));
    assert!(validate_scene(&data).is_ok());
    let mut triangle = Vec::new();
    for value in [408u32, 720, 2, 7, 0, 1, 0, 0, 3] {
        triangle.extend(value.to_le_bytes());
    }
    for value in [
        0f32, 0., 0., 0., 1., 1., 1., 1., 1., 0., 1., 0., 1., 1., 1., 1., 0., 1., 0., 1., 1., 1.,
        1., 1.,
    ] {
        triangle.extend(value.to_le_bytes());
    }
    let draw = packet(0x4d520008, triangle);
    assert!(validate_scene(&draw).is_ok());
    for (offset, value) in [
        (24, 16u32),
        (28, 999),
        (32, 999),
        (36, 3),
        (40, 4),
        (44, f32::NAN.to_bits()),
    ] {
        let mut bad = draw.clone();
        bad[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
        assert!(validate_scene(&bad).is_err());
    }
    let mut repeated = scene();
    repeated.extend(scene());
    assert!(validate_scene(&repeated).is_err());
    let mut truncated = scene();
    truncated.pop();
    assert!(validate_scene(&truncated).is_err());
}
