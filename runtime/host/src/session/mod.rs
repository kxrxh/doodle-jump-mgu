mod commands;
mod presentation;
mod runner;
mod ui;

use crate::{
    Result,
    drawing::World,
    gpu::Worker,
    host::{self, FrameState, Host},
    input::{Controls, Placement},
    output::Output,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    io::Write,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use runner::run_inner;

const BIND_RETRY_DELAY: Duration = Duration::from_millis(1500);
const PERIOD: Duration = Duration::from_nanos(16_666_667);
// Vehicle speed is polled every two seconds; tolerate two missed polls.
const SPEED_MAX_AGE: Duration = Duration::from_secs(6);
const HELP: &str = "Ring / joystick: steer. Joystick: move menu cursor. Press: select / shoot. Touch sides: hold to steer; touch game: shoot / select.";

#[derive(Clone, Debug, PartialEq, Eq)]
struct SurfaceKey {
    source: u64,
    socket: u64,
    generation: u64,
    worker: u64,
    rectangle: [i32; 4],
}
#[derive(Clone, Default)]
struct Renderer {
    ready: bool,
    native: bool,
    video: bool,
    visible: bool,
    on_screen: bool,
    source: u64,
    socket: u64,
    generation: u64,
    path: String,
    stats: Option<(f64, f64)>,
    interruption: String,
}
#[derive(Clone)]
struct Ui {
    shown: bool,
    focused: bool,
    active: bool,
    sheet: bool,
    parked_only: bool,
    show_fps: bool,
    ring_sensitivity: f64,
    place: Placement,
    origin: [i32; 2],
    renderer: Renderer,
}
impl Default for Ui {
    fn default() -> Self {
        Self {
            shown: false,
            focused: false,
            active: false,
            sheet: false,
            parked_only: true,
            show_fps: false,
            ring_sensitivity: 1.,
            place: Placement {
                w: 1600.,
                h: 520.,
                scale: 1.,
                ox: 0.,
                oy: 0.,
                full: false,
            },
            origin: [0, 0],
            renderer: Renderer::default(),
        }
    }
}

struct Clock {
    last: Instant,
    next: Instant,
}
impl Clock {
    fn new(now: Instant) -> Self {
        Self {
            last: now,
            next: now + PERIOD,
        }
    }
    fn restart(&mut self, now: Instant) {
        *self = Self::new(now);
    }
    fn submit(&mut self, now: Instant) -> f64 {
        let dt = now
            .saturating_duration_since(self.last)
            .as_secs_f64()
            .clamp(1. / 120., 0.1);
        self.last = now;
        self.next = (self.next + PERIOD).max(now);
        dt
    }
}
struct Binding {
    id: u32,
    key: SurfaceKey,
    rectangle: [i32; 4],
    retry: bool,
    stage: &'static str,
    deadline: Instant,
}
struct PendingFrame {
    key: SurfaceKey,
    began: Instant,
    timing: Option<(f64, f64)>,
}
struct Sample {
    at: Instant,
    frames: u64,
    rate: Option<u64>,
    timing: Option<(Instant, f64, f64, f64)>,
}
#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Pixels,
    Video,
    Native,
}

