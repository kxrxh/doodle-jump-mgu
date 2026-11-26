"""Compare a Rust host with the frozen Python release in the same Linux environment."""
import argparse
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import select
import statistics
import struct
import subprocess
import sys
import tempfile
import time

from prepare_reference import BASELINE

ROOT = Path(__file__).resolve().parents[1]
THEMES = (1, 14, 12, 11, 9, 8, 5, 6, 7, 4, 2, 10)
MAX_PACKET = 64 * 1024 * 1024
FRAME_KINDS = (b'GPF1', b'FRM1', b'FRM2')


def read_packet(source, timeout=30):
    deadline = time.monotonic() + timeout
    def exact(size):
        result = bytearray()
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([source], [], [], max(0, remaining))[0]:
                raise TimeoutError('Host did not complete its packet')
            part = os.read(source.fileno(), min(size - len(result), 1 << 20))
            if not part:
                raise EOFError('Host stopped in the middle of a packet')
            result.extend(part)
        return bytes(result)
    header = exact(8)
    size = struct.unpack_from('<I', header, 4)[0]
    if size > MAX_PACKET:
        raise ValueError(f'Host packet exceeds {MAX_PACKET} bytes')
    return header[:4], exact(size)


def json_value(data):
    def invalid(value):
        raise ValueError(f'Non-finite host JSON: {value}')
    return json.loads(data, parse_constant=invalid)


def equal_json(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal_json(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal_json(a, b) for a, b in zip(left, right))
    if isinstance(left, (float, int)) and not isinstance(left, bool) and isinstance(right, (float, int)) and not isinstance(right, bool):
        if isinstance(left, int) or isinstance(right, int):
            return left == right
        return left == right or math.isclose(left, right, rel_tol=0, abs_tol=1e-12)
    return type(left) is type(right) and left == right


def compare_packets(left, right, label):
    if len(left) != len(right):
        raise AssertionError(f'{label}: packet count differs ({len(left)} vs {len(right)})')
    for index, ((kind, before), (other, after)) in enumerate(zip(left, right)):
        if kind != other:
            raise AssertionError(f'{label}, packet {index}: {kind!r} vs {other!r}')
        same = equal_json(json_value(before), json_value(after)) if kind == b'EVT1' else before == after
        if not same:
            if kind == b'EVT1':
                detail = f'{before.decode()} vs {after.decode()}'
            else:
                offset = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), min(len(before), len(after)))
                detail = f'bytes differ at {offset}; lengths {len(before)} vs {len(after)}'
            raise AssertionError(f'{label}, packet {index} ({kind.decode()}): {detail}')


def audio_voice_bound(events):
    loaded, voices, paused = set(), {}, False
    maximum = 0
    for event in events:
        identifier = event['id']
        if event['type'] == 'load':
            loaded.add(identifier)
            voices.pop(identifier, None)
        elif event['type'] == 'message':
            message = event['message']
            if message == 7:
                loaded.discard(identifier)
                voices.pop(identifier, None)
            elif message == 8 and identifier in loaded:
                voices[identifier] = (event['gain'], False)
            elif message == 9:
                voices.pop(identifier, None)
            elif message in (10, 11) and identifier in voices:
                voices[identifier] = (voices[identifier][0], message == 10)
            elif message in (12, 13):
                paused = message == 12
            elif message == 14:
                voices.clear()
            elif message == 15 and identifier in voices:
                voices[identifier] = (event['gain'], voices[identifier][1])
        if not paused:
            # Ended sounds may remain in this set; this is a conservative upper bound.
            maximum = max(maximum, sum(gain > 0 and not stopped for gain, stopped in voices.values()))
    return maximum


def compare_pcm(left, right, frames, samples_per_frame=1470, tolerance=1):
    before, after = left.read_bytes(), right.read_bytes()
    expected = frames * samples_per_frame * 2
    if len(before) != expected or len(after) != expected:
        raise AssertionError(f'PCM length mismatch: expected {expected}, got {len(before)} vs {len(after)}')
    a, b = array('h', before), array('h', after)
    if sys.byteorder != 'little':
        a.byteswap()
        b.byteswap()
    maximum = changed = squared = 0
    for x, y in zip(a, b):
        difference = abs(x - y)
        maximum = max(maximum, difference)
        changed += difference != 0
        squared += difference * difference
    report = dict(samples=len(a), maxDifference=maximum, changedSamples=changed,
                  rmsDifference=math.sqrt(squared / len(a)) if a else 0,
                  tolerance=tolerance, audible=any(a),
                  pythonSha256=hashlib.sha256(before).hexdigest(), rustSha256=hashlib.sha256(after).hexdigest())
    if maximum > tolerance:
        raise AssertionError(f'PCM differs by {maximum} S16 units, above the {tolerance}-voice quantization bound')
    return report


