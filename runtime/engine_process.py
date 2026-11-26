"""Original engine process driven by the MGU runtime's frame and input events."""
import argparse
import json
from pathlib import Path
import platform
import struct
import sys
import time

from game import Game, ORIGINAL
from input_state import Input
from audio import Audio
from scenery import Scenery
from frame_encoder import FrameEncoder

DEFAULT_BACKEND = 'aot' if sys.platform == 'linux' and platform.machine() == 'x86_64' else 'unicorn'


def run(original, data, backend=DEFAULT_BACKEND):
    output = sys.stdout.buffer
    sys.stdout = sys.stderr
    def packet(kind, *parts):
        output.write(kind + struct.pack('<I', sum(map(len, parts))))
        for part in parts:
            output.write(part)
        output.flush()
    def event(value):
        packet(b'EVT1', json.dumps(value, allow_nan=False).encode())
    def texture(identifier,image):
        packet(b'TEX1',struct.pack('<III',identifier,image.width,image.height)+image.tobytes())
    try:
        game = Game(original, data, render_size=(408, 720), backend=backend)
        encoder = None
        gpu_mode = False
        audio = Audio(game.jni.assets)
        game.jni.audio = audio
        scenery=Scenery(game)
        for identifier,image in scenery.assets():
            texture(identifier,image)
        event(dict(type='scenery',sprites=scenery.sprites))
        paper_projection = None
        state = Input()
        event(dict(type='ready'))
        paused = False
        playing = False
        for line in sys.stdin:
            if len(line) > 4096:
                raise ValueError('Engine input exceeds its size limit')
            command = json.loads(line)
            if 'dialog' in command:
                reply = command['dialog']
                if not isinstance(reply, dict) or type(reply.get('id')) is not int or not isinstance(reply.get('button'), str):
                    raise ValueError('Invalid dialog reply')
                game.jni.reply_dialog(reply['id'], reply['button'])
            state.apply(command)
            tilt, touches, actions = state.take()
            for action in actions:
                if action == 'back':
                    game.jni.send(-7)
                else:
                    paused = action == 'pause'
                    game.jni.send(28 if paused else 29)
                    audio(12 if paused else 13)
            if not command.get('frame') or paused:
                continue
            dt = Input.number(command.get('dt', 1/60), 0, .1)
            side_width=command.get('side_width',1008)
            if type(side_width) is not int or not 0<=side_width<=2048:
                raise ValueError('Invalid panorama width')
            gpu = command.get('gpu', False)
            reset = command.get('gpu_reset', False)
            if type(gpu) is not bool or type(reset) is not bool:
                raise ValueError('Invalid GPU frame mode')
            timing = command.get('timing', False)
            if type(timing) is not bool:
                raise ValueError('Invalid frame timing flag')
            if timing:
                wall = time.perf_counter_ns()
                cpu = time.thread_time_ns()
            if gpu or gpu_mode:
                game.graphics.begin_gpu_frame(gpu, reset)
            if gpu != gpu_mode:
                encoder = None
                gpu_mode = gpu
            frame = game.step(tilt, touches, dt, readback=False)
            planes=scenery.paper(side_width)
            for identifier,plane in planes:
                texture(identifier,plane)
            if scenery.projection != paper_projection:
                paper_projection = scenery.projection
                event(dict(type='scenery',paper=paper_projection))
            if planes:
                del plane
            del planes
            while game.jni.dialogs:
                event(game.jni.dialogs.pop(0))
            if game.jni.playing != playing:
                playing = game.jni.playing
                event(dict(type='playing', value=playing))
            if gpu:
                kind, payload = b'GPF1', game.graphics.gpu_frame()
            else:
                if encoder is None:
                    encoder = FrameEncoder(game.graphics.width, game.graphics.height)
                kind, payload = b'FRM2', encoder.encode(frame)
            if timing:
                event(dict(type='timing', wallMs=(time.perf_counter_ns()-wall)/1e6,
                           cpuMs=(time.thread_time_ns()-cpu)/1e6))
            packet(kind, payload)
            del payload
    except BrokenPipeError:
        pass
    except BaseException as error:
        import traceback
        traceback.print_exc()
        event(dict(type='error', message=str(error) or type(error).__name__))
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--original', type=Path, default=ORIGINAL)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--backend', choices=('aot', 'unicorn'), default=DEFAULT_BACKEND)
    args = parser.parse_args()
    run(args.original, args.data, args.backend)
