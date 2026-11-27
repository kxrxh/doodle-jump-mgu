use super::{FrameRequest, FrameState, Host};
use crate::{
    Result,
    images::{self, Image},
};
use serde_json::{Value, json};
use std::{
    io::{self, BufRead, Read, Write},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub(crate) fn packet(output: &mut impl Write, kind: &[u8; 4], bytes: &[u8]) -> Result<()> {
    let size = u32::try_from(bytes.len()).map_err(|_| "Engine packet is too large")?;
    output
        .write_all(kind)
        .and_then(|_| output.write_all(&size.to_le_bytes()))
        .and_then(|_| output.write_all(bytes))
        .and_then(|_| output.flush())
        .map_err(|e| e.to_string())
}
pub(crate) fn event(out: &mut impl Write, value: Value) -> Result<()> {
    packet(
        out,
        b"EVT1",
        &serde_json::to_vec(&value).map_err(|e| e.to_string())?,
    )
}
pub(crate) fn texture(out: &mut impl Write, id: u32, image: &Image) -> Result<()> {
    let mut bytes = Vec::with_capacity(12 + image.pixels.len());
    for v in [id, image.width, image.height] {
        bytes.extend(v.to_le_bytes());
    }
    bytes.extend_from_slice(&image.pixels);
    packet(out, b"TEX1", &bytes)
}
fn number(value: &Value, low: f64, high: f64) -> Result<f64> {
    let n = value
        .as_f64()
        .filter(|n| n.is_finite() && *n >= low && *n <= high)
        .ok_or("Invalid game input number")?;
    Ok(n)
}
fn boolean(value: Option<&Value>, default: bool) -> Result<bool> {
    match value {
        None => Ok(default),
        Some(Value::Bool(b)) => Ok(*b),
        _ => Err("Invalid game frame flag".into()),
    }
}

pub fn run() -> Result<()> {
    let mut original = None;
    let mut data = None;
    let mut runtime = None;
    let mut playback = true;
    let mut session = false;
    let mut audio_output = None;
    let mut audio_events = None;
    let mut epoch = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_secs() as i64;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--original" => original = args.next().map(PathBuf::from),
            "--data" => data = args.next().map(PathBuf::from),
            "--runtime" => runtime = args.next().map(PathBuf::from),
            "--no-audio" => playback = false,
            "--session" => session = true,
            "--audio-output" => {
                audio_output = args.next().map(PathBuf::from);
                playback = false;
            }
            "--audio-events" => audio_events = args.next().map(PathBuf::from),
            "--epoch" => {
                epoch = args
                    .next()
                    .ok_or("Missing epoch")?
                    .parse()
                    .map_err(|_| "Invalid epoch")?
            }
            _ => return Err(format!("Unknown argument {a}")),
        }
    }
    let binary = std::env::current_exe().map_err(|e| e.to_string())?;
    let folder = binary
        .parent()
        .ok_or("Executable has no parent directory")?;
    let original = original.unwrap_or_else(|| folder.join("original"));
    let runtime = runtime.unwrap_or_else(|| original.parent().unwrap_or(folder).to_path_buf());
    let data = data.ok_or("A data directory is required")?;
    let mut output = io::stdout().lock();
    let captures = (audio_output, audio_events);
    let result = if session {
        crate::session::run(&original, &data, &runtime, epoch, playback, captures)
    } else {
        run_process(
            &original,
            &data,
            &runtime,
            epoch,
            playback,
            captures,
            &mut output,
        )
    };
    if let Err(e) = &result
        && !session
    {
        let _ = event(&mut output, json!({"type":"error","message":e}));
    }
    result
}

