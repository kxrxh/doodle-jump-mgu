"""Original engine process driven by the MGU runtime's frame and input events."""
import argparse
import json
from pathlib import Path
import struct
import sys

from game import Game, ORIGINAL
from input_state import Input
from audio import Audio
from scenery import Scenery


def run(original, data):
    output = sys.stdout.buffer
    sys.stdout = sys.stderr
    def packet(kind, payload):
        output.write(kind + struct.pack('<I', len(payload)) + payload)
        output.flush()
    def event(value):
        packet(b'EVT1', json.dumps(value, allow_nan=False).encode())
    def texture(identifier,image):
        packet(b'TEX1',struct.pack('<III',identifier,image.width,image.height)+image.tobytes())
    try:
        game = Game(original, data)
        audio = Audio(game.jni.assets)
        game.jni.audio = audio
        scenery=Scenery(game)
        for identifier,image in scenery.assets():
            texture(identifier,image)
        event(dict(type='scenery',sprites=scenery.sprites))
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
            image = game.step(tilt, touches, dt)
            planes=scenery.paper(side_width)
            for identifier,plane in planes:
                texture(identifier,plane)
            if planes:
                event(dict(type='scenery',paperWidth=side_width))
            while game.jni.dialogs:
                event(game.jni.dialogs.pop(0))
            if game.jni.playing != playing:
                playing = game.jni.playing
                event(dict(type='playing', value=playing))
            packet(b'FRM1', struct.pack('<II', image.width, image.height) + image.tobytes())
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
    args = parser.parse_args()
    run(args.original, args.data)
