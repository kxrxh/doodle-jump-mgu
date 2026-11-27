mod command;
mod frame;
mod graphics;

pub use command::run;
pub(crate) use command::{event, packet, texture};
pub(crate) use frame::{FrameRequest, FrameState};
use graphics::{Graphics, PaperTexture};

use crate::{
    Result,
    audio::Audio,
    ffi,
    jni::{self, Jni},
    libc::LibC,
    machine::{BASE, Machine, Return},
};
use serde_json::{Value, json};
use std::{
    cell::{Cell, OnceCell, RefCell},
    io::Write,
    panic::{AssertUnwindSafe, catch_unwind},
    path::{Path, PathBuf},
};
const PREFIX: &str = "Java_com_limasky_doodlejumpandroid_NotificationCenter_";
pub struct Host {
    pub assets: PathBuf,
    pub data: PathBuf,
    libc: OnceCell<LibC>,
    jni: OnceCell<Jni>,
    graphics: OnceCell<Graphics>,
    audio: Audio,
    audio_events: RefCell<Option<std::fs::File>>,
    audio_enabled: Cell<bool>,
    paper_texture: RefCell<Option<PaperTexture>>,
    touches: Cell<u32>,
    acceleration: Cell<u32>,
    keys: Cell<u32>,
    // Guest mappings outlive the graphics snapshots and all host resources.
    pub machine: Machine,
}
unsafe extern "C" fn callback(user: ffi::Ptr, address: u64, output: *mut ffi::NativeResult) -> i32 {
    // Host lives in one Box until all dispatch hooks are destroyed. Guest calls are reentrant
    // on this thread; state borrows end before any call that can invoke another callback.
    let host = unsafe { &*user.cast::<Host>() };
    let result = catch_unwind(AssertUnwindSafe(|| host.invoke(address as u32)));
    match result {
        Ok(Ok(value)) => {
            let (value, flags) = match value {
                Return::Word(v) => (v as u64, 1),
                Return::Wide(v) => (v, 3),
                Return::Void => (0, 0),
            };
            unsafe {
                (*output).value = value;
                (*output).flags = flags
            };
            0
        }
        other => {
            let e = match other {
                Ok(Err(e)) => e,
                _ => "Native host callback panicked".into(),
            };
            if let Some(slot) = host.machine.errors.borrow_mut().last_mut() {
                *slot = Some(e);
            }
            1
        }
    }
}
impl Host {
    pub(crate) fn new(
        original: &Path,
        data: &Path,
        runtime: &Path,
        epoch: i64,
        playback: bool,
        audio_events: Option<std::fs::File>,
    ) -> Result<Box<Self>> {
        std::fs::create_dir_all(data).map_err(|e| e.to_string())?;
        let m = Machine::new(runtime)?;
        let h = Box::new(Self {
            machine: m,
            assets: original
                .join("extracted/assets")
                .canonicalize()
                .map_err(|e| e.to_string())?,
            data: data.canonicalize().map_err(|e| e.to_string())?,
            libc: OnceCell::new(),
            jni: OnceCell::new(),
            graphics: OnceCell::new(),
            audio: Audio::new(playback)?,
            audio_events: RefCell::new(audio_events),
            audio_enabled: Cell::new(false),
            paper_texture: RefCell::new(None),
            touches: Cell::new(0),
            acceleration: Cell::new(0),
            keys: Cell::new(0),
        });
        h.machine
            .bind(callback, (&*h as *const Self).cast_mut().cast())?;
        h.machine
            .load(&original.join("extracted/lib/armeabi-v7a/libDoodleJump.so"))?;
        h.libc
            .set(LibC::new(&h.machine, epoch)?)
            .map_err(|_| "Duplicate libc initialization")?;
        h.machine.initialize()?;
        h.jni
            .set(Jni::new(&h.machine)?)
            .map_err(|_| "Duplicate JNI initialization")?;
        h.graphics
            .set(Graphics::new(&h.machine)?)
            .map_err(|_| "Duplicate graphics initialization")?;
        h.machine.intercept(BASE + 0xadc58, "host.decode_png")?;
        let j = h.jni()?;
        h.touches.set(j.array(&h.machine, 16 * 4, 4)?);
        h.acceleration.set(j.array(&h.machine, 12, 4)?);
        h.keys.set(j.array(&h.machine, 32 * 2, 4)?);
        for message in [-1, -4, 29] {
            h.send(message, 0, 0, 0)?;
        }
        h.send(0, j.configuration(&h.machine)?, 0, 0)?;
        h.audio_enabled.set(true);
        Ok(h)
    }
    fn jni(&self) -> Result<&Jni> {
        self.jni.get().ok_or("JNI is not initialized".into())
    }
    fn graphics(&self) -> Result<&Graphics> {
        self.graphics
            .get()
            .ok_or("Graphics is not initialized".into())
    }
    fn invoke(&self, address: u32) -> Result<Return> {
        let name = self
            .machine
            .hooks
            .borrow()
            .get(&address)
            .cloned()
            .ok_or_else(|| format!("Unknown native callback {address:#x}"))?;
        let m = &self.machine;
        if name == "malloc" {
            return Ok(Return::Word(m.malloc(m.arg(0)? as usize)?));
        }
        if name == "free" {
            m.free(m.arg(0)?)?;
            return Ok(Return::Word(0));
        }
        if let Some(index) = name.strip_prefix("JNI.") {
            return self
                .jni()?
                .invoke(self, index.parse().map_err(|_| "Invalid JNI slot")?);
        }
        if let Some(index) = name.strip_prefix("JavaVM.") {
            let i = index.parse::<u32>().map_err(|_| "Invalid JavaVM slot")?;
            if [4, 6, 7].contains(&i) {
                m.put32(m.arg(1)?, self.jni()?.env)?;
            }
            return Ok(Return::Word(0));
        }
        if name == "host.decode_png" {
            return self.decode_png();
        }
        if name.starts_with("AAsset")
            && let Some(v) = self.jni()?.asset_call(self, &name)?
        {
            return Ok(v);
        }
        if let Some(v) = self
            .libc
            .get()
            .ok_or("libc is not initialized")?
            .invoke(&name, m)?
        {
            return Ok(v);
        }
        // Normally these are emitted as guest helpers; their slow forms remain explicit.
        match name.as_str() {
            "memcpy" | "memmove" | "__aeabi_memcpy" | "__aeabi_memmove" => Ok(Return::Word(
                m.copy(m.arg(0)?, m.arg(1)?, m.arg(2)? as usize)?,
            )),
            "memset" => Ok(Return::Word(m.fill(
                m.arg(0)?,
                m.arg(1)? as u8,
                m.arg(2)? as usize,
            )?)),
            "__aeabi_memset" => Ok(Return::Word(m.fill(
                m.arg(0)?,
                m.arg(2)? as u8,
                m.arg(1)? as usize,
            )?)),
            _ => Err(format!("Host import {name}, caller {:#x}", m.reg(10)?)),
        }
    }
    pub fn load_audio(&self, id: u32, name: &str, looping: bool) -> Result<()> {
        if !self.audio_enabled.get() {
            return Ok(());
        }
        let mut p = jni::path(&self.assets, name)?;
        if !p.is_file() {
            p = jni::path(
                &self.assets,
                Path::new(name)
                    .file_name()
                    .and_then(|n| n.to_str())
                    .ok_or("Invalid sound filename")?,
            )?;
        }
        self.trace_audio(json!({"type":"load","id":id,"path":p.strip_prefix(&self.assets).map_err(|e|e.to_string())?,"loop":looping}))?;
        self.audio.load(id, &p, looping)
    }
    pub fn audio_message(&self, message: i32, id: u32, gain: f64, loops: i32) -> Result<()> {
        if !self.audio_enabled.get() {
            return Ok(());
        }
        self.trace_audio(
            json!({"type":"message","message":message,"id":id,"gain":gain,"loops":loops}),
        )?;
        self.audio.message(message, id, gain, loops)
    }
    fn trace_audio(&self, value: Value) -> Result<()> {
        if let Some(file) = self.audio_events.borrow_mut().as_mut() {
            serde_json::to_writer(&mut *file, &value).map_err(|e| e.to_string())?;
            file.write_all(b"\n").map_err(|e| e.to_string())?;
        }
        Ok(())
    }
    fn send(&self, message: i32, obj: u32, target: u32, sender: u32) -> Result<u32> {
        let j = self.jni()?;
        self.machine.call(
            self.machine.symbol(&format!("{PREFIX}sendMessageNative"))?,
            &[j.env, j.center.get(), message as u32, obj, target, sender],
        )
    }
    fn step(&self, tilt: f64, touches: &[[f64; 4]], dt: f64) -> Result<()> {
        self.libc
            .get()
            .ok_or("libc is not initialized")?
            .advance(dt);
        let m = &self.machine;
        let j = self.jni()?;
        let g = self.graphics()?;
        let touches = touches
            .iter()
            .filter(|e| !g.blocked(e[2], e[3]))
            .collect::<Vec<_>>();
        unsafe { ffi::graphics_reset_rectangles(g.handle) };
        loop {
            let next = {
                let mut pending = j.deferred.borrow_mut();
                if pending.is_empty() {
                    None
                } else {
                    Some(pending.remove(0))
                }
            };
            let Some((msg, obj, target, sender)) = next else {
                break;
            };
            self.send(msg, obj, target, sender)?;
        }
        if !touches.is_empty() {
            let data = touches
                .iter()
                .flat_map(|t| t.iter().flat_map(|v| (*v as f32).to_le_bytes()))
                .collect::<Vec<_>>();
            m.write(j.array_pointer(self.touches.get())?, &data)?;
        }
        let data = [(-tilt.clamp(-1., 1.) * 6.) as f32, 0., 9.81]
            .iter()
            .flat_map(|v| v.to_le_bytes())
            .collect::<Vec<_>>();
        m.write(j.array_pointer(self.acceleration.get())?, &data)?;
        m.call(
            m.symbol(&format!("{PREFIX}runGameLoopNative"))?,
            &[
                j.env,
                j.center.get(),
                touches.len() as u32,
                self.touches.get(),
                1,
                self.acceleration.get(),
                0,
                self.keys.get(),
            ],
        )?;
        Ok(())
    }
    pub(crate) fn action(&self, action: &str) -> Result<()> {
        match action {
            "back" => self.send(-7, 0, 0, 0).map(|_| ()),
            "pause" | "resume" => {
                let paused = action == "pause";
                self.send(if paused { 28 } else { 29 }, 0, 0, 0)?;
                self.audio_message(if paused { 12 } else { 13 }, 0, 1., 0)
            }
            _ => Err("Invalid game action".into()),
        }
    }
    pub(crate) fn reply_dialog(&self, id: u32, button: &str) -> Result<()> {
        self.jni()?.reply_dialog(&self.machine, id, button)
    }
}
