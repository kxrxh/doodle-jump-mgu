use crate::Result;
use std::{
    ffi::{CStr, CString, c_char, c_double, c_int, c_void},
    os::unix::ffi::OsStrExt,
    path::Path,
    ptr::NonNull,
};

unsafe extern "C" {
    fn host_audio_create(playback: c_int) -> *mut c_void;
    fn host_audio_destroy(handle: *mut c_void);
    fn host_audio_error(handle: *mut c_void) -> *const c_char;
    fn host_audio_load(handle: *mut c_void, id: u32, path: *const c_char, looping: c_int) -> c_int;
    fn host_audio_message(
        handle: *mut c_void,
        message: c_int,
        id: u32,
        gain: c_double,
        looping: c_int,
    ) -> c_int;
    fn host_audio_mix(handle: *mut c_void, output: *mut i16, frames: usize) -> c_int;
}

pub struct Audio {
    handle: NonNull<c_void>,
}

impl Audio {
    pub fn new(playback: bool) -> Result<Self> {
        // The C adapter owns the playback thread and joins it before freeing this handle.
        let handle = NonNull::new(unsafe { host_audio_create(i32::from(playback)) });
        handle
            .map(|handle| Self { handle })
            .ok_or_else(|| "Cannot allocate audio mixer".into())
    }

    fn check(&self, status: c_int) -> Result<()> {
        if status == 0 {
            return Ok(());
        }
        // The adapter's error text is valid until the next call on the render thread.
        Err(
            unsafe { CStr::from_ptr(host_audio_error(self.handle.as_ptr())) }
                .to_string_lossy()
                .into_owned(),
        )
    }

    pub fn load(&self, id: u32, path: &Path, looping: bool) -> Result<()> {
        let name = CString::new(path.as_os_str().as_bytes())
            .map_err(|_| "Audio path contains a NUL byte")?;
        self.check(unsafe {
            host_audio_load(self.handle.as_ptr(), id, name.as_ptr(), i32::from(looping))
        })
    }

    pub fn message(&self, message: i32, id: u32, volume: f64, loops: i32) -> Result<()> {
        self.check(unsafe { host_audio_message(self.handle.as_ptr(), message, id, volume, loops) })
    }

    pub fn mix(&self, frames: usize) -> Result<Vec<i16>> {
        if frames > 1 << 20 {
            return Err("Invalid audio buffer".into());
        }
        let mut output = vec![0; frames * 2];
        self.check(unsafe { host_audio_mix(self.handle.as_ptr(), output.as_mut_ptr(), frames) })?;
        Ok(output)
    }
}

impl Drop for Audio {
    fn drop(&mut self) {
        unsafe { host_audio_destroy(self.handle.as_ptr()) };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn absent_sound_file_is_an_error_and_does_not_break_mixing() {
        let audio = Audio::new(false).unwrap();
        assert!(
            audio
                .load(1, Path::new("/missing-doodle-sound.ogg"), false)
                .is_err()
        );
        assert_eq!(audio.mix(8).unwrap(), vec![0; 16]);
    }

    #[test]
    fn audio_buffers_are_bounded_before_allocation() {
        let audio = Audio::new(false).unwrap();
        assert!(audio.mix((1 << 20) + 1).is_err());
        assert!(audio.load(1, Path::new("nul\0sound"), false).is_err());
    }
}
