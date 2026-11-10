"""Original Ogg effects and SoundPool messages, played through the host audio device."""
from array import array
import ctypes
import threading

import miniaudio

from jni import Jni
from native_backend import library


class Audio:
    def __init__(self, assets, playback=True):
        self.assets=assets
        self.lock=threading.RLock()
        self.device=None
        self.library=library()
        self.library.audio_create.argtypes=[]
        self.library.audio_create.restype=ctypes.c_void_p
        self.library.audio_destroy.argtypes=[ctypes.c_void_p]
        self.library.audio_destroy.restype=None
        self.library.audio_error.argtypes=[ctypes.c_void_p]
        self.library.audio_error.restype=ctypes.c_char_p
        self.library.audio_load.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]
        self.library.audio_load.restype=ctypes.c_int
        self.library.audio_message.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_uint32,ctypes.c_double,ctypes.c_int]
        self.library.audio_message.restype=ctypes.c_int
        self.library.audio_mix.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t]
        self.library.audio_mix.restype=ctypes.c_int
        self.handle=self.library.audio_create()
        if not self.handle:
            raise RuntimeError(self.library.audio_error(None).decode())
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

    def check(self, result):
        if result:
            raise ValueError(self.library.audio_error(self.handle).decode())

    def load(self, identifier, samples, loop=False):
        samples=array('h',samples)
        pointer=(ctypes.c_int16*len(samples)).from_buffer(samples)
        with self.lock:
            self.check(self.library.audio_load(self.handle,identifier,pointer,len(samples),bool(loop)))

    def __call__(self, message, identifier=0, volume=1.0, loop=0):
        with self.lock:
            if message==6:
                path=Jni.path(self.assets,volume)
                self.load(identifier,miniaudio.decode_file(str(path)).samples,loop)
            else:
                self.check(self.library.audio_message(self.handle,message,identifier,volume,loop))

    def mix(self, frames):
        if type(frames) is not int or not 0<=frames<=1<<20:
            raise ValueError('Invalid audio buffer')
        output=array('h',b'\0'*(frames*4))
        pointer=(ctypes.c_int16*len(output)).from_buffer(output)
        with self.lock:
            self.check(self.library.audio_mix(self.handle,pointer,frames))
        return output

    def stream(self):
        frames=yield b''
        while True:
            frames=yield self.mix(frames)

    def close(self):
        if self.device:
            self.device.close()
            self.device=None
        with self.lock:
            if self.handle:
                self.library.audio_destroy(self.handle)
                self.handle=None

    def __del__(self):
        if getattr(self,'handle',None):
            self.close()