PYTHON_WRAPPER = r'''
import json,sys
from pathlib import Path
engine,original,data,pcm_name,event_name,epoch=sys.argv[1:]
sys.path[:0]=[engine,str(Path(engine)/'site-packages')]
import libc
libc.time.time=lambda:int(epoch)
from audio import Audio
from game import Game
from jni import Jni
import engine_process
with open(pcm_name,'wb',buffering=0) as pcm,open(event_name,'w',encoding='utf-8',buffering=1) as events:
 class RecordedAudio(Audio):
  def __init__(self,assets):super().__init__(assets,playback=False)
  def __call__(self,message,identifier=0,volume=1.,loop=0):
   identifier &= 0xffffffff
   loop=(int(loop)+0x80000000)%0x100000000-0x80000000
   if message==6:
    path=Jni.path(self.assets,volume)
    if not path.exists():path=Jni.path(self.assets,path.name)
    event=dict(type='load',id=identifier,path=path.relative_to(self.assets).as_posix(),loop=bool(loop))
   else:event=dict(type='message',message=message,id=identifier,gain=volume,loops=loop)
   events.write(json.dumps(event,allow_nan=False)+'\n')
   return super().__call__(message,identifier,volume,loop)
 step=Game.step
 def recorded_step(game,*args,**kwargs):
  result=step(game,*args,**kwargs)
  if game.jni.audio:pcm.write(game.jni.audio.mix(735).tobytes())
  return result
 Game.step=recorded_step
 engine_process.Audio=RecordedAudio
 engine_process.run(Path(original),Path(data),backend='aot')
'''


class Host:
    def __init__(self, label, command, environment, data, directory, timeout):
        self.label, self.data, self.directory, self.timeout = label, data, directory, timeout
        self.log = (directory / f'{label}.log').open('wb')
        self.started = time.perf_counter()
        self.child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=self.log, env=environment, bufsize=0)
        self.frames, self.playing, self.frame_bytes = 0, False, 0
        self.digest = hashlib.sha256()
        try:
            self.startup = self.until(b'ready')
        except BaseException:
            self.close(check=False)
            raise
        self.ready_ms = (time.perf_counter() - self.started) * 1000

    def until(self, desired):
        packets = []
        while True:
            kind, data = read_packet(self.child.stdout, self.timeout)
            if kind == b'EVT1':
                event = json_value(data)
                if event.get('type') == 'error':
                    raise RuntimeError(f'{self.label}: {event.get("message")}')
                if event.get('type') == 'playing':
                    self.playing = event['value']
                if event.get('type') == 'timing':
                    continue
                packets.append((kind, data))
                if desired == b'ready' and event.get('type') == 'ready':
                    return packets
            else:
                packets.append((kind, data))
            if kind in FRAME_KINDS:
                if desired != b'frame':
                    raise ValueError(f'{self.label} produced a game frame before ready')
                self.frames += 1
                self.frame_bytes += len(data)
                self.digest.update(data)
                return packets
            if kind not in (b'EVT1', b'TEX1'):
                raise ValueError(f'Unknown host packet {kind!r}')

    def step(self, gpu=True, tilt=0, touches=()):
        command = dict(frame=True, gpu=gpu, gpu_reset=self.frames == 0,
                       dt=1/60, tilt=tilt, touches=touches, side_width=1008)
        self.send(command)
        return self.until(b'frame')

    def send(self, command):
        self.child.stdin.write(json.dumps(command, allow_nan=False).encode() + b'\n')
        self.child.stdin.flush()

    def process_metrics(self):
        status = Path(f'/proc/{self.child.pid}/status')
        if not status.is_file():
            raise RuntimeError('Run host comparisons on Linux so peak child RSS is measurable')
        fields = dict(line.split(':', 1) for line in status.read_text().splitlines() if ':' in line)
        times = Path(f'/proc/{self.child.pid}/stat').read_text().rsplit(')', 1)[1].split()
        return dict(peakRssKiB=int(fields['VmHWM'].split()[0]),
                    cpuMs=(int(times[11]) + int(times[12])) * 1000 / os.sysconf('SC_CLK_TCK'))

    def close(self, check=True):
        if self.child.stdin and not self.child.stdin.closed:
            self.child.stdin.close()
        try:
            code = self.child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()
            code = -1
        self.child.stdout.close()
        self.log.close()
        if check and code:
            reason = (self.directory / f'{self.label}.log').read_text(errors='replace')[-2000:]
            raise RuntimeError(f'{self.label} exited {code}: {reason}')


