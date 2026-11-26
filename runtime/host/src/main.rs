mod audio;
mod ffi;
mod host;
mod images;
mod jni;
mod libc;
mod machine;
type Result<T> = std::result::Result<T, String>;
fn main() {
    if let Err(error) = host::run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
