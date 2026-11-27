//! Nonblocking client for the independently installed GPU worker.
use serde_json::Value;
use std::{
    collections::VecDeque,
    fs::File,
    io::Read,
    os::{
        fd::{AsRawFd, RawFd},
        unix::net::UnixStream,
    },
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

mod connection;

const END: &[u8] = &[1, 0, 74, 68, 0, 0, 0, 0];
const MAX_FRAME: usize = 64 << 20;
const MAX_REPLY: usize = 4096;
const MAX_QUEUE: usize = MAX_FRAME + (1 << 20) + (64 << 10);
const IO_BUDGET: usize = 1 << 20;
const RETRY: Duration = Duration::from_secs(3);

#[derive(Debug)]
pub enum Event {
    Connected,
    Bound {
        native: bool,
        reason: Option<String>,
    },
    Frame(Value),
    Disconnected,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Bind,
    Frame,
    End,
    View,
    Image,
    Cursor,
}
enum Bytes {
    Owned(Vec<u8>),
    Shared(Arc<Vec<u8>>),
    Static(&'static [u8]),
}
impl Bytes {
    fn slice(&self) -> &[u8] {
        match self {
            Self::Owned(data) => data,
            Self::Shared(data) => data,
            Self::Static(data) => data,
        }
    }
}
struct Chunk {
    kind: Kind,
    bytes: Bytes,
    offset: usize,
}
#[derive(Clone, Copy)]
struct Pending {
    kind: Kind,
    deadline: Instant,
    written: bool,
}

pub struct Worker {
    path: PathBuf,
    socket: Option<UnixStream>,
    connecting: Option<Instant>,
    ready: bool,
    native: bool,
    closed: bool,
    generation: u64,
    sequence: u32,
    video_path: Option<String>,
    pending: Option<Pending>,
    retry_at: Option<Instant>,
    last_error: Option<String>,
    input: Vec<u8>,
    output: VecDeque<Chunk>,
    queued_bytes: usize,
    desired_view: Option<[i32; 5]>,
    last_view: Option<[i32; 5]>,
    cursor_image: Option<Arc<Vec<u8>>>,
    cursor_position: Option<Arc<Vec<u8>>>,
}

impl Worker {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            socket: None,
            connecting: None,
            ready: false,
            native: false,
            closed: false,
            generation: 0,
            sequence: 0,
            video_path: None,
            pending: None,
            retry_at: None,
            last_error: None,
            input: Vec::with_capacity(MAX_REPLY),
            output: VecDeque::new(),
            queued_bytes: 0,
            desired_view: None,
            last_view: None,
            cursor_image: None,
            cursor_position: None,
        }
    }
    pub fn ready(&self) -> bool {
        self.ready
    }
    pub fn native(&self) -> bool {
        self.native
    }
    pub fn pending(&self) -> bool {
        self.pending.is_some()
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }
    pub fn last_error(&self) -> Option<&str> {
        self.last_error.as_deref()
    }
    pub fn fd(&self) -> Option<RawFd> {
        self.socket.as_ref().map(AsRawFd::as_raw_fd)
    }
    pub fn wants_write(&self) -> bool {
        self.connecting.is_some() || !self.output.is_empty()
    }

    fn validate_reply(&self, reply: &Value) -> Result<(), String> {
        let expected = if self.sequence == u32::MAX {
            1
        } else {
            self.sequence + 1
        };
        if integer(&reply["sequence"]) != Some(expected) {
            return Err("Invalid GPU frame sequence".into());
        }
        if reply["mode"] == "native" {
            if !self.native
                || !integer(&reply["width"]).is_some_and(|n| (16..=8192).contains(&n))
                || !integer(&reply["height"]).is_some_and(|n| (16..=8192).contains(&n))
                || reply["presented"].as_bool().is_none()
                || integer(&reply["timestamp"]).is_none()
            {
                return Err("Invalid native GPU frame".into());
            }
        } else {
            let mode = reply.get("mode").is_none_or(|mode| mode == "video");
            let path = reply["path"].as_str();
            let safe = path
                .and_then(|path| path.strip_prefix("/dev/shm/mgu-video-doodle-"))
                .is_some_and(|name| {
                    (1..=48).contains(&name.len())
                        && name
                            .bytes()
                            .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte))
                });
            if !mode
                || !safe
                || integer(&reply["width"]) != Some(408)
                || integer(&reply["height"]) != Some(720)
                || self
                    .video_path
                    .as_deref()
                    .is_some_and(|previous| Some(previous) != path)
            {
                return Err(reply["error"]
                    .as_str()
                    .unwrap_or("Invalid GPU worker answer")
                    .into());
            }
        }
        Ok(())
    }
    fn replay_controls(&mut self) -> Result<(), String> {
        if let Some(view) = self.desired_view {
            self.enqueue(Kind::View, Bytes::Owned(view_packet(view)))?;
            self.last_view = Some(view);
        }
        if let Some(image) = &self.cursor_image {
            self.enqueue(Kind::Image, Bytes::Shared(Arc::clone(image)))?;
        }
        if let Some(position) = &self.cursor_position {
            self.enqueue(Kind::Cursor, Bytes::Shared(Arc::clone(position)))?;
        }
        Ok(())
    }
    pub fn submit_frame(&mut self, data: Vec<u8>, now: Instant) -> Result<bool, String> {
        validate_scene(&data)?;
        if !self.ready || self.pending.is_some() {
            return Ok(false);
        }
        if self.queued_bytes + data.len() + END.len() > MAX_QUEUE {
            return Err("GPU worker output queue exceeds its limit".into());
        }
        // The scene keeps the allocation returned by native.so; END is a separate static chunk.
        self.enqueue(Kind::Frame, Bytes::Owned(data))?;
        self.enqueue(Kind::End, Bytes::Static(END))?;
        self.pending = Some(Pending {
            kind: Kind::Frame,
            deadline: now + Duration::from_secs(5),
            written: false,
        });
        Ok(true)
    }
    pub fn bind(&mut self, path: &str, rect: [i32; 4], now: Instant) -> Result<bool, String> {
        if !path.starts_with('/')
            || path.len() > 107
            || path.as_bytes().contains(&0)
            || !(16..=4096).contains(&rect[2])
            || !(16..=4096).contains(&rect[3])
        {
            return Err("Invalid GPU surface binding".into());
        }
        if !self.ready || self.pending.is_some() {
            return Ok(false);
        }
        self.view([0, 0, 408, 720], false)?;
        let mut body = Vec::with_capacity(17 + path.len());
        for value in rect {
            body.extend(value.to_le_bytes());
        }
        body.extend(path.as_bytes());
        body.push(0);
        self.enqueue(Kind::Bind, Bytes::Owned(packet(0x444a0002, body)))?;
        self.native = false;
        self.video_path = None;
        self.last_view = None;
        self.desired_view = Some([0, 0, 408, 720, 0]);
        self.pending = Some(Pending {
            kind: Kind::Bind,
            deadline: now + Duration::from_secs(3),
            written: false,
        });
        Ok(true)
    }
    pub fn view(&mut self, rect: [i32; 4], shown: bool) -> Result<(), String> {
        if rect[0] < 0
            || rect[1] < 0
            || !(16..=4096).contains(&rect[2])
            || !(16..=4096).contains(&rect[3])
            || i64::from(rect[0]) + i64::from(rect[2]) > 4096
            || i64::from(rect[1]) + i64::from(rect[3]) > 4096
        {
            return Err("Invalid native GPU view".into());
        }
        let values = [rect[0], rect[1], rect[2], rect[3], i32::from(shown)];
        if self.ready && self.native && self.last_view != Some(values) {
            self.enqueue(Kind::View, Bytes::Owned(view_packet(values)))?;
            self.last_view = Some(values);
        }
        self.desired_view = Some(values);
        Ok(())
    }
    pub fn set_cursor_image(&mut self, w: u32, h: u32, rgba: Vec<u8>) -> Result<(), String> {
        if !(1..=512).contains(&w)
            || !(1..=512).contains(&h)
            || rgba.len() != w as usize * h as usize * 4
        {
            return Err("Invalid GPU cursor image".into());
        }
        let mut body = Vec::with_capacity(8 + rgba.len());
        body.extend(w.to_le_bytes());
        body.extend(h.to_le_bytes());
        body.extend(rgba);
        let message = Arc::new(packet(0x444a0004, body));
        if self.cursor_image.as_deref() == Some(message.as_ref()) {
            return Ok(());
        }
        if self.ready && self.native {
            self.enqueue(Kind::Image, Bytes::Shared(Arc::clone(&message)))?;
        }
        self.cursor_image = Some(message);
        Ok(())
    }
    pub fn cursor(&mut self, values: [f32; 4], shown: bool) -> Result<(), String> {
        if !values
            .iter()
            .all(|value| value.is_finite() && (0.0..=1024.0).contains(value))
        {
            return Err("Invalid GPU cursor position".into());
        }
        let mut body = Vec::with_capacity(20);
        for value in values {
            body.extend(value.to_le_bytes());
        }
        body.extend(u32::from(shown).to_le_bytes());
        let message = Arc::new(packet(0x444a0005, body));
        if self.cursor_position.as_deref() == Some(message.as_ref()) {
            return Ok(());
        }
        if self.ready && self.native {
            self.enqueue(Kind::Cursor, Bytes::Shared(Arc::clone(&message)))?;
        }
        self.cursor_position = Some(message);
        Ok(())
    }
    fn disconnect(&mut self, error: String, now: Instant, events: &mut Vec<Event>) {
        self.socket = None;
        self.connecting = None;
        self.ready = false;
        self.native = false;
        self.pending = None;
        self.input.clear();
        self.output.clear();
        self.queued_bytes = 0;
        self.last_view = None;
        self.desired_view = None;
        self.video_path = None;
        self.last_error = Some(error);
        self.retry_at = (!self.closed).then_some(now + RETRY);
        events.push(Event::Disconnected);
    }
    pub fn close(&mut self) {
        self.closed = true;
        self.disconnect("GPU worker stopped".into(), Instant::now(), &mut Vec::new());
    }
    pub fn reset(&mut self, now: Instant, reason: &str) {
        self.disconnect(reason.into(), now, &mut Vec::new());
    }
}

