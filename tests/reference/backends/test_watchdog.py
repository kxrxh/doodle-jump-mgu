"""Exercise current C++ backends through pinned external Python adapters."""
import _support

from pathlib import Path
import subprocess
import sys
import unittest

import arm
from test_arm import LIBRARY


class WatchdogTests(unittest.TestCase):
    def test_infinite_arm_guest_loop_is_bounded_and_next_call_recovers(self):
        self.check_budget('arm')

    def test_infinite_thumb_guest_loop_is_bounded_and_next_call_recovers(self):
        self.check_budget('thumb')

    def test_nested_guest_loop_is_bounded_and_next_call_recovers(self):
        self.check_budget('nested')

    def test_caught_nested_failure_preserves_outer_budget_and_later_calls(self):
        self.check_budget('caught')

    def test_exhausted_outer_budget_cannot_be_reset_by_catching_nested_errors(self):
        self.check_budget('outer-exhausted')

    def check_budget(self, scenario):
        program = r'''
from pathlib import Path
import struct
import sys
import time
sys.path.insert(0, sys.argv[1])
from prepare_reference import activate
activate()
import arm
arm.CALL_INSTRUCTION_LIMIT = 20_000
machine = arm.Arm(Path(sys.argv[2]))
loop = machine.malloc(4)
machine.uc.mem_write(loop, bytes.fromhex('feffffea'))
empty = machine.malloc(4)
machine.put32(empty, 0)
strlen = machine.import_address('strlen')
scenario = sys.argv[3]
if scenario == 'thumb':
    machine.uc.mem_write(loop, bytes.fromhex('fee70000'))
    target = loop | 1
elif scenario == 'nested':
    target = machine.register('nested_test', lambda: machine.call(loop))
elif scenario in ('caught', 'outer-exhausted'):
    recovered = []
    if scenario == 'outer-exhausted':
        arm.CALL_INSTRUCTION_LIMIT = 2_000
    def recover():
        arm.CALL_INSTRUCTION_LIMIT = 20_000 if scenario == 'outer-exhausted' else 2_000
        try:
            machine.call(loop)
        except RuntimeError as error:
            assert 'instruction limit' in str(error), str(error)
        else:
            raise AssertionError('The nested loop did not exhaust its budget')
        finally:
            arm.CALL_INSTRUCTION_LIMIT = 20_000
        if scenario == 'outer-exhausted':
            try:
                machine.call(strlen, empty)
            except RuntimeError as error:
                assert 'instruction limit' in str(error), str(error)
            else:
                raise AssertionError('A nested call reset the exhausted outer budget')
        else:
            assert machine.call(strlen, empty) == 0
        recovered.append(True)
        return 0
    callback = machine.register('recover_test', recover)
    target = machine.malloc(12)
    # PUSH {lr}; BLX r3; B . continues in guest code after the caught failure.
    machine.uc.mem_write(target, struct.pack('<3I', 0xE52DE004, 0xE12FFF33, 0xEAFFFFFE))
else:
    target = loop
started = time.monotonic()
try:
    machine.call(target, 0, 0, 0, callback if scenario in ('caught', 'outer-exhausted') else 0)
except RuntimeError as error:
    assert 'instruction limit' in str(error), str(error)
else:
    raise AssertionError('An infinite guest loop returned without exhausting its budget')
assert time.monotonic() - started < 1
if scenario in ('caught', 'outer-exhausted'):
    assert recovered == [True], recovered
assert machine.call(strlen, empty) == 0
'''
        result = subprocess.run(
            [sys.executable, '-c', program, str(_support.ROOT / 'tools'), str(LIBRARY), scenario],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
