"""Build and package the precompiled Linux x86-64 game and native Rust host."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
BUNDLE = ROOT / 'bundle'
HOST = PROJECT / '.cache/rust-host/target/release/doodle-host'


def check_native(path):
    binary = path.read_bytes()
    if binary[:6] != b'\x7fELF\x02\x01' or binary[18:20] != b'\x3e\x00':
        raise ValueError(f'Expected Linux x86-64 native code: {path}')


def licenses(destination):
    shutil.copytree(ROOT / 'licenses', destination)
    shutil.copy2(PROJECT / 'native/doodle-gpu/LICENSE', destination / 'GPL-3.txt')
    media = ROOT / 'data/host-media/miniaudio-1.71/LICENSE'
    shutil.copy2(media, destination / 'miniaudio.txt')
    rust = PROJECT / '.cache/rust-host/toolchain/share/doc/rust'
    standard = destination / 'rust-std'
    standard.mkdir()
    shutil.copy2(rust / 'COPYRIGHT-library.html', standard)
    shutil.copytree(rust / 'licenses', standard / 'licenses')
    sys.path.insert(0, str(PROJECT / 'tools'))
    from build_host import cargo_in_docker, prepare_certificates
    prepare_certificates()
    metadata = subprocess.run(
        cargo_in_docker('metadata', '--locked', '--format-version', '1'),
        stdout=subprocess.PIPE, text=True, check=True)
    packages = json.loads(metadata.stdout)['packages']
    notices = []
    for package in packages:
        if package['source'] is None:
            continue
        source = PROJECT / Path(package['manifest_path']).relative_to('/work').parent
        target = destination / 'rust-crates' / f'{package["name"]}-{package["version"]}'
        files = sorted(path for path in source.rglob('*') if path.is_file() and
                       path.name.lower().startswith(('license', 'copying', 'notice', 'copyright', 'unlicense')))
        if not files:
            raise ValueError(f'Crate license notices are missing: {package["id"]}')
        for path in files:
            output = target / path.relative_to(source)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
        notices.append(dict(name=package['name'], version=package['version'],
                            license=package['license'], source=package['source']))
    (destination / 'rust-crates/manifest.json').write_text(json.dumps(notices, indent=2) + '\n')


def pack_bundle(host=HOST):
    """Compose a clean production pack using the already-built libraries and host."""
    host = Path(host)
    for path in (host, BUNDLE / 'native.so', BUNDLE / 'recomp/game.so'):
        check_native(path)
    (ROOT / 'data').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='runtime-pack-', dir=ROOT / 'data') as temporary:
        stage = Path(temporary) / 'bundle'
        (stage / 'recomp').mkdir(parents=True)
        shutil.copy2(host, stage / 'doodle-host')
        (stage / 'doodle-host').chmod(0o755)
        shutil.copy2(BUNDLE / 'native.so', stage)
        shutil.copy2(BUNDLE / 'recomp/game.so', stage / 'recomp')
        shutil.copy2(ROOT / 'arm_helpers.o', stage)
        shutil.copy2(ROOT / 'LICENSE', stage)
        shutil.copy2(PROJECT / 'NOTICE', stage)
        for name in ('LICENSE', 'upstream.json'):
            shutil.copy2(ROOT / 'recomp' / name, stage / 'recomp' / name)
        (stage / 'assets').mkdir()
        shutil.copy2(ROOT / 'assets/cursor.png', stage / 'assets/cursor.png')
        licenses(stage / 'licenses')
        shutil.copy2(ROOT / 'bundle.packignore', stage / '.packignore')
        shutil.rmtree(BUNDLE)
        stage.rename(BUNDLE)
    spec = importlib.util.spec_from_file_location('pack_data', PROJECT / 'kit/tools/pack_data.py')
    pack_data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pack_data)
    output = PROJECT / 'dist/runtime-linux-x86_64.pack'
    output.parent.mkdir(exist_ok=True)
    raw, size, count = pack_data.pack(str(BUNDLE), str(output))
    print(f'{output.name}: {count} files, {raw/1e6:.1f} MB extracted, {size/1e6:.1f} MB packed')
    return output


def main():
    original = PROJECT / 'original/extracted/lib/armeabi-v7a/libDoodleJump.so'
    if not original.is_file():
        raise SystemExit('Verified game engine missing; run python3 tools/get_original.py first')
    generated = ROOT / 'data/recomp-build'
    shutil.rmtree(generated, ignore_errors=True)
    sys.path.insert(0, str(PROJECT / 'tools'))
    from build_host import docker
    from recompile import PROFILE, generate
    generate(original, generated, PROFILE, container=True)
    shutil.rmtree(BUNDLE, ignore_errors=True)
    (BUNDLE / 'recomp').mkdir(parents=True)
    subprocess.run(docker('bash', '-ec', r'''
cc -c -fPIC -O3 raster.c -o data/raster.o
g++ -std=c++14 -shared -fPIC -O3 -pthread -static-libstdc++ -static-libgcc \
    dispatch.cpp image_memory.cpp graphics.cpp audio.cpp frame.cpp raster_parallel.cpp data/raster.o -o bundle/native.so
strip --strip-unneeded bundle/native.so
for source in data/recomp-build/*.c; do
    cc -std=c11 -fPIC -fexceptions -fno-strict-aliasing -ffp-contract=off -O2 \
        -Idata/recomp-build -Irecomp/guest -c "$source" -o "${source%.c}.o"
done
g++ -std=c++14 -fPIC -fexceptions -fno-strict-aliasing -ffp-contract=off -O2 \
    -Idata/recomp-build -Irecomp/guest -c recomp/guest/guest.cpp -o data/recomp-build/guest.o
g++ -shared -static-libstdc++ -static-libgcc data/recomp-build/*.o -lm -o bundle/recomp/game.so
strip --strip-unneeded bundle/recomp/game.so
''', work='/work/runtime'), check=True)
    subprocess.run([sys.executable, str(PROJECT / 'tools/build_host.py')], check=True)
    pack_bundle()


if __name__ == '__main__':
    main()
