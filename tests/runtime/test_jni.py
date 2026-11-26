import importlib
from pathlib import Path
import tempfile
import unittest

from arm import Arm
from libc import LibC
from test_arm import LIBRARY


class JniTests(unittest.TestCase):
    def test_original_engine_initializes_android_message_bridge(self):
        try:
            module = importlib.import_module('jni')
        except ModuleNotFoundError:
            self.fail('The original engine Java message bridge is missing')
        machine = Arm(LIBRARY)
        LibC(machine)
        machine.initialize()
        assets = LIBRARY.parents[2] / 'assets'
        with tempfile.TemporaryDirectory() as data:
            jni = module.Jni(machine, assets, Path(data))
            jni.send(-1)
            self.assertIn('com/limasky/doodlejumpandroid/SyncVariant$HighScoreEntry', jni.classes)
            dialog = jni.object(jni.center, dict(alert_dialog_id=7, user_data=123,
                title=jni.new_string('Reset scores'), message=jni.new_string('Reset local scores?'),
                positive_button_text=jni.new_string('Reset'), negative_button_text=jni.new_string('Cancel')))
            jni.receive(25, dialog, 0, 0)
            request = jni.dialogs.pop()
            self.assertEqual(request['buttons'][-1]['key'], 'negative')
            with self.assertRaises(ValueError):
                jni.reply_dialog(request['id'], 'invalid')
            jni.reply_dialog(request['id'], 'negative')
            message, result, _, _ = jni.deferred[-1]
            self.assertEqual(message, 26)
            self.assertEqual(jni.field(result, 'negative_button_selected'), 1)


if __name__ == '__main__':
    unittest.main()
