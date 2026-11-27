use super::{Host, event, packet, texture};
use crate::{
    Result, ffi,
    images::{Image, Paper},
};
use serde_json::{Value, json};
use std::{
    io::{self, Write},
    time::Instant,
};

fn thread_cpu_ms() -> Result<f64> {
    let mut time = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    if unsafe { libc::clock_gettime(libc::CLOCK_THREAD_CPUTIME_ID, &mut time) } != 0 {
        return Err(io::Error::last_os_error().to_string());
    }
    Ok(time.tv_sec as f64 * 1000. + time.tv_nsec as f64 / 1_000_000.)
}

#[derive(Default)]
pub(crate) struct FrameState {
    playing: bool,
    gpu_mode: bool,
    paper: Paper,
    projection: Value,
}

pub(crate) struct FrameRequest<'a> {
    pub tilt: f64,
    pub touches: &'a [[f64; 4]],
    pub dt: f64,
    pub side_width: u64,
    pub gpu: bool,
    pub reset: bool,
    pub timing: bool,
}

pub(crate) struct FrameUpdate {
    pub texture: Option<Image>,
    pub paper: Option<Value>,
    pub dialogs: Vec<Value>,
    pub playing: Option<bool>,
    pub payload: Vec<u8>,
    pub gpu: bool,
    pub timing: Option<(f64, f64)>,
}

impl FrameState {
    pub(crate) fn render(
        &mut self,
        host: &Host,
        request: FrameRequest<'_>,
        audio: Option<&mut std::fs::File>,
    ) -> Result<FrameUpdate> {
        let began = Instant::now();
        let cpu_started = if request.timing { thread_cpu_ms()? } else { 0. };
        if request.gpu || self.gpu_mode {
            host.graphics()?.begin(request.gpu, request.reset)?;
        }
        if request.gpu != self.gpu_mode {
            let g = host.graphics()?;
            if !g.encoder.get().is_null() {
                unsafe { ffi::frame_encoder_destroy(g.encoder.replace(std::ptr::null_mut())) }
            }
            self.gpu_mode = request.gpu;
        }
        host.step(request.tilt, request.touches, request.dt)?;
        if let Some(file) = audio {
            let pcm = host.audio.mix(735)?;
            let bytes = pcm
                .iter()
                .flat_map(|sample| sample.to_le_bytes())
                .collect::<Vec<_>>();
            file.write_all(&bytes).map_err(|e| e.to_string())?;
        }
        let (projection, texture) = host.paper(&mut self.paper, request.side_width)?;
        let paper = if projection != self.projection {
            self.projection = projection.clone();
            Some(projection)
        } else {
            None
        };
        let dialogs = std::mem::take(&mut *host.jni()?.dialogs.borrow_mut());
        let now = host.jni()?.playing.get();
        let playing = if now != self.playing {
            self.playing = now;
            Some(now)
        } else {
            None
        };
        let payload = if request.gpu {
            host.graphics()?.gpu()?
        } else {
            host.graphics()?.pixels()?
        };
        let timing = if request.timing {
            Some((
                began.elapsed().as_secs_f64() * 1000.,
                thread_cpu_ms()? - cpu_started,
            ))
        } else {
            None
        };
        Ok(FrameUpdate {
            texture,
            paper,
            dialogs,
            playing,
            payload,
            gpu: request.gpu,
            timing,
        })
    }
}

impl FrameUpdate {
    pub(crate) fn emit(&self, out: &mut impl Write) -> Result<()> {
        if let Some(image) = &self.texture {
            texture(out, 2, image)?;
        }
        if let Some(paper) = &self.paper {
            event(out, json!({"type":"scenery","paper":paper}))?;
        }
        for dialog in &self.dialogs {
            event(out, dialog.clone())?;
        }
        if let Some(playing) = self.playing {
            event(out, json!({"type":"playing","value":playing}))?;
        }
        if let Some((wall, cpu)) = self.timing {
            event(out, json!({"type":"timing","wallMs":wall,"cpuMs":cpu}))?;
        }
        packet(out, if self.gpu { b"GPF1" } else { b"FRM2" }, &self.payload)
    }
}
