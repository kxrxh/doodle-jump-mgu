"""Build the native module from C emitted by the Rust ARM translator."""
import argparse
import concurrent.futures
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TRANSLATOR = ROOT / 'runtime/recomp/translator/Cargo.toml'
PROFILE = TRANSLATOR.with_name('entries.json')


def assemble_helpers():
    path = ROOT / 'runtime/arm_helpers.o'
    with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
        target = Path(temporary) / path.name
        subprocess.run(['clang', '-target', 'armv7-none-eabi', '-c',
                        str(ROOT / 'runtime/arm_helpers.S'), '-o', str(target)], check=True)
        target.replace(path)
    return path


def generate(original, output, profile=None, *, container=False):
    helpers = assemble_helpers()
    inputs = [original, helpers, output]
    if profile is not None:
        inputs.append(profile)
    environment = dict(os.environ)
    if container:
        from build_host import cargo_in_docker, prepare_certificates, prepare_toolchain
        prepare_toolchain()
        prepare_certificates()
        manifest, *paths = (str(Path('/work') / Path(path).resolve().relative_to(ROOT))
                            for path in (TRANSLATOR, *inputs))
        command = cargo_in_docker('run', '--locked', '--release', '--manifest-path', manifest,
                                  '--', *paths, work='/work', target='recomp-target')
    else:
        environment.setdefault('CARGO_TARGET_DIR', str(ROOT / '.cache/recomp-rust'))
        command = ['cargo', 'run', '--locked', '--release', '--manifest-path', str(TRANSLATOR),
                   '--', *map(str, inputs)]
    subprocess.run(command, env=environment, check=True)


def compile_module(output, library, optimization):
    directory = ROOT / 'runtime/recomp/guest'
    flags = ['-fPIC', '-fexceptions', '-fno-strict-aliasing', '-ffp-contract=off',
             f'-O{optimization}', '-I' + str(output), '-I' + str(directory)]
    sources = sorted(output.glob('*.c'))
    def compile_one(source):
        target = source.with_suffix('.o')
        subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', *flags, '-c', str(source), '-o', str(target)], check=True)
        return target
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        objects = list(pool.map(compile_one, sources))
    guest = output / 'guest.o'
    cxx = os.environ.get('CXX', 'c++')
    subprocess.run([cxx, '-std=c++14', *flags, '-c', str(directory / 'guest.cpp'), '-o', str(guest)], check=True)
    objects.append(guest)
    library.parent.mkdir(parents=True, exist_ok=True)
    static = [] if sys.platform == 'darwin' else ['-static-libstdc++', '-static-libgcc']
    with tempfile.TemporaryDirectory(dir=library.parent) as temporary:
        target = Path(temporary) / library.name
        subprocess.run([cxx, '-shared', *static, *(str(path) for path in objects), '-lm', '-o', str(target)], check=True)
        target.replace(library)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, default=ROOT / 'original/extracted/lib/armeabi-v7a/libDoodleJump.so')
    parser.add_argument('--output', type=Path, default=ROOT / '.cache/recomp-build')
    parser.add_argument('--library', type=Path, help='defaults to game.so in the output directory')
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--profile', type=Path, default=PROFILE)
    parser.add_argument('--optimization', choices=('0', '1', '2', '3'), default='2')
    args = parser.parse_args()
    generate(args.original, args.output, args.profile)
    if not args.report:
        compile_module(args.output, args.library or args.output / 'game.so', args.optimization)


if __name__ == '__main__':
    main()
