import importlib
import unittest


class InputTests(unittest.TestCase):
    def state(self):
        try:
            return importlib.import_module('input_state').Input()
        except ModuleNotFoundError:
            self.fail('The local original-game preview input bridge is missing')

    def test_touch_coordinates_and_release_are_forwarded_to_original_engine(self):
        state = self.state()
        state.apply({'touch': [0, 0, 272, 480]})
        state.apply({'tilt': -1})
        tilt, touches, actions = state.take()
        self.assertEqual(tilt, -1)
        self.assertEqual(touches, [(0, 0, 272.0, 480.0)])
        state.apply({'touch': [0, 2, 272, 480], 'tilt': 0})
        self.assertEqual(state.take()[:2], (0, [(0, 2, 272.0, 480.0)]))
        self.assertEqual(state.take()[1], [])

    def test_invalid_input_is_rejected_before_it_reaches_native_memory(self):
        state = self.state()
        for message in ({'tilt': float('nan')}, {'touch': [0, 0, -1, 0]},
                        {'touch': [0, 7, 0, 0]}, {'action': 'arbitrary'}):
            with self.assertRaises(ValueError):
                state.apply(message)

    def test_mgu_frame_delivers_all_touch_events(self):
        state = self.state()
        state.apply({'frame': True, 'touches': [[0, 0, 180, 295], [0, 2, 180, 295]]})
        self.assertEqual(state.take()[1], [(0, 0, 180, 295), (0, 2, 180, 295)])
        with self.assertRaises(ValueError):
            state.apply({'touches': [[0, 0, 10, 10], [0, 2, float('nan'), 10]]})
        self.assertEqual(state.take()[1], [])


if __name__ == '__main__':
    unittest.main()
