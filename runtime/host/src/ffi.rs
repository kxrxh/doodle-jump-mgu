use std::ffi::{CStr, c_char, c_int, c_void};
pub type Ptr = *mut c_void;
#[repr(C)]
#[derive(Default)]
pub struct GuestApi {
    pub functions: [usize; 10],
    pub registers: [c_int; 7],
}
#[repr(C)]
#[derive(Default)]
pub struct NativeResult {
    pub value: u64,
    pub flags: u32,
}
#[repr(C)]
pub struct GuestState {
    pub registers: [u32; 16],
    pub flags: [u32; 5],
    pub vectors: [u64; 32],
    pub fpscr: u32,
    pub image_base: u32,
    pub itstate: u32,
    pub cpsr: u32,
    pub cpacr: u32,
    pub fpexc: u32,
}
pub type Fallback = unsafe extern "C" fn(Ptr, u64, *mut NativeResult) -> c_int;
#[repr(C)]
#[derive(Default)]
pub struct TextureInfo {
    pub width: u32,
    pub height: u32,
    pub size: usize,
    pub asset: *const c_char,
    pub excluded: c_int,
    pub revision: u64,
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Rectangle {
    pub left: f64,
    pub top: f64,
    pub right: f64,
    pub bottom: f64,
    pub excluded: c_int,
}
unsafe extern "C" {
    pub fn guest_create(resolver: usize) -> Ptr;
    pub fn guest_free(guest: Ptr);
    pub fn guest_context(guest: Ptr) -> Ptr;
    pub fn guest_context_size() -> usize;
    pub fn guest_context_save(guest: Ptr, out: Ptr, size: usize) -> c_int;
    pub fn guest_context_restore(guest: Ptr, input: *const c_void, size: usize) -> c_int;
    pub fn guest_context_jump(guest: Ptr, input: *const c_void, size: usize) -> c_int;
    pub fn guest_get_api(out: *mut GuestApi);
    pub fn guest_run(guest: Ptr, target: u32, until: u32) -> c_int;
    pub fn guest_error(guest: Ptr) -> *const c_char;
    pub fn guest_reg_read(guest: Ptr, reg: c_int, out: Ptr) -> c_int;
    pub fn guest_reg_write(guest: Ptr, reg: c_int, input: *const c_void) -> c_int;
    pub fn guest_mem_map(guest: Ptr, address: u64, size: usize, permissions: u32) -> c_int;
    pub fn guest_mem_read(guest: Ptr, address: u64, out: Ptr, size: usize) -> c_int;
    pub fn guest_mem_write(guest: Ptr, address: u64, input: *const c_void, size: usize) -> c_int;
    pub fn doodle_resolve(context: Ptr, address: u32);
    pub fn doodle_engine_sha256() -> *const c_char;
    pub fn dispatch_create(
        guest: Ptr,
        api: *const GuestApi,
        code: c_int,
        block: c_int,
        write: c_int,
        begin: u64,
        end: u64,
        fallback: Fallback,
        user: Ptr,
    ) -> Ptr;
    pub fn dispatch_destroy(dispatch: Ptr);
    pub fn dispatch_add(
        dispatch: Ptr,
        address: u64,
        name: *const c_char,
        intercept: c_int,
    ) -> c_int;
    pub fn dispatch_register(
        dispatch: Ptr,
        name: *const c_char,
        callback: usize,
        user: Ptr,
    ) -> c_int;
    pub fn dispatch_begin_call(dispatch: Ptr, limit: u64) -> c_int;
    pub fn dispatch_end_call(dispatch: Ptr);
    pub fn dispatch_error(dispatch: Ptr) -> *const c_char;
    pub fn dispatch_clear_error(dispatch: Ptr);
    pub fn dispatch_allocate_image(dispatch: Ptr, address: u32, size: usize) -> Ptr;
    pub fn dispatch_release_image(dispatch: Ptr, address: u32) -> c_int;
    pub fn dispatch_before_write(dispatch: Ptr, address: u64, size: usize) -> c_int;
    pub fn graphics_create(w: c_int, h: c_int, frame: *mut u8) -> Ptr;
    pub fn graphics_destroy(handle: Ptr);
    pub fn graphics_render_target(handle: Ptr, w: c_int, h: c_int, frame: *mut u8) -> c_int;
    pub fn graphics_call(handle: Ptr, call: Ptr, result: *mut NativeResult) -> c_int;
    pub fn graphics_gpu_begin(handle: Ptr, enabled: c_int, reset: c_int) -> c_int;
    pub fn graphics_gpu_finish(handle: Ptr) -> c_int;
    pub fn graphics_gpu_data(handle: Ptr) -> *const u8;
    pub fn graphics_gpu_size(handle: Ptr) -> usize;
    pub fn graphics_gpu_error(handle: Ptr) -> *const c_char;
    pub fn graphics_asset(handle: Ptr, name: *const c_char, excluded: c_int);
    pub fn graphics_rectangles(handle: Ptr, out: *mut *const Rectangle) -> u32;
    pub fn graphics_reset_rectangles(handle: Ptr);
    pub fn graphics_background(handle: Ptr, texture: *mut u32, vertices: *mut f64) -> u32;
    pub fn graphics_texture_info(handle: Ptr, texture: u32, out: *mut TextureInfo) -> c_int;
    pub fn graphics_texture_rgba(handle: Ptr, texture: u32, out: *mut u8, size: usize) -> c_int;
    pub fn graphics_text_texture(handle: Ptr, w: c_int, h: c_int, rgba: *const u8) -> u32;
    pub fn png_pixels(out: Ptr, rgba: Ptr, w: c_int, h: c_int, tw: c_int, mode: c_int);
    pub fn frame_encoder_create(w: c_int, h: c_int) -> Ptr;
    pub fn frame_encoder_destroy(handle: Ptr);
    pub fn frame_encoder_encode(handle: Ptr, frame: *const u8, size: usize) -> c_int;
    pub fn frame_encoder_data(handle: Ptr) -> *const u8;
    pub fn frame_encoder_size(handle: Ptr) -> usize;
}
/// The backend owns these error strings until the next call on this thread.
pub unsafe fn text(pointer: *const c_char) -> String {
    if pointer.is_null() {
        String::new()
    } else {
        unsafe { CStr::from_ptr(pointer) }
            .to_string_lossy()
            .into_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn guest_api_registers_match_the_native_bridge() {
        let mut api = GuestApi::default();
        unsafe { guest_get_api(&mut api) };
        assert_eq!(api.registers, [66, 67, 68, 69, 12, 10, 11]);
    }
}
