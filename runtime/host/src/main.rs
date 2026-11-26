mod audio;
mod drawing;
mod ffi;
mod gpu;
mod host;
mod images;
mod input;
mod jni;
mod libc;
mod machine;
mod output;
mod session;
type Result<T> = std::result::Result<T, String>;
fn main() {
    if let Err(error) = host::run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
