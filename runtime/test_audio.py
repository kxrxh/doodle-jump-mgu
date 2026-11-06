from array import array
from pathlib import Path
import unittest
from audio import Audio

class AudioTests(unittest.TestCase):
    def test_original_sound_messages_pause_resume_stop_and_loop(self):
        sound=Audio(Path('.'), playback=False)
        sound.sounds[123]=(array('h',[1000,-1000,2000,-2000]),False)
        sound(8,123,.5,0)
        self.assertEqual(sound.mix(1).tolist(),[500,-500])
        sound(10,123,0,0)
        self.assertEqual(sound.mix(1).tolist(),[0,0])
        sound(11,123,0,0)
        self.assertEqual(sound.mix(2).tolist(),[1000,-1000,0,0])
        sound(8,123,1,-1)
        self.assertEqual(sound.mix(3).tolist(),[1000,-1000,2000,-2000,1000,-1000])
        sound(14,0,0,0)
        self.assertEqual(sound.mix(1).tolist(),[0,0])
