"""Check the actual production runtime after rebuilding runtime/prepare.py."""
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import zlib


PACK = Path(__file__).resolve().parents[2] / 'dist/runtime-linux-x86_64.pack'


class DataPackTests(unittest.TestCase):
    def test_packer_preserves_files_for_the_actual_engine_loader(self):
        root = PACK.parents[1]
        spec = importlib.util.spec_from_file_location('pack_data', root / 'kit/tools/pack_data.py')
        packer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packer)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'input'
            folder.mkdir()
            expected = {'metadata.json': json.dumps({'long-value': 'x' * 65536}).encode(),
                        'pixels.bin': bytes(range(256))}
            for name, data in {**expected, 'excluded': b'unused', '.packignore': b'excluded\n'}.items():
                (folder / name).write_bytes(data)
            output = Path(directory) / 'runtime.pack'
            packer.pack(str(folder), str(output))
            extracted = Path(directory) / 'extracted'
            subprocess.run(['node', '-e', 'require(process.argv[1]).unpack(process.argv[2],process.argv[3])',
                            str(root / 'apps/kxrxh_doodle_jump/engine.js'), str(output), str(extracted)], check=True)
            for name, data in expected.items():
                self.assertEqual((extracted / name).read_bytes(), data)
            self.assertFalse((extracted / 'excluded').exists())


@unittest.skipUnless(PACK.is_file(), 'requires a built production runtime')
class RuntimePackTests(unittest.TestCase):
    def test_production_host_is_prebuilt_native_code_without_python(self):
        with PACK.open('rb') as stream:
            header = stream.read(12)
            self.assertEqual(header[:8], b'MGUPACK1')
            size = struct.unpack_from('<I', header, 8)[0]
            index = json.loads(zlib.decompress(stream.read(size), -15))
            base = 12 + size
            files = set(index['files'])
            self.assertTrue({'doodle-host', 'native.so', 'recomp/game.so', 'arm_helpers.o',
                             'assets/cursor.png', 'NOTICE', 'LICENSE', 'recomp/LICENSE',
                             'licenses/GPL-3.txt'} <= files)
            offset, length = index['files']['licenses/GPL-3.txt']
            stream.seek(base + offset)
            gpl = (PACK.parents[1] / 'native/doodle-gpu/LICENSE').read_bytes()
            self.assertEqual(zlib.decompress(stream.read(length), -15), gpl)
            unwanted = sorted(name for name in files if name.endswith(('.py', '.pyc', '.rs', '.cpp', '.c', '.S')) or any(
                part.startswith(('unicorn', 'libunicorn', 'capstone', 'site-packages')) for part in name.split('/')))
            self.assertEqual(unwanted, [], 'Production must not contain source build fallbacks or Python dependencies')
            for name in ('doodle-host', 'native.so', 'recomp/game.so'):
                offset, length = index['files'][name]
                stream.seek(base + offset)
                binary = zlib.decompress(stream.read(length), -15)
                self.assertEqual(binary[:6], b'\x7fELF\x02\x01', name)
                self.assertEqual(binary[18:20], b'\x3e\x00', name)


if __name__ == '__main__':
    unittest.main()
