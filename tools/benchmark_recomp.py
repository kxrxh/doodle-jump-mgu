"""Measure one engine backend with deterministic gameplay and sound."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get('DOODLE_ENGINE', str(ROOT / 'runtime')))
from audio import Audio
from game import Game, ORIGINAL


def benchmark(backend, theme, frames):
    work = []
    output = hashlib.sha256()
    gpu_bytes = 0
    audible = False
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as temporary, open(os.devnull, 'w') as quiet, \
            contextlib.redirect_stdout(quiet):
        data = Path(temporary)
        (data / 'appdata.i3d').write_text(
            '<DoodleJumpHScore value="10000" />\n'
            f'<DoodleJumpActiveTheme value="{theme}" />\n'
            f'<DoodleJumpDefaultTheme value="{theme}" />\n')
        with patch('libc.time.time', return_value=1700000000):
            game = Game(ORIGINAL, data, render_size=(408, 720), backend=backend)
        with contextlib.closing(Audio(game.jni.assets, playback=False)) as audio:
            game.jni.audio = audio

            def frame(touches=(), tilt=0, reset=False):
                nonlocal gpu_bytes, audible
                game.graphics.begin_gpu_frame(True, reset=reset)
                game.step(tilt=tilt, touches=touches, readback=False)
                commands = game.graphics.gpu_frame()
                gpu_bytes += len(commands)
                output.update(commands)
                pcm = audio.mix(800).tobytes()
                output.update(pcm)
                audible |= any(pcm)

            for index in range(240):
                frame(reset=index == 0)
            frame(touches=[(0, 0, 180, 295)])
            frame(touches=[(0, 2, 180, 295)])
            ready = time.monotonic()
            for index in range(frames):
                begin = time.monotonic()
                frame(tilt=.25 if index % 240 < 3 else -.25 if 120 <= index % 240 < 123 else 0)
                work.append((time.monotonic() - begin) * 1000)
            elapsed = time.monotonic() - ready
            playing = game.jni.playing
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == 'darwin':
        rss //= 1024
    cgroup = {}
    for name in ('memory.peak', 'memory.events'):
        path = Path('/sys/fs/cgroup') / name
        if path.exists():
            cgroup[name] = path.read_text().strip()
    return dict(
        scope='Uncapped engine, GPU commands and 48 kHz PCM; excludes GPU worker, compositor and physical MGU',
        backend=backend, theme=theme, frames=frames, playing=playing, audible=audible,
        startupMs=(ready - started) * 1000, uncappedEngineFps=frames / elapsed,
        medianWorkMs=statistics.median(work), p95WorkMs=sorted(work)[int(frames * .95)],
        p99WorkMs=sorted(work)[int(frames * .99)], peakProcessRssKiB=rss,
        gpuBytes=gpu_bytes, outputSha256=output.hexdigest(), cgroup=cgroup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('unicorn', 'aot'), required=True)
    parser.add_argument('--theme', type=int, choices=(1, 14, 12, 11, 9, 8, 5, 6, 7, 4, 2, 10), default=1)
    parser.add_argument('--frames', type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.frames <= 100000:
        parser.error('Frame count must be between 1 and 100000')
    print(json.dumps(benchmark(args.backend, args.theme, args.frames), indent=2))


if __name__ == '__main__':
    main()