struct Session {
    ui: Ui,
    ui_at: Instant,
    running: bool,
    gated: bool,
    speed: Option<(f64, Instant)>,
    dialogs: BTreeSet<u32>,
    failure: Option<String>,
    controls: Controls,
    world: World,
    scene: Value,
    playing: bool,
    frames: u64,
    steps: u64,
    frame_dt: f64,
    mode: Option<Mode>,
    clock: Clock,
    started: Instant,
    last_paint: Instant,
    last_ccu: Option<Instant>,
    worker: Worker,
    attempted: Option<SurfaceKey>,
    bind_retry_at: Option<Instant>,
    logged_worker_error: Option<String>,
    bound_surface: Option<SurfaceKey>,
    binding: Option<Binding>,
    next_binding: u32,
    pending: Option<PendingFrame>,
    captured_worker: Option<u64>,
    force_reset: bool,
    frame_state: FrameState,
    sample: Sample,
    next_timing: Instant,
    dirty: bool,
    last_info: String,
}
impl Session {
    fn new(scene: Value, worker: Worker, now: Instant) -> Self {
        Self {
            ui: Ui::default(),
            ui_at: now,
            running: false,
            gated: false,
            speed: None,
            dialogs: BTreeSet::new(),
            failure: None,
            controls: Controls::new(),
            world: World::new(),
            scene,
            playing: false,
            frames: 0,
            steps: 0,
            frame_dt: 0.,
            mode: None,
            clock: Clock::new(now),
            started: now,
            last_paint: now,
            last_ccu: None,
            worker,
            attempted: None,
            bind_retry_at: None,
            logged_worker_error: None,
            bound_surface: None,
            binding: None,
            next_binding: 0,
            pending: None,
            captured_worker: None,
            force_reset: true,
            frame_state: FrameState::default(),
            sample: Sample {
                at: now,
                frames: 0,
                rate: None,
                timing: None,
            },
            next_timing: now,
            dirty: true,
            last_info: String::new(),
        }
    }
    fn parked_only_reason(&self, now: Instant) -> Option<&'static str> {
        if !self.ui.parked_only {
            return None;
        }
        match self
            .speed
            .filter(|(_, at)| now.saturating_duration_since(*at) < SPEED_MAX_AGE)
        {
            None => Some("Parked only: waiting for a current vehicle speed."),
            Some((speed, _)) if speed > 3. => Some("Parked only: the game waits while driving."),
            _ => None,
        }
    }
    fn wants_run(&self, now: Instant) -> bool {
        self.ui.shown
            && self.ui.focused
            && self.parked_only_reason(now).is_none()
            && self.dialogs.is_empty()
            && self.failure.is_none()
            && now.saturating_duration_since(self.ui_at) < Duration::from_secs(2)
    }
    fn sync(&mut self, host: &Host, out: &mut impl Write, now: Instant) -> Result<()> {
        let wanted = self.wants_run(now);
        let changed = wanted != self.running;
        if changed {
            self.running = wanted;
            self.controls.reset();
            self.clock.restart(now);
            self.sample = Sample {
                at: now,
                frames: self.frames,
                rate: None,
                timing: None,
            };
            self.next_timing = now;
            self.dirty = true;
            host.action(if wanted { "resume" } else { "pause" })?;
        }
        let parked = self.parked_only_reason(now);
        let info = if let Some(error) = &self.failure {
            format!("Engine stopped: {error}")
        } else if let Some(reason) = parked {
            reason.into()
        } else {
            HELP.into()
        };
        if changed || info != self.last_info {
            self.last_info = info.clone();
            let mut event = json!({"type":"lifecycle","running":self.running,"info":info});
            if !self.running && self.ui.shown && parked.is_some() {
                event["open"] = json!("MAIN");
            }
            host::event(out, event)?;
        }
        self.sync_gate(now);
        self.sync_view()?;
        Ok(())
    }
    fn sync_gate(&mut self, now: Instant) {
        let gated = self.binding.is_some()
            || (self.gpu_mode()
                && self.bound()
                && (!self.ui.renderer.visible || !self.ui.renderer.on_screen || self.ui.sheet));
        if gated != self.gated {
            self.gated = gated;
            self.clock.restart(now);
            self.controls.reset();
        }
    }
    fn ms(&self, now: Instant) -> f64 {
        now.saturating_duration_since(self.started).as_secs_f64() * 1000.
    }
}

pub(crate) fn run(
    original: &Path,
    data: &Path,
    runtime: &Path,
    epoch: i64,
    playback: bool,
    captures: (Option<PathBuf>, Option<PathBuf>),
) -> Result<()> {
    let mut out = Output::new(1).map_err(|e| e.to_string())?;
    let result = run_inner(original, data, runtime, epoch, playback, captures, &mut out);
    if let Err(error) = &result {
        let _ = host::event(&mut out, json!({"type":"error","message":error}));
    }
    // Finish queued lifecycle/errors without waiting indefinitely for a stalled parent.
    let until = Instant::now() + Duration::from_millis(100);
    while out.wants_write() && Instant::now() < until {
        if out.pump().is_err() {
            break;
        }
        if out.wants_write() {
            let mut fd = libc::pollfd {
                fd: 1,
                events: libc::POLLOUT,
                revents: 0,
            };
            unsafe { libc::poll(&mut fd, 1, 5) };
        }
    }
    result
}
#[cfg(test)]
#[path = "../../tests/unit/session.rs"]
mod tests;