def prepare_data(path, theme):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'appdata.i3d').write_text('<DoodleJumpHScore value="10000" />\n'
        f'<DoodleJumpActiveTheme value="{theme}" />\n<DoodleJumpDefaultTheme value="{theme}" />\n')


def start_host(args, label, theme, directory, null_audio=False):
    data = Path(tempfile.mkdtemp(prefix=f'{label}-data-', dir=directory))
    prepare_data(data, theme)
    environment = dict(os.environ, TZ='UTC', PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
                       MALLOC_MMAP_THRESHOLD_='1048576')
    pcm = Path('/dev/null') if null_audio else directory / f'{label}.pcm'
    events = directory / f'{label}-audio.jsonl'
    if label == 'python':
        environment['PYTHONPATH'] = str(args.python_engine / 'site-packages')
        command = [str(args.python), '-c', PYTHON_WRAPPER, str(args.python_engine), str(args.original),
                   str(data), str(pcm), str(events), '1700000000']
    else:
        command = [str(args.host), '--original', str(args.original), '--data', str(data),
                   '--no-audio', '--epoch', '1700000000', '--audio-output', str(pcm), '--audio-events', str(events)]
        if args.runtime:
            command.extend(['--runtime', str(args.runtime)])
    return Host(label, command, environment, data, directory, args.timeout)


def compare_files(left, right):
    a = {p.relative_to(left).as_posix(): p.read_bytes() for p in left.rglob('*') if p.is_file()}
    b = {p.relative_to(right).as_posix(): p.read_bytes() for p in right.rglob('*') if p.is_file()}
    if a != b:
        raise AssertionError(f'Saved files differ: {sorted(a)} vs {sorted(b)}')
    return {name: hashlib.sha256(value).hexdigest() for name, value in a.items()}


def parity(args, theme, directory):
    left = right = None
    try:
        left = start_host(args, 'python', theme, directory)
        right = start_host(args, 'rust', theme, directory)
        compare_packets(left.startup, right.startup, f'theme {theme} startup')
        counts = dict(texturePackets=0, framePackets=0)
        def step(tilt=0, touches=()):
            before = left.step(not args.cpu, tilt, touches)
            after = right.step(not args.cpu, tilt, touches)
            try:
                compare_packets(before, after, f'theme {theme}, frame {left.frames}')
            except AssertionError:
                for label, packets in (('python', before), ('rust', after)):
                    (directory / f'{label}-mismatch.bin').write_bytes(b''.join(k + struct.pack('<I', len(p)) + p for k, p in packets))
                raise
            counts['texturePackets'] += sum(k == b'TEX1' for k, _ in before)
            counts['framePackets'] += 1
        def tap(x, y):
            step(touches=[(0, 0, x, y)])
            step(touches=[(0, 2, x, y)])
        if args.scenario == 'opening':
            for index in range(args.frames):
                touches = [(0, 0, 180, 295)] if index == 240 else [(0, 2, 180, 295)] if index == 241 else ()
                tilt = .25 if 300 <= index < 303 else -.25 if 360 <= index < 363 else 0
                step(tilt, touches)
        else:
            for _ in range(240):
                step()
            tap(180, 295)
            for _ in range(120):
                step()
                if left.playing:
                    break
            if not left.playing:
                raise AssertionError('Reference did not start gameplay')
            for _ in range(1200):
                step(tilt=1)
                if not left.playing:
                    break
            if left.playing:
                raise AssertionError('Reference did not complete the fall')
            for _ in range(90):
                step()
            for x, y in ((275, 760), (381, 760), (60, 920)):
                tap(x, y)
            for _ in range(60):
                step()
            tap(272, 742 if theme == 14 else 665)
            for _ in range(120):
                step()
                if left.playing:
                    break
            if not left.playing:
                raise AssertionError('Reference did not restart')
        if left.playing != right.playing:
            raise AssertionError('Playing states differ')
        metrics = dict(python=left.process_metrics(), rust=right.process_metrics())
        frames, frame_digest = left.frames, left.digest.hexdigest()
        saved = compare_files(left.data, right.data)
        left.close()
        right.close()
        left = right = None
        events = [json_value(line) for line in (directory / 'python-audio.jsonl').read_bytes().splitlines()]
        native_events = [json_value(line) for line in (directory / 'rust-audio.jsonl').read_bytes().splitlines()]
        if not equal_json(events, native_events):
            first = next((i for i, (a, b) in enumerate(zip(events, native_events)) if not equal_json(a, b)), min(len(events), len(native_events)))
            raise AssertionError(f'theme {theme}: audio routing differs at event {first}: {events[first:first+1]} vs {native_events[first:first+1]}')
        pcm = compare_pcm(directory / 'python.pcm', directory / 'rust.pcm', frames,
                          tolerance=audio_voice_bound(events))
        return dict(theme=theme, frames=frames, frameSha256=frame_digest, **counts,
                    audioEvents=len(events), audioRoutesIdentical=True, pcm=pcm, savedFiles=saved, **metrics)
    finally:
        for host in (left, right):
            if host:
                host.close(check=False)


