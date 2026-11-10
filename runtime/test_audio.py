from array import array
from pathlib import Path
import tempfile
import unittest
import wave
from audio import Audio

class AudioTests(unittest.TestCase):
    def test_loaded_pcm_is_owned_until_the_sound_is_unloaded(self):
        sound=Audio(Path('.'), playback=False)
        self.addCleanup(sound.close)
        self.assertTrue(callable(getattr(sound,'load',None)), 'Audio must load PCM into its native mixer')
        samples=array('h',[1000,-1000])
        sound.load(123,samples)
        samples[0]=0
        sound(8,123)
        self.assertEqual(sound.mix(2).tolist(),[1000,-1000,0,0])

    def test_original_sound_messages_pause_resume_stop_and_loop(self):
        sound=Audio(Path('.'), playback=False)
        self.addCleanup(sound.close)
        sound.load(123,array('h',[1000,-1000,2000,-2000]))
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

    def test_mixing_rounds_each_voice_then_clips_the_sum(self):
        sound=Audio(Path('.'), playback=False)
        self.addCleanup(sound.close)
        sound.load(1,array('h',[1,3,-1,-3]))
        sound(8,1,.5)
        self.assertEqual(sound.mix(2).tolist(),[0,2,0,-2])
        sound.load(1,array('h',[30000,-30000]))
        sound.load(2,array('h',[30000,-30000]))
        sound(8,1)
        sound(8,2)
        self.assertEqual(sound.mix(1).tolist(),[32767,-32768])

    def test_finite_loops_global_pause_and_volume_changes_preserve_position(self):
        sound=Audio(Path('.'), playback=False)
        self.addCleanup(sound.close)
        sound.load(1,array('h',[1000,-1000,2000,-2000]))
        sound(8,1,1,1)
        self.assertEqual(sound.mix(1).tolist(),[1000,-1000])
        sound(12)
        self.assertEqual(sound.mix(2).tolist(),[0,0,0,0])
        sound(15,1,.25)
        sound(13)
        self.assertEqual(sound.mix(4).tolist(),[500,-500,250,-250,500,-500,0,0])

    def test_asset_loop_unload_and_reload_remove_stale_voices(self):
        sound=Audio(Path('.'), playback=False)
        self.addCleanup(sound.close)
        sound.load(1,array('h',[500,-500]),True)
        sound(8,1)
        self.assertEqual(sound.mix(3).tolist(),[500,-500,500,-500,500,-500])
        sound.load(1,array('h',[1000,-1000]))
        self.assertEqual(sound.mix(1).tolist(),[0,0])
        sound(8,1)
        sound(7,1)
        sound(8,1)
        self.assertEqual(sound.mix(1).tolist(),[0,0])
        sound.load(1,array('h'))
        sound(8,1)
        self.assertEqual(sound.mix(1).tolist(),[0,0])

    def test_decoded_sound_messages_load_and_stop_real_pcm(self):
        with tempfile.TemporaryDirectory() as directory:
            with wave.open(str(Path(directory)/'test.wav'),'wb') as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(44100)
                output.writeframes(array('h',[1000,-1000,2000,-2000]).tobytes())
            sound=Audio(Path(directory).resolve(), playback=False)
            self.addCleanup(sound.close)
            sound(6,123,'test.wav')
            sound(8,123)
            self.assertEqual(sound.mix(1).tolist(),[1000,-1000])
            sound(9,123)
            self.assertEqual(sound.mix(1).tolist(),[0,0])

    def test_invalid_pcm_buffers_and_closed_handles_are_rejected(self):
        sound=Audio(Path('.'), playback=False)
        self.addCleanup(sound.close)
        with self.assertRaises(ValueError):
            sound.load(1,array('h',[1000]))
        for frames in (-1,1.5,True,2**32):
            with self.subTest(frames=frames), self.assertRaises(ValueError):
                sound.mix(frames)
        sound.load(1,array('h',[1000,-1000]))
        with self.assertRaises(ValueError):
            sound(8,1,float('nan'))
        self.assertEqual(sound.mix(0).tolist(),[])
        sound.close()
        sound.close()
        with self.assertRaises(ValueError):
            sound.mix(1)
