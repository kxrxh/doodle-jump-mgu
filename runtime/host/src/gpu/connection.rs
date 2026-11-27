use super::{Bytes, Chunk, Event, IO_BUDGET, Kind, MAX_QUEUE, MAX_REPLY, RETRY, Worker, integer};
use serde_json::Value;
use std::{
    io::{self, IoSlice, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{ffi::OsStrExt, net::UnixStream},
    },
    path::Path,
    time::Instant,
};

impl Worker {
    pub fn poll(&mut self, now: Instant) -> Vec<Event> {
        let mut events = Vec::new();
        if self.closed {
            return events;
        }
        if let Some(pending) = self.pending
            && now >= pending.deadline
        {
            let operation = if pending.kind == Kind::Bind {
                "surface binding"
            } else {
                "frame"
            };
            self.disconnect(
                format!("GPU worker {operation} timed out"),
                now,
                &mut events,
            );
            return events;
        }
        if let Some(started) = self.connecting
            && now.saturating_duration_since(started) >= RETRY
        {
            self.disconnect("GPU worker connection timed out".into(), now, &mut events);
            return events;
        }
        if self.socket.is_none() && self.retry_at.is_none_or(|retry| now >= retry) {
            match connect(&self.path) {
                Ok((socket, immediate)) => {
                    self.socket = Some(socket);
                    self.retry_at = None;
                    if immediate {
                        self.connected(&mut events);
                    } else {
                        self.connecting = Some(now);
                    }
                }
                Err(error) => {
                    self.disconnect(format!("GPU worker connection: {error}"), now, &mut events)
                }
            }
        }
        if self.connecting.is_some() {
            match self.finish_connect() {
                Ok(true) => self.connected(&mut events),
                Ok(false) => return events,
                Err(error) => {
                    self.disconnect(format!("GPU worker connection: {error}"), now, &mut events);
                    return events;
                }
            }
        }
        if self.ready {
            let result = self.read(&mut events).and_then(|_| self.flush());
            if let Err(error) = result {
                self.disconnect(error, now, &mut events);
            }
        }
        events
    }
    fn connected(&mut self, events: &mut Vec<Event>) {
        self.connecting = None;
        self.ready = true;
        self.native = false;
        self.generation = self.generation.wrapping_add(1).max(1);
        self.sequence = 0;
        self.video_path = None;
        self.last_view = None;
        self.desired_view = None;
        self.last_error = None;
        events.push(Event::Connected);
    }
    fn finish_connect(&self) -> io::Result<bool> {
        let socket = self
            .socket
            .as_ref()
            .ok_or_else(|| io::Error::other("missing socket"))?;
        let mut state = libc::pollfd {
            fd: socket.as_raw_fd(),
            events: libc::POLLOUT,
            revents: 0,
        };
        // This only checks readiness; the owning session decides when to sleep.
        let result = unsafe { libc::poll(&mut state, 1, 0) };
        if result < 0 {
            return Err(io::Error::last_os_error());
        }
        if result == 0 {
            return Ok(false);
        }
        if let Some(error) = socket.take_error()? {
            return Err(error);
        }
        socket.peer_addr().map(|_| true)
    }
    pub(super) fn enqueue(&mut self, kind: Kind, bytes: Bytes) -> Result<(), String> {
        let replace = if matches!(kind, Kind::View | Kind::Image | Kind::Cursor) {
            self.output
                .iter()
                .rposition(|chunk| chunk.kind == kind && chunk.offset == 0)
        } else {
            None
        };
        let old = replace.map_or(0, |index| self.output[index].bytes.slice().len());
        let total = self.queued_bytes - old + bytes.slice().len();
        if total > MAX_QUEUE {
            return Err("GPU worker output queue exceeds its limit".into());
        }
        let chunk = Chunk {
            kind,
            bytes,
            offset: 0,
        };
        if let Some(index) = replace {
            self.output[index] = chunk;
        } else {
            self.output.push_back(chunk);
        }
        self.queued_bytes = total;
        Ok(())
    }
    fn flush(&mut self) -> Result<(), String> {
        let socket = self.socket.as_mut().ok_or("GPU worker disconnected")?;
        let mut budget = IO_BUDGET;
        while budget > 0 && !self.output.is_empty() {
            let written = {
                let mut room = budget;
                let mut slices = Vec::with_capacity(self.output.len());
                for chunk in &self.output {
                    let bytes = &chunk.bytes.slice()[chunk.offset..];
                    let size = room.min(bytes.len());
                    if size > 0 {
                        slices.push(IoSlice::new(&bytes[..size]));
                    }
                    room -= size;
                    if room == 0 {
                        break;
                    }
                }
                match socket.write_vectored(&slices) {
                    Ok(0) => return Err("GPU worker stopped accepting data".into()),
                    Ok(size) => size,
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(()),
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                    Err(error) => return Err(format!("GPU worker write: {error}")),
                }
            };
            budget -= written;
            let mut remaining = written;
            while remaining > 0 {
                let chunk = self
                    .output
                    .front_mut()
                    .ok_or("GPU worker output state is invalid")?;
                let size = remaining.min(chunk.bytes.slice().len() - chunk.offset);
                chunk.offset += size;
                remaining -= size;
                if chunk.offset == chunk.bytes.slice().len() {
                    if matches!(chunk.kind, Kind::Bind | Kind::End)
                        && let Some(pending) = &mut self.pending
                    {
                        pending.written = true;
                    }
                    self.queued_bytes -= chunk.bytes.slice().len();
                    self.output.pop_front();
                }
            }
        }
        Ok(())
    }
    fn read(&mut self, events: &mut Vec<Event>) -> Result<(), String> {
        let mut buffer = [0; MAX_REPLY + 1];
        loop {
            let read = self
                .socket
                .as_mut()
                .ok_or("GPU worker disconnected")?
                .read(&mut buffer);
            match read {
                Ok(0) => return Err("GPU worker disconnected".into()),
                Ok(size) => {
                    if self.input.len() + size > MAX_REPLY {
                        return Err("GPU worker answer is too large".into());
                    }
                    self.input.extend_from_slice(&buffer[..size]);
                    if let Some(newline) = self.input.iter().position(|byte| *byte == b'\n') {
                        if newline + 1 != self.input.len() {
                            return Err("Invalid GPU worker answer".into());
                        }
                        let reply: Value = serde_json::from_slice(&self.input[..newline])
                            .map_err(|_| "Invalid GPU worker answer")?;
                        self.input.clear();
                        if !reply.is_object() {
                            return Err("Invalid GPU worker answer".into());
                        }
                        let pending = self.pending.ok_or("Unsolicited GPU worker answer")?;
                        if !pending.written {
                            return Err("GPU worker acknowledged an incomplete command".into());
                        }
                        if pending.kind == Kind::Bind {
                            if reply["event"] != "bind" {
                                return Err("Invalid GPU binding answer".into());
                            }
                            let native = reply["native"]
                                .as_bool()
                                .ok_or("Invalid GPU binding answer")?;
                            self.pending = None;
                            self.native = native;
                            self.last_view = None;
                            if native {
                                self.replay_controls()?;
                            }
                            let reason = reply.get("reason").map(|reason| {
                                reason
                                    .as_str()
                                    .filter(|code| {
                                        !code.is_empty()
                                            && code.len() <= 48
                                            && code
                                                .bytes()
                                                .all(|c| c.is_ascii_lowercase() || c == b'_')
                                    })
                                    .unwrap_or("unknown")
                                    .to_owned()
                            });
                            events.push(Event::Bound { native, reason });
                        } else {
                            self.validate_reply(&reply)?;
                            self.pending = None;
                            self.sequence =
                                integer(&reply["sequence"]).ok_or("Invalid GPU frame sequence")?;
                            self.video_path = if reply["mode"] == "native" {
                                None
                            } else {
                                reply["path"].as_str().map(str::to_owned)
                            };
                            events.push(Event::Frame(reply));
                        }
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(()),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(format!("GPU worker read: {error}")),
            }
        }
    }
}

fn connect(path: &Path) -> io::Result<(UnixStream, bool)> {
    let bytes = path.as_os_str().as_bytes();
    if bytes.is_empty() || bytes.len() > 107 || bytes.contains(&0) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid GPU socket path",
        ));
    }
    // Own the descriptor immediately so every failed connect closes it.
    let fd = unsafe {
        libc::socket(
            libc::AF_UNIX,
            libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
            0,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let socket = unsafe { UnixStream::from_raw_fd(fd) };
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (target, source) in address.sun_path.iter_mut().zip(bytes) {
        *target = *source as libc::c_char;
    }
    let length = std::mem::offset_of!(libc::sockaddr_un, sun_path) + bytes.len() + 1;
    let result = unsafe {
        libc::connect(
            fd,
            (&address as *const libc::sockaddr_un).cast(),
            length as libc::socklen_t,
        )
    };
    if result == 0 {
        return Ok((socket, true));
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::EINPROGRESS) {
        Ok((socket, false))
    } else {
        Err(error)
    }
}
