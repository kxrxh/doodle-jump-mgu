"""Build the Rust UI as MVP WebAssembly for Store's Node 8, off the device."""
import argparse
import hashlib
import shutil
import subprocess

from build_host import prepare_toolchain, prepare_wasm, IMAGE, ROOT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='verify that the committed asset matches the source')
    args=parser.parse_args()
    prepare_toolchain()
    prepare_wasm()
    command=['docker','run','--rm','--platform','linux/amd64','--network=none',
        '-v',f'{ROOT}:/work','-w','/work/runtime/ui',
        '-e','PATH=/work/.cache/rust-host/toolchain/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        '-e','CARGO_HOME=/work/.cache/rust-host/cargo',
        '-e','CARGO_TARGET_DIR=/work/.cache/rust-host/ui-target',
        '-e','RUSTFLAGS=-C link-arg=--initial-memory=262144 -C link-arg=--max-memory=262144 -C link-arg=-z -C link-arg=stack-size=65536',
        IMAGE,'cargo','build','--release','--locked','--offline','--target','wasm32v1-none']
    subprocess.run(command,check=True)
    built=ROOT/'.cache/rust-host/ui-target/wasm32v1-none/release/doodle_ui.wasm'
    asset=ROOT/'apps/kxrxh_doodle_jump/ui.wasm'
    if args.check:
        if not asset.is_file() or asset.read_bytes()!=built.read_bytes():
            raise SystemExit('The precompiled Rust UI does not match its source; run tools/build_ui.py')
    else:
        shutil.copy2(built,asset)
        asset.chmod(0o644)
    print(f'{asset.name}: {built.stat().st_size} bytes, SHA-256 {hashlib.sha256(built.read_bytes()).hexdigest()}')


if __name__=='__main__':main()
