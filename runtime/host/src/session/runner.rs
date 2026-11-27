use super::{Result, Session};
use crate::{
    gpu::Worker,
    host::{self, Host},
    images,
    output::Output,
};
use serde_json::json;
use std::{
    io::{self, Read},
    os::fd::RawFd,
    path::{Path, PathBuf},
    time::Instant,
};

fn wait(input: RawFd, worker: &Worker, output: &Output, timeout: i32) -> Result<()> {
    let mut fds = [
        libc::pollfd {
            fd: input,
            events: libc::POLLIN,
            revents: 0,
        },
        libc::pollfd {
            fd: worker.fd().unwrap_or(-1),
            events: libc::POLLIN
                | if worker.wants_write() {
                    libc::POLLOUT
                } else {
                    0
                },
            revents: 0,
        },
        libc::pollfd {
            fd: if output.wants_write() { 1 } else { -1 },
            events: libc::POLLOUT,
            revents: 0,
        },
    ];
    let result = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, timeout) };
    if result < 0 && io::Error::last_os_error().kind() != io::ErrorKind::Interrupted {
        return Err(io::Error::last_os_error().to_string());
    }
    Ok(())
}

pub(super) fn run_inner(
    original: &Path,
    data: &Path,
    runtime: &Path,
    epoch: i64,
    playback: bool,
    captures: (Option<PathBuf>, Option<PathBuf>),
    out: &mut Output,
) -> Result<()> {
    let audio_events = captures
        .1
        .map(std::fs::File::create)
        .transpose()
        .map_err(|e| e.to_string())?;
    let mut audio = captures
        .0
        .map(std::fs::File::create)
        .transpose()
        .map_err(|e| e.to_string())?;
    let host = Host::new(original, data, runtime, epoch, playback, audio_events)?;
    host.action("pause")?;
    let (sprites, metadata) =
        images::scenery_assets(&host.assets, &runtime.join("assets/cursor.png"))?;
    let path = std::env::var_os("DOODLE_GPU_SOCKET")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/run/doodle-gpu/render.sock"));
    let mut worker = Worker::new(path);
    for (id, image) in sprites {
        if id == 20 {
            worker.set_cursor_image(image.width, image.height, image.pixels.clone())?;
        }
        host::texture(out, id, &image)?;
    }
    let now = Instant::now();
    let mut session = Session::new(json!({"sprites":metadata,"paper":null}), worker, now);
    host::event(out, json!({"type":"ready"}))?;
    session.sync(&host, out, now)?;
    session.draw(out, now)?;
    let flags = unsafe { libc::fcntl(0, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(0, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error().to_string());
    }
    let mut input = io::stdin().lock();
    let mut buffered = Vec::with_capacity(4096);
    let mut scratch = [0; 4096];
    let result = (|| -> Result<()> {
        loop {
            let mut read_bytes = 0;
            loop {
                match input.read(&mut scratch) {
                    Ok(0) => return Ok(()),
                    Ok(n) => {
                        read_bytes += n;
                        for byte in &scratch[..n] {
                            buffered.push(*byte);
                            if buffered.len() > 4096 {
                                return Err("Session input exceeds its size limit".into());
                            }
                            if *byte == b'\n' {
                                let command =
                                    serde_json::from_slice(&buffered).map_err(|e| e.to_string())?;
                                buffered.clear();
                                if !session.handle(&host, command, out, Instant::now())? {
                                    return Ok(());
                                }
                            }
                        }
                        if read_bytes >= 65536 {
                            break;
                        }
                    }
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                    Err(error) => return Err(error.to_string()),
                }
            }
            let now = Instant::now();
            session.worker_events(&host, out, now)?;
            if session
                .binding
                .as_ref()
                .is_some_and(|binding| now >= binding.deadline)
            {
                session.worker.reset(now, "Store surface binding timed out");
                session.bind_retry_at = None;
                session.bound_surface = None;
                session.force_reset = true;
                if session
                    .binding
                    .as_ref()
                    .is_some_and(|binding| binding.stage == "restore")
                {
                    session.binding = None;
                } else {
                    session.restore(out, now)?;
                }
            }
            session.try_bind(out, now)?;
            session.sync_gate(now);
            // Deliver completed frames before another guest call can hold the actor.
            out.pump().map_err(|e| e.to_string())?;
            if out.can_accept_frame() && session.can_frame() && now >= session.clock.next {
                session.render(&host, audio.as_mut(), out, now)?;
            }
            session.sync_view()?;
            session.draw(out, Instant::now())?;
            out.pump().map_err(|e| e.to_string())?;
            let now = Instant::now();
            let timeout = if out.can_accept_frame() && session.can_frame() {
                session
                    .clock
                    .next
                    .saturating_duration_since(now)
                    .as_nanos()
                    .div_ceil(1_000_000)
                    .min(100) as i32
            } else {
                100
            };
            wait(0, &session.worker, out, timeout)?;
        }
    })();
    let _ = host.action("pause");
    session.worker.close();
    let _ = unsafe { libc::fcntl(0, libc::F_SETFL, flags) };
    result
}
