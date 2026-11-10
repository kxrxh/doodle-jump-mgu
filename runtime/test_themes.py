import contextlib
import gc
import io
from pathlib import Path
import tempfile
import unittest

from game import Game
from test_arm import LIBRARY


# IDs reached through the original menu's theme selector, with each theme's main texture.
THEMES = (
    (1, 'gametiles_L.png'),
    (14, '8bit/8bit-bck_L.png'),
    (12, 'pirate/game-tiles-pirates_L.png'),
    (11, 'ninja/game-tiles-ninja_L.png'),
    (9, 'bunny/gametilesbunny_L.png'),
    (8, 'doodlestein/gametileshalloween_L.png'),
    (5, 'space/gametilesspace_L.png'),
    (6, 'soccer/gametilessoccer_L.png'),
    (7, 'underwater/gametilesunderwater_L.png'),
    (4, 'jungle/gametilesjungle_L.png'),
    (2, 'snow/gametilessnow_L.png'),
    (10, 'ice/gametilesice_L.png'),
)


class ThemeTests(unittest.TestCase):
    def test_every_selectable_theme_renders_and_starts_gameplay(self):
        for identifier, texture in THEMES:
            with self.subTest(theme=identifier), tempfile.TemporaryDirectory() as directory:
                data = Path(directory)
                (data / 'appdata.i3d').write_text(
                    '<DoodleJumpHScore value="10000" />\n'
                    f'<DoodleJumpActiveTheme value="{identifier}" />\n'
                    f'<DoodleJumpDefaultTheme value="{identifier}" />\n')
                with contextlib.redirect_stdout(io.StringIO()):
                    game = Game(LIBRARY.parents[3], data)
                    for _ in range(240):
                        image = game.step()
                    self.assertGreater(len(image.convert('RGB').getcolors(image.width * image.height)), 32)
                    self.assertIn(texture, {item.get('asset') for item in game.graphics.textures.values()},
                                  'The selected theme must load its own texture instead of falling back to classic')
                    game.step(touches=[(0, 0, 180, 295)])
                    game.step(touches=[(0, 2, 180, 295)])
                    for _ in range(120):
                        image = game.step()
                        if game.jni.playing and len(image.convert('RGB').getcolors(image.width * image.height)) > 32:
                            break
                    self.assertTrue(game.jni.playing, 'The selected theme must start when Play is pressed')
                    self.assertGreater(len(image.convert('RGB').getcolors(image.width * image.height)), 32)
                del game
                gc.collect()


if __name__ == '__main__':
    unittest.main()
