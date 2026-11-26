"""Check the actual production runtime after rebuilding runtime/prepare.py."""
import json
from pathlib import Path
import struct
import unittest
import zlib


PACK = Path(__file__).resolve().parents[2] / 'dist/runtime-linux-x86_64.pack'


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
                             'assets/cursor.png', 'NOTICE', 'LICENSE', 'recomp/LICENSE'} <= files)
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
