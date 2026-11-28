"""Exercise autonomous native sessions through the real process and GPU socket boundaries."""
from pathlib import Path
import time
import unittest

from session_fixture import NativeSession


class SessionTests(unittest.TestCase):
    def test_native_bind_gets_a_fresh_deadline_after_store_preparation(self):
        with NativeSession(gpu=True, native=True, auto_bind=False) as session:
            send = session.gpu._send
            def delayed_bind(value):
                if value.get('event') == 'bind':
                    time.sleep(1.2)
                send(value)
            session.gpu._send = delayed_bind
            session.set_ui()
            prepared = session.wait_event('binding', lambda value: value['stage'] == 'prepare')
            session.pump_for(2.1)
            session.set_ui(renderer=dict(visible=False))
            session.binding_reply(prepared, 'prepared')
            restored = session.wait_event('binding', lambda value: value['stage'] == 'restore')
            session.set_ui(renderer=dict(visible=True))
            session.binding_reply(restored, 'restored')
            session.wait_event('presented', timeout=4)
            self.assertEqual(session.gpu.connection_count, 1)

    def test_native_frame_timeout_uses_video_until_the_renderer_changes(self):
        with NativeSession(gpu=True, native=True) as session:
            session.set_ui()
            session.wait_event('presented')
            session.gpu.hold_frame()
            session.pump_until(lambda: session.gpu.pending is not None)
            session.wait_event('video', timeout=12)
            self.assertEqual(session.gpu.connection_count, 2)
            self.assertEqual(len(session.gpu.binds), 1)
            session.pump_for(.3)
            session.set_ui(renderer=dict(generation=2))
            session.wait_event('presented')
            self.assertEqual(len(session.gpu.binds), 2)

    def test_unpresented_native_frame_keeps_the_last_cpu_picture(self):
        with NativeSession(gpu=True, native=True) as session:
            session.set_ui(active=False)
            session.wait_event('draw', lambda value: value.get('mode') == 'cpu')
            session.gpu.hold_frame()
            session.set_ui(active=True)
            session.pump_until(lambda: session.gpu.pending is not None)
            session.set_ui(renderer=dict(visible=False))
            session.pump_until(lambda: bool(session.gpu.views and not session.gpu.views[-1][4]))
            session.gpu.release_frame()
            draw = session.wait_event('draw', lambda value: not value.get('pending'))
            self.assertEqual(draw['mode'], 'cpu')
            self.assertTrue(any(command[:2] == ['image', 1] for command in draw['commands']))

    def test_parked_only_rejects_invalid_speed_and_recovers(self):
        with NativeSession() as session:
            session.send(vehicle=dict(speedVehicleCentreOfGravity=0))
            session.set_ui(parkedOnly=True)
            session.wait_event('lifecycle', lambda value: value['running'])
            for vehicle in ({'speedVehicleCentreOfGravity': 65535},
                            {'speedVehicleCentreOfGravity': -1},
                            {'speedVehicleCentreOfGravity': 65536},
                            {'speedVehicleCentreOfGravity': None},
                            {'speedVehicleCentreOfGravity': '0'}, {}):
                with self.subTest(vehicle=vehicle):
                    session.send(vehicle=vehicle)
                    paused = session.wait_event('lifecycle', lambda value: not value['running'], timeout=2)
                    self.assertIn('speed', paused['info'])
                    self.assertEqual(paused.get('open'), 'MAIN')
                    steps = session.wait_event('draw')['steps']
                    session.pump_for(.2)
                    self.assertEqual(session.wait_event('draw')['steps'], steps)
                    session.send(vehicle=dict(speedVehicleCentreOfGravity=0))
                    session.wait_event('lifecycle', lambda value: value['running'])
                    session.wait_event('draw', lambda value: value['steps'] > steps)

    def test_parked_only_expires_speed_while_ui_heartbeats_continue(self):
        with NativeSession() as session:
            session.send(vehicle=dict(speedVehicleCentreOfGravity=0))
            session.set_ui(parkedOnly=True)
            session.wait_event('lifecycle', lambda value: value['running'])
            paused = session.wait_event('lifecycle', lambda value: not value['running'], timeout=8)
            self.assertIn('speed', paused['info'])
            steps = session.wait_event('draw')['steps']
            session.pump_for(.2)
            self.assertEqual(session.wait_event('draw')['steps'], steps)
            session.send(vehicle=dict(speedVehicleCentreOfGravity=0))
            session.wait_event('lifecycle', lambda value: value['running'])
            session.wait_event('draw', lambda value: value['steps'] > steps)

    def test_frames_are_scheduled_without_javascript_frame_requests(self):
        with NativeSession() as session:
            session.set_ui()
            began = time.monotonic()
            session.pump_until(lambda: session.counts[b'FRM2'] >= 8)
            self.assertLess(time.monotonic() - began, 2, 'native scheduling stalled without browser ticks')
            session.close_input()
            self.assertEqual(session.child.wait(timeout=5), 0)

    def test_video_does_not_require_a_native_renderer_socket_path(self):
        with NativeSession(gpu=True) as session:
            session.set_ui(renderer=dict(path=''))
            session.wait_event('video')
            draw = session.wait_event('draw', lambda value: any(c[0] == 'video' for c in value['commands']))
            self.assertTrue(any(c[0] == 'video' for c in draw['commands']))
            self.assertEqual(session.gpu.binds, [])
            session.pump_until(lambda: len(session.gpu.frames) >= 8)
            self.assertEqual(session.counts[b'GPF1'], 0)

    def test_native_bind_retry_keeps_video_running(self):
        for reason in ('layer_not_ready', None):
            with self.subTest(reason=reason), NativeSession(gpu=True, native=True) as session:
                session.gpu.bind_failures.append(reason)
                session.set_ui()
                session.wait_event('video')
                session.wait_event('presented', timeout=7)
                first, retry = session.gpu.binds
                self.assertEqual(first['path'], retry['path'])
                self.assertEqual(first['rectangle'][2:], retry['rectangle'][2:])
                self.assertGreaterEqual(retry['frames'] - first['frames'], 2)
                self.assertEqual(session.gpu.connection_count, 1)
                if reason:
                    self.assertIn(reason, Path(session.stderr.name).read_text())

    def test_native_bind_retry_stops_after_second_failure(self):
        with NativeSession(gpu=True, native=True) as session:
            session.gpu.bind_failures.extend(['layer_not_ready'] * 2)
            session.set_ui()
            session.wait_event('video')
            session.pump_until(lambda: len(session.gpu.binds) == 2)
            session.wait_event('video')
            session.pump_for(2.0)
            self.assertEqual(len(session.gpu.binds), 2)

    def test_native_bind_permanent_failure_stays_on_video(self):
        with NativeSession(gpu=True, native=True) as session:
            session.gpu.bind_failures.append('wayland_unavailable')
            session.set_ui()
            session.wait_event('video')
            session.pump_for(2.0)
            self.assertEqual(len(session.gpu.binds), 1)
            self.assertGreater(len(session.gpu.frames), 2)

    def test_native_presentation_does_not_require_video_capability(self):
        with NativeSession(gpu=True, native=True) as session:
            session.set_ui(renderer=dict(video=False))
            session.wait_event('presented')
            draw = session.wait_event('draw', lambda value: value.get('mode') == 'native')
            self.assertFalse(any(command[0] == 'video' or command[:2] == ['image', 1]
                                 for command in draw['commands']))
            session.pump_until(lambda: len(session.gpu.frames) >= 8)
            self.assertEqual(session.counts[b'FRM2'], 0, 'native presentation must avoid CPU game-frame copies')
            self.assertEqual(session.counts[b'GPF1'], 0, 'native GPU commands must stay off the Store pipe')
            self.assertEqual(len(session.gpu.binds), 1)

    def test_native_cursor_and_view_survive_temporary_surface_hiding(self):
        with NativeSession(gpu=True, native=True) as session:
            session.set_ui()
            session.pump_until(lambda: bool(session.gpu.cursors and session.gpu.cursors[-1][4]))
            self.assertEqual(session.gpu.views[-1], (756, 0, 408, 720, 1))
            self.assertEqual(len(session.gpu.cursor_images), 1)
            original_x = session.gpu.cursors[-1][0]
            session.send(ccu=dict(source='controller', value=dict(frontRotaryEncoderDeflection=4,
                                                                  frontRotaryEncoderPushButton=0,
                                                                  frontRotaryEncoderOperation=0)))
            session.pump_until(lambda: session.gpu.cursors[-1][0] > original_x)
            session.send(ccu=dict(source='controller', value=dict(frontRotaryEncoderDeflection=0,
                                                                  frontRotaryEncoderPushButton=0,
                                                                  frontRotaryEncoderOperation=0)))
            for blocked, released in ((dict(sheet=True), dict(sheet=False)),
                                      (dict(focused=False), dict(focused=True)),
                                      (dict(renderer=dict(onScreen=False)), dict(renderer=dict(onScreen=True))),
                                      (dict(renderer=dict(visible=False)), dict(renderer=dict(visible=True)))):
                session.set_ui(**blocked)
                session.pump_until(lambda: not session.gpu.views[-1][4])
                before = len(session.gpu.frames)
                session.pump_for(.2)
                self.assertEqual(len(session.gpu.frames), before, 'a hidden native layer must stop capturing')
                session.set_ui(**released)
                session.pump_until(lambda: bool(session.gpu.views[-1][4]))
                session.pump_until(lambda: len(session.gpu.frames) > before)
                self.assertEqual(len(session.gpu.binds), 1, 'temporary hiding must preserve the parent binding')
                self.assertEqual(len(session.gpu.cursor_images), 1, 'temporary hiding must retain the cursor pixels')

    def test_loss_of_video_capability_removes_unsupported_video_before_the_next_frame(self):
        with NativeSession(gpu=True) as session:
            session.set_ui()
            session.wait_event('video')
            session.wait_event('draw', lambda value: any(c[0] == 'video' for c in value['commands']))
            session.gpu.hold_frame()
            session.pump_until(lambda: session.gpu.pending is not None)
            # The overlay flag identifies draws produced after the new capabilities arrived.
            session.set_ui(renderer=dict(video=False), showFps=False)
            draw = session.wait_event('draw', lambda value: not any(
                c[0] == 'text' and c[1].endswith(' FPS') for c in value['commands']))
            self.assertTrue(draw['pending'])
            self.assertFalse(any(c[0] == 'video' for c in draw['commands']),
                             'unsupported VIDEO must not be sent to a renderer that lost that capability')

    def test_worker_disconnect_keeps_the_last_video_until_cpu_replacement(self):
        with NativeSession(gpu=True) as session:
            session.set_ui()
            session.pump_until(lambda: len(session.gpu.frames) >= 260, timeout=30)
            session.wait_event('draw', lambda value: any(c[0] == 'video' for c in value['commands']))
            session.gpu.disconnect()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                kind, body = session.next()
                if kind == b'FRM2':
                    break
                if kind == b'EVT1' and body.get('type') == 'draw':
                    self.assertTrue(any(c[0] == 'video' for c in body['commands']),
                                    'fallback must keep the completed picture instead of exposing background')
            else:
                self.fail('CPU fallback never replaced the disconnected worker frame')
            session.wait_event('video', timeout=10)
            self.assertEqual(session.gpu.connection_count, 2,
                             'a real worker disconnect must reconnect and resume VIDEO')

    def test_ack_after_pause_preserves_worker_textures_and_binding(self):
        with NativeSession(gpu=True, native=True) as session:
            session.set_ui()
            session.pump_until(lambda: len(session.gpu.frames) >= 260, timeout=30)
            with session.gpu.lock:
                resident_bytes = sum(texture['bytes'] for texture in session.gpu.textures.values())
                bindings = len(session.gpu.binds)
                connections = session.gpu.connection_count
            self.assertGreater(resident_bytes, 1 << 20, 'the original game textures must be resident')
            session.gpu.hold_frame()
            session.pump_until(lambda: session.gpu.pending is not None)
            session.set_ui(shown=False, focused=False)
            session.wait_event('lifecycle', lambda value: not value['running'])
            session.gpu.release_frame()
            paused_frames = len(session.gpu.frames)
            session.pump_for(.15)
            self.assertEqual(len(session.gpu.frames), paused_frames, 'hidden game must stop admitting frames')
            with session.gpu.lock:
                uploads_before_resume = len(session.gpu.uploads)
            session.set_ui(shown=True, focused=True)
            session.wait_event('lifecycle', lambda value: value['running'])
            session.pump_until(lambda: len(session.gpu.frames) >= paused_frames + 3)
            with session.gpu.lock:
                resumed_upload_bytes = sum(texture['bytes'] for texture in session.gpu.uploads[uploads_before_resume:])
                self.assertEqual(len(session.gpu.binds), bindings, 'focus changes keep the same native surface')
                self.assertEqual(session.gpu.connection_count, connections)
            self.assertLess(resumed_upload_bytes, resident_bytes // 2,
                            'a late completion while paused must not upload the whole resident texture set again')

    def test_stale_binding_answers_cannot_release_a_resized_surface_transaction(self):
        with NativeSession(gpu=True, native=True, auto_bind=False) as session:
            session.set_ui()
            first = session.wait_event('binding', lambda value: value['stage'] == 'prepare')
            session.set_ui(renderer=dict(visible=False))
            before = session.counts[b'FRM2']
            session.pump_for(.15)
            self.assertEqual(session.gpu.frames, [], 'SDK preparation must finish before the first GPU frame')
            self.assertEqual(session.counts[b'FRM2'], before)
            session.set_ui(place=dict(w=1700), renderer=dict(source=2, socket=2, generation=2, visible=True))
            session.binding_reply(first, 'prepared')
            restored = session.wait_event('binding', lambda value: value['stage'] == 'restore')
            self.assertEqual(restored['id'], first['id'])
            self.assertEqual(session.gpu.binds, [], 'an obsolete source must never be bound')
            session.binding_reply(restored, 'restored', ok=False)
            current = session.wait_event('binding', lambda value: value['stage'] == 'prepare', timeout=10)
            self.assertNotEqual(current['id'], first['id'])
            self.assertEqual(current['rectangle'][2:], [1700, 720])
            self.assertEqual((current['source'], current['socket']), (2, 2))
            before = session.counts[b'FRM2']
            session.binding_reply(first, 'restored')
            session.pump_for(.15)
            self.assertEqual(session.gpu.frames, [], 'an old completion cannot release a new transaction')
            self.assertEqual(session.counts[b'FRM2'], before)
            session.set_ui(renderer=dict(visible=False))
            session.binding_reply(current, 'prepared')
            restored = session.wait_event('binding', lambda value: value['stage'] == 'restore')
            self.assertEqual(restored['id'], current['id'])
            self.assertEqual([bind['rectangle'][2:] for bind in session.gpu.binds], [(1700, 720)])
            session.pump_for(.15)
            self.assertEqual(session.gpu.frames, [], 'SDK restoration must finish before native presentation')
            session.set_ui(renderer=dict(visible=True))
            session.binding_reply(restored, 'restored')
            presented = session.wait_event('presented')
            self.assertEqual((presented['source'], presented['socket']), (2, 2))
            self.assertEqual(session.gpu.connection_count, 1,
                             'a renderer restart before worker binding must keep the worker connected')
            draw = session.wait_event('draw')
            self.assertEqual((draw['w'], draw['h'], draw['full']), (1700, 720, True))

    def test_visibility_focus_driving_and_stale_parent_stop_admitting_frames(self):
        with NativeSession(gpu=True) as session:
            session.set_ui()
            session.wait_event('video')
            for field in ('shown', 'focused'):
                session.set_ui(**{field: False})
                session.wait_event('lifecycle', lambda value: not value['running'])
                # A frame already sent to the worker can complete after the pause.
                paused_steps = session.wait_event('draw')['steps']
                session.pump_for(.25)
                self.assertEqual(session.wait_event('draw')['steps'], paused_steps,
                                 field + ' must stop the original game')
                before = len(session.gpu.frames)
                session.set_ui(**{field: True})
                session.wait_event('lifecycle', lambda value: value['running'])
                session.pump_until(lambda: len(session.gpu.frames) > before)
            session.send(vehicle=dict(speedVehicleCentreOfGravity=0))
            session.set_ui(parkedOnly=True)
            session.send(vehicle=dict(speedVehicleCentreOfGravity=640))
            parked = session.wait_event('lifecycle', lambda value: not value['running'])
            self.assertEqual(parked.get('open'), 'MAIN')
            session.set_ui(shown=False, focused=False)
            session.send(vehicle=dict(speedVehicleCentreOfGravity=0))
            before = len(session.gpu.frames)
            session.pump_for(.25)
            self.assertEqual(len(session.gpu.frames), before)
            session.set_ui(shown=True, focused=True)
            session.wait_event('lifecycle', lambda value: value['running'])
            session.heartbeat_stop.set()
            session.heartbeat.join(timeout=1)
            session.wait_event('lifecycle', lambda value: not value['running'], timeout=4)
            before = len(session.gpu.frames)
            session.pump_for(.2)
            self.assertEqual(len(session.gpu.frames), before, 'a lost Store parent must stop the game')

    def test_eof_stops_the_real_host_and_releases_its_gpu_connection(self):
        with NativeSession(gpu=True, native=True) as session:
            session.set_ui()
            session.wait_event('presented')
            session.close_input()
            self.assertEqual(session.child.wait(timeout=5), 0)
            deadline = time.monotonic() + 1
            while session.gpu.connection is not None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNone(session.gpu.connection)

    def test_native_sheet_and_visibility_gates_resume_with_a_fresh_physics_step(self):
        with NativeSession(gpu=True, native=True) as session:
            session.set_ui()
            session.wait_event('draw', lambda value: value.get('steps', 0) >= 8)
            for blocked, released in ((dict(sheet=True), dict(sheet=False)),
                                      (dict(renderer=dict(onScreen=False)), dict(renderer=dict(onScreen=True)))):
                session.set_ui(**blocked)
                session.pump_for(.25)
                before = len(session.gpu.frames)
                session.pump_for(1)
                self.assertEqual(len(session.gpu.frames), before, 'a hidden native surface must stop stepping')
                previous = next(event['steps'] for event in reversed(session.history) if event.get('type') == 'draw')
                session.set_ui(**released)
                resumed = session.wait_event('draw', lambda value: value.get('steps', 0) > previous)
                self.assertLess(resumed['frameDt'], .05, 'returning from a surface gate must discard hidden elapsed time')


if __name__ == '__main__':
    unittest.main()
