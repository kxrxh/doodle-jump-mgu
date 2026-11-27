"""Completed GPU frames must reach Store before another guest frame can block."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from session_fixture import NativeSession


class DeliveryTests(unittest.TestCase):
    def test_completed_presentation_is_delivered_before_the_next_guest_frame(self):
        with tempfile.TemporaryDirectory(prefix='doodle-delivery-') as temporary:
            directory = Path(temporary)
            probe = directory / 'delivery.so'
            arm = directory / 'arm'
            subprocess.run(['cc', '-shared', '-fPIC', '-O2',
                            str(Path(__file__).with_name('delivery_probe.c')), '-ldl', '-o', str(probe)], check=True)
            with patch.dict(os.environ, LD_PRELOAD=str(probe), DOODLE_DELIVERY_ARM=str(arm)), \
                    NativeSession(gpu=True, native=True) as session:
                session.set_ui()
                session.wait_event('presented', lambda event: event['frames'] >= 32)
                session.gpu.hold_frame()
                session.pump_until(lambda: session.gpu.pending is not None)
                expected = session.gpu.pending['sequence']
                # Hold this completion past the next deadline, then block exactly its successor.
                session.pump_for(.05)
                arm.touch()
                try:
                    session.gpu.release_frame()
                    deadline = time.monotonic() + 5
                    stopped = False
                    while time.monotonic() < deadline:
                        pid, status = os.waitpid(session.child.pid, os.WNOHANG | os.WUNTRACED)
                        if pid:
                            self.assertTrue(os.WIFSTOPPED(status), 'the guest probe must stop the next frame')
                            self.assertEqual(os.WSTOPSIG(status), signal.SIGSTOP)
                            stopped = True
                            break
                        session.pump_for(.01)
                    self.assertTrue(stopped, 'the next guest frame was never admitted')
                    delivered = any(event.get('type') == 'presented' and event.get('frames') == expected
                                    for event in session.history)
                    if not delivered:
                        session.wait_event('presented', lambda event: event['frames'] == expected, timeout=1)
                finally:
                    if session.child.poll() is None:
                        os.kill(session.child.pid, signal.SIGCONT)


if __name__ == '__main__':
    unittest.main()