fn integer(value: &Value) -> Option<u32> {
    value
        .as_u64()
        .and_then(|value| u32::try_from(value).ok())
        .or_else(|| {
            value
                .as_f64()
                .filter(|value| {
                    value.is_finite()
                        && value.fract() == 0.0
                        && *value >= 0.0
                        && *value <= u32::MAX as f64
                })
                .map(|value| value as u32)
        })
}
fn packet(kind: u32, body: Vec<u8>) -> Vec<u8> {
    let mut data = Vec::with_capacity(8 + body.len());
    data.extend(kind.to_le_bytes());
    data.extend((body.len() as u32).to_le_bytes());
    data.extend(body);
    data
}
fn view_packet(values: [i32; 5]) -> Vec<u8> {
    let mut body = Vec::with_capacity(20);
    for value in values {
        body.extend(value.to_le_bytes());
    }
    packet(0x444a0003, body)
}
pub fn challenge(w: i32, h: i32) -> Result<[i32; 4], String> {
    if !(16..=4096).contains(&w) || !(16..=4096).contains(&h) {
        return Err("Invalid GPU surface dimensions".into());
    }
    let mut random = [0; 4];
    File::open("/dev/urandom")
        .and_then(|mut source| source.read_exact(&mut random))
        .map_err(|error| error.to_string())?;
    Ok([
        -8192 - i32::from(u16::from_le_bytes([random[0], random[1]])),
        -8192 - i32::from(u16::from_le_bytes([random[2], random[3]])),
        w,
        h,
    ])
}
fn validate_scene(data: &[u8]) -> Result<(), String> {
    if data.len() > MAX_FRAME {
        return Err("Invalid GPU scene size".into());
    }
    let word =
        |offset: usize| u32::from_le_bytes(data[offset..offset + 4].try_into().unwrap_or([0; 4]));
    let mut scene = false;
    let mut offset = 0;
    while offset < data.len() {
        if data.len() - offset < 8 {
            return Err("Truncated GPU message".into());
        }
        let kind = word(offset);
        let length = word(offset + 4) as usize;
        let start = offset + 8;
        if length > data.len() - start || !length.is_multiple_of(4) {
            return Err("Invalid GPU message length".into());
        }
        let end = start + length;
        if kind == 0x4d520007 {
            if length < 12 {
                return Err("Truncated GPU texture".into());
            }
            let (id, w, h) = (word(start), word(start + 4), word(start + 8));
            let removed = w == 0 && h == 0;
            if id == 0
                || if removed {
                    !scene || length != 12
                } else {
                    scene
                        || w == 0
                        || h == 0
                        || w > 2048
                        || h > 2048
                        || length != 12 + w as usize * h as usize * 4
                }
            {
                return Err("Invalid GPU texture".into());
            }
        } else if kind == 0x4d520008 {
            if scene || length < 8 {
                return Err("Invalid GPU scene".into());
            }
            if !matches!((word(start), word(start + 4)), (408, 720) | (544, 960)) {
                return Err("Invalid GPU scene dimensions".into());
            }
            let mut command = start + 8;
            while command < end {
                let (floats, next) = match word(command) {
                    1 => (command + 4, command + 20),
                    2 => {
                        if end - command < 28 {
                            return Err("Truncated GPU scene draw".into());
                        }
                        let (flags, source, destination, replace, count) = (
                            word(command + 8),
                            word(command + 12),
                            word(command + 16),
                            word(command + 20),
                            word(command + 24),
                        );
                        if flags > 15
                            || ![0, 1, 770, 771].contains(&source)
                            || ![0, 1, 768, 770, 771].contains(&destination)
                            || replace > 2
                            || count == 0
                            || count % 3 != 0
                            || count > 65536
                        {
                            return Err("Invalid GPU scene draw".into());
                        }
                        (command + 28, command + 28 + count as usize * 32)
                    }
                    _ => return Err("Unknown GPU scene command".into()),
                };
                if next > end {
                    return Err("Truncated GPU scene command".into());
                }
                if (floats..next)
                    .step_by(4)
                    .any(|at| !f32::from_bits(word(at)).is_finite())
                {
                    return Err("Nonfinite GPU scene vertex".into());
                }
                command = next;
            }
            scene = true;
        } else {
            return Err("Unknown GPU message".into());
        }
        offset = end;
    }
    if !scene {
        return Err("Missing GPU scene".into());
    }
    Ok(())
}

#[cfg(test)]
#[path = "../../tests/unit/gpu.rs"]
mod tests;
