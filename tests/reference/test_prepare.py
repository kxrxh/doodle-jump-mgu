"""Reference setup verifies its archive and never changes the frozen baseline."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('prepare_reference', ROOT / 'tools/prepare_reference.py')
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


def archive(files):
    payload = bytearray(); entries = {}
    for name, value in files.items():
        encoded = zlib.compress(value)[2:-4]
        entries[name] = [len(payload), len(encoded)]; payload.extend(encoded)
    index = zlib.compress(json.dumps(dict(files=entries)).encode())[2:-4]
    return b'MGUPACK1' + struct.pack('<I', len(index)) + index + payload


class ReferenceSetupTests(unittest.TestCase):
    def test_verified_pack_extracts_every_file_without_modifying_an_existing_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); pack = root / 'runtime.pack'; data = archive({'arm.py': b'adapter', 'recomp/game.so': b'game'})
            pack.write_bytes(data); digest = hashlib.sha256(data).hexdigest(); output = root / 'reference'
            reference.unpack(pack, output, digest)
            self.assertTrue((output / 'arm.py').is_file(), 'The verified adapter must be extracted')
            self.assertEqual((output / 'arm.py').read_bytes(), b'adapter')
            self.assertEqual((output / 'recomp/game.so').read_bytes(), b'game')
            (output / 'local-note').write_text('keep')
            reference.unpack(pack, output, digest)
            self.assertEqual((output / 'local-note').read_text(), 'keep')

    def test_unverified_or_unsafe_archives_leave_the_destination_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); pack = root / 'runtime.pack'; output = root / 'reference'
            data = archive({'arm.py': b'adapter'}); pack.write_bytes(data)
            with self.assertRaises(ValueError): reference.unpack(pack, output, '0' * 64)
            self.assertFalse(output.exists())
            for name in ('../escape', '/absolute', 'a/../../escape', 'a\\escape'):
                data = archive({name: b'unsafe'}); pack.write_bytes(data)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    reference.unpack(pack, output, hashlib.sha256(data).hexdigest())
                self.assertFalse(output.exists())
            self.assertFalse((root / 'escape').exists())

    def test_current_backend_overlay_preserves_the_frozen_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); baseline = root / 'baseline'; baseline.mkdir()
            for name, value in {'native.so': b'frozen-native', 'native_backend.py': b'adapter',
                                'raster.c': b'frozen-source', 'recomp/game.so': b'frozen-game'}.items():
                path = baseline / name; path.parent.mkdir(exist_ok=True); path.write_bytes(value)
            native = root / 'current'; native.mkdir(); (native / 'native.so').write_bytes(b'current-native')
            game = root / 'game.so'; game.write_bytes(b'current-game')
            source = root / 'source'; source.mkdir(); (source / 'raster.c').write_bytes(b'current-source')
            (source / 'graphics.cpp').write_bytes(b'current-cpp')
            output = root / 'candidate'
            reference.overlay(baseline, output, native, game, source)
            self.assertTrue((output / 'native.so').is_file(), 'Tests must load the current backend')
            self.assertEqual((output / 'native.so').read_bytes(), b'current-native')
            self.assertEqual((output / 'recomp/game.so').read_bytes(), b'current-game')
            self.assertEqual((output / 'raster.c').read_bytes(), b'current-source')
            self.assertEqual((baseline / 'native.so').read_bytes(), b'frozen-native')
            self.assertEqual((baseline / 'recomp/game.so').read_bytes(), b'frozen-game')
            self.assertEqual((baseline / 'raster.c').read_bytes(), b'frozen-source')


if __name__ == '__main__': unittest.main()
