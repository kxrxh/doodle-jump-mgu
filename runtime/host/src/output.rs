//! Bounded stdout transport; slow Store callbacks cannot block the native actor.
use serde_json::Value;
use std::{
    collections::VecDeque,
    io::{self, Write},
    os::fd::RawFd,
};

const MAX_BODY: usize = 2048 * 2048 * 4 + 12;
const MAX_PACKET: usize = MAX_BODY + 8;
const MAX_QUEUE: usize = 64 << 20;
const FRAME_ADMISSION: usize = 8 << 20;
const IO_BUDGET: usize = 1 << 20;
const MAX_CRITICAL: usize = 128;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Replace {
    Draw,
    Presented,
}
enum Data {
    Wire(Vec<u8>),
    Parts { header: [u8; 8], body: Vec<u8> },
}
impl Data {
    fn len(&self) -> usize {
        match self {
            Self::Wire(bytes) => bytes.len(),
            Self::Parts { body, .. } => 8 + body.len(),
        }
    }
    fn memory(&self) -> usize {
        match self {
            Self::Wire(bytes) => bytes.capacity(),
            Self::Parts { body, .. } => body.capacity().saturating_add(8),
        }
    }
    fn slices(&self, offset: usize) -> [&[u8]; 2] {
        match self {
            Self::Wire(bytes) => [&bytes[offset..], &[]],
            Self::Parts { header, body } if offset < 8 => [&header[offset..], body],
            Self::Parts { body, .. } => [&body[offset - 8..], &[]],
        }
    }
}
struct Packet {
    data: Data,
    offset: usize,
    replace: Option<Replace>,
    cpu: bool,
}

