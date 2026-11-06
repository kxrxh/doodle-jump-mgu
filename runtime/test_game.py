import importlib
from pathlib import Path
import tempfile
import unittest

from test_arm import LIBRARY


class GameTests(unittest.TestCase):
    def test_original_engine_menu_gameplay_and_game_over(self):
        try:
            module = importlib.import_module('game')
        except ModuleNotFoundError:
            self.fail('The original engine game-loop host is missing')
        with tempfile.TemporaryDirectory() as data:
            # A returning player bypasses the original first-game practice floor.
            (Path(data) / 'appdata.i3d').write_text('<DoodleJumpHScore value="10000" />\n')
            game = module.Game(LIBRARY.parents[3], Path(data))
            for _ in range(240):
                image = game.step()
            colors = image.convert('RGB').getcolors(image.width * image.height)
            self.assertGreater(len(colors), 32, 'The original menu must render rather than a blank framebuffer')
            self.assertGreater(game.graphics.draws, 0)
            self.assertFalse(game.jni.playing)
            self.assertGreaterEqual(len(game.graphics.excluded_rectangles), 2, 'Purchase buttons must be omitted')
            game.step(touches=[(0, 0, 180, 295)])
            game.step(touches=[(0, 2, 180, 295)])
            for _ in range(120):
                game.step()
                if game.jni.playing:
                    break
            self.assertTrue(game.jni.playing, 'Touching Play must start the original game')
            for _ in range(900):
                image = game.step(tilt=1)
                if not game.jni.playing:
                    break
            self.assertFalse(game.jni.playing, 'A missed platform must reach the original game-over screen')
            self.assertTrue(any(Path(data).iterdir()), 'Original game must persist its data')
            for _ in range(90):
                game.step()
            # A new profile enters its name with the original on-screen keyboard.
            for x, y in ((275, 760), (381, 760), (60, 920)):
                game.step(touches=[(0, 0, x, y)])
                game.step(touches=[(0, 2, x, y)])
            for _ in range(60):
                game.step()
            game.step(touches=[(0, 0, 272, 665)])
            game.step(touches=[(0, 2, 272, 665)])
            for _ in range(120):
                game.step()
                if game.jni.playing:
                    break
            self.assertTrue(game.jni.playing, 'Play Again must restart the original game')


if __name__ == '__main__':
    unittest.main()
