"""Android JNI/message ABI, adapted from the MIT Doodle Jump Vita loader.

Copyright (C) 2026 Doodle Jump Vita port contributors. See LICENSE.
"""
from pathlib import Path
import struct

from libc import signed


PREFIX = 'com/limasky/doodlejumpandroid/'
NATIVE = 'Java_com_limasky_doodlejumpandroid_NotificationCenter_'


class Jni:
    def __init__(self, machine, assets, data):
        self.m = m = machine
        self.assets = Path(assets).resolve()
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.values = {}
        self.classes = {}
        self.fields = {}
        self.methods = {}
        self.buffers = {}
        self.messages = {}
        self.deferred = []
        self.playing = False
        self.audio = None
        self.graphics = None
        self.dialogs = []
        self.pending_dialogs = {}
        self.next_dialog = 0
        self.env = m.malloc(4)
        table = m.malloc(233 * 4)
        m.put32(self.env, table)
        for index in range(4, 233):
            m.put32(table + index * 4, m.register(f'JNI.{index}', lambda index=index: self.invoke(index)))
        self.vm = m.malloc(4)
        vm_table = m.malloc(8 * 4)
        m.put32(self.vm, vm_table)
        for index in range(3, 8):
            m.put32(vm_table + index * 4, m.register(f'JavaVM.{index}', lambda index=index: self.vm_call(index)))
        self.center = self.find_class(PREFIX + 'NotificationCenter')
        self.asset_manager = self.object(self.find_class('android/content/res/AssetManager'))
        m.handlers.update({
            'AAssetManager_fromJava': lambda: self.asset_manager,
            'AAssetManager_open': self.asset_open,
            'AAsset_getBuffer': lambda: self.buffers[m.arg(0)]['data'],
            'AAsset_getLength': lambda: self.buffers[m.arg(0)]['length'],
            'AAsset_close': self.asset_close,
        })

    def handle(self, value):
        pointer = self.m.malloc(16)
        self.values[pointer] = value
        return pointer

    def find_class(self, name):
        if name not in self.classes:
            self.classes[name] = self.handle({'name': name, 'fields': {}})
        return self.classes[name]

    def object(self, cls, fields=None):
        return self.handle({'class': cls, 'fields': fields or {}})

    def new_string(self, text):
        return self.handle(text)

    def array(self, length, size):
        pointer = self.m.malloc(max(1, length * size))
        self.m.fill(pointer, 0, length * size)
        return self.handle({'data': pointer, 'length': length, 'size': size})

    def text(self, handle):
        value = self.values.get(handle, '')
        return value if isinstance(value, str) else ''

    def string_buffer(self, handle, utf8):
        key = (handle, utf8)
        if key not in self.buffers:
            value = self.text(handle).encode('utf-8' if utf8 else 'utf-16-le')
            pointer = self.m.malloc(len(value) + 2)
            self.m.uc.mem_write(pointer, value + b'\0\0')
            self.buffers[key] = pointer
        return self.buffers[key]

    def field(self, obj, name, default=0):
        return self.values[obj]['fields'].get(name, default)

    def field_text(self, obj, name):
        return self.text(self.field(obj, name))

    def field_float(self, obj, name):
        return struct.unpack('<f', struct.pack('<I', self.field(obj, name)))[0]

    def set_field(self, obj, name, value):
        self.values[obj]['fields'][name] = value

    def vm_call(self, index):
        if index in (4, 6, 7):
            self.m.put32(self.m.arg(1), self.env)
        return 0

    def invoke(self, index):
        m = self.m
        a = m.arg
        if index == 4:
            return 0x10006
        if index == 6:
            return self.find_class(m.string(a(1)).decode())
        if index in (15, 17, 19, 26, 217, 218, 228):
            return 0
        if index in (21, 25, 226):
            return a(1)
        if index in (22, 23, 227):
            return 0
        if index == 24:
            return int(a(1) == a(2))
        if index in (27, 28, 29, 30):
            return self.object(a(1))
        if index == 31:
            return self.values[a(1)].get('class', self.find_class('java/lang/Object'))
        if index == 32:
            return int(not a(1) or self.values.get(a(1), {}).get('class') == a(2))
        if index in (33, 113):
            key = (a(1), m.string(a(2)).decode(), m.string(a(3)).decode())
            if key not in self.methods:
                self.methods[key] = self.handle({'method': key})
            return self.methods[key]
        if index in (94, 144):
            key = (a(1), m.string(a(2)).decode(), m.string(a(3)).decode())
            if key not in self.fields:
                self.fields[key] = self.handle({'field': key})
            return self.fields[key]
        if 95 <= index <= 103 or 145 <= index <= 153:
            name = self.values[a(2)]['field'][1]
            value = self.field(a(1), name)
            if index in (101, 103, 151, 153):
                return value & 0xFFFFFFFF, value >> 32
            return value
        if 104 <= index <= 112 or 154 <= index <= 162:
            name = self.values[a(2)]['field'][1]
            kind = (index - (104 if index < 113 else 154))
            value = (a(4) | a(5) << 32) if kind in (6, 8) else a(3)
            self.set_field(a(1), name, value)
            return 0
        if 114 <= index <= 143:
            method = self.values[a(2)]['method']
            return self.static_call(method, self.method_args(method[2], (index - 114) % 3))
        if index == 163:
            return self.new_string(bytes(m.uc.mem_read(a(1), a(2) * 2)).decode('utf-16-le'))
        if index in (164, 168):
            return len(self.text(a(1)).encode('utf-8' if index == 168 else 'utf-16-le')) // (1 if index == 168 else 2)
        if index in (165, 169, 224):
            return self.string_buffer(a(1), index == 169)
        if index == 167:
            return self.new_string(m.string(a(1)).decode('utf-8', 'replace'))
        if index in (166, 170, 225):
            return 0
        if index == 171:
            return self.values[a(1)]['length']
        if index == 172:
            array = self.array(a(1), 4)
            for i in range(a(1)):
                m.put32(self.values[array]['data'] + i * 4, a(3))
            return array
        if index == 173:
            return m.u32(self.values[a(1)]['data'] + a(2) * 4)
        if index == 174:
            m.put32(self.values[a(1)]['data'] + a(2) * 4, a(3))
            return 0
        if 175 <= index <= 182:
            return self.array(a(1), (1, 1, 2, 2, 4, 8, 4, 8)[index - 175])
        if 183 <= index <= 190 or index == 222:
            if a(2):
                m.uc.mem_write(a(2), b'\0')
            return self.values[a(1)]['data']
        if 191 <= index <= 198 or index == 223:
            return 0
        if 199 <= index <= 214:
            array = self.values[a(1)]
            source, target = array['data'] + a(2) * array['size'], a(4)
            if index >= 207:
                source, target = target, source
            if a(2) + a(3) > array['length']:
                raise IndexError('JNI array region exceeds its bounds')
            m.copy(target, source, a(3) * array['size'])
            return 0
        if index == 219:
            m.put32(a(1), self.vm)
            return 0
        if index in (220, 221):
            text = self.text(a(1))[a(2):a(2) + a(3)]
            m.uc.mem_write(a(4), text.encode('utf-8' if index == 221 else 'utf-16-le'))
            return 0
        if index == 229:
            return self.handle({'data': a(1), 'length': a(2) | a(3) << 32, 'size': 1})
        if index in (230, 231):
            value = self.values[a(1)]['data' if index == 230 else 'length']
            return (value & 0xFFFFFFFF, value >> 32) if index == 231 else value
        raise NotImplementedError(f'JNI function slot {index}')

    def method_args(self, signature, variant):
        m = self.m
        types = []
        sig = signature[1:signature.index(')')]
        while sig:
            t = sig[0]
            if t == '[':
                while sig.startswith('['):
                    sig = sig[1:]
                t = 'L'
            if sig[0] == 'L':
                sig = sig[sig.index(';') + 1:]
            else:
                sig = sig[1:]
            types.append(t)
        values = []
        slot = 3
        pointer = m.arg(3)
        for i, kind in enumerate(types):
            wide = kind in 'JD' or kind == 'F' and variant != 2
            if variant == 2:
                value = int.from_bytes(m.uc.mem_read(pointer + i * 8, 8 if wide else 4), 'little')
            elif variant == 1:
                if wide:
                    pointer = (pointer + 7) & ~7
                value = int.from_bytes(m.uc.mem_read(pointer, 8 if wide else 4), 'little')
                pointer += 8 if wide else 4
            else:
                if wide:
                    slot += slot & 1
                value = m.arg(slot) | (m.arg(slot + 1) << 32 if wide else 0)
                slot += 2 if wide else 1
            if kind == 'F':
                value = struct.unpack('<d' if variant != 2 else '<f', value.to_bytes(8 if variant != 2 else 4, 'little'))[0]
            values.append(value)
        return values

    def static_call(self, method, arguments):
        name = method[1]
        if name == 'receiveMessageFromNative':
            return self.receive(signed(arguments[0]), *arguments[1:])
        if name == 'receiveAudioMessageFromNative':
            if self.audio:
                self.audio(*arguments)
            return 0
        if name == 'getAssetManager':
            return self.asset_manager
        raise NotImplementedError(f'Java method {method}')

    @staticmethod
    def path(root, name):
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f'Game path escapes its data directory: {name}')
        return path

    def asset_open(self):
        m = self.m
        name = m.string(m.arg(1)).decode('utf-8')
        if self.graphics:
            self.graphics.asset_name = name
        path = self.path(self.assets, name)
        if not path.is_file():
            return 0
        value = path.read_bytes()
        pointer = m.malloc(len(value))
        m.uc.mem_write(pointer, value)
        handle = m.malloc(16)
        self.buffers[handle] = {'data': pointer, 'length': len(value)}
        return handle

    def asset_close(self):
        buffer = self.buffers.pop(self.m.arg(0))
        self.m.free(buffer['data'])
        self.m.free(self.m.arg(0))
        return 0

    def receive(self, message, obj, target, sender):
        self.messages[message] = self.messages.get(message, 0) + 1
        if self.messages[message] == 1:
            print('[java] message', message, flush=True)
        if message in (17, 18, 27):
            name = self.field_text(obj, 'filename')
            if message == 27:
                self.set_field(obj, 'file_exists', int(self.path(self.assets, name).is_file()))
            elif message == 17:
                path = self.path(self.data, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(path.suffix + '.tmp')
                temporary.write_text(self.field_text(obj, 'text'), encoding='utf-8')
                temporary.replace(path)
                self.set_field(obj, 'status', 1)
            else:
                path = self.path(self.data, name)
                if not path.is_file():
                    path = self.path(self.assets, name)
                self.set_field(obj, 'status', int(path.is_file()))
                if path.is_file():
                    text = path.read_text(encoding='utf-8', errors='replace').replace('\r', '').replace('\n', '')
                    self.set_field(obj, 'text', self.new_string(text))
                    self.set_field(obj, 'text_size', len(text.encode('utf-16-le')) // 2)
                else:
                    self.set_field(obj, 'text', 0)
                    self.set_field(obj, 'text_size', 0)
        elif message in (30, 31):
            self.set_field(obj, 'status', 0)
            self.set_field(obj, 'adTypesAvailable', 0)
            self.set_field(obj, 'adTypeSelected', 0)
        elif message in (54, 55):
            self.playing = message == 54
        elif message in (-11, 245):
            self.deferred.append((-12, 0, 0, 0))
        elif message in (21, 63):
            if self.graphics is None:
                raise RuntimeError('Text texture requested before graphics initialization')
            self.graphics.text_texture(message, obj, self)
        elif message == 6:
            if self.audio:
                self.audio(message, self.field(obj, 'identifier'), self.field_text(obj, 'filename'), self.field(obj, 'isLooping'))
        elif message in (7, 8, 9, 10, 11, 12, 13, 14, 15):
            if self.audio:
                self.audio(message, self.field(obj, 'identifier'), self.field_float(obj, 'volume'), self.field(obj, 'loop'))
        elif message in (-3, 19, 20, 22, 24, 43):
            pass
        elif message == 36:
            raise SystemExit('The original game requested exit')
        elif message == 25:
            self.next_dialog += 1
            buttons = [dict(key=key, label=self.field_text(obj, key + '_button_text'))
                       for key in ('positive', 'neutral', 'negative') if self.field(obj, key + '_button_text')]
            if not buttons:
                buttons = [dict(key='positive', label='OK')]
            self.pending_dialogs[self.next_dialog] = (self.field(obj, 'alert_dialog_id'), self.field(obj, 'user_data'), buttons)
            self.dialogs.append(dict(type='dialog', id=self.next_dialog, title=self.field_text(obj, 'title'),
                                     message=self.field_text(obj, 'message'), buttons=buttons))
        else:
            print('[java] unsupported platform message', message, flush=True)
        return 0

    def reply_dialog(self, identifier, choice):
        dialog = self.pending_dialogs.get(identifier)
        if dialog is None or choice not in [button['key'] for button in dialog[2]]:
            raise ValueError('Unknown original game dialog or response')
        self.pending_dialogs.pop(identifier)
        fields = {key + '_button_selected': int(key == choice) for key in ('positive', 'neutral', 'negative')}
        fields.update(alert_dialog_id=dialog[0], user_data=dialog[1])
        result = self.object(self.find_class(PREFIX + 'UtilityMessageHandler$AlertDialogResultData'), fields)
        self.deferred.append((26, result, 0, 0))

    def send(self, message, obj=0, target=0, sender=0):
        return self.m.call(NATIVE + 'sendMessageNative', self.env, self.center, message, obj, target, sender)

    def configuration(self, width=544, height=960):
        fields = dict(screenWidth=width, screenHeight=height, ifga=0, areAllThemesAvailable=1,
                      enableSlowAppAlert=0, numCpuCores=3, totalMemSize=524288, version=3910006)
        fields.update({k: self.new_string(v) for k, v in dict(locale='en_US', country='us', languageCode='en').items()})
        return self.object(self.find_class(PREFIX + 'Messages$MsgSystemConfigurationData'), fields)
