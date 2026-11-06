"""Local host for the original, unmodified Doodle Jump 3.10.0 engine."""
import argparse
from pathlib import Path
import struct
import time

from arm import Arm
from gles import GLES
from jni import Jni, NATIVE
from libc import LibC
from commerce import blocked
from png_decoder import PngDecoder


DIRECTORY = Path(__file__).resolve().parent
ORIGINAL = DIRECTORY / 'original' if (DIRECTORY / 'original').is_dir() else DIRECTORY.parent / 'original'


class Game:
    def __init__(self, original=ORIGINAL, data=Path(__file__).resolve().parent / 'data'):
        self.machine = m = Arm(original / 'extracted/lib/armeabi-v7a/libDoodleJump.so')
        self.libc = LibC(m)
        m.initialize()
        self.jni = j = Jni(m, original / 'extracted/assets', data)
        self.graphics = GLES(m, 544, 960)
        self.png_decoder = PngDecoder(m, self.graphics.library)
        j.graphics = self.graphics
        self.touches = j.array(16 * 4, 4)
        self.acceleration = j.array(12, 4)
        self.keys = j.array(32 * 2, 4)
        self.frames = 0
        for message in (-1, -4, 29):
            j.send(message)
        j.send(0, j.configuration())

    def step(self, tilt=0.0, touches=(), dt=1/60):
        if len(touches) > 16:
            raise ValueError('At most 16 touch events per original engine frame')
        self.libc.elapsed += dt
        j, m = self.jni, self.machine
        touches = [event for event in touches if not blocked(self.graphics.input_rectangles, event[2], event[3])]
        self.graphics.excluded_rectangles = []
        self.graphics.input_rectangles = []
        while j.deferred:
            j.send(*j.deferred.pop(0))
        if touches:
            m.uc.mem_write(j.values[self.touches]['data'], struct.pack('<' + 'f' * (len(touches)*4),
                                                                     *(x for event in touches for x in event)))
        m.uc.mem_write(j.values[self.acceleration]['data'], struct.pack('<3f', -max(-1.0, min(1.0, tilt))*6, 0, 9.81))
        m.call(NATIVE + 'runGameLoopNative', j.env, j.center, len(touches), self.touches, 1, self.acceleration, 0, self.keys)
        self.frames += 1
        return self.graphics.image()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--frames', type=int, default=3)
    parser.add_argument('--output', type=Path, default=Path('data/original-menu.png'))
    args = parser.parse_args()
    game = Game()
    started = time.monotonic()
    for _ in range(args.frames):
        image = game.step()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    print(f'{args.frames} original engine frames in {time.monotonic()-started:.3f}s; saved {args.output}')