fn run_process(
    original: &Path,
    data: &Path,
    runtime: &Path,
    epoch: i64,
    playback: bool,
    captures: (Option<PathBuf>, Option<PathBuf>),
    out: &mut impl Write,
) -> Result<()> {
    let mut audio_output = captures
        .0
        .map(std::fs::File::create)
        .transpose()
        .map_err(|e| e.to_string())?;
    let audio_events = captures
        .1
        .map(std::fs::File::create)
        .transpose()
        .map_err(|e| e.to_string())?;
    let host = Host::new(original, data, runtime, epoch, playback, audio_events)?;
    let (sprites, metadata) =
        images::scenery_assets(&host.assets, &runtime.join("assets/cursor.png"))?;
    for (id, image) in sprites {
        texture(out, id, &image)?;
    }
    event(out, json!({"type":"scenery","sprites":metadata}))?;
    event(out, json!({"type":"ready"}))?;
    let mut tilt = 0.;
    let mut paused = false;
    let mut frame_state = FrameState::default();
    let mut input = io::stdin().lock();
    loop {
        let mut bytes = vec![];
        let n = input
            .by_ref()
            .take(4098)
            .read_until(b'\n', &mut bytes)
            .map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        if n > 4096 {
            return Err("Engine input exceeds its size limit".into());
        }
        let c: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
        if !c.is_object() {
            return Err("Expected an input object".into());
        }
        if let Some(reply) = c.get("dialog") {
            let id = reply["id"]
                .as_u64()
                .and_then(|n| u32::try_from(n).ok())
                .ok_or("Invalid dialog reply")?;
            host.jni()?.reply_dialog(
                &host.machine,
                id,
                reply["button"].as_str().ok_or("Invalid dialog reply")?,
            )?;
        }
        if let Some(value) = c.get("tilt") {
            tilt = number(value, -1., 1.)?;
        }
        let mut touches = vec![];
        if let Some(value) = c.get("touches") {
            touches = value.as_array().ok_or("Invalid touch events")?.clone();
        }
        if let Some(value) = c.get("touch") {
            touches.push(value.clone());
        }
        if touches.len() > 16 {
            return Err("Invalid touch events".into());
        }
        let mut parsed = vec![];
        for touch in touches {
            let a = touch
                .as_array()
                .filter(|a| a.len() == 4)
                .ok_or("Invalid touch event")?;
            let id = a[0]
                .as_u64()
                .filter(|n| *n < 16)
                .ok_or("Invalid touch pointer")?;
            let phase = a[1]
                .as_u64()
                .filter(|n| *n <= 2)
                .ok_or("Invalid touch phase")?;
            parsed.push([
                id as f64,
                phase as f64,
                number(&a[2], 0., 544.)?,
                number(&a[3], 0., 960.)?,
            ]);
        }
        if let Some(action) = c.get("action") {
            match action.as_str() {
                Some("back") => {
                    host.send(-7, 0, 0, 0)?;
                }
                Some("pause") | Some("resume") => {
                    paused = action == "pause";
                    host.send(if paused { 28 } else { 29 }, 0, 0, 0)?;
                    host.audio_message(if paused { 12 } else { 13 }, 0, 1., 0)?;
                }
                _ => return Err("Invalid game action".into()),
            }
        }
        if !boolean(c.get("frame"), false)? || paused {
            continue;
        }
        let dt = number(c.get("dt").unwrap_or(&json!(1. / 60.)), 0., 0.1)?;
        let width = c
            .get("side_width")
            .map(|n| {
                n.as_u64()
                    .filter(|n| *n <= 2048)
                    .ok_or("Invalid panorama width")
            })
            .transpose()?
            .unwrap_or(1008);
        let gpu = boolean(c.get("gpu"), false)?;
        let reset = boolean(c.get("gpu_reset"), false)?;
        let timing = boolean(c.get("timing"), false)?;
        let update = frame_state.render(
            &host,
            FrameRequest {
                tilt,
                touches: &parsed,
                dt,
                side_width: width,
                gpu,
                reset,
                timing,
            },
            audio_output.as_mut(),
        )?;
        update.emit(out)?;
    }
    Ok(())
}

#[cfg(test)]
#[path = "../../tests/unit/host.rs"]
mod tests;
