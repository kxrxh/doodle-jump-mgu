"""Build the native Rust host against the MGU's glibc 2.27, off the device."""
import argparse
from pathlib import Path
import subprocess
import ssl
import tarfile

from store_build import download

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache/rust-host'
TOOLCHAIN = CACHE / 'toolchain'
VERSION = '1.100.0'
TARGET = 'x86_64-unknown-linux-gnu'
IMAGE = 'doodle-recomp-target'
RUST_COMPONENTS = {
    'rustc': 'f1d0b1dec81dab6f523cd098507763487b0d4bd7fd2c87582f154128719be03b',
    'cargo': '7e29ce07d7c6e92d1826870575ba27d57fff6cb3e754aa57d99099cb88fab35d',
    'rust-std': '4591b6486594ded52ecd6135db95bf9355bbd3ca7a380888b141528bbcf9a5d4',
    'clippy': '0d18df3bff6c852ef8243727fed075ecb33dbf93b8ab8c138c2f45e3f270102a',
    'rustfmt': '4c9389e4722b16499e5d47d89837ec84a4ef755f105ed2a84c038156064e01ab',
}
MEDIA_URL = 'https://files.pythonhosted.org/packages/d8/d5/e5439dc08561f73656bfeb3340fc64ab63163e101426593d8fb9a025ff1e/miniaudio-1.71.tar.gz'
MEDIA_SHA256 = 'ff51e2887bb673e2e757752b586b3dc924d59aa5fbcae9bbc45f4a111bd3262b'
MEDIA = ROOT / 'runtime/data/host-media'


def docker(*command, work='/work'):
    return ['docker', 'run', '--rm', '--platform', 'linux/amd64',
            '-v', f'{ROOT}:/work', '-w', work, IMAGE, *command]


def prepare_media():
    source = MEDIA / 'miniaudio-1.71'
    archive = download(MEDIA_URL, MEDIA_SHA256, CACHE / 'downloads/miniaudio-1.71.tar.gz')
    if not (source / 'miniaudio/miniaudio.h').is_file():
        MEDIA.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as package:
            package.extractall(MEDIA, filter='data')
    return source


def prepare_certificates():
    # The minimal glibc build container has no system CA package.
    certificates = ssl.create_default_context().get_ca_certs(binary_form=True)
    if not certificates:
        raise RuntimeError('The build host has no trusted TLS certificate authorities')
    output = CACHE / 'cacert.pem'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(''.join(ssl.DER_cert_to_PEM_cert(value) for value in certificates))
    return output


def prepare_toolchain():
    if subprocess.run(['docker', 'image', 'inspect', IMAGE], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        subprocess.run(['docker', 'build', '--platform', 'linux/amd64', '-t', IMAGE,
                        '-f', str(ROOT / 'runtime/recomp/Dockerfile'), str(ROOT)], check=True)
    for name, checksum in RUST_COMPONENTS.items():
        binary = {'clippy': 'cargo-clippy', 'rustfmt': 'rustfmt'}.get(name, name)
        present = (TOOLCHAIN / 'bin' / binary).is_file() if name != 'rust-std' else any(
            (TOOLCHAIN / 'lib/rustlib' / TARGET / 'lib').glob('libstd-*.rlib'))
        if present:
            continue
        filename = f'{name}-{VERSION}-{TARGET}'
        archive = download(f'https://static.rust-lang.org/dist/{filename}.tar.xz', checksum,
                           CACHE / 'downloads' / f'{filename}.tar.xz')
        with tarfile.open(archive) as package:
            package.extractall(CACHE, filter='data')
        subprocess.run(docker(f'/work/.cache/rust-host/{filename}/install.sh',
                              '--prefix=/work/.cache/rust-host/toolchain', '--disable-ldconfig'), check=True)
    return TOOLCHAIN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--command', choices=('build', 'test', 'clippy', 'fmt'), default='build')
    parser.add_argument('--setup-only', action='store_true')
    args = parser.parse_args()
    prepare_media()
    prepare_toolchain()
    prepare_certificates()
    if args.setup_only:
        return
    native = ROOT / 'runtime/bundle'
    if not all((native / name).is_file() for name in ('native.so', 'recomp/game.so')):
        raise SystemExit('Build the verified native game first with runtime/prepare.py')
    command = ['docker', 'run', '--rm', '--platform', 'linux/amd64',
               '-v', f'{ROOT}:/work', '-w', '/work/runtime/host',
               '-e', 'PATH=/work/.cache/rust-host/toolchain/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
               '-e', 'CARGO_HOME=/work/.cache/rust-host/cargo',
               '-e', 'CARGO_HTTP_CAINFO=/work/.cache/rust-host/cacert.pem',
               '-e', 'CARGO_TARGET_DIR=/work/.cache/rust-host/target',
               '-e', 'DOODLE_NATIVE_DIR=/work/runtime/bundle',
               '-e', 'LD_LIBRARY_PATH=/work/runtime/bundle:/work/runtime/bundle/recomp', IMAGE, 'cargo']
    cargo = {'build': ['build', '--release', '--locked'],
             'test': ['test', '--locked'],
             'clippy': ['clippy', '--all-targets', '--all-features', '--locked', '--', '-D', 'warnings'],
             'fmt': ['fmt', '--all', '--', '--check']}[args.command]
    subprocess.run(command + cargo, check=True)
    if args.command == 'build':
        print(CACHE / 'target/release/doodle-host')


if __name__ == '__main__':
    main()
