import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from PIL import Image
import engine_process


class ProcessTests(unittest.TestCase):
    def packets(self, data):
        packets = []
        while data:
            size = struct.unpack_from('<I', data, 4)[0]
            self.assertGreaterEqual(len(data), size + 8)
            packets.append((data[:4], data[8:8 + size]))
            data = data[8 + size:]
        return packets

    def test_gpu_frames_skip_pixel_encoding_and_switch_back_to_a_full_image(self):
        from audio import Audio
        from game import ORIGINAL

        commands = [dict(frame=True, gpu=True, gpu_reset=True, side_width=0),
                    dict(frame=True, gpu=True, side_width=0, timing=True),
                    dict(frame=True, gpu=False, side_width=0, timing=True)]
        output = SimpleNamespace(buffer=io.BytesIO())
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(engine_process, 'Audio', side_effect=lambda assets: Audio(assets, playback=False)), \
             patch.multiple(engine_process.sys, stdin=iter(json.dumps(c) + '\n' for c in commands),
                            stdout=output, stderr=io.StringIO()):
            engine_process.run(ORIGINAL, Path(directory))
        packets = self.packets(output.buffer.getvalue())
        measured = []
        for i, (kind, data) in enumerate(packets):
            if kind == b'EVT1' and json.loads(data)['type'] == 'timing':
                sample = json.loads(data)
                self.assertGreaterEqual(sample['wallMs'], 0)
                self.assertGreaterEqual(sample['cpuMs'], 0)
                measured.append(packets[i + 1][0])
        self.assertEqual(measured, [b'GPF1', b'FRM2'])
        frames = [(kind, data) for kind, data in packets
                  if kind in (b'GPF1', b'FRM2')]
        self.assertEqual([kind for kind, _ in frames], [b'GPF1', b'GPF1', b'FRM2'])
        for _, data in frames[:2]:
            messages = []
            while data:
                kind, size = struct.unpack_from('<II', data)
                self.assertLessEqual(size + 8, len(data))
                messages.append(kind)
                data = data[8 + size:]
            self.assertIn(0x4d520008, messages)
        width, height, count = struct.unpack_from('<III', frames[-1][1])
        self.assertEqual((width, height, count), (408, 720, 1))
        self.assertEqual(struct.unpack_from('<IIII', frames[-1][1], 12), (0, 0, 408, 720))

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
