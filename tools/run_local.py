"""Build the native preview off-device and run its host in Linux Docker."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys

from build_host import IMAGE


ROOT = Path(__file__).resolve().parents[1]


def host(arguments):
    try:
        runtime = Path(arguments[arguments.index('--runtime') + 1]).resolve()
    except (ValueError, IndexError):
        raise SystemExit('The local host requires --runtime')
    command = ['docker', 'run', '--rm', '-i', '--platform', 'linux/amd64',
               '--network=none', '--memory=512m', '--memory-swap=512m',
               '-v', f'{ROOT}:{ROOT}', '-w', str(runtime),
               '-e', f'LD_LIBRARY_PATH={runtime}:{runtime / "recomp"}',
               '-e', 'MALLOC_MMAP_THRESHOLD_', IMAGE, str(runtime / 'doodle-host'), *arguments]
    os.execvp(command[0], command)


def main():
    if sys.argv[1:2] == ['--host']:
        host(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8079)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    if sys.version_info < (3, 12):
        raise SystemExit('Local builds require Python 3.12 or newer')
    for script in ('tools/get_original.py', 'runtime/prepare.py', 'tools/build_ui.py'):
        subprocess.run([sys.executable, str(ROOT / script)], cwd=ROOT, check=True)
    wrapper = ROOT / '.cache/run-local/doodle-host'
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), '--host'])
    wrapper.write_text('#!/bin/sh\nexec ' + command + ' "$@"\n')
    wrapper.chmod(0o755)
    environment = dict(os.environ, DOODLE_ENGINE=str(ROOT / 'runtime/bundle'),
                       DOODLE_ORIGINAL=str(ROOT / 'original'), DOODLE_HOST=str(wrapper))
    command = ['node', str(ROOT / 'kit/tools/emu.js'), 'kxrxh_doodle_jump', '--port', str(args.port)]
    os.execvpe(command[0], command, environment)


if __name__ == '__main__':
    main()
