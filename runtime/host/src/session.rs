use crate::{
    Result,
    drawing::World,
    gpu::{self, Event, Worker},
    host::{self, FrameRequest, FrameState, Host},
    images,
    input::{Controls, Placement},
    output::Output,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    io::{self, Read, Write},
    os::fd::RawFd,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const PERIOD: Duration = Duration::from_nanos(16_666_667);
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
fn flag(value: &Value, name: &str, default: bool) -> Result<bool> {
    match value.get(name) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Bool(v)) => Ok(*v),
        _ => Err(format!("Invalid UI flag {name}")),
    }
}
fn scalar(value: &Value, name: &str, default: f64, low: f64, high: f64) -> Result<f64> {
    match value.get(name) {
        None | Some(Value::Null) => Ok(default),
        Some(v) => v
            .as_f64()
            .filter(|n| n.is_finite() && *n >= low && *n <= high)
            .ok_or_else(|| format!("Invalid UI number {name}")),
    }
}
fn identifier(value: &Value, name: &str) -> Result<u64> {
    match value.get(name) {
        None | Some(Value::Null) => Ok(0),
        Some(v) => v
            .as_u64()
            .filter(|n| *n <= 9_007_199_254_740_991)
            .ok_or_else(|| format!("Invalid UI identifier {name}")),
    }
}
impl Ui {
    fn parse(value: &Value) -> Result<Self> {
        if !value.is_object() {
            return Err("Expected UI state".into());
        }
        let mut ui = Self {
            shown: flag(value, "shown", false)?,
            focused: flag(value, "focused", false)?,
            active: flag(value, "active", false)?,
            sheet: flag(value, "sheet", false)?,
            parked_only: flag(value, "parkedOnly", true)?,
            show_fps: flag(value, "showFps", false)?,
            ..Self::default()
        };
        if let Some(p) = value.get("place").filter(|v| !v.is_null()) {
            ui.place = Placement {
                w: scalar(p, "w", 1600., 16., 8192.)?,
                h: scalar(p, "h", 520., 16., 8192.)?,
                scale: scalar(p, "scale", 1., 0.01, 64.)?,
                ox: scalar(p, "ox", 0., -65536., 65536.)?,
                oy: scalar(p, "oy", 0., -65536., 65536.)?,
                full: flag(p, "full", false)?,
            };
            for (i, name) in ["x", "y"].iter().enumerate() {
                let n = scalar(p, name, 0., i32::MIN as f64, i32::MAX as f64)?;
                if n.fract() != 0. {
                    return Err("Invalid renderer origin".into());
                }
                ui.origin[i] = n as i32;
            }
        }
        if let Some(r) = value.get("renderer").filter(|v| !v.is_null()) {
            let path = r["path"].as_str().unwrap_or("");
            if path.len() > 107
                || path.contains('\0')
                || (!path.is_empty() && !path.starts_with('/'))
            {
                return Err("Invalid renderer socket".into());
            }
            let stats = r
                .get("stats")
                .filter(|s| s.is_object())
                .map(|s| -> Result<(f64, f64)> {
                    Ok((
                        scalar(s, "fps", 0., 0., 1000.)?,
                        scalar(s, "at", 0., 0., 9_007_199_254_740_991.)?,
                    ))
                })
                .transpose()?;
            ui.renderer = Renderer {
                ready: flag(r, "ready", false)?,
                native: flag(r, "native", false)?,
                video: flag(r, "video", false)?,
                visible: flag(r, "visible", false)?,
                on_screen: flag(r, "onScreen", false)?,
                source: identifier(r, "source")?,
                socket: identifier(r, "socket")?,
                generation: identifier(r, "generation")?,
                path: path.into(),
                stats,
                interruption: r["interruption"]
                    .as_str()
                    .unwrap_or("")
                    .chars()
                    .take(160)
                    .collect(),
            };
        }
        Ok(ui)
    }
    fn frame_surface(&self, worker: &Worker) -> Option<SurfaceKey> {
        let r = &self.renderer;
        if !r.ready || !r.native || !worker.ready() {
            return None;
        }
        Some(SurfaceKey {
            source: r.source,
            socket: r.socket,
            generation: r.generation,
            worker: worker.generation(),
            rectangle: [
                self.origin[0],
                self.origin[1],
                self.place.w as i32,
                self.place.h as i32,
            ],
        })
    }
    fn surface(&self, worker: &Worker) -> Option<SurfaceKey> {
        if self.renderer.path.is_empty() || self.place.w > 4096. || self.place.h > 4096. {
            return None;
        }
        self.frame_surface(worker)
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
    speed: Option<f64>,
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
    fn moving(&self) -> bool {
        self.ui.parked_only && self.speed.is_some_and(|s| s > 3.)
    }
    fn wants_run(&self, now: Instant) -> bool {
        self.ui.shown
            && self.ui.focused
            && !self.moving()
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
        let info = if let Some(error) = &self.failure {
            format!("Engine stopped: {error}")
        } else if self.moving() {
            "Parked only: the game waits while driving.".into()
        } else {
            HELP.into()
        };
        if changed || info != self.last_info {
            self.last_info = info.clone();
            let mut event = json!({"type":"lifecycle","running":self.running,"info":info});
            if !self.running && self.ui.shown && self.moving() {
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
    fn bound(&self) -> bool {
        self.worker.native()
            && self.bound_surface.is_some()
            && self.bound_surface == self.ui.surface(&self.worker)
    }
    fn sync_view(&mut self) -> Result<()> {
        let h = self.ui.place.h;
        let w = h * 544. / 960.;
        let rect = [
            ((self.ui.place.w - w) / 2.).round() as i32,
            0,
            w.round() as i32,
            h.round() as i32,
        ];
        self.worker.view(
            rect,
            self.bound()
                && self.binding.is_none()
                && self.running
                && !self.ui.sheet
                && self.ui.active
                && self.ui.renderer.on_screen
                && self.ui.renderer.visible,
        )?;
        if let Some(cursor) = self.scene["sprites"]["cursor"].as_object() {
            let point = self.controls.cursor();
            self.worker.cursor(
                [
                    point[0] as f32,
                    point[1] as f32,
                    cursor["hotspotX"].as_f64().unwrap_or(0.) as f32,
                    cursor["hotspotY"].as_f64().unwrap_or(0.) as f32,
                ],
                self.bound() && !self.playing && self.frames > 0 && self.failure.is_none(),
            )?;
        }
        Ok(())
    }
    fn binding_event(&self, binding: &Binding, stage: &str, out: &mut impl Write) -> Result<()> {
        host::event(
            out,
            json!({"type":"binding","id":binding.id,"stage":stage,"source":binding.key.source,
            "socket":binding.key.socket,"rectangle":binding.rectangle}),
        )
    }
    fn try_bind(&mut self, out: &mut impl Write, now: Instant) -> Result<()> {
        if !self.running
            || !self.ui.active
            || !self.ui.renderer.visible
            || self.binding.is_some()
            || self.pending.is_some()
            || self.worker.pending()
        {
            return Ok(());
        }
        let Some(key) = self.ui.surface(&self.worker) else {
            return Ok(());
        };
        if self.attempted.as_ref() == Some(&key) {
            return Ok(());
        }
        let rectangle = gpu::challenge(key.rectangle[2], key.rectangle[3])?;
        self.next_binding = self.next_binding.wrapping_add(1).max(1);
        let binding = Binding {
            id: self.next_binding,
            key: key.clone(),
            rectangle,
            stage: "prepare",
            deadline: now + Duration::from_secs(3),
        };
        self.attempted = Some(key);
        self.binding_event(&binding, "prepare", out)?;
        self.binding = Some(binding);
        self.sync_view()?;
        Ok(())
    }
    fn restore(&mut self, out: &mut impl Write, now: Instant) -> Result<()> {
        if let Some(mut binding) = self.binding.take() {
            binding.stage = "restore";
            binding.deadline = now + Duration::from_secs(3);
            self.binding_event(&binding, "restore", out)?;
            self.binding = Some(binding);
        }
        Ok(())
    }
    fn handle(
        &mut self,
        host: &Host,
        command: Value,
        out: &mut impl Write,
        now: Instant,
    ) -> Result<bool> {
        if !command.is_object() {
            return Err("Expected a session input object".into());
        }
        if command.get("shutdown") == Some(&Value::Bool(true)) {
            return Ok(false);
        }
        if let Some(ui) = command.get("ui") {
            self.ui = Ui::parse(ui)?;
            self.ui_at = now;
            self.dirty = true;
        }
        if let Some(vehicle) = command.get("vehicle")
            && let Some(raw) = vehicle["speedVehicleCentreOfGravity"]
                .as_f64()
                .filter(|n| n.is_finite() && *n != 65535.)
        {
            self.speed = Some(raw * 0.015625);
        }
        if let Some(ccu) = command.get("ccu") {
            let signal = ccu["source"] == "signal";
            if !signal {
                self.last_ccu = Some(now);
            }
            if !signal
                || self
                    .last_ccu
                    .is_none_or(|at| now.saturating_duration_since(at) >= Duration::from_secs(1))
            {
                let value = &ccu["value"];
                let operation = value["frontRotaryEncoderOperation"]
                    .as_f64()
                    .unwrap_or(f64::NAN);
                if self.running {
                    let deflection = code(
                        &value["frontRotaryEncoderDeflection"],
                        &[
                            ("MITTELSTELLUNG", 0.),
                            ("NORD_KURZ_GEDRUECKT", 1.),
                            ("NORD_LANG_GEDRUECKT", 2.),
                            ("OST_KURZ_GEDRUECKT", 4.),
                            ("OST_LANG_GEDRUECKT", 5.),
                            ("SUED_KURZ_GEDRUECKT", 7.),
                            ("SUED_LANG_GEDRUECKT", 8.),
                            ("WEST_KURZ_GEDRUECKT", 10.),
                            ("WEST_LANG_GEDRUECKT", 11.),
                        ],
                    );
                    let push = code(
                        &value["frontRotaryEncoderPushButton"],
                        &[
                            ("KEINE_BETAETIGUNG", 0.),
                            ("DREHGEBER_KURZ_GEDRUECKT", 1.),
                            ("DREHGEBER_LANG_GEDRUECKT", 2.),
                        ],
                    );
                    self.controls
                        .raw(deflection, push, operation, self.playing, self.ms(now));
                } else {
                    self.controls.observe(operation);
                }
                self.dirty = true;
            }
        }
        if let Some(touch) = command
            .get("touch")
            .filter(|_| self.running && self.ui.active)
        {
            let phase = touch["phase"]
                .as_u64()
                .unwrap_or_else(|| match touch["phase"].as_str() {
                    Some("down") => 1,
                    Some("move") => 2,
                    Some("up") => 3,
                    _ => 0,
                });
            self.controls.touch(
                u32::try_from(phase).map_err(|_| "Invalid touch phase")?,
                scalar(touch, "x", 0., -65536., 65536.)?,
                scalar(touch, "y", 0., -65536., 65536.)?,
                self.ui.place,
                self.playing,
                self.ms(now),
            );
            self.dirty = true;
        }
        if let Some(button) = command.get("button") {
            match button.as_str() {
                Some("play") => host::event(
                    out,
                    json!({"type":"lifecycle","running":self.running,"info":self.last_info,"open":"GAME"}),
                )?,
                Some("back") => host.action("back")?,
                Some("select") if self.running => self.controls.select(self.playing),
                Some("left") if self.running => self.controls.steer(-1., self.ms(now)),
                Some("right") if self.running => self.controls.steer(1., self.ms(now)),
                Some("select" | "left" | "right") => {}
                _ => return Err("Invalid session button".into()),
            }
            self.dirty = true;
        }
        if let Some(reply) = command.get("dialog") {
            let id = reply["id"]
                .as_u64()
                .and_then(|v| u32::try_from(v).ok())
                .ok_or("Invalid dialog reply")?;
            host.reply_dialog(id, reply["button"].as_str().ok_or("Invalid dialog reply")?)?;
            self.dialogs.remove(&id);
            self.dirty = true;
        }
        if let Some(reply) = command.get("binding") {
            let id = reply["id"]
                .as_u64()
                .and_then(|v| u32::try_from(v).ok())
                .ok_or("Invalid binding reply")?;
            let ok = reply["ok"].as_bool().ok_or("Invalid binding reply")?;
            if let Some(binding) = &self.binding
                && binding.id == id
            {
                match reply["stage"].as_str() {
                    Some("prepared") if binding.stage == "prepare" => {
                        let bind =
                            if ok && self.ui.surface(&self.worker).as_ref() == Some(&binding.key) {
                                self.worker
                                    .bind(&self.ui.renderer.path, binding.rectangle, now)?
                            } else {
                                false
                            };
                        if bind {
                            self.bound_surface = None;
                            self.binding.as_mut().unwrap().stage = "worker";
                        } else {
                            self.worker.reset(now, "Store surface preparation failed");
                            self.bound_surface = None;
                            self.force_reset = true;
                            self.restore(out, now)?;
                        }
                    }
                    Some("restored") if binding.stage == "restore" => {
                        if !ok {
                            self.worker.reset(now, "Store surface restoration failed");
                            self.bound_surface = None;
                            self.force_reset = true;
                        }
                        self.binding = None;
                        self.clock.restart(now);
                        self.dirty = true;
                    }
                    Some("prepared" | "restored") => {}
                    _ => return Err("Invalid binding reply".into()),
                }
            }
        }
        self.sync(host, out, now)?;
        Ok(true)
    }
    fn ms(&self, now: Instant) -> f64 {
        now.saturating_duration_since(self.started).as_secs_f64() * 1000.
    }
    fn gpu_mode(&self) -> bool {
        self.ui.renderer.ready
            && self.ui.renderer.native
            && self.ui.active
            && self.worker.ready()
            && (self.ui.renderer.video || self.bound())
    }
    fn can_frame(&self) -> bool {
        if !self.running
            || self.binding.is_some()
            || self.pending.is_some()
            || self.worker.pending()
        {
            return false;
        }
        if self.ui.active
            && self
                .ui
                .surface(&self.worker)
                .is_some_and(|key| self.attempted.as_ref() != Some(&key))
        {
            return false;
        }
        !(self.gpu_mode()
            && self.bound()
            && (!self.ui.renderer.visible || !self.ui.renderer.on_screen || self.ui.sheet))
    }
    fn worker_events(&mut self, host: &Host, out: &mut impl Write, now: Instant) -> Result<()> {
        for event in self.worker.poll(now) {
            match event {
                Event::Connected => {
                    self.force_reset = true;
                    self.dirty = true;
                }
                Event::Bound(native) => {
                    self.bound_surface = if native {
                        self.binding.as_ref().map(|b| b.key.clone())
                    } else {
                        None
                    };
                    self.restore(out, now)?;
                }
                Event::Disconnected(error) => {
                    if self.captured_worker.is_some() {
                        eprintln!("GPU worker: {error}");
                    }
                    self.bound_surface = None;
                    self.pending = None;
                    self.force_reset = true;
                    self.captured_worker = None;
                    self.dirty = true;
                    if self.binding.is_some() {
                        self.restore(out, now)?;
                    }
                }
                Event::Frame(frame) => {
                    if let Some(PendingFrame { key, began, timing }) = self.pending.take() {
                        let current = self.ui.frame_surface(&self.worker);
                        if current.as_ref() == Some(&key) {
                            if frame["mode"] == "native" {
                                self.mode = Some(Mode::Native);
                                if frame["presented"] == true && self.running {
                                    self.record_timing(timing, began, now);
                                    self.completed(began, now);
                                    host::event(
                                        out,
                                        json!({"type":"presented","source":key.source,"socket":key.socket,"fps":self.sample.rate.unwrap_or(60),"frames":self.frames,"playing":self.playing}),
                                    )?;
                                }
                            } else {
                                self.mode = Some(Mode::Video);
                                if self.running {
                                    self.record_timing(timing, began, now);
                                    self.completed(began, now);
                                }
                                host::event(
                                    out,
                                    json!({"type":"video","path":frame["path"],"width":frame["width"],"height":frame["height"]}),
                                )?;
                            }
                        } else {
                            self.force_reset = true;
                        }
                    }
                }
            }
        }
        self.sync(host, out, now)?;
        Ok(())
    }
    fn completed(&mut self, _began: Instant, now: Instant) {
        self.frames += 1;
        self.dirty = true;
        self.update_rate(now);
    }
    fn record_timing(&mut self, timing: Option<(f64, f64)>, began: Instant, now: Instant) {
        if let Some((wall, cpu)) = timing {
            self.sample.timing = Some((
                now,
                wall,
                cpu,
                now.saturating_duration_since(began).as_secs_f64() * 1000.,
            ));
        }
    }
    fn update_rate(&mut self, now: Instant) {
        let elapsed = now.saturating_duration_since(self.sample.at).as_secs_f64();
        if elapsed >= 1. {
            self.sample.rate =
                Some(((self.frames - self.sample.frames) as f64 / elapsed).round() as u64);
            self.sample.frames = self.frames;
            self.sample.at = now;
        }
    }
    fn render(
        &mut self,
        host: &Host,
        audio: Option<&mut std::fs::File>,
        out: &mut Output,
        now: Instant,
    ) -> Result<()> {
        let dt = self.clock.submit(now);
        self.frame_dt = dt;
        self.steps += 1;
        self.controls.move_cursor(self.playing, dt);
        let (tilt, mut touches) = self.controls.take(self.playing, self.ms(now));
        let tilt = if tilt == 0. { 0. } else { tilt };
        for touch in &mut touches {
            for value in touch {
                if *value == 0. {
                    *value = 0.;
                }
            }
        }
        let (w, h) = if self.ui.active && self.ui.place.full {
            (self.ui.place.w, self.ui.place.h)
        } else {
            (1600., 520.)
        };
        let side_width = ((w - h * 544. / 960.) / 2. * 960. / h)
            .ceil()
            .clamp(0., 2048.) as u64;
        let gpu = self.gpu_mode();
        let timing = self.ui.show_fps && now >= self.next_timing;
        let mut update = self.frame_state.render(
            host,
            FrameRequest {
                tilt,
                touches: &touches,
                dt,
                side_width,
                gpu,
                reset: gpu
                    && (self.force_reset || self.captured_worker != Some(self.worker.generation())),
                timing,
            },
            audio,
        )?;
        if let Some(image) = &update.texture {
            host::texture(out, 2, image)?;
        }
        if let Some(paper) = update.paper {
            self.scene["paper"] = paper;
        }
        if let Some(playing) = update.playing {
            self.playing = playing;
        }
        if update.timing.is_some() {
            self.next_timing = now + Duration::from_secs(1);
        }
        for dialog in update.dialogs {
            let id = dialog["id"]
                .as_u64()
                .and_then(|v| u32::try_from(v).ok())
                .ok_or("Invalid native dialog")?;
            self.dialogs.insert(id);
            self.sync(host, out, now)?;
            host::event(out, dialog)?;
        }
        if gpu {
            let key = self
                .ui
                .frame_surface(&self.worker)
                .ok_or("GPU renderer disappeared")?;
            if self
                .worker
                .submit_frame(std::mem::take(&mut update.payload), now)?
            {
                self.pending = Some(PendingFrame {
                    key,
                    began: now,
                    timing: update.timing,
                });
                self.captured_worker = Some(self.worker.generation());
                self.force_reset = false;
            } else {
                self.force_reset = true;
            }
        } else {
            out.packet(b"FRM2", update.payload)
                .map_err(|e| e.to_string())?;
            self.mode = Some(Mode::Pixels);
            let end = Instant::now();
            self.record_timing(update.timing, now, end);
            self.completed(now, end);
        }
        Ok(())
    }
    fn draw(&mut self, out: &mut impl Write, now: Instant) -> Result<()> {
        if !self.dirty {
            return Ok(());
        }
        self.dirty = false;
        self.update_rate(now);
        let dt = if self.running {
            now.saturating_duration_since(self.last_paint)
                .as_secs_f64()
                .clamp(0., 0.1)
        } else {
            0.
        };
        self.last_paint = now;
        let (w, h) = if self.ui.active && self.ui.place.full {
            (self.ui.place.w, self.ui.place.h)
        } else {
            (1600., 520.)
        };
        let tilt = self.controls.steering(self.playing, self.ms(now));
        let mut commands = self.world.draw(w, h, &self.scene, tilt, self.playing, dt);
        let gw = h * 544. / 960.;
        let gx = (w - gw) / 2.;
        match self.mode {
            Some(Mode::Pixels) => commands.push(json!(["image", 1, gx, 0., gw, h])),
            Some(Mode::Video)
                if self.ui.active
                    && self.ui.renderer.ready
                    && self.ui.renderer.native
                    && self.ui.renderer.video =>
            {
                commands.push(json!(["video", 1, gx, 0., gw, h]))
            }
            _ => {}
        }
        let message = self
            .failure
            .clone()
            .map(|e| format!("Engine stopped: {e}"))
            .or_else(|| {
                if self.frames == 0 {
                    Some("Loading original Doodle Jump...".into())
                } else {
                    None
                }
            });
        if let Some(message) = message {
            commands.push(
                json!(["text",message,w/2.,h/2.,24,"#573f2b",{"align":"center","font":"bold"}]),
            );
        }
        if self.frames > 0 && !self.playing && self.failure.is_none() {
            let cursor = &self.scene["sprites"]["cursor"];
            if let (Some(id), Some(cw), Some(ch)) = (
                cursor["id"].as_u64(),
                cursor["width"].as_f64(),
                cursor["height"].as_f64(),
            ) {
                let point = self.controls.cursor();
                let scale = 32. * h / 720. / ch;
                commands.push(json!([
                    "image",
                    id,
                    gx + point[0] * gw / 544. - cursor["hotspotX"].as_f64().unwrap_or(0.) * scale,
                    point[1] * h / 960. - cursor["hotspotY"].as_f64().unwrap_or(0.) * scale,
                    cw * scale,
                    ch * scale
                ]));
            }
        }
        if self.running && self.ui.show_fps && self.mode.is_some() {
            let size = (22. * h / 720.).min(gx / 20.);
            if size >= 9. {
                let mode = match self.mode {
                    Some(Mode::Native) => "Native",
                    Some(Mode::Video) => "Video",
                    _ => "CPU",
                };
                let rate = self
                    .sample
                    .rate
                    .map(|n| n.to_string())
                    .unwrap_or("--".into());
                let wall = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_err(|e| e.to_string())?
                    .as_secs_f64()
                    * 1000.;
                let render = self
                    .ui
                    .renderer
                    .stats
                    .filter(|(_, at)| wall >= *at && wall - *at < 2500.)
                    .map(|(fps, _)| fps.round().to_string())
                    .unwrap_or("--".into());
                let label = if mode == "Native" {
                    format!("Native | Presented {rate} FPS")
                } else {
                    format!("{mode} | Game {rate} | Render {render} FPS")
                };
                let timed = self.sample.timing.filter(|(at, _, _, _)| {
                    now.saturating_duration_since(*at) < Duration::from_millis(2500)
                });
                let reason = if mode == "CPU" {
                    if !self.ui.renderer.ready {
                        "No active renderer"
                    } else if !self.ui.renderer.native {
                        "Legacy renderer"
                    } else if !self.ui.renderer.video {
                        "Renderer video unavailable"
                    } else if self.worker.last_error().is_some() {
                        "GPU worker unavailable"
                    } else {
                        "GPU worker connecting"
                    }
                } else {
                    ""
                };
                let reason = if reason.is_empty() && !self.ui.renderer.interruption.is_empty() {
                    format!("Last | {}", self.ui.renderer.interruption)
                } else {
                    reason.into()
                };
                commands.push(json!([
                    "fillRect",
                    size / 2.,
                    size / 2.,
                    18. * size,
                    ((if timed.is_some() { 3.5 } else { 2. })
                        + if reason.is_empty() { 0. } else { 1.5 })
                        * size,
                    "#342b25"
                ]));
                commands.push(json!(["text",label,size,1.88*size,size,"#ffffff",{"literal":true}]));
                if let Some((_, wall, cpu, trip)) = timed {
                    commands.push(json!(["text",format!("Engine {} | CPU {} | Trip {} ms",wall.round(),cpu.round(),trip.round()),size,3.3*size,size*0.85,"#ffffff",{"literal":true}]));
                }
                if !reason.is_empty() {
                    commands.push(json!(["text",reason,size,(if timed.is_some(){4.75}else{3.3})*size,size*0.85,"#ffffff",{"literal":true}]));
                }
            }
        }
        host::event(
            out,
            json!({"type":"draw","w":w,"h":h,"full":self.ui.active&&self.ui.place.full,"frames":self.frames,"steps":self.steps,"playing":self.playing,"mode":match self.mode {Some(Mode::Native)=>"native",Some(Mode::Video)=>"video",Some(Mode::Pixels)=>"cpu",None=>"loading"},"pending":self.pending.is_some(),"frameDt":self.frame_dt,"commands":commands}),
        )
    }
}
fn code(value: &Value, names: &[(&str, f64)]) -> f64 {
    value.as_f64().unwrap_or_else(|| {
        value
            .as_str()
            .and_then(|name| names.iter().find(|(key, _)| *key == name).map(|(_, v)| *v))
            .unwrap_or(f64::NAN)
    })
}
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
fn run_inner(
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixListener;

    fn session(now: Instant) -> Session {
        Session::new(
            json!({"sprites":{},"paper":null}),
            Worker::new(PathBuf::from("/nonexistent/session-test.sock")),
            now,
        )
    }

    #[test]
    fn frame_deadlines_keep_sixty_hertz_under_small_wakeup_delays() {
        let start = Instant::now();
        let mut clock = Clock::new(start);
        for i in 1..=120 {
            let now = start + PERIOD * i + Duration::from_millis(2);
            clock.submit(now);
            assert_eq!(clock.next, start + PERIOD * (i + 1));
        }
    }

    #[test]
    fn resuming_discards_time_spent_hidden() {
        let start = Instant::now();
        let mut clock = Clock::new(start);
        clock.submit(start + PERIOD);
        let resume = start + Duration::from_secs(120);
        clock.restart(resume);
        assert!((clock.submit(resume + PERIOD) - 1. / 60.).abs() < 0.000001);
    }

    #[test]
    fn focus_visibility_driving_dialogs_and_stale_parent_each_stop_the_game() {
        let now = Instant::now();
        let mut s = session(now);
        s.ui.shown = true;
        s.ui.focused = true;
        assert!(s.wants_run(now));
        s.ui.shown = false;
        assert!(!s.wants_run(now));
        s.ui.shown = true;
        s.ui.focused = false;
        assert!(!s.wants_run(now));
        s.ui.focused = true;
        s.speed = Some(4.);
        assert!(!s.wants_run(now));
        s.ui.parked_only = false;
        assert!(s.wants_run(now));
        s.dialogs.insert(1);
        assert!(!s.wants_run(now));
        s.dialogs.clear();
        assert!(!s.wants_run(now + Duration::from_secs(2)));
        s.ui_at = now + Duration::from_secs(2);
        assert!(s.wants_run(now + Duration::from_secs(2)));
        s.failure = Some("stopped".into());
        assert!(!s.wants_run(now + Duration::from_secs(2)));
    }

    #[test]
    fn inactive_store_surface_draws_in_the_hmi_canvas_dimensions() {
        let now = Instant::now();
        let mut s = session(now);
        s.ui.place = Placement {
            w: 1920.,
            h: 720.,
            full: true,
            scale: 1.164,
            ox: 71.,
            oy: -38.,
        };
        s.ui.active = false;
        let mut out = vec![];
        s.draw(&mut out, now).unwrap();
        let draw: Value = serde_json::from_slice(&out[8..]).unwrap();
        assert_eq!(
            (
                draw["w"].as_f64(),
                draw["h"].as_f64(),
                draw["full"].as_bool()
            ),
            (Some(1600.), Some(520.), Some(false))
        );
    }

    #[test]
    fn an_inactive_native_renderer_can_bootstrap_with_cpu_frames() {
        let now = Instant::now();
        let directory =
            std::env::temp_dir().join(format!("doodle-session-gate-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("worker.sock");
        let _listener = UnixListener::bind(&path).unwrap();
        let mut worker = Worker::new(path);
        worker.poll(now);
        assert!(worker.ready());
        let mut s = Session::new(json!({"sprites":{},"paper":null}), worker, now);
        s.running = true;
        s.ui.renderer.ready = true;
        s.ui.renderer.native = true;
        s.ui.renderer.video = true;
        s.ui.renderer.path = "/tmp/renderer.sock".into();
        s.ui.active = false;
        assert!(
            s.can_frame(),
            "an unavailable Store surface must not deadlock CPU fallback"
        );
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn an_attempt_does_not_make_a_previous_native_binding_current() {
        let now = Instant::now();
        let directory =
            std::env::temp_dir().join(format!("doodle-session-bound-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("worker.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let mut worker = Worker::new(path);
        worker.poll(now);
        assert!(worker.ready());
        let (mut peer, _) = listener.accept().unwrap();
        assert!(
            worker
                .bind("/tmp/old-renderer.sock", [-9000, -9000, 1920, 720], now)
                .unwrap()
        );
        worker.poll(now);
        let mut header = [0; 8];
        peer.read_exact(&mut header).unwrap();
        let mut payload = vec![0; u32::from_le_bytes(header[4..8].try_into().unwrap()) as usize];
        peer.read_exact(&mut payload).unwrap();
        peer.write_all(b"{\"event\":\"bind\",\"native\":true}\n")
            .unwrap();
        worker.poll(now);
        assert!(worker.native());
        let mut s = Session::new(json!({"sprites":{},"paper":null}), worker, now);
        s.ui.renderer = Renderer {
            ready: true,
            native: true,
            video: true,
            visible: true,
            on_screen: true,
            source: 2,
            socket: 2,
            path: "/tmp/new-renderer.sock".into(),
            ..Renderer::default()
        };
        s.attempted = s.ui.surface(&s.worker);
        assert!(
            !s.bound(),
            "a new challenge must not adopt the previous renderer's native surface"
        );
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn untimed_frames_do_not_make_old_measurements_fresh() {
        let now = Instant::now();
        let mut s = session(now);
        s.sample.timing = Some((now, 2., 1., 4.));
        s.completed(
            now + Duration::from_millis(50),
            now + Duration::from_millis(60),
        );
        assert_eq!(s.sample.timing, Some((now, 2., 1., 4.)));
    }

    #[test]
    fn the_fps_display_reports_zero_when_no_frame_is_completed() {
        let now = Instant::now();
        let mut s = session(now);
        s.running = true;
        s.ui.show_fps = true;
        s.frames = 60;
        s.sample.frames = 60;
        s.sample.rate = Some(60);
        s.mode = Some(Mode::Native);
        let mut out = vec![];
        s.draw(&mut out, now + Duration::from_secs(2)).unwrap();
        let draw: Value = serde_json::from_slice(&out[8..]).unwrap();
        assert!(
            draw["commands"]
                .as_array()
                .unwrap()
                .iter()
                .any(|command| command[0] == "text" && command[1] == "Native | Presented 0 FPS")
        );
    }

    #[test]
    fn invalid_layout_and_socket_inputs_are_rejected() {
        for ui in [
            json!({"place":{"w":0}}),
            json!({"place":{"x":1.5}}),
            json!({"renderer":{"path":"relative.sock"}}),
            json!({"focused":1}),
        ] {
            assert!(Ui::parse(&ui).is_err());
        }
    }
}
