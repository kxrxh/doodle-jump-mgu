"""The production engine must run without importing its development oracle."""
import subprocess
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class NativeOnlyTests(unittest.TestCase):
    def test_aot_starts_and_handles_callbacks_with_unicorn_imports_blocked(self):
        script = '''
import builtins
import sys
import tempfile
from pathlib import Path

original_import = builtins.__import__
def no_unicorn(name, *args, **kwargs):
    if name == 'unicorn' or name.startswith('unicorn.'):
        raise AssertionError('The production engine imported Unicorn')
    return original_import(name, *args, **kwargs)
builtins.__import__ = no_unicorn

from game import Game
from audio import Audio
with tempfile.TemporaryDirectory() as temporary:
    game = Game(Path(sys.argv[1]), Path(temporary), render_size=(408, 720), backend='aot')
    audio = Audio(game.jni.assets, playback=False)
    game.jni.audio = audio
    try:
        for frame in range(8):
            game.graphics.begin_gpu_frame(True, reset=frame == 0)
            game.step(readback=False)
            assert game.graphics.gpu_frame(), 'The game did not produce a frame'
            assert len(audio.mix(800)) == 1600, 'The stereo audio path did not run'
        def failed_asset():
            raise ValueError('failed asset without emulator')
        try:
            game.machine.call(game.machine.register('failed_asset', failed_asset))
        except ValueError as error:
            assert str(error) == 'failed asset without emulator'
        else:
            raise AssertionError('The host callback error was lost')
        assert game.machine.call(game.machine.register('recovered', lambda: 123)) == 123
        assert not any(name == 'unicorn' or name.startswith('unicorn.') for name in sys.modules)
    finally:
        audio.close()
'''
        result = subprocess.run([sys.executable, '-c', script, str(ROOT / 'original')],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shared_guest_abi_matches_the_development_oracle(self):
        import arm
        from unicorn import UC_HOOK_CODE, UC_HOOK_BLOCK, UC_HOOK_MEM_WRITE_PROT
        from unicorn import arm_const
        for name, value in {'UC_HOOK_CODE': UC_HOOK_CODE, 'UC_HOOK_BLOCK': UC_HOOK_BLOCK,
                            'UC_HOOK_MEM_WRITE_PROT': UC_HOOK_MEM_WRITE_PROT}.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(arm, name), value)
        for name in ('R0', 'R1', 'R2', 'R3', 'SP', 'LR', 'PC', 'C1_C0_2', 'FPEXC', 'CPSR'):
            with self.subTest(register=name):
                self.assertEqual(getattr(arm, 'UC_ARM_REG_' + name),
                                 getattr(arm_const, 'UC_ARM_REG_' + name))

    def test_unicorn_fault_keeps_guest_addresses_and_next_call_works(self):
        from arm import Arm
        machine = Arm(ROOT / 'original/extracted/lib/armeabi-v7a/libDoodleJump.so')
        address = machine.malloc(4)
        machine.uc.mem_write(address, bytes.fromhex('f000f0e7'))  # ARM UDF #0
        with self.assertRaisesRegex(RuntimeError, f'ARM fault PC={address:#x} LR=0x10ff000:'):
            machine.call(address)
        self.assertEqual(machine.call(machine.register('next_call', lambda: 123)), 123)


if __name__ == '__main__':
    unittest.main()
