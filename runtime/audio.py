"""Original Ogg effects and SoundPool messages, played through the host audio device."""
from array import array
import threading

import miniaudio

from jni import Jni


class Audio:
    def __init__(self, assets, playback=True):
        self.assets=assets
        self.sounds={}
        self.voices={}
        self.paused=False
        self.lock=threading.RLock()
        self.device=None
        if playback:
            try:
                self.device=miniaudio.PlaybackDevice(buffersize_msec=60)
                stream=self.stream()
                next(stream)
                self.device.start(stream)
            except miniaudio.MiniaudioError as error:
                print(f'[audio] No playback device: {error}',flush=True)
                if self.device:
                    self.device.close()
                self.device=None

    def __call__(self, message, identifier=0, volume=1.0, loop=0):
        with self.lock:
            if message==6:
                path=Jni.path(self.assets,volume)
                self.sounds[identifier]=(miniaudio.decode_file(str(path)).samples,bool(loop))
            elif message==7:
                self.sounds.pop(identifier,None)
                self.voices.pop(identifier,None)
            elif message==8 and identifier in self.sounds:
                self.voices[identifier]=dict(at=0,volume=max(0,min(1,volume)),loops=loop,
                                             paused=False)
            elif message==9:
                self.voices.pop(identifier,None)
            elif message in (10,11) and identifier in self.voices:
                self.voices[identifier]['paused']=message==10
            elif message in (12,13):
                self.paused=message==12
            elif message==14:
                self.voices.clear()
            elif message==15 and identifier in self.voices:
                self.voices[identifier]['volume']=max(0,min(1,volume))

    def mix(self, frames):
        output=[0]*(frames*2)
        with self.lock:
            if not self.paused:
                for identifier,voice in list(self.voices.items()):
                    if voice['paused']:
                        continue
                    samples,looping=self.sounds[identifier]
                    if not samples:
                        self.voices.pop(identifier,None)
                        continue
                    offset=0
                    while offset<len(output):
                        count=min(len(output)-offset,len(samples)-voice['at'])
                        for i in range(count):
                            output[offset+i]+=round(samples[voice['at']+i]*voice['volume'])
                        offset+=count
                        voice['at']+=count
                        if voice['at']==len(samples):
                            if looping or voice['loops']:
                                voice['at']=0
                                if voice['loops']>0:
                                    voice['loops']-=1
                            else:
                                self.voices.pop(identifier,None)
                                break
        return array('h',(max(-32768,min(32767,value)) for value in output))

    def stream(self):
        frames=yield b''
        while True:
            frames=yield self.mix(frames)

    def close(self):
        if self.device:
            self.device.close()
