use super::{Result, Session, Ui, ui::scalar};
use crate::host::{self, Host};
use serde_json::{Value, json};
use std::{
    io::Write,
    time::{Duration, Instant},
};

impl Session {
    pub(super) fn handle(
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
                let current = self.ui.surface(&self.worker).as_ref() == Some(&binding.key);
                match reply["stage"].as_str() {
                    Some("prepared") if binding.stage == "prepare" => {
                        let bind = if ok && current {
                            self.worker
                                .bind(&self.ui.renderer.path, binding.rectangle, now)?
                        } else {
                            false
                        };
                        if bind {
                            self.bound_surface = None;
                            self.binding.as_mut().unwrap().stage = "worker";
                        } else {
                            // A replaced Store renderer may reply after its surface is gone.
                            // The worker can keep serving VIDEO if it owns no native surface.
                            if current || self.worker.native() {
                                self.worker.reset(now, "Store surface preparation failed");
                            }
                            self.bound_surface = None;
                            self.force_reset = true;
                            self.restore(out, now)?;
                        }
                    }
                    Some("restored") if binding.stage == "restore" => {
                        if !ok && (current || self.worker.native()) {
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
}

fn code(value: &Value, names: &[(&str, f64)]) -> f64 {
    value.as_f64().unwrap_or_else(|| {
        value
            .as_str()
            .and_then(|name| names.iter().find(|(key, _)| *key == name).map(|(_, v)| *v))
            .unwrap_or(f64::NAN)
    })
}
