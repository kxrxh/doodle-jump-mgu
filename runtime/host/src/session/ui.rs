use super::{Placement, Renderer, Result, Session, SurfaceKey, Ui};
use crate::gpu::Worker;
use serde_json::Value;

fn flag(value: &Value, name: &str, default: bool) -> Result<bool> {
    match value.get(name) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Bool(v)) => Ok(*v),
        _ => Err(format!("Invalid UI flag {name}")),
    }
}
pub(super) fn scalar(value: &Value, name: &str, default: f64, low: f64, high: f64) -> Result<f64> {
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
    pub(super) fn parse(value: &Value) -> Result<Self> {
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
    pub(super) fn frame_surface(&self, worker: &Worker) -> Option<SurfaceKey> {
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
    pub(super) fn surface(&self, worker: &Worker) -> Option<SurfaceKey> {
        if self.renderer.path.is_empty() || self.place.w > 4096. || self.place.h > 4096. {
            return None;
        }
        self.frame_surface(worker)
    }
}

impl Session {
    pub(super) fn sync_view(&mut self) -> Result<()> {
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
}