def benchmark(args, label, theme, directory):
    host = start_host(args, label, theme, directory, null_audio=True)
    try:
        for _ in range(240):
            host.step(not args.cpu)
        host.step(not args.cpu, touches=[(0, 0, 180, 295)])
        host.step(not args.cpu, touches=[(0, 2, 180, 295)])
        for _ in range(120):
            host.step(not args.cpu)
            if host.playing:
                break
        if not host.playing:
            raise AssertionError('Reference/candidate did not start benchmark gameplay')
        startup = (time.perf_counter() - host.started) * 1000
        cpu_start = host.process_metrics()['cpuMs']
        work = []
        for index in range(args.frames):
            tilt = .25 if index % 240 < 3 else -.25 if 120 <= index % 240 < 123 else 0
            begin = time.perf_counter_ns()
            host.step(not args.cpu, tilt=tilt)
            work.append((time.perf_counter_ns() - begin) / 1e6)
        metrics = host.process_metrics()
        ordered = sorted(work)
        return dict(host=label, theme=theme, measuredFrames=args.frames, totalFrames=host.frames,
                    uncappedProtocolFps=args.frames * 1000 / sum(work), medianWorkMs=statistics.median(work),
                    p95WorkMs=ordered[min(len(work)-1, int(len(work)*.95))],
                    p99WorkMs=ordered[min(len(work)-1, int(len(work)*.99))], startupMs=startup,
                    cpuMs=metrics['cpuMs']-cpu_start, peakRssKiB=metrics['peakRssKiB'],
                    frameBytes=host.frame_bytes, frameSha256=host.digest.hexdigest(), playing=host.playing)
    finally:
        host.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-engine', type=Path, default=BASELINE)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--host', type=Path, default=ROOT / '.cache/rust-host/target/release/doodle-host')
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--original', type=Path)
    parser.add_argument('--themes', type=int, choices=THEMES, nargs='+', default=list(THEMES))
    parser.add_argument('--mode', choices=('parity', 'benchmark'), default='parity')
    parser.add_argument('--scenario', choices=('full', 'opening'), default='full')
    parser.add_argument('--frames', type=int, default=600)
    parser.add_argument('--cpu', action='store_true')
    parser.add_argument('--rust-first', action='store_true', help='reverse serial benchmark order for repeated measurements')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--output', type=Path, default=ROOT / '.cache/rust-host/comparison.json')
    args = parser.parse_args()
    args.python_engine = args.python_engine.resolve()
    args.host = args.host.resolve()
    args.python = args.python.resolve()
    args.original = (args.original or args.python_engine / 'original').resolve()
    if not 1 <= args.frames <= 100000 or not 0 < args.timeout <= 300:
        parser.error('Frames must be 1..100000 and timeout 0..300 seconds')
    args.output = args.output.resolve()
    work = args.output.with_suffix('')
    work.mkdir(parents=True, exist_ok=True)
    report = dict(scope='Same Linux machine; native host process, GPU commands/CPU frame encoding, IPC and 44.1 kHz stereo PCM. Excludes Node, GPU worker, compositor and physical MGU.',
                  mode=args.mode, gpu=not args.cpu, pythonEngine=str(args.python_engine), rustHost=str(args.host),
                  rustHostSha256=hashlib.sha256(args.host.read_bytes()).hexdigest(),
                  epoch=1700000000, themes=[])
    for theme in args.themes:
        directory = work / str(theme)
        directory.mkdir(parents=True, exist_ok=True)
        try:
            if args.mode == 'parity':
                result = parity(args, theme, directory)
            else:
                order = ('rust', 'python') if args.rust_first else ('python', 'rust')
                measurements = {label: benchmark(args, label, theme, directory) for label in order}
                before, after = measurements['python'], measurements['rust']
                if before['frameSha256'] != after['frameSha256']:
                    raise AssertionError(f'theme {theme}: benchmark workloads differ')
                result = dict(theme=theme, python=before, rust=after,
                              speedup=after['uncappedProtocolFps']/before['uncappedProtocolFps'],
                              rssSavedKiB=before['peakRssKiB']-after['peakRssKiB'])
        except Exception as error:
            report['failure'] = dict(theme=theme, type=type(error).__name__, message=str(error))
            args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
            raise
        report['themes'].append(result)
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
        print(json.dumps(result, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
