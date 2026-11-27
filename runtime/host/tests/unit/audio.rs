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
