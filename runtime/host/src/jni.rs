//! Android ABI compatibility, adapted from the MIT Doodle Jump Vita loader.
//! Copyright (C) 2026 Doodle Jump Vita port contributors. See ../LICENSE.
use crate::{
    Result,
    host::Host,
    machine::{Machine, Return},
};
use serde_json::{Value, json};
use std::{
    cell::{Cell, RefCell},
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};
const PREFIX: &str = "com/limasky/doodlejumpandroid/";
#[derive(Clone)]
enum Object {
    Text(String),
    Class(BTreeMap<String, u64>),
    Instance(u32, BTreeMap<String, u64>),
    Array(u32, u64, u32),
    Method(String, String),
    Field(String),
}
#[derive(Clone)]
enum Argument {
    Word(u64),
    Float(f64),
}
impl Argument {
    fn word(&self) -> u64 {
        match self {
            Self::Word(v) => *v,
            Self::Float(v) => *v as u64,
        }
    }
    fn number(&self) -> f64 {
        match self {
            Self::Word(v) => *v as f64,
            Self::Float(v) => *v,
        }
    }
}
type PendingDialog = (u64, u64, Vec<String>);
pub struct Jni {
    pub env: u32,
    pub vm: u32,
    pub center: Cell<u32>,
    asset_manager: Cell<u32>,
    values: RefCell<BTreeMap<u32, Object>>,
    classes: RefCell<BTreeMap<String, u32>>,
    methods: RefCell<BTreeMap<(u32, String, String), u32>>,
    fields: RefCell<BTreeMap<(u32, String, String), u32>>,
    strings: RefCell<BTreeMap<(u32, bool), u32>>,
    assets: RefCell<BTreeMap<u32, (u32, u32)>>,
    pub playing: Cell<bool>,
    pub deferred: RefCell<Vec<(i32, u32, u32, u32)>>,
    pub dialogs: RefCell<Vec<Value>>,
    pending: RefCell<BTreeMap<u32, PendingDialog>>,
    next_dialog: Cell<u32>,
    seen: RefCell<BTreeSet<i32>>,
}
impl Jni {
    pub fn new(m: &Machine) -> Result<Self> {
        let env = m.malloc(4)?;
        let table = m.malloc(233 * 4)?;
        m.put32(env, table)?;
        for i in 4..233 {
            m.put32(table + i * 4, m.import_address(&format!("JNI.{i}"))?)?;
        }
        let vm = m.malloc(4)?;
        let table = m.malloc(32)?;
        m.put32(vm, table)?;
        for i in 3..8 {
            m.put32(table + i * 4, m.import_address(&format!("JavaVM.{i}"))?)?;
        }
        let j = Self {
            env,
            vm,
            center: Cell::new(0),
            asset_manager: Cell::new(0),
            values: RefCell::new(BTreeMap::new()),
            classes: RefCell::new(BTreeMap::new()),
            methods: RefCell::new(BTreeMap::new()),
            fields: RefCell::new(BTreeMap::new()),
            strings: RefCell::new(BTreeMap::new()),
            assets: RefCell::new(BTreeMap::new()),
            playing: Cell::new(false),
            deferred: RefCell::new(vec![]),
            dialogs: RefCell::new(vec![]),
            pending: RefCell::new(BTreeMap::new()),
            next_dialog: Cell::new(0),
            seen: RefCell::new(BTreeSet::new()),
        };
        j.center
            .set(j.class(m, &format!("{PREFIX}NotificationCenter"))?);
        let cls = j.class(m, "android/content/res/AssetManager")?;
        j.asset_manager.set(j.object(m, cls, BTreeMap::new())?);
        Ok(j)
    }
    fn handle(&self, m: &Machine, value: Object) -> Result<u32> {
        let p = m.malloc(16)?;
        self.values.borrow_mut().insert(p, value);
        Ok(p)
    }
    fn value(&self, p: u32) -> Result<Object> {
        self.values
            .borrow()
            .get(&p)
            .cloned()
            .ok_or_else(|| format!("Unknown JNI object {p:#x}"))
    }
    pub fn class(&self, m: &Machine, name: &str) -> Result<u32> {
        if let Some(v) = self.classes.borrow().get(name) {
            return Ok(*v);
        }
        let p = self.handle(m, Object::Class(BTreeMap::new()))?;
        self.classes.borrow_mut().insert(name.into(), p);
        Ok(p)
    }
    pub fn object(&self, m: &Machine, class: u32, fields: BTreeMap<String, u64>) -> Result<u32> {
        self.handle(m, Object::Instance(class, fields))
    }
    pub fn text(&self, p: u32) -> String {
        match self.values.borrow().get(&p) {
            Some(Object::Text(s)) => s.clone(),
            _ => String::new(),
        }
    }
    pub fn string(&self, m: &Machine, value: String) -> Result<u32> {
        self.handle(m, Object::Text(value))
    }
    pub fn array(&self, m: &Machine, length: u64, size: u32) -> Result<u32> {
        let bytes = length
            .checked_mul(size as u64)
            .filter(|v| *v <= 64 << 20)
            .ok_or("Invalid JNI array size")?;
        let p = m.malloc((bytes as usize).max(1))?;
        m.fill(p, 0, bytes as usize)?;
        self.handle(m, Object::Array(p, length, size))
    }
    fn array_value(&self, p: u32) -> Result<(u32, u64, u32)> {
        match self.value(p)? {
            Object::Array(a, n, s) => Ok((a, n, s)),
            _ => Err("JNI value is not an array".into()),
        }
    }
    pub fn array_pointer(&self, p: u32) -> Result<u32> {
        Ok(self.array_value(p)?.0)
    }
    pub fn field(&self, p: u32, name: &str) -> Result<u64> {
        match self.value(p)? {
            Object::Instance(_, fields) | Object::Class(fields) => {
                Ok(*fields.get(name).unwrap_or(&0))
            }
            _ => Err("JNI value has no fields".into()),
        }
    }
    pub fn set(&self, p: u32, name: &str, value: u64) -> Result<()> {
        match self.values.borrow_mut().get_mut(&p) {
            Some(Object::Instance(_, fields) | Object::Class(fields)) => {
                fields.insert(name.into(), value);
                Ok(())
            }
            _ => Err("JNI value has no fields".into()),
        }
    }
    fn field_text(&self, p: u32, name: &str) -> Result<String> {
        Ok(self.text(self.field(p, name)? as u32))
    }
    fn field_float(&self, p: u32, name: &str) -> Result<f64> {
        Ok(f32::from_bits(self.field(p, name)? as u32) as f64)
    }
    fn string_buffer(&self, m: &Machine, p: u32, utf8: bool) -> Result<u32> {
        if let Some(v) = self.strings.borrow().get(&(p, utf8)) {
            return Ok(*v);
        }
        let text = self.text(p);
        let mut bytes = if utf8 {
            text.into_bytes()
        } else {
            text.encode_utf16().flat_map(u16::to_le_bytes).collect()
        };
        let out = m.malloc(bytes.len() + 2)?;
        bytes.extend([0, 0]);
        m.write(out, &bytes)?;
        self.strings.borrow_mut().insert((p, utf8), out);
        Ok(out)
    }
    pub fn invoke(&self, h: &Host, index: u32) -> Result<Return> {
        let m = &h.machine;
        let a = |i| m.arg(i);
        let word = |v| Ok(Return::Word(v));
        match index {
            4 => word(0x10006),
            6 => {
                let n = String::from_utf8(m.string(a(1)?)?).map_err(|e| e.to_string())?;
                word(self.class(m, &n)?)
            }
            15
            | 17
            | 19
            | 26
            | 217
            | 218
            | 228
            | 22
            | 23
            | 227
            | 166
            | 170
            | 225
            | 191..=198
            | 223 => word(0),
            21 | 25 | 226 => word(a(1)?),
            24 => word((a(1)? == a(2)?) as u32),
            27..=30 => word(self.object(m, a(1)?, BTreeMap::new())?),
            31 => {
                let default = self.class(m, "java/lang/Object")?;
                word(match self.value(a(1)?)? {
                    Object::Instance(c, _) => c,
                    _ => default,
                })
            }
            32 => {
                let p = a(1)?;
                word(
                    (p == 0 || matches!(self.value(p),Ok(Object::Instance(c,_))if c==a(2)?)) as u32,
                )
            }
            33 | 113 | 94 | 144 => {
                let key = (
                    a(1)?,
                    String::from_utf8_lossy(&m.string(a(2)?)?).into_owned(),
                    String::from_utf8_lossy(&m.string(a(3)?)?).into_owned(),
                );
                let field = index == 94 || index == 144;
                let cache = if field { &self.fields } else { &self.methods };
                if let Some(v) = cache.borrow().get(&key) {
                    return word(*v);
                }
                let p = self.handle(
                    m,
                    if field {
                        Object::Field(key.1.clone())
                    } else {
                        Object::Method(key.1.clone(), key.2.clone())
                    },
                )?;
                cache.borrow_mut().insert(key, p);
                word(p)
            }
            95..=103 | 145..=153 => {
                let name = match self.value(a(2)?)? {
                    Object::Field(s) => s,
                    _ => return Err("Invalid JNI field".into()),
                };
                let v = self.field(a(1)?, &name)?;
                Ok(if [101, 103, 151, 153].contains(&index) {
                    Return::Wide(v)
                } else {
                    Return::Word(v as u32)
                })
            }
            104..=112 | 154..=162 => {
                let name = match self.value(a(2)?)? {
                    Object::Field(s) => s,
                    _ => return Err("Invalid JNI field".into()),
                };
                let k = index - if index < 113 { 104 } else { 154 };
                let v = if k == 6 || k == 8 {
                    a(4)? as u64 | ((a(5)? as u64) << 32)
                } else {
                    a(3)? as u64
                };
                self.set(a(1)?, &name, v)?;
                word(0)
            }
            114..=143 => {
                let (name, signature) = match self.value(a(2)?)? {
                    Object::Method(n, s) => (n, s),
                    _ => return Err("Invalid JNI method".into()),
                };
                let args = self.method_args(m, &signature, (index - 114) % 3)?;
                self.static_call(h, &name, &args)
            }
            163 => {
                let bytes = m.read(a(1)?, a(2)? as usize * 2)?;
                let units = bytes
                    .as_chunks::<2>()
                    .0
                    .iter()
                    .map(|p| u16::from_le_bytes([p[0], p[1]]))
                    .collect::<Vec<_>>();
                word(self.string(m, String::from_utf16(&units).map_err(|e| e.to_string())?)?)
            }
            164 | 168 => {
                let s = self.text(a(1)?);
                word(if index == 168 {
                    s.len() as u32
                } else {
                    s.encode_utf16().count() as u32
                })
            }
            165 | 169 | 224 => word(self.string_buffer(m, a(1)?, index == 169)?),
            167 => word(self.string(m, String::from_utf8_lossy(&m.string(a(1)?)?).into_owned())?),
            171 => word(self.array_value(a(1)?)?.1 as u32),
            172 => {
                let p = self.array(m, a(1)? as u64, 4)?;
                let address = self.array_pointer(p)?;
                for i in 0..a(1)? {
                    m.put32(address + i * 4, a(3)?)?;
                }
                word(p)
            }
            173 => {
                let (p, n, _) = self.array_value(a(1)?)?;
                let i = a(2)?;
                if i as u64 >= n {
                    return Err("JNI object array index exceeds its bounds".into());
                }
                word(m.u32(p + i * 4)?)
            }
            174 => {
                let (p, n, _) = self.array_value(a(1)?)?;
                let i = a(2)?;
                if i as u64 >= n {
                    return Err("JNI object array index exceeds its bounds".into());
                }
                m.put32(p + i * 4, a(3)?)?;
                word(0)
            }
            175..=182 => word(self.array(
                m,
                a(1)? as u64,
                [1, 1, 2, 2, 4, 8, 4, 8][(index - 175) as usize],
            )?),
            183..=190 | 222 => {
                if a(2)? != 0 {
                    m.write(a(2)?, &[0])?;
                }
                word(self.array_pointer(a(1)?)?)
            }
            199..=214 => {
                let (p, n, size) = self.array_value(a(1)?)?;
                let offset = a(2)? as u64;
                let count = a(3)? as u64;
                if offset + count > n {
                    return Err("JNI array region exceeds its bounds".into());
                }
                let mut source = p + (offset * size as u64) as u32;
                let mut target = a(4)?;
                if index >= 207 {
                    std::mem::swap(&mut source, &mut target);
                }
                m.copy(target, source, (count * size as u64) as usize)?;
                word(0)
            }
            219 => {
                m.put32(a(1)?, self.vm)?;
                word(0)
            }
            220 | 221 => {
                let s = self
                    .text(a(1)?)
                    .chars()
                    .skip(a(2)? as usize)
                    .take(a(3)? as usize)
                    .collect::<String>();
                let bytes = if index == 221 {
                    s.into_bytes()
                } else {
                    s.encode_utf16().flat_map(u16::to_le_bytes).collect()
                };
                m.write(a(4)?, &bytes)?;
                word(0)
            }
            229 => word(self.handle(
                m,
                Object::Array(a(1)?, a(2)? as u64 | ((a(3)? as u64) << 32), 1),
            )?),
            230 => word(self.array_pointer(a(1)?)?),
            231 => Ok(Return::Wide(self.array_value(a(1)?)?.1)),
            _ => Err(format!("Unimplemented JNI function slot {index}")),
        }
    }
    fn method_args(&self, m: &Machine, signature: &str, variant: u32) -> Result<Vec<Argument>> {
        let end = signature.find(')').ok_or("Invalid JNI signature")?;
        let mut sig = &signature[1..end];
        let mut kinds = vec![];
        while !sig.is_empty() {
            let mut kind = sig.as_bytes()[0];
            if kind == b'[' {
                while sig.starts_with('[') {
                    sig = &sig[1..];
                }
                kind = b'L';
            }
            if sig.starts_with('L') {
                sig = &sig[sig.find(';').ok_or("Invalid JNI class signature")? + 1..];
            } else {
                sig = &sig[1..];
            }
            kinds.push(kind);
        }
        let mut slot = 3;
        let mut pointer = m.arg(3)?;
        let mut out = vec![];
        for (i, kind) in kinds.into_iter().enumerate() {
            let wide = kind == b'J' || kind == b'D' || (kind == b'F' && variant != 2);
            let value = if variant == 2 {
                let p = pointer + i as u32 * 8;
                m.u32(p)? as u64
                    | if wide {
                        (m.u32(p + 4)? as u64) << 32
                    } else {
                        0
                    }
            } else if variant == 1 {
                if wide {
                    pointer = (pointer + 7) & !7;
                }
                let v = m.u32(pointer)? as u64
                    | if wide {
                        (m.u32(pointer + 4)? as u64) << 32
                    } else {
                        0
                    };
                pointer += if wide { 8 } else { 4 };
                v
            } else {
                if wide {
                    slot += slot & 1;
                }
                let v = m.arg(slot)? as u64
                    | if wide {
                        (m.arg(slot + 1)? as u64) << 32
                    } else {
                        0
                    };
                slot += if wide { 2 } else { 1 };
                v
            };
            out.push(if kind == b'F' {
                Argument::Float(if variant == 2 {
                    f32::from_bits(value as u32) as f64
                } else {
                    f64::from_bits(value)
                })
            } else {
                Argument::Word(value)
            });
        }
        Ok(out)
    }
    fn static_call(&self, h: &Host, name: &str, args: &[Argument]) -> Result<Return> {
        let a = |i: usize| {
            args.get(i)
                .ok_or("Missing Java method argument".to_string())
        };
        match name {
            "receiveMessageFromNative" => {
                self.receive(
                    h,
                    a(0)?.word() as i32,
                    a(1)?.word() as u32,
                    a(2)?.word() as u32,
                    a(3)?.word() as u32,
                )?;
            }
            "receiveAudioMessageFromNative" => {
                h.audio_message(
                    a(0)?.word() as i32,
                    a(1)?.word() as u32,
                    a(2)?.number(),
                    a(3)?.word() as i32,
                )?;
            }
            "getAssetManager" => return Ok(Return::Word(self.asset_manager.get())),
            _ => return Err(format!("Unimplemented Java method {name}")),
        }
        Ok(Return::Word(0))
    }
    pub fn asset_call(&self, h: &Host, name: &str) -> Result<Option<Return>> {
        let m = &h.machine;
        let a = |i| m.arg(i);
        let v = match name {
            "AAssetManager_fromJava" => self.asset_manager.get(),
            "AAssetManager_open" => {
                let name = String::from_utf8_lossy(&m.string(a(1)?)?).into_owned();
                h.set_asset(&name)?;
                let path = path(&h.assets, &name)?;
                if !path.is_file() {
                    0
                } else {
                    let data = std::fs::read(path).map_err(|e| e.to_string())?;
                    let pointer = m.malloc(data.len())?;
                    m.write(pointer, &data)?;
                    let handle = m.malloc(16)?;
                    self.assets
                        .borrow_mut()
                        .insert(handle, (pointer, data.len() as u32));
                    handle
                }
            }
            "AAsset_getBuffer" => {
                self.assets
                    .borrow()
                    .get(&a(0)?)
                    .ok_or("Invalid asset handle")?
                    .0
            }
            "AAsset_getLength" => {
                self.assets
                    .borrow()
                    .get(&a(0)?)
                    .ok_or("Invalid asset handle")?
                    .1
            }
            "AAsset_close" => {
                let handle = a(0)?;
                let (p, _) = self
                    .assets
                    .borrow_mut()
                    .remove(&handle)
                    .ok_or("Invalid asset handle")?;
                m.free(p)?;
                m.free(handle)?;
                0
            }
            _ => return Ok(None),
        };
        Ok(Some(Return::Word(v)))
    }
    fn strings(&self, m: &Machine, obj: u32, name: &str) -> Result<Vec<String>> {
        let handle = self.field(obj, name)? as u32;
        if handle == 0 {
            return Ok(vec![]);
        }
        let (p, len, _) = self.array_value(handle)?;
        (0..len)
            .map(|i| Ok(self.text(m.u32(p + i as u32 * 4)?)))
            .collect()
    }
    pub fn receive(
        &self,
        h: &Host,
        message: i32,
        obj: u32,
        _target: u32,
        _sender: u32,
    ) -> Result<()> {
        let m = &h.machine;
        self.seen.borrow_mut().insert(message);
        match message {
            17 | 18 | 27 => {
                let name = self.field_text(obj, "filename")?;
                if message == 27 {
                    self.set(obj, "file_exists", path(&h.assets, &name)?.is_file() as u64)?;
                } else if message == 17 {
                    let p = path(&h.data, &name)?;
                    if let Some(parent) = p.parent() {
                        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
                    }
                    let temp = p.with_extension(format!(
                        "{}tmp",
                        p.extension()
                            .map(|s| format!("{}.", s.to_string_lossy()))
                            .unwrap_or_default()
                    ));
                    std::fs::write(&temp, self.field_text(obj, "text")?)
                        .map_err(|e| e.to_string())?;
                    std::fs::rename(temp, p).map_err(|e| e.to_string())?;
                    self.set(obj, "status", 1)?;
                } else {
                    let mut p = path(&h.data, &name)?;
                    if !p.is_file() {
                        p = path(&h.assets, &name)?;
                    }
                    self.set(obj, "status", p.is_file() as u64)?;
                    if p.is_file() {
                        let text =
                            String::from_utf8_lossy(&std::fs::read(p).map_err(|e| e.to_string())?)
                                .replace(['\r', '\n'], "");
                        let size = text.encode_utf16().count();
                        let s = self.string(m, text)?;
                        self.set(obj, "text", s as u64)?;
                        self.set(obj, "text_size", size as u64)?;
                    } else {
                        self.set(obj, "text", 0)?;
                        self.set(obj, "text_size", 0)?;
                    }
                }
            }
            30 | 31 => {
                for name in ["status", "adTypesAvailable", "adTypeSelected"] {
                    self.set(obj, name, 0)?;
                }
            }
            54 | 55 => self.playing.set(message == 54),
            -11 | 245 => self.deferred.borrow_mut().push((-12, 0, 0, 0)),
            21 | 63 => {
                let width = self.field(obj, "width")? as u32;
                let height = self.field(obj, "height")? as u32;
                let tm = self.field_float(obj, "texture_multiplier")?;
                let image = crate::images::text_texture(
                    &h.assets,
                    width,
                    height,
                    tm,
                    message,
                    &self.strings(m, obj, "texts")?,
                    &self.strings(m, obj, "local_names")?,
                    &self.strings(m, obj, "recent_names")?,
                )?;
                let id = h.text_texture(&image)?;
                self.set(obj, "texture_id", id as u64)?;
                self.set(obj, "status", 1)?;
            }
            6 => {
                let name = self.field_text(obj, "filename")?;
                h.load_audio(
                    self.field(obj, "identifier")? as u32,
                    &name,
                    self.field(obj, "isLooping")? != 0,
                )?;
            }
            7..=15 => h.audio_message(
                message,
                self.field(obj, "identifier")? as u32,
                self.field_float(obj, "volume")?,
                self.field(obj, "loop")? as i32,
            )?,
            25 => {
                let id = self.next_dialog.get() + 1;
                self.next_dialog.set(id);
                let mut buttons = vec![];
                for key in ["positive", "neutral", "negative"] {
                    let label = self.field_text(obj, &format!("{key}_button_text"))?;
                    if !label.is_empty() {
                        buttons.push(json!({"key":key,"label":label}));
                    }
                }
                if buttons.is_empty() {
                    buttons.push(json!({"key":"positive","label":"OK"}));
                }
                let keys = buttons
                    .iter()
                    .filter_map(|b| b["key"].as_str().map(str::to_string))
                    .collect();
                self.pending.borrow_mut().insert(
                    id,
                    (
                        self.field(obj, "alert_dialog_id")?,
                        self.field(obj, "user_data")?,
                        keys,
                    ),
                );
                self.dialogs.borrow_mut().push(json!({"type":"dialog","id":id,"title":self.field_text(obj,"title")?,"message":self.field_text(obj,"message")?,"buttons":buttons}));
            }
            36 => return Err("The original game requested exit".into()),
            _ => {}
        }
        Ok(())
    }
    pub fn reply_dialog(&self, m: &Machine, id: u32, choice: &str) -> Result<()> {
        let (a, u, keys) = self
            .pending
            .borrow()
            .get(&id)
            .cloned()
            .ok_or("Unknown original game dialog")?;
        if !keys.iter().any(|k| k == choice) {
            return Err("Unknown original game dialog response".into());
        }
        self.pending.borrow_mut().remove(&id);
        let mut fields = BTreeMap::new();
        for key in ["positive", "neutral", "negative"] {
            fields.insert(format!("{key}_button_selected"), (key == choice) as u64);
        }
        fields.insert("alert_dialog_id".into(), a);
        fields.insert("user_data".into(), u);
        let cls = self.class(
            m,
            &format!("{PREFIX}UtilityMessageHandler$AlertDialogResultData"),
        )?;
        let result = self.object(m, cls, fields)?;
        self.deferred.borrow_mut().push((26, result, 0, 0));
        Ok(())
    }
    pub fn configuration(&self, m: &Machine) -> Result<u32> {
        let mut fields = BTreeMap::new();
        for (k, v) in [
            ("screenWidth", 544),
            ("screenHeight", 960),
            ("ifga", 0),
            ("areAllThemesAvailable", 1),
            ("enableSlowAppAlert", 0),
            ("numCpuCores", 3),
            ("totalMemSize", 524288),
            ("version", 3910006),
        ] {
            fields.insert(k.into(), v);
        }
        for (k, v) in [
            ("locale", "en_US"),
            ("country", "us"),
            ("languageCode", "en"),
        ] {
            fields.insert(k.into(), self.string(m, v.into())? as u64);
        }
        let cls = self.class(m, &format!("{PREFIX}Messages$MsgSystemConfigurationData"))?;
        self.object(m, cls, fields)
    }
}
/// Resolve existing ancestors too, so a saved-game symlink cannot escape the data root.
pub fn path(root: &Path, name: &str) -> Result<PathBuf> {
    let root = root.canonicalize().map_err(|e| e.to_string())?;
    let mut out = root.clone();
    for part in Path::new(name).components() {
        match part {
            std::path::Component::Normal(p) => out.push(p),
            std::path::Component::CurDir => {}
            _ => return Err("Game path escapes its data directory".into()),
        }
        if out.exists() {
            out = out.canonicalize().map_err(|e| e.to_string())?;
            if !out.starts_with(&root) {
                return Err("Game path escapes its data directory".into());
            }
        }
    }
    Ok(out)
}
