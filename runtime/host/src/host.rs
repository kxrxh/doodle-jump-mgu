use crate::{
    Result,
    audio::Audio,
    ffi,
    images::{self, Image, Paper},
    jni::{self, Jni},
    libc::LibC,
    machine::{BASE, Machine, Return},
};
use serde_json::{Value, json};
use std::{
    cell::{Cell, OnceCell, RefCell, UnsafeCell},
    ffi::CString,
    io::{self, BufRead, Read, Write},
    panic::{AssertUnwindSafe, catch_unwind},
    path::{Path, PathBuf},
    time::{Instant, SystemTime, UNIX_EPOCH},
};
const PREFIX: &str = "Java_com_limasky_doodlejumpandroid_NotificationCenter_";
const GL: &str = "glGenTextures glBindTexture glDeleteTextures glTexParameteri glTexImage2D glTexEnvi glTexEnvfv glEnable glDisable glEnableClientState glDisableClientState glBlendFunc glVertexPointer glColorPointer glTexCoordPointer glPointSizePointerOES glColor4f glColor4ub glClearColor glClear glMatrixMode glLoadIdentity glPushMatrix glPopMatrix glTranslatef glScalef glRotatef glOrthof glViewport glGetIntegerv glDrawArrays glDrawElements glGenBuffers glBindBuffer glBufferData glBufferSubData glDeleteBuffers glActiveTexture glClientActiveTexture glDepthMask eglGetCurrentDisplay eglGetCurrentSurface eglSwapBuffers";
struct Graphics {
    handle: ffi::Ptr,
    frame: UnsafeCell<Vec<u8>>,
    encoder: Cell<ffi::Ptr>,
}
impl Graphics {
    fn new(m: &Machine) -> Result<Self> {
        let mut frame = vec![0; 408 * 720 * 4];
        let handle = unsafe { ffi::graphics_create(544, 960, frame.as_mut_ptr()) };
        if handle.is_null() {
            return Err("Could not allocate native GLES renderer".into());
        }
        let result = Self {
            handle,
            frame: UnsafeCell::new(frame),
            encoder: Cell::new(std::ptr::null_mut()),
        };
        if unsafe { ffi::graphics_render_target(handle, 408, 720, result.frame_pointer()) } == 0 {
            return Err("Could not bind GLES framebuffer".into());
        }
        for name in GL.split_whitespace() {
            m.register_native(name, ffi::graphics_call as *const () as usize, handle)?;
        }
        Ok(result)
    }
    // The backend owns writes during a guest call; no Rust slice lives across that call.
    fn frame_pointer(&self) -> *mut u8 {
        unsafe { (*self.frame.get()).as_mut_ptr() }
    }
    fn begin(&self, gpu: bool, reset: bool) -> Result<()> {
        if unsafe { ffi::graphics_gpu_begin(self.handle, gpu as i32, reset as i32) } == 0 {
            return Err(unsafe { ffi::text(ffi::graphics_gpu_error(self.handle)) });
        }
        Ok(())
    }
    fn gpu(&self) -> Result<Vec<u8>> {
        if unsafe { ffi::graphics_gpu_finish(self.handle) } == 0 {
            return Err(unsafe { ffi::text(ffi::graphics_gpu_error(self.handle)) });
        }
        let size = unsafe { ffi::graphics_gpu_size(self.handle) };
        Ok(
            unsafe { std::slice::from_raw_parts(ffi::graphics_gpu_data(self.handle), size) }
                .to_vec(),
        )
    }
    fn pixels(&self) -> Result<Vec<u8>> {
        let mut encoder = self.encoder.get();
        if encoder.is_null() {
            encoder = unsafe { ffi::frame_encoder_create(408, 720) };
            if encoder.is_null() {
                return Err("Could not allocate frame encoder".into());
            }
            self.encoder.set(encoder);
        }
        if unsafe { ffi::frame_encoder_encode(encoder, self.frame_pointer(), 408 * 720 * 4) } == 0 {
            return Err("Could not encode frame regions".into());
        }
        Ok(unsafe {
            std::slice::from_raw_parts(
                ffi::frame_encoder_data(encoder),
                ffi::frame_encoder_size(encoder),
            )
        }
        .to_vec())
    }
    fn blocked(&self, x: f64, y: f64) -> bool {
        let mut pointer = std::ptr::null();
        let n = unsafe { ffi::graphics_rectangles(self.handle, &mut pointer) } as usize;
        if n == 0 {
            return false;
        }
        unsafe { std::slice::from_raw_parts(pointer, n) }
            .iter()
            .rev()
            .find(|r| r.left <= x && x <= r.right && r.top <= y && y <= r.bottom)
            .is_some_and(|r| r.excluded != 0)
    }
}
impl Drop for Graphics {
    fn drop(&mut self) {
        unsafe {
            if !self.encoder.get().is_null() {
                ffi::frame_encoder_destroy(self.encoder.get())
            }
            ffi::graphics_destroy(self.handle)
        }
    }
}
struct PaperTexture {
    identifier: u32,
    revision: u64,
    image: Image,
    content: (u32, u32),
    asset: String,
}
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
    fn new(
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
    fn decode_png(&self) -> Result<Return> {
        let m = &self.machine;
        let source = m.arg(0)?;
        let length = m.arg(1)? as usize;
        let mode = m.arg(2)? as i32;
        let npot = m.arg(3)? != 0;
        let result = m.arg(4)?;
        if length > 64 << 20 {
            return Err("PNG exceeds the asset size limit".into());
        }
        let image = match images::decode_bytes(&m.read(source, length)?) {
            Ok(image) => image,
            Err(_) => return Ok(Return::Word(0)),
        };
        let (w, h) = (image.width, image.height);
        if w == 0 || h == 0 || w > 4096 || h > 4096 {
            return Err("Invalid game image dimensions".into());
        }
        let tw = if npot { w } else { w.next_power_of_two() };
        let th = if npot { h } else { h.next_power_of_two() };
        let (fmt, kind, bpp) = match mode {
            2 | 13 => (0x1908, 0x8033, 2),
            4 => (0x1907, 0x8363, 2),
            6 => (0x1909, 0x1401, 1),
            7 => (0x1906, 0x1401, 1),
            8 => (0x190a, 0x1401, 2),
            _ => (0x1908, 0x1401, 4),
        };
        let size = tw as usize * th as usize * bpp;
        let (pointer, out) = m.image_malloc(size)?;
        unsafe {
            ffi::png_pixels(
                out,
                image.pixels.as_ptr().cast_mut().cast(),
                w as i32,
                h as i32,
                tw as i32,
                mode,
            )
        };
        for (i, v) in [pointer, fmt, kind, bpp as u32, size as u32, w, h, tw, th]
            .iter()
            .enumerate()
        {
            m.put32(result + i as u32 * 4, *v)?;
        }
        Ok(Return::Word(1))
    }
    pub fn set_asset(&self, name: &str) -> Result<()> {
        let n = CString::new(name).map_err(|e| e.to_string())?;
        unsafe {
            ffi::graphics_asset(
                self.graphics()?.handle,
                n.as_ptr(),
                images::excluded(name) as i32,
            )
        };
        Ok(())
    }
    pub fn text_texture(&self, image: &Image) -> Result<u32> {
        let id = unsafe {
            ffi::graphics_text_texture(
                self.graphics()?.handle,
                image.width as i32,
                image.height as i32,
                image.pixels.as_ptr(),
            )
        };
        if id == 0 {
            Err("Could not create native text texture".into())
        } else {
            Ok(id)
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
    fn paper(&self, paper: &mut Paper, width: u64) -> Result<(Value, Option<Image>)> {
        if width == 0 {
            return Ok((Value::Null, None));
        }
        let g = self.graphics()?;
        let mut id = 0;
        let mut vertices = [0.; 48];
        let n =
            unsafe { ffi::graphics_background(g.handle, &mut id, vertices.as_mut_ptr()) } as usize;
        if n == 0 {
            return Ok((Value::Null, None));
        }
        let mut info = ffi::TextureInfo::default();
        if unsafe { ffi::graphics_texture_info(g.handle, id, &mut info) } == 0 {
            return Err("Could not inspect paper texture".into());
        }
        let mut cached = self.paper_texture.borrow_mut();
        if cached
            .as_ref()
            .is_none_or(|source| source.identifier != id || source.revision != info.revision)
        {
            let asset = unsafe { ffi::text(info.asset) };
            let content = images::png_dimensions(&jni::path(&self.assets, &asset)?)?;
            let mut pixels = vec![0; info.size];
            if unsafe {
                ffi::graphics_texture_rgba(g.handle, id, pixels.as_mut_ptr(), pixels.len())
            } == 0
            {
                return Err("Could not read paper texture".into());
            }
            *cached = Some(PaperTexture {
                identifier: id,
                revision: info.revision,
                content,
                asset,
                image: Image {
                    width: info.width,
                    height: info.height,
                    pixels,
                },
            });
        }
        let source = cached.as_ref().ok_or("Missing paper source")?;
        let vertices = vertices[..n * 8].as_chunks::<8>().0;
        paper.update(
            &source.image,
            source.content,
            vertices,
            id,
            info.revision,
            &source.asset,
        )
    }
}
fn packet(output: &mut impl Write, kind: &[u8; 4], bytes: &[u8]) -> Result<()> {
    let size = u32::try_from(bytes.len()).map_err(|_| "Engine packet is too large")?;
    output
        .write_all(kind)
        .and_then(|_| output.write_all(&size.to_le_bytes()))
        .and_then(|_| output.write_all(bytes))
        .and_then(|_| output.flush())
        .map_err(|e| e.to_string())
}
fn event(out: &mut impl Write, value: Value) -> Result<()> {
    packet(
        out,
        b"EVT1",
        &serde_json::to_vec(&value).map_err(|e| e.to_string())?,
    )
}
fn texture(out: &mut impl Write, id: u32, image: &Image) -> Result<()> {
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

pub fn run() -> Result<()> {
    let mut original = None;
    let mut data = None;
    let mut runtime = None;
    let mut playback = true;
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
    let result = run_process(
        &original,
        &data,
        &runtime,
        epoch,
        playback,
        captures,
        &mut output,
    );
    if let Err(e) = &result {
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
    let mut playing = false;
    let mut gpu_mode = false;
    let mut paper = Paper::default();
    let mut projection = Value::Null;
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
        let began = Instant::now();
        let cpu_started = if timing { thread_cpu_ms()? } else { 0. };
        if gpu || gpu_mode {
            host.graphics()?.begin(gpu, reset)?;
        }
        if gpu != gpu_mode {
            let g = host.graphics()?;
            if !g.encoder.get().is_null() {
                unsafe { ffi::frame_encoder_destroy(g.encoder.replace(std::ptr::null_mut())) }
            }
            gpu_mode = gpu;
        }
        host.step(tilt, &parsed, dt)?;
        if let Some(file) = audio_output.as_mut() {
            let pcm = host.audio.mix(735)?;
            let bytes = pcm
                .iter()
                .flat_map(|sample| sample.to_le_bytes())
                .collect::<Vec<_>>();
            file.write_all(&bytes).map_err(|e| e.to_string())?;
        }
        let (p, image) = host.paper(&mut paper, width)?;
        if let Some(image) = image {
            texture(out, 2, &image)?;
        }
        if p != projection {
            projection = p;
            event(out, json!({"type":"scenery","paper":projection}))?;
        }
        for dialog in std::mem::take(&mut *host.jni()?.dialogs.borrow_mut()) {
            event(out, dialog)?;
        }
        let now = host.jni()?.playing.get();
        if now != playing {
            playing = now;
            event(out, json!({"type":"playing","value":playing}))?;
        }
        let payload = if gpu {
            host.graphics()?.gpu()?
        } else {
            host.graphics()?.pixels()?
        };
        if timing {
            event(
                out,
                json!({"type":"timing","wallMs":began.elapsed().as_secs_f64()*1000.,"cpuMs":thread_cpu_ms()?-cpu_started}),
            )?;
        }
        packet(out, if gpu { b"GPF1" } else { b"FRM2" }, &payload)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deferred_callbacks_observe_the_new_clock_and_drain_in_the_same_frame() {
        struct Probe {
            host: *const Host,
            clocks: RefCell<Vec<u32>>,
        }
        unsafe extern "C" fn observe(
            user: ffi::Ptr,
            _call: ffi::Ptr,
            out: *mut ffi::NativeResult,
        ) -> i32 {
            let probe = unsafe { &*user.cast::<Probe>() };
            let host = unsafe { &*probe.host };
            let value = host
                .libc
                .get()
                .unwrap()
                .invoke("clock", &host.machine)
                .unwrap()
                .unwrap();
            let Return::Word(clock) = value else {
                return 1;
            };
            let first = probe.clocks.borrow().is_empty();
            probe.clocks.borrow_mut().push(clock);
            if first {
                host.jni().unwrap().deferred.borrow_mut().push((1, 0, 0, 0));
            }
            unsafe {
                (*out).value = 0;
                (*out).flags = 1;
            }
            0
        }
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let data = std::env::temp_dir().join(format!("doodle-deferred-{}", std::process::id()));
        let host = Host::new(
            &root.join("original"),
            &data,
            &root.join("runtime/bundle"),
            1700000000,
            false,
            None,
        )
        .unwrap();
        let probe = Probe {
            host: &*host,
            clocks: RefCell::new(vec![]),
        };
        host.machine
            .register_native(
                "test.clock",
                observe as *const () as usize,
                (&probe as *const Probe).cast_mut().cast(),
            )
            .unwrap();
        let send = host
            .machine
            .symbol(&format!("{PREFIX}sendMessageNative"))
            .unwrap();
        host.machine.intercept(send & !1, "test.clock").unwrap();
        host.jni().unwrap().deferred.borrow_mut().push((1, 0, 0, 0));
        host.step(0., &[], 0.05).unwrap();
        assert_eq!(*probe.clocks.borrow(), vec![50000, 50000]);
        drop(host);
        std::fs::remove_dir_all(data).unwrap();
    }

    #[test]
    fn input_numbers_reject_wrong_types_and_out_of_range_values() {
        for value in [
            json!(true),
            json!(null),
            json!("0.5"),
            json!(-1.01),
            json!(1.01),
        ] {
            assert!(number(&value, -1., 1.).is_err());
        }
        assert_eq!(number(&json!(0.123456789), -1., 1.).unwrap(), 0.123456789);
    }

    #[test]
    fn frame_flags_do_not_coerce_json_values() {
        assert!(boolean(Some(&json!(1)), false).is_err());
        assert!(!boolean(None, false).unwrap());
        assert!(boolean(Some(&json!(true)), false).unwrap());
    }

    #[test]
    fn packet_framing_uses_payload_bytes_without_text_conversion() {
        let mut out = vec![];
        packet(&mut out, b"GPF1", &[0, 128, 255]).unwrap();
        assert_eq!(
            out,
            [b"GPF1".as_slice(), &[3, 0, 0, 0, 0, 128, 255]].concat()
        );
    }
}
