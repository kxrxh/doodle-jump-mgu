import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from PIL import Image
import engine_process


class ProcessTests(unittest.TestCase):
    def test_panorama_images_are_released_before_waiting_for_another_frame(self):
        images = []
        game = SimpleNamespace(
            graphics=SimpleNamespace(width=408, height=720),
            jni=SimpleNamespace(assets=Path('.'), dialogs=[], playing=False),
            step=lambda *args, **kwargs: b'frame')

        class Scenery:
            sprites = {}
            def assets(self):
                return []
            def paper(self, width):
                planes = [(i, Image.new('RGBA', (width, 960))) for i in (2, 3)]
                images.extend(weakref.ref(image) for _, image in planes)
                return planes

        def commands():
            yield '{"frame": true, "side_width": 1}\n'
            self.assertEqual(len(images), 2)
            self.assertTrue(all(image() is None for image in images),
                            'sent panorama images must not stay resident between frames')

        output = SimpleNamespace(buffer=io.BytesIO())
        with patch.object(engine_process, 'Game', return_value=game), \
             patch.object(engine_process, 'Audio'), \
             patch.object(engine_process, 'Scenery', return_value=Scenery()), \
             patch.object(engine_process, 'FrameEncoder') as encoder, \
             patch.multiple(engine_process.sys, stdin=commands(), stdout=output, stderr=io.StringIO()):
            encoder.return_value.encode.return_value = b'frame'
            engine_process.run(Path('.'), Path('.'))


if __name__ == '__main__':
    unittest.main()
