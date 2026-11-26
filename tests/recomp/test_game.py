"""Compare original game output through Unicorn and the recompiled backend."""
from pathlib import Path as _Path
import sys as _sys
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / 'tools'))
from prepare_reference import activate as _activate
_REFERENCE = _activate()

import contextlib
import gc
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from game import Game
from audio import Audio


ROOT = Path(__file__).resolve().parents[2]
THEMES = (1, 14, 12, 11, 9, 8, 5, 6, 7, 4, 2, 10)


class RecompiledGameTests(unittest.TestCase):
    def test_host_failures_keep_their_reason_and_next_call_works(self):
        from arm import Arm
        machine = Arm(ROOT / 'original/extracted/lib/armeabi-v7a/libDoodleJump.so', backend='aot')

        def broken():
            raise ValueError('failed game asset')

        address = machine.register('broken_asset', broken)
        with self.assertRaisesRegex(ValueError, 'failed game asset'):
            machine.call(address)
        with self.assertRaisesRegex(NotImplementedError, 'Host import missing_callback'):
            machine.call(machine.import_address('missing_callback'))

        def recover():
            try:
                machine.call(address)
            except ValueError:
                return 456
            self.fail('Nested callback error was lost')

        self.assertEqual(machine.call(machine.register('recover', recover)), 456)
        self.assertEqual(machine.call(machine.register('next_call', lambda: 123)), 123)

    def test_initial_frames_match_unicorn(self):
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            with patch('libc.time.time', return_value=1700000000):
                reference = Game(ROOT / 'original', Path(left), render_size=(408, 720))
                with patch('unicorn.Uc', side_effect=AssertionError('AOT must not create an emulator')):
                    candidate = Game(ROOT / 'original', Path(right), render_size=(408, 720), backend='aot')
            self.assertEqual(candidate.machine.backend, 'aot')
            for frame in range(8):
                with self.subTest(frame=frame):
                    reference.graphics.begin_gpu_frame(True, reset=frame == 0)
                    candidate.graphics.begin_gpu_frame(True, reset=frame == 0)
                    reference.step(readback=False)
                    candidate.step(readback=False)
                    self.assertEqual(candidate.graphics.gpu_frame(), reference.graphics.gpu_frame())

    def test_all_themes_match_graphics_sound_and_gameplay(self):
        for theme in THEMES:
            with self.subTest(theme=theme), tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
                for directory in (left, right):
                    (Path(directory) / 'appdata.i3d').write_text(
                        '<DoodleJumpHScore value="10000" />\n'
                        f'<DoodleJumpActiveTheme value="{theme}" />\n'
                        f'<DoodleJumpDefaultTheme value="{theme}" />\n')
                with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), \
                        patch('libc.time.time', return_value=1700000000):
                    reference = Game(ROOT / 'original', Path(left), render_size=(408, 720))
                    with patch('unicorn.Uc', side_effect=AssertionError('AOT must not create an emulator')):
                        candidate = Game(ROOT / 'original', Path(right), render_size=(408, 720), backend='aot')
                    sounds = [Audio(game.jni.assets, playback=False) for game in (reference, candidate)]
                    for game, audio in zip((reference, candidate), sounds):
                        game.jni.audio = audio
                    audible = False
                    try:
                        for frame in range(480):
                            touches = [(0, 0, 180, 295)] if frame == 240 else [(0, 2, 180, 295)] if frame == 241 else []
                            tilt = .25 if 300 <= frame < 303 else -.25 if 360 <= frame < 363 else 0
                            commands, samples = [], []
                            for game, audio in zip((reference, candidate), sounds):
                                game.graphics.begin_gpu_frame(True, reset=frame == 0)
                                game.step(tilt=tilt, touches=touches, readback=False)
                                commands.append(game.graphics.gpu_frame())
                                samples.append(audio.mix(800).tobytes())
                            self.assertEqual(commands[1], commands[0], f'theme {theme}, frame {frame}: graphics')
                            self.assertEqual(samples[1], samples[0], f'theme {theme}, frame {frame}: PCM')
                            self.assertEqual(candidate.jni.playing, reference.jni.playing)
                            audible |= any(samples[1])
                        self.assertTrue(audible, 'Gameplay must produce its original sounds')
                        self.assertTrue(candidate.jni.playing)
                    finally:
                        for audio in sounds:
                            audio.close()
                del reference, candidate, sounds, game, audio
                gc.collect()

    def test_game_over_save_and_restart_match_unicorn(self):
        for theme in THEMES:
            with self.subTest(theme=theme):
                with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right, \
                        open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), \
                        patch('libc.time.time', return_value=1700000000):
                    for directory in (left, right):
                        (Path(directory) / 'appdata.i3d').write_text(
                            '<DoodleJumpHScore value="10000" />\n'
                            f'<DoodleJumpActiveTheme value="{theme}" />\n'
                            f'<DoodleJumpDefaultTheme value="{theme}" />\n')
                    games = [Game(ROOT / 'original', Path(left), render_size=(408, 720)),
                             Game(ROOT / 'original', Path(right), render_size=(408, 720), backend='aot')]
                    sounds = [Audio(game.jni.assets, playback=False) for game in games]
                    for game, audio in zip(games, sounds):
                        game.jni.audio = audio
                    frame = 0

                    def step(tilt=0, touches=()):
                        nonlocal frame
                        commands, samples = [], []
                        for game, audio in zip(games, sounds):
                            game.graphics.begin_gpu_frame(True, reset=frame == 0)
                            game.step(tilt=tilt, touches=touches, readback=False)
                            commands.append(game.graphics.gpu_frame())
                            samples.append(audio.mix(800).tobytes())
                        self.assertEqual(commands[1], commands[0], f'frame {frame}: graphics')
                        self.assertEqual(samples[1], samples[0], f'frame {frame}: PCM')
                        self.assertEqual(games[1].jni.playing, games[0].jni.playing)
                        frame += 1

                    def tap(x, y):
                        step(touches=[(0, 0, x, y)])
                        step(touches=[(0, 2, x, y)])

                    try:
                        for _ in range(240):
                            step()
                        tap(180, 295)
                        for _ in range(120):
                            step()
                            if games[0].jni.playing:
                                break
                        self.assertTrue(games[1].jni.playing)
                        for _ in range(1200):
                            step(tilt=1)
                            if not games[0].jni.playing:
                                break
                        self.assertFalse(games[0].jni.playing, 'The reference did not reach game over')
                        self.assertFalse(games[1].jni.playing)
                        for _ in range(90):
                            step()
                        for x, y in ((275, 760), (381, 760), (60, 920)):
                            tap(x, y)
                        for _ in range(60):
                            step()
                        # The 8-bit theme places its replay button lower.
                        tap(272, 742 if theme == 14 else 665)
                        for _ in range(120):
                            step()
                            if games[0].jni.playing:
                                break
                        self.assertTrue(games[1].jni.playing)
                        self.assertTrue(games[0].jni.playing, 'The reference replay did not start the game')
                        files = [{path.name: path.read_bytes() for path in Path(directory).iterdir()}
                                 for directory in (left, right)]
                        self.assertEqual(files[1], files[0])
                    finally:
                        for audio in sounds:
                            audio.close()
                        del games, sounds, game, audio
                        gc.collect()


if __name__ == '__main__':
    unittest.main()
