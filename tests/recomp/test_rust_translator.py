"""Compare the Rust translator's complete output with the Python reference."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = ROOT / 'original/extracted/lib/armeabi-v7a/libDoodleJump.so'
PROFILE = ROOT / 'runtime/recomp/translator/entries.json'
CLI = ROOT / 'tools/recompile.py'


@unittest.skipUnless(ORIGINAL.is_file(), 'requires the verified original engine')
class RustTranslatorTests(unittest.TestCase):
    def test_translator_matches_python_generated_c_and_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            rust = path / 'rust'
            python = path / 'python'
            result = subprocess.run(
                [sys.executable, '-I', '-S', str(CLI), '--report', '--original', str(ORIGINAL),
                 '--output', str(rust)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([
                sys.executable, '-c',
                'from pathlib import Path; from recomp_reference.emit import generate; import sys; '
                'generate(*map(Path, sys.argv[1:]))',
                str(ORIGINAL), str(python), str(PROFILE)],
                cwd=ROOT / 'tools', stdout=subprocess.DEVNULL, check=True)
            self.assertEqual(sorted(p.name for p in rust.iterdir()),
                             sorted(p.name for p in python.iterdir()))
            for expected in sorted(python.iterdir()):
                actual = rust / expected.name
                with self.subTest(artifact=expected.name):
                    if expected.suffix == '.json':
                        self.assertEqual(json.loads(actual.read_text()), json.loads(expected.read_text()))
                    else:
                        self.assertTrue(actual.read_bytes() == expected.read_bytes(), expected.name)

    def test_translator_rejects_a_profile_for_another_engine_before_writing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            profile = path / 'profile.json'
            profile.write_text(json.dumps({'engine_sha256': '0' * 64, 'entries': {}}))
            output = path / 'output'
            result = subprocess.run(
                [sys.executable, '-I', '-S', str(CLI), '--report', '--original', str(ORIGINAL),
                 '--output', str(output), '--profile', str(profile)],
                capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Trace does not match the original engine', result.stderr)
            self.assertFalse(output.exists())
