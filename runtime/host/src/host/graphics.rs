use super::Host;
use crate::{
    Result, ffi,
    images::{self, Image, Paper},
    jni,
    machine::{Machine, Return},
};
use serde_json::Value;
use std::{
    cell::{Cell, UnsafeCell},
    ffi::CString,
};

const GL: &str = "glGenTextures glBindTexture glDeleteTextures glTexParameteri glTexImage2D glTexEnvi glTexEnvfv glEnable glDisable glEnableClientState glDisableClientState glBlendFunc glVertexPointer glColorPointer glTexCoordPointer glPointSizePointerOES glColor4f glColor4ub glClearColor glClear glMatrixMode glLoadIdentity glPushMatrix glPopMatrix glTranslatef glScalef glRotatef glOrthof glViewport glGetIntegerv glDrawArrays glDrawElements glGenBuffers glBindBuffer glBufferData glBufferSubData glDeleteBuffers glActiveTexture glClientActiveTexture glDepthMask eglGetCurrentDisplay eglGetCurrentSurface eglSwapBuffers";
pub(super) struct Graphics {
    pub(super) handle: ffi::Ptr,
    frame: UnsafeCell<Vec<u8>>,
    pub(super) encoder: Cell<ffi::Ptr>,
}
impl Graphics {
    pub(super) fn new(m: &Machine) -> Result<Self> {
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
    pub(super) fn begin(&self, gpu: bool, reset: bool) -> Result<()> {
        if unsafe { ffi::graphics_gpu_begin(self.handle, gpu as i32, reset as i32) } == 0 {
            return Err(unsafe { ffi::text(ffi::graphics_gpu_error(self.handle)) });
        }
        Ok(())
    }
    pub(super) fn gpu(&self) -> Result<Vec<u8>> {
        if unsafe { ffi::graphics_gpu_finish(self.handle) } == 0 {
            return Err(unsafe { ffi::text(ffi::graphics_gpu_error(self.handle)) });
        }
        let size = unsafe { ffi::graphics_gpu_size(self.handle) };
        Ok(
            unsafe { std::slice::from_raw_parts(ffi::graphics_gpu_data(self.handle), size) }
                .to_vec(),
        )
    }
    pub(super) fn pixels(&self) -> Result<Vec<u8>> {
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
    pub(super) fn blocked(&self, x: f64, y: f64) -> bool {
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
pub(super) struct PaperTexture {
    identifier: u32,
    revision: u64,
    image: Image,
    content: (u32, u32),
    asset: String,
}
impl Host {
    pub(super) fn decode_png(&self) -> Result<Return> {
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
    pub(super) fn paper(&self, paper: &mut Paper, width: u64) -> Result<(Value, Option<Image>)> {
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