pub struct Output {
    fd: RawFd,
    staging: Vec<u8>,
    queue: VecDeque<Packet>,
    queued_bytes: usize,
    queued_memory: usize,
}
impl Output {
    /// The caller keeps fd open and gives this transport exclusive write access.
    pub fn new(fd: RawFd) -> io::Result<Self> {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
        if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self {
            fd,
            staging: Vec::new(),
            queue: VecDeque::new(),
            queued_bytes: 0,
            queued_memory: 0,
        })
    }
    pub fn wants_write(&self) -> bool {
        !self.queue.is_empty()
    }
    pub fn can_accept_frame(&self) -> bool {
        self.staging.is_empty()
            && self.queued_bytes < FRAME_ADMISSION
            && !self.queue.iter().any(|packet| packet.cpu)
    }
    /// Takes the encoder allocation directly; its header is written as a separate slice.
    pub fn packet(&mut self, kind: &[u8; 4], body: Vec<u8>) -> io::Result<()> {
        if !self.staging.is_empty() {
            self.staging = Vec::new();
            return Err(invalid("Uncommitted session packet"));
        }
        let header = header(kind, body.len())?;
        let replace = validate(kind, &body)?;
        self.enqueue(Packet {
            data: Data::Parts { header, body },
            offset: 0,
            replace,
            cpu: kind == b"FRM1" || kind == b"FRM2",
        })
    }
    fn enqueue(&mut self, packet: Packet) -> io::Result<()> {
        let removes = |old: &Packet| {
            packet.replace.is_some() && old.replace == packet.replace && old.offset == 0
        };
        let removed_bytes: usize = self
            .queue
            .iter()
            .filter(|old| removes(old))
            .map(|old| old.data.len())
            .sum();
        let removed_memory: usize = self
            .queue
            .iter()
            .filter(|old| removes(old))
            .map(|old| old.data.memory())
            .sum();
        let memory = self.queued_memory - removed_memory + packet.data.memory();
        if memory > MAX_QUEUE {
            return Err(invalid("Session output queue exceeds its memory limit"));
        }
        let critical = self
            .queue
            .iter()
            .filter(|old| old.replace.is_none())
            .count()
            + usize::from(packet.replace.is_none());
        if critical > MAX_CRITICAL {
            return Err(invalid(
                "Session output queue has too many critical messages",
            ));
        }
        // Append the newest replaceable state after intervening lifecycle/binding events.
        self.queue.retain(|old| !removes(old));
        self.queued_memory = memory;
        self.queued_bytes = self.queued_bytes - removed_bytes + packet.data.len();
        self.queue.push_back(packet);
        Ok(())
    }
    pub fn pump(&mut self) -> io::Result<()> {
        let mut budget = IO_BUDGET;
        while budget > 0 && !self.queue.is_empty() {
            let mut room = budget;
            let mut vectors = Vec::with_capacity(self.queue.len() * 2);
            for packet in &self.queue {
                for slice in packet.data.slices(packet.offset) {
                    let size = room.min(slice.len());
                    if size > 0 {
                        vectors.push(libc::iovec {
                            iov_base: slice.as_ptr().cast_mut().cast(),
                            iov_len: size,
                        });
                    }
                    room -= size;
                    if room == 0 {
                        break;
                    }
                }
                if room == 0 {
                    break;
                }
            }
            // All referenced allocations stay owned by the queue until this syscall returns.
            let written =
                unsafe { libc::writev(self.fd, vectors.as_ptr(), vectors.len() as libc::c_int) };
            if written < 0 {
                let error = io::Error::last_os_error();
                match error.kind() {
                    io::ErrorKind::WouldBlock => return Ok(()),
                    io::ErrorKind::Interrupted => continue,
                    _ => return Err(error),
                }
            }
            if written == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::WriteZero,
                    "Session output stopped accepting data",
                ));
            }
            let mut remaining = written as usize;
            budget -= remaining;
            while remaining > 0 {
                let packet = self
                    .queue
                    .front_mut()
                    .ok_or_else(|| invalid("Invalid session output state"))?;
                let size = remaining.min(packet.data.len() - packet.offset);
                packet.offset += size;
                remaining -= size;
                if packet.offset == packet.data.len() {
                    self.queued_bytes -= packet.data.len();
                    self.queued_memory -= packet.data.memory();
                    self.queue.pop_front();
                }
            }
        }
        Ok(())
    }
}
impl Write for Output {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let result = (|| {
            let size = self
                .staging
                .len()
                .checked_add(bytes.len())
                .filter(|size| *size <= MAX_PACKET)
                .ok_or_else(|| invalid("Session packet exceeds its size limit"))?;
            if size >= 8 {
                let mut prefix = [0; 8];
                let saved = self.staging.len().min(8);
                prefix[..saved].copy_from_slice(&self.staging[..saved]);
                prefix[saved..].copy_from_slice(&bytes[..8 - saved]);
                let kind: [u8; 4] = prefix[..4]
                    .try_into()
                    .map_err(|_| invalid("Invalid session packet header"))?;
                let body = u32::from_le_bytes(
                    prefix[4..]
                        .try_into()
                        .map_err(|_| invalid("Invalid session packet size"))?,
                ) as usize;
                header(&kind, body)?;
                if size > 8 + body {
                    return Err(invalid(
                        "Multiple or oversized session packets before flush",
                    ));
                }
            }
            self.staging
                .try_reserve_exact(bytes.len())
                .map_err(|error| io::Error::new(io::ErrorKind::OutOfMemory, error))?;
            self.staging.extend_from_slice(bytes);
            Ok(bytes.len())
        })();
        if result.is_err() {
            self.staging = Vec::new();
        }
        result
    }
    fn flush(&mut self) -> io::Result<()> {
        // host::packet uses flush as its boundary; only pump writes to the nonblocking fd.
        if self.staging.is_empty() {
            return Ok(());
        }
        let bytes = std::mem::take(&mut self.staging);
        if bytes.len() < 8 {
            return Err(invalid("Incomplete session packet header"));
        }
        let kind: [u8; 4] = bytes[..4]
            .try_into()
            .map_err(|_| invalid("Invalid session packet header"))?;
        let body = u32::from_le_bytes(
            bytes[4..8]
                .try_into()
                .map_err(|_| invalid("Invalid session packet size"))?,
        ) as usize;
        header(&kind, body)?;
        if bytes.len() != body + 8 {
            return Err(invalid("Incomplete session packet body"));
        }
        let replace = validate(&kind, &bytes[8..])?;
        self.enqueue(Packet {
            data: Data::Wire(bytes),
            offset: 0,
            replace,
            cpu: kind == *b"FRM1" || kind == *b"FRM2",
        })
    }
}
fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn header(kind: &[u8; 4], size: usize) -> io::Result<[u8; 8]> {
    if !matches!(kind, b"EVT1" | b"TEX1" | b"FRM1" | b"FRM2") {
        return Err(invalid("Unknown session packet kind"));
    }
    if size > MAX_BODY {
        return Err(invalid("Session packet exceeds its size limit"));
    }
    let mut header = [0; 8];
    header[..4].copy_from_slice(kind);
    header[4..].copy_from_slice(&(size as u32).to_le_bytes());
    Ok(header)
}
fn word(body: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes([
        body[offset],
        body[offset + 1],
        body[offset + 2],
        body[offset + 3],
    ])
}
fn validate(kind: &[u8; 4], body: &[u8]) -> io::Result<Option<Replace>> {
    if kind == b"EVT1" {
        let event: Value =
            serde_json::from_slice(body).map_err(|_| invalid("Invalid session event"))?;
        let event_type = event
            .get("type")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("Invalid session event type"))?;
        return Ok(match event_type {
            "draw" => Some(Replace::Draw),
            "presented" => Some(Replace::Presented),
            _ => None,
        });
    }
    if kind == b"TEX1" {
        if body.len() < 12 {
            return Err(invalid("Incomplete session texture"));
        }
        let (id, width, height) = (word(body, 0), word(body, 4), word(body, 8));
        if !(2..=100).contains(&id)
            || !(1..=2048).contains(&width)
            || !(1..=2048).contains(&height)
            || body.len() != 12 + width as usize * height as usize * 4
        {
            return Err(invalid("Invalid session texture"));
        }
        return Ok(None);
    }
    if body.len() < 8 || !matches!((word(body, 0), word(body, 4)), (408, 720) | (544, 960)) {
        return Err(invalid("Invalid session frame dimensions"));
    }
    let (width, height) = (word(body, 0), word(body, 4));
    if kind == b"FRM1" {
        if body.len() != 8 + width as usize * height as usize * 4 {
            return Err(invalid("Invalid session frame size"));
        }
        return Ok(None);
    }
    if body.len() < 12 {
        return Err(invalid("Incomplete session frame regions"));
    }
    let count = word(body, 8) as usize;
    if count > 64 || 12 + count * 16 > body.len() {
        return Err(invalid("Invalid session frame regions"));
    }
    let mut size = 12 + count * 16;
    for index in 0..count {
        let at = 12 + index * 16;
        let (x, y, w, h) = (
            word(body, at),
            word(body, at + 4),
            word(body, at + 8),
            word(body, at + 12),
        );
        if x > width || y > height || w == 0 || h == 0 || w > width - x || h > height - y {
            return Err(invalid("Invalid session frame region"));
        }
        let pixels = w as usize * h as usize * 4;
        if pixels > body.len() - size {
            return Err(invalid("Incomplete session frame pixels"));
        }
        size += pixels;
    }
    if size != body.len() {
        return Err(invalid("Invalid session frame size"));
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
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
            let size =
                u32::from_le_bytes(wire[offset + 4..offset + 8].try_into().unwrap()) as usize;
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
}
