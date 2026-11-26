use std::{env, path::PathBuf, process::Command};
fn main() {
    let root = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("Cargo supplies its root"));
    let native = env::var_os("DOODLE_NATIVE_DIR")
        .map(PathBuf::from)
        .unwrap_or(root.join("../bundle"));
    println!("cargo:rustc-link-search=native={}", native.display());
    println!(
        "cargo:rustc-link-search=native={}",
        native.join("recomp").display()
    );
    println!("cargo:rustc-link-lib=dylib:+verbatim=native.so");
    println!("cargo:rustc-link-lib=dylib:+verbatim=game.so");
    println!("cargo:rustc-link-arg=-Wl,-rpath,$ORIGIN:$ORIGIN/recomp");
    println!("cargo:rustc-link-arg=-Wl,--dynamic-linker=/lib/ld-linux-x86-64.so.2");
    println!("cargo:rerun-if-env-changed=DOODLE_NATIVE_DIR");
    let media = root.join("../data/host-media/miniaudio-1.71");
    if root.join("audio.c").is_file() && media.join("miniaudio/miniaudio.h").is_file() {
        let out = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo supplies OUT_DIR"));
        let object = out.join("media.o");
        assert!(
            Command::new("cc")
                .args(["-O3", "-ffast-math", "-fPIC", "-std=c99", "-I"])
                .arg(&media)
                .arg("-c")
                .arg(root.join("audio.c"))
                .arg("-o")
                .arg(&object)
                .status()
                .expect("run C compiler")
                .success()
        );
        assert!(
            Command::new("ar")
                .arg("crs")
                .arg(out.join("libmedia.a"))
                .arg(object)
                .status()
                .expect("run archiver")
                .success()
        );
        println!("cargo:rustc-link-search=native={}", out.display());
        println!("cargo:rustc-link-lib=static=media");
        for lib in ["dl", "pthread", "m"] {
            println!("cargo:rustc-link-lib={lib}");
        }
    }
    println!("cargo:rerun-if-changed=audio.c");
    println!("cargo:rerun-if-changed={}", media.display());
}
