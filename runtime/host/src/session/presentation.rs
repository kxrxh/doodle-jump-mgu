use super::{BIND_RETRY_DELAY, Binding, Mode, PendingFrame, Result, Session};
use crate::{
    gpu::{self, Event},
    host::{self, FrameRequest, Host},
    output::Output,
};
use serde_json::json;
use std::{
    io::Write,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

impl Session {
    fn native_failed(&self, key: &super::SurfaceKey) -> bool {
        self.failed_native.as_ref().is_some_and(|failed| {
            failed.source == key.source
                && failed.socket == key.socket
                && failed.generation == key.generation
                && failed.rectangle == key.rectangle
        })
    }
    pub(super) fn bound(&self) -> bool {
        self.worker.native()
            && self.bound_surface.is_some()
            && self.bound_surface == self.ui.surface(&self.worker)
    }
    fn binding_event(&self, binding: &Binding, stage: &str, out: &mut impl Write) -> Result<()> {
        host::event(
            out,
            json!({"type":"binding","id":binding.id,"stage":stage,"source":binding.key.source,
            "socket":binding.key.socket,"rectangle":binding.rectangle}),
        )
    }
    pub(super) fn try_bind(&mut self, out: &mut impl Write, now: Instant) -> Result<()> {
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
        if self.native_failed(&key) {
            return Ok(());
        }
        let retry = self.attempted.as_ref() == Some(&key);
        if retry && self.bind_retry_at.is_none_or(|deadline| now < deadline) {
            return Ok(());
        }
        self.bind_retry_at = None;
        let rectangle = gpu::challenge(key.rectangle[2], key.rectangle[3])?;
        self.next_binding = self.next_binding.wrapping_add(1).max(1);
        let binding = Binding {
            id: self.next_binding,
            key: key.clone(),
            rectangle,
            retry,
            stage: "prepare",
            deadline: now + Duration::from_secs(3),
        };
        self.attempted = Some(key);
        self.binding_event(&binding, "prepare", out)?;
        self.binding = Some(binding);
        self.sync_view()?;
        Ok(())
    }
    pub(super) fn restore(&mut self, out: &mut impl Write, now: Instant) -> Result<()> {
        if let Some(mut binding) = self.binding.take() {
            binding.stage = "restore";
            binding.deadline = now + Duration::from_secs(3);
            self.binding_event(&binding, "restore", out)?;
            self.binding = Some(binding);
        }
        Ok(())
    }
    pub(super) fn gpu_mode(&self) -> bool {
        self.ui.renderer.ready
            && self.ui.renderer.native
            && self.ui.active
            && self.worker.ready()
            && (self.ui.renderer.video || self.bound())
    }
    pub(super) fn can_frame(&self) -> bool {
        if !self.running
            || self.binding.is_some()
            || self.pending.is_some()
            || self.worker.pending()
        {
            return false;
        }
        if self.ui.active
            && self.ui.surface(&self.worker).is_some_and(|key| {
                !self.native_failed(&key) && self.attempted.as_ref() != Some(&key)
            })
        {
            return false;
        }
        !(self.gpu_mode()
            && self.bound()
            && (!self.ui.renderer.visible || !self.ui.renderer.on_screen || self.ui.sheet))
    }
    pub(super) fn worker_events(
        &mut self,
        host: &Host,
        out: &mut impl Write,
        now: Instant,
    ) -> Result<()> {
        for event in self.worker.poll(now) {
            match event {
                Event::Connected => {
                    eprintln!("GPU worker: socket connected");
                    self.force_reset = true;
                    self.dirty = true;
                }
                Event::Bound { native, reason } => {
                    self.logged_worker_error = None;
                    self.bound_surface = if native {
                        self.binding.as_ref().map(|b| b.key.clone())
                    } else {
                        None
                    };
                    if !native {
                        let retry = self
                            .binding
                            .as_ref()
                            .is_some_and(|binding| !binding.retry && binding.stage == "worker")
                            && matches!(
                                reason.as_deref(),
                                None | Some("renderer_unavailable" | "layer_not_ready")
                            );
                        if retry {
                            // Keep the attempted key so VIDEO remains eligible during backoff.
                            self.bind_retry_at = Some(now + BIND_RETRY_DELAY);
                        }
                        let status = if retry { " (one retry scheduled)" } else { "" };
                        eprintln!(
                            "GPU native bind failed: {}{status}",
                            reason
                                .as_deref()
                                .unwrap_or("worker did not report a reason")
                        );
                    }
                    self.restore(out, now)?;
                }
                Event::Disconnected => {
                    if self.worker.last_error() == Some("GPU worker frame timed out")
                        && self.bound_surface.is_some()
                    {
                        self.failed_native = self.bound_surface.clone();
                        eprintln!(
                            "Native frame timed out; using fallback until the Store renderer changes"
                        );
                    }
                    self.bind_retry_at = None;
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
                    self.logged_worker_error = None;
                    if let Some(PendingFrame { key, began, timing }) = self.pending.take() {
                        let current = self.ui.frame_surface(&self.worker);
                        if current.as_ref() == Some(&key) {
                            if frame["mode"] == "native" {
                                if frame["presented"] == true && self.running {
                                    self.failed_native = None;
                                    self.set_mode(Mode::Native);
                                    self.record_timing(timing, began, now);
                                    self.completed(began, now);
                                    host::event(
                                        out,
                                        json!({"type":"presented","source":key.source,"socket":key.socket,"fps":self.sample.rate.unwrap_or(60),"frames":self.frames,"playing":self.playing}),
                                    )?;
                                }
                            } else {
                                self.set_mode(Mode::Video);
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
        if let Some(error) = self.worker.last_error()
            && self.logged_worker_error.as_deref() != Some(error)
        {
            eprintln!("GPU worker unavailable: {error}");
            self.logged_worker_error = Some(error.to_owned());
        }
        self.sync(host, out, now)?;
        Ok(())
    }
    fn set_mode(&mut self, mode: Mode) {
        if self.mode != Some(mode) {
            let label = match mode {
                Mode::Pixels => "CPU",
                Mode::Video => "VIDEO",
                Mode::Native => "Native",
            };
            eprintln!("GPU presentation: {label}");
            self.mode = Some(mode);
        }
    }
    pub(super) fn completed(&mut self, _began: Instant, now: Instant) {
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
    pub(super) fn render(
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
            self.set_mode(Mode::Pixels);
            let end = Instant::now();
            self.record_timing(update.timing, now, end);
            self.completed(now, end);
        }
        Ok(())
    }
    pub(super) fn draw(&mut self, out: &mut impl Write, now: Instant) -> Result<()> {
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
