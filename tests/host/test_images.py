"""Compare Rust pixels with the existing Pillow host, using original game assets."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from prepare_reference import activate
activate(current=False)
from PIL import Image, UnidentifiedImageError
from gles import GLES
from scenery import Scenery


def fixtures(directory):
    assets = ROOT / 'original/extracted/assets'
    output = {'pngs': [], 'invalid_pngs': [], 'texts': [], 'resizes': [], 'papers': []}
    for source in sorted(assets.rglob('*.png')) + [ROOT / 'runtime/assets/cursor.png']:
        try:
            with Image.open(source) as image:
                image = image.convert('RGBA')
                output['pngs'].append(dict(path=str(source), width=image.width, height=image.height,
                                          sha256=hashlib.sha256(image.tobytes()).hexdigest()))
        except (UnidentifiedImageError, OSError):
            output['invalid_pngs'].append(str(source))


    for number, (sw, sh, width, height) in enumerate(((2, 1, 3, 1), (8, 5, 3, 2), (8, 5, 13, 11),
            (7, 4, 1, 1), (7, 4, 7, 8), (7, 4, 12, 4), (31, 17, 6, 3))):
        source = bytes((i * 37 + i // sw * 19) % 256 for i in range(sw * sh))
        resized = Image.frombytes('L', (sw, sh), source).resize((width, height), Image.Resampling.BILINEAR)
        output['resizes'].append(dict(sw=sw, sh=sh, width=width, height=height,
                                     source=list(source), pixels=list(resized.tobytes())))

    def text(message, tm, width, height, texts, local, recent):
        fields = dict(width=width, height=height, texture_multiplier=struct.unpack('<I', struct.pack('<f', tm))[0])
        values, strings, memory = {}, {}, {}
        for name, entries in (('texts', texts), ('local_names', local), ('recent_names', recent)):
            pointer = 0x1000 + len(values) * 0x100
            fields[name] = pointer
            values[pointer] = dict(data=pointer, length=len(entries))
            for i, entry in enumerate(entries):
                handle = 0x10000 + len(strings) * 16
                memory[pointer + i * 4] = handle
                strings[handle] = entry
        captured = []
        def capture(_handle, _width, _height, pixels):
            captured.append(pixels)
            return 42
        machine = SimpleNamespace(u32=lambda address: memory[address])
        jni = SimpleNamespace(assets=assets, values=values, field=lambda _obj, name: fields.get(name, 0),
                              text=lambda pointer: strings.get(pointer, ''), set_field=lambda *_args: None)
        graphics = SimpleNamespace(font=None, _handle=None, m=machine,
                                   library=SimpleNamespace(graphics_text_texture=capture))
        GLES.text_texture(graphics, message, 0, jni)
        name = f'text-{len(output["texts"])}.rgba'
        (directory / name).write_bytes(captured[0])
        output['texts'].append(dict(message=message, tm=tm, width=width, height=height, texts=texts,
                                    local=local, recent=recent, pixels=name))

    for tm in (.5, 1, 1.5, 2, 3, 4):
        text(63, tm, int(256 * tm), int(200 * tm), ['Classic', '8-bit', 'Doodlestein', 'Snow'], [], [])
        text(21, tm, int(80 * tm), int(160 * tm), [], ['abcdefghijk', 'JÖHN', '01234567'], ['A B', 'Zoë', ''])
    text(63, 1, 10, 10, ['long label clipped outside image'], [], [])

    sprite_scene = Scenery(SimpleNamespace(jni=SimpleNamespace(assets=assets)))
    output['sprites'] = sprite_scene.sprites
    output['sprite_images'] = []
    for identifier, image in sprite_scene.assets():
        name = f'sprite-{identifier}.rgba'
        (directory / name).write_bytes(image.tobytes())
        output['sprite_images'].append(dict(id=identifier, width=image.width, height=image.height, pixels=name))

    source = Image.new('RGBA', (2, 3))
    source.putdata([(255, 0, 0, 255), (0, 255, 0, 128), (0, 0, 255, 32),
                    (255, 255, 255, 0), (180, 90, 45, 200), (7, 13, 51, 254)])
    source.save(directory / 'paper.png')
    padded = Image.new('RGBA', (4, 4)); padded.paste(source, (0, 0))
    texture = dict(width=4, height=4, pixels=padded.tobytes(), asset='paper.png', revision=1)
    for flip_x, flip_y, tint in ((False, False, (1, 1, 1)), (True, False, (1, 1, 1)),
                                (False, True, (1, 1, 1)), (True, True, (.6, .9, .3))):
        for phase in (.25, -.875):
            color = (*tint, .5)
            u0, u1 = (.5, 0) if flip_x else (0, .5)
            v0, v1 = (phase + .75, phase) if flip_y else (phase, phase + .75)
            vertices = ((0, 0, u0, v0, *color), (544, 0, u1, v0, *color),
                        (0, 960, u0, v1, *color), (544, 960, u1, v1, *color))
            graphics = SimpleNamespace(background_info=(7, vertices), texture_info=lambda _id: texture)
            scene = Scenery(SimpleNamespace(graphics=graphics, jni=SimpleNamespace(assets=directory)))
            image = scene.paper(1008)[0][1]
            name = f'paper-{len(output["papers"])}.rgba'; (directory / name).write_bytes(image.tobytes())
            output['papers'].append(dict(width=4, height=4, texture=list(padded.tobytes()), content=[2, 3],
                vertices=vertices, projection=scene.projection, pixels=name))
    output['assets'] = str(assets)
    output['cursor'] = str(ROOT / 'runtime/assets/cursor.png')
    (directory / 'fixtures.json').write_text(json.dumps(output))
    return output


class ImageParityTests(unittest.TestCase):
    def test_rust_images_match_the_pillow_host(self):
        with tempfile.TemporaryDirectory(prefix='doodle-image-oracle-') as temporary:
            directory = Path(temporary).resolve()
            fixtures(directory)
            environment = dict(os.environ, DOODLE_IMAGE_FIXTURES=str(directory))
            environment.setdefault('CARGO_HOME', str(ROOT / '.cache/cargo'))
            manifest = environment.get('DOODLE_HOST_MANIFEST', str(ROOT / 'runtime/host/Cargo.toml'))
            result = subprocess.run([environment.get('DOODLE_HOST_CARGO', 'cargo'), 'test', '--manifest-path', manifest,
                'images::tests::matches_pillow_fixtures', '--', '--exact', '--ignored'],
                cwd=ROOT, env=environment, capture_output=True, text=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
