#![no_std]

use core::ptr::{addr_of, addr_of_mut};

const SLOTS: usize = 64;

#[link(wasm_import_module = "host")]
unsafe extern "C" {
    fn now() -> f64;
    fn playing(id: u32) -> u32;
    fn floor(value: f64) -> f64;
    fn ceil(value: f64) -> f64;
    fn round(value: f64) -> f64;
    fn exp(value: f64) -> f64;
    fn alpha(value: f64);
    fn clip(x: f64, y: f64, width: f64, height: f64);
    fn image(id: u32, x: f64, y: f64, width: f64, height: f64);
    fn line(x1: f64, y1: f64, x2: f64, y2: f64, width: f64, color: u32);
    fn noclip();
}

#[panic_handler]
fn panic(_: &core::panic::PanicInfo<'_>) -> ! {
    core::arch::wasm32::unreachable()
}

fn min(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if a == 0.0 && b == 0.0 {
        if a.is_sign_negative() || b.is_sign_negative() {
            -0.0
        } else {
            0.0
        }
    } else if a < b {
        a
    } else {
        b
    }
}
fn max(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if a == 0.0 && b == 0.0 {
        if a.is_sign_positive() || b.is_sign_positive() {
            0.0
        } else {
            -0.0
        }
    } else if a > b {
        a
    } else {
        b
    }
}
fn bounded(value: f64, low: f64, high: f64) -> f64 {
    max(low, min(high, value))
}
fn sign(value: f64) -> f64 {
    if value > 0.0 {
        1.0
    } else if value < 0.0 {
        -1.0
    } else {
        value
    }
}
fn active(id: u32) -> bool {
    unsafe { playing(id) != 0 }
}
fn clock() -> f64 {
    unsafe { now() }
}

// The adapter exposes these scalar fields directly; no frame or image pixels enter WASM.
const X: usize = 0;
const Y: usize = 1;
const TILT: usize = 2;
const DIRECTION: usize = 3;
const HORIZONTAL: usize = 4;
const PRESSED: usize = 5;
const FINGER: usize = 6;
const UNTIL: usize = 7;
const RING: usize = 8;
const STEP: usize = 9;
const RING_UNTIL: usize = 10;
const POSITION: usize = 11;
const TOUCH_COUNT: usize = 12;
const TAKEN: usize = 13;
const PHASE: usize = 14;
const TOUCH_X: usize = 15;
const TOUCH_Y: usize = 16;

#[repr(C)]
#[derive(Clone, Copy)]
struct Control {
    fields: [f64; 17],
    touches: [[f64; 3]; 16],
}
impl Control {
    const EMPTY: Self = Self {
        fields: [0.0; 17],
        touches: [[0.0; 3]; 16],
    };
    fn reset(&mut self) {
        let (x, y, position) = (self.fields[X], self.fields[Y], self.fields[POSITION]);
        *self = Self::EMPTY;
        self.fields[X] = x;
        self.fields[Y] = y;
        self.fields[POSITION] = position;
    }
    fn observe(&mut self, position: f64) {
        if position.is_finite() && position % 1.0 == 0.0 {
            self.fields[POSITION] = (position % 65536.0 + 65536.0) % 65536.0;
        }
    }
    fn point(&mut self, phase: f64, x: f64, y: f64) {
        let count = self.fields[TOUCH_COUNT] as usize;
        let point = [phase, x, y];
        if count > 0 && phase == 1.0 && self.touches[count - 1][0] == 1.0 {
            self.touches[count - 1] = point;
        } else if count < 16 {
            self.touches[count] = point;
            self.fields[TOUCH_COUNT] += 1.0;
        } else if phase == 2.0 {
            self.touches[15] = point;
        }
    }
    fn tap(&mut self, x: f64, y: f64) {
        if self.fields[TOUCH_COUNT] <= 14.0 {
            self.point(0.0, x, y);
            self.point(2.0, x, y);
        }
    }
    fn select(&mut self, id: u32) {
        if active(id) {
            self.tap(272.0, 240.0);
        } else {
            self.tap(self.fields[X], self.fields[Y]);
        }
    }
    fn turn(&mut self, delta: f64) {
        let current = clock();
        if current >= self.fields[RING_UNTIL] || sign(delta) != sign(self.fields[RING]) {
            self.fields[RING] = 0.0;
        }
        self.fields[RING] = bounded(self.fields[RING] + delta * 0.75, -6.0, 6.0);
        self.fields[STEP] = min(1.0, self.fields[RING].abs() / 3.0);
        self.fields[RING_UNTIL] = current + 250.0;
    }
    fn steering(&mut self, id: u32) -> f64 {
        if self.fields[UNTIL] != 0.0 && clock() >= self.fields[UNTIL] {
            self.fields[TILT] = 0.0;
            self.fields[UNTIL] = 0.0;
        }
        if self.fields[RING] != 0.0 && (!active(id) || clock() >= self.fields[RING_UNTIL]) {
            self.fields[RING] = 0.0;
        }
        if self.fields[TILT] != 0.0 {
            self.fields[TILT]
        } else {
            sign(self.fields[RING]) * min(self.fields[RING].abs(), self.fields[STEP])
        }
    }
}

static mut CONTROLS: [Control; SLOTS] = [Control::EMPTY; SLOTS];
static mut CONTROL_COUNT: usize = 0;

fn control(id: u32) -> Option<Control> {
    let index = id.wrapping_sub(1) as usize;
    // One non-shared WASM memory is used on the JS thread. Work on a value so host
    // callbacks never overlap a Rust borrow of the exported scalar storage.
    if index < unsafe { CONTROL_COUNT } {
        Some(unsafe { addr_of!(CONTROLS).cast::<Control>().add(index).read() })
    } else {
        None
    }
}
fn store_control(id: u32, value: Control) {
    let index = id.wrapping_sub(1) as usize;
    if index < unsafe { CONTROL_COUNT } {
        unsafe {
            addr_of_mut!(CONTROLS)
                .cast::<Control>()
                .add(index)
                .write(value);
        }
    }
}
fn change(id: u32, operation: impl FnOnce(&mut Control)) {
    if let Some(mut value) = control(id) {
        operation(&mut value);
        store_control(id, value);
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn controls_create() -> u32 {
    let index = unsafe { CONTROL_COUNT };
    if index >= SLOTS {
        return 0;
    }
    let mut value = Control::EMPTY;
    value.fields[X] = 180.0;
    value.fields[Y] = 295.0;
    value.fields[POSITION] = f64::NAN;
    unsafe {
        CONTROL_COUNT += 1;
    }
    store_control(index as u32 + 1, value);
    index as u32 + 1
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_state(id: u32) -> u32 {
    if control(id).is_none() {
        return 0;
    }
    unsafe {
        addr_of_mut!(CONTROLS)
            .cast::<Control>()
            .add(id as usize - 1) as u32
    }
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_reset(id: u32) {
    change(id, Control::reset);
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_observe(id: u32, position: f64) {
    change(id, |c| c.observe(position));
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_tap(id: u32, x: f64, y: f64) {
    change(id, |c| c.tap(x, y));
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_point(id: u32, phase: f64, x: f64, y: f64) {
    change(id, |c| c.point(phase, x, y));
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_select(id: u32) {
    change(id, |c| c.select(id));
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_steer(id: u32, direction: f64) {
    change(id, |c| {
        c.fields[RING] = 0.0;
        c.fields[TILT] = direction;
        c.fields[UNTIL] = clock() + 220.0;
    });
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_turn(id: u32, delta: f64) {
    change(id, |c| c.turn(delta));
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_raw(id: u32, deflection: f64, push: f64, position: f64) {
    change(id, |c| {
        c.fields[DIRECTION] = if deflection == 1.0 || deflection == 2.0 {
            -1.0
        } else if deflection == 7.0 || deflection == 8.0 {
            1.0
        } else {
            0.0
        };
        c.fields[HORIZONTAL] = if deflection == 4.0 || deflection == 5.0 {
            1.0
        } else if deflection == 10.0 || deflection == 11.0 {
            -1.0
        } else {
            0.0
        };
        if c.fields[FINGER] != 2.0 {
            c.fields[TILT] = c.fields[HORIZONTAL];
            c.fields[UNTIL] = 0.0;
        }
        if c.fields[TILT] != 0.0 {
            c.fields[RING] = 0.0;
        }
        let previous = c.fields[POSITION];
        c.observe(position);
        let delta = if previous.is_nan() {
            0
        } else {
            ((c.fields[POSITION] as i32 - previous as i32 + 32768) & 0xffff) - 32768
        };
        if delta != 0 && delta.abs() <= 64 {
            if !active(id) {
                c.fields[X] = bounded(c.fields[X] + f64::from(delta) * 16.0, 0.0, 543.0);
            } else if c.fields[TILT] == 0.0 {
                c.turn(f64::from(delta));
            }
        }
        let down = push == 1.0 || push == 2.0;
        if down && c.fields[PRESSED] == 0.0 {
            c.select(id);
        }
        c.fields[PRESSED] = f64::from(down);
    });
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_touch(
    id: u32,
    phase: u32,
    x: f64,
    y: f64,
    w: f64,
    h: f64,
    scale: f64,
    ox: f64,
    oy: f64,
) {
    change(id, |c| {
        let (sx, sy) = (x * scale + ox, y * scale + oy);
        let gw = h * 544.0 / 960.0;
        let gx = (w - gw) / 2.0;
        if active(id) && (sx < gx || sx > gx + gw) {
            if phase == 3 {
                c.fields[TILT] = 0.0;
                c.fields[FINGER] = 0.0;
            } else {
                c.fields[RING] = 0.0;
                c.fields[FINGER] = 2.0;
                c.fields[TILT] = if sx < gx { -1.0 } else { 1.0 };
                c.fields[UNTIL] = 0.0;
            }
            return;
        }
        if c.fields[FINGER] == 2.0 {
            if phase == 3 {
                c.fields[TILT] = 0.0;
                c.fields[FINGER] = 0.0;
            }
            return;
        }
        let px = bounded((sx - gx) * 544.0 / gw, 0.0, 544.0);
        let py = bounded(sy * 960.0 / h, 0.0, 960.0);
        if phase == 1 && sx >= gx && sx <= gx + gw {
            c.fields[FINGER] = 1.0;
            c.point(0.0, px, py);
        }
        if c.fields[FINGER] == 1.0 {
            c.fields[X] = px;
            c.fields[Y] = py;
        }
        if phase == 2 && c.fields[FINGER] == 1.0 {
            c.point(1.0, px, py);
        }
        if phase == 3 && c.fields[FINGER] == 1.0 {
            c.point(2.0, px, py);
        }
        if phase == 3 {
            c.fields[FINGER] = 0.0;
        }
    });
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_move(id: u32, dt: f64) {
    change(id, |c| {
        if !active(id) {
            c.fields[X] = bounded(c.fields[X] + c.fields[HORIZONTAL] * 240.0 * dt, 0.0, 543.0);
            c.fields[Y] = bounded(c.fields[Y] + c.fields[DIRECTION] * 240.0 * dt, 0.0, 959.0);
        }
    });
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_steering(id: u32) -> f64 {
    let Some(mut c) = control(id) else {
        return 0.0;
    };
    let value = c.steering(id);
    store_control(id, c);
    value
}
#[unsafe(no_mangle)]
pub extern "C" fn controls_take(id: u32) -> f64 {
    let Some(mut c) = control(id) else {
        return 0.0;
    };
    let value = c.steering(id);
    if c.fields[RING] != 0.0 {
        c.fields[RING] -= value;
        c.fields[RING_UNTIL] = clock() + 250.0;
    }
    let count = c.fields[TOUCH_COUNT] as usize;
    c.fields[TAKEN] = f64::from(count > 0);
    if count > 0 {
        c.fields[PHASE] = c.touches[0][0];
        c.fields[TOUCH_X] = c.touches[0][1];
        c.fields[TOUCH_Y] = c.touches[0][2];
        for i in 1..count {
            c.touches[i - 1] = c.touches[i];
        }
        c.fields[TOUCH_COUNT] -= 1.0;
    }
    store_control(id, c);
    value
}

#[repr(C)]
#[derive(Clone, Copy)]
struct World {
    phase: f64,
    lean: f64,
    inputs: [f64; 32],
}
impl World {
    const EMPTY: Self = Self {
        phase: 0.0,
        lean: 0.0,
        inputs: [0.0; 32],
    };
}
static mut WORLDS: [World; SLOTS] = [World::EMPTY; SLOTS];
static mut WORLD_COUNT: usize = 0;
#[unsafe(no_mangle)]
pub extern "C" fn world_create() -> u32 {
    let index = unsafe { WORLD_COUNT };
    if index >= SLOTS {
        return 0;
    }
    unsafe {
        WORLD_COUNT += 1;
    }
    index as u32 + 1
}
#[unsafe(no_mangle)]
pub extern "C" fn world_state(id: u32) -> u32 {
    let index = id.wrapping_sub(1) as usize;
    if index >= unsafe { WORLD_COUNT } {
        return 0;
    }
    unsafe { addr_of_mut!(WORLDS).cast::<World>().add(index) as u32 }
}
#[derive(Clone, Copy)]
struct Item {
    sprite: usize,
    x: f64,
    y: f64,
    size: f64,
    opacity: f64,
    depth: f64,
}
const LEFT: [Item; 5] = [
    Item {
        sprite: 0,
        x: 0.24,
        y: 0.20,
        size: 128.0,
        opacity: 0.23,
        depth: 0.25,
    },
    Item {
        sprite: 1,
        x: 0.71,
        y: 0.37,
        size: 70.0,
        opacity: 0.22,
        depth: 0.6,
    },
    Item {
        sprite: 1,
        x: 0.30,
        y: 0.59,
        size: 86.0,
        opacity: 0.18,
        depth: 0.6,
    },
    Item {
        sprite: 2,
        x: 0.67,
        y: 0.81,
        size: 76.0,
        opacity: 0.18,
        depth: 0.6,
    },
    Item {
        sprite: 3,
        x: 0.16,
        y: 0.85,
        size: 96.0,
        opacity: 0.13,
        depth: 0.25,
    },
];
const RIGHT: [Item; 5] = [
    Item {
        sprite: 4,
        x: 0.70,
        y: 0.25,
        size: 140.0,
        opacity: 0.20,
        depth: 0.25,
    },
    Item {
        sprite: 1,
        x: 0.26,
        y: 0.45,
        size: 76.0,
        opacity: 0.20,
        depth: 0.6,
    },
    Item {
        sprite: 5,
        x: 0.28,
        y: 0.40,
        size: 26.0,
        opacity: 0.20,
        depth: 0.6,
    },
    Item {
        sprite: 1,
        x: 0.65,
        y: 0.71,
        size: 90.0,
        opacity: 0.18,
        depth: 0.6,
    },
    Item {
        sprite: 2,
        x: 0.22,
        y: 0.86,
        size: 70.0,
        opacity: 0.17,
        depth: 0.6,
    },
];
#[derive(Clone, Copy)]
struct Tiles {
    pw: f64,
    ph: f64,
    bx: f64,
    by: f64,
    iw: f64,
    ih: f64,
    top: f64,
    bottom: f64,
    columns: [[f64; 2]; 2],
}
fn tile_plan(p: &[f64; 32], scale: f64, gx: f64, clips: &[[f64; 4]; 2], h: f64) -> Option<Tiles> {
    if p[0] == 0.0
        || !p[1..8].iter().all(|v| v.is_finite())
        || p[1] % 1.0 != 0.0
        || p[2] % 1.0 != 0.0
        || p[1] < 1.0
        || p[1] > 2046.0
        || p[2] < 1.0
        || p[2] > 2046.0
        || p[3] < 0.0
        || p[3] >= 1.0
        || p[4] < 0.0
        || p[4] >= 1.0
        || p[5] <= 0.0
        || p[6] <= 0.0
        || p[7] < 0.0
        || p[7] > 1.0
    {
        return None;
    }
    let pw = scale / p[5];
    let ph = scale / p[6];
    let bx = gx - p[3] * pw;
    let by = -p[4] * ph;
    let iw = pw * (1.0 + 2.0 / p[1]);
    let ih = ph * (1.0 + 2.0 / p[2]);
    if ![pw, ph, bx, by, iw, ih]
        .iter()
        .all(|v| v.is_finite() && (*v as f32).is_finite())
        || pw <= 0.0
        || ph <= 0.0
    {
        return None;
    }
    let top = unsafe { floor(-by / ph) };
    let bottom = unsafe { ceil((round(h) - by) / ph) };
    let mut columns = [[0.0; 2]; 2];
    for i in 0..2 {
        columns[i] = [unsafe { floor((clips[i][0] - bx) / pw) }, unsafe {
            ceil((clips[i][0] + clips[i][2] - bx) / pw)
        }];
    }
    let count =
        (bottom - top) * ((columns[0][1] - columns[0][0]) + (columns[1][1] - columns[1][0]));
    if !count.is_finite() || count <= 0.0 || count > 1024.0 {
        return None;
    }
    Some(Tiles {
        pw,
        ph,
        bx,
        by,
        iw,
        ih,
        top,
        bottom,
        columns,
    })
}
#[unsafe(no_mangle)]
pub extern "C" fn world_draw(id: u32, w: f64, h: f64, tilt: f64, is_playing: u32, dt: f64) {
    let index = id.wrapping_sub(1) as usize;
    if index >= unsafe { WORLD_COUNT } {
        return;
    }
    let mut world = unsafe { addr_of!(WORLDS).cast::<World>().add(index).read() };
    let scale = h / 960.0;
    let gw = 544.0 * scale;
    let gx = (w - gw) / 2.0;
    let sw = max(0.0, gx);
    world.phase += dt * if is_playing != 0 { 8.0 } else { 1.5 };
    world.lean += (tilt * 18.0 - world.lean) * (1.0 - unsafe { exp(-dt * 6.0) });
    unsafe {
        addr_of_mut!(WORLDS).cast::<World>().add(index).write(world);
    }
    if sw == 0.0 || sw.is_nan() {
        return;
    }
    let margins = [0.0, gx + gw];
    let mut clips = [[0.0; 4]; 2];
    for i in 0..2 {
        let x = margins[i];
        clips[i] = [
            unsafe { round(x) },
            0.0,
            unsafe { round(x + sw) - round(x) },
            unsafe { round(h) },
        ];
    }
    let tiles = tile_plan(&world.inputs, scale, gx, &clips, h);
    for side in 0..2 {
        let x = margins[side];
        let c = clips[side];
        unsafe {
            clip(c[0], c[1], c[2], c[3]);
        }
        if let Some(t) = tiles {
            unsafe {
                alpha(world.inputs[7]);
            }
            let mut j = t.top;
            while j < t.bottom {
                let mut k = t.columns[side][0];
                while k < t.columns[side][1] {
                    let tx = t.bx + k * t.pw;
                    let ty = t.by + j * t.ph;
                    let left = max(c[0], unsafe { round(tx) });
                    let right = min(c[0] + c[2], unsafe { round(t.bx + (k + 1.0) * t.pw) });
                    let y = max(0.0, unsafe { round(ty) });
                    let end = min(c[3], unsafe { round(t.by + (j + 1.0) * t.ph) });
                    if right > left && end > y {
                        unsafe {
                            clip(left, y, right - left, end - y);
                            image(
                                2,
                                tx - t.pw / world.inputs[1],
                                ty - t.ph / world.inputs[2],
                                t.iw,
                                t.ih,
                            );
                        }
                    }
                    k += 1.0;
                }
                j += 1.0;
            }
            unsafe {
                clip(c[0], c[1], c[2], c[3]);
            }
        }
        unsafe {
            alpha(1.0);
        }
        for item in if side == 0 { LEFT } else { RIGHT } {
            let s = 8 + item.sprite * 4;
            if world.inputs[s] == 0.0 {
                continue;
            }
            let width = item.size * h / 720.0;
            let height = width * world.inputs[s + 3] / world.inputs[s + 2];
            let y = ((item.y * h + world.phase * item.depth + 120.0) % (h + 240.0)) - 120.0;
            unsafe {
                alpha(item.opacity);
                image(
                    world.inputs[s + 1] as u32,
                    x + item.x * sw - width / 2.0 - world.lean * item.depth,
                    y - height / 2.0,
                    width,
                    height,
                );
            }
        }
        let direction = if side == 0 { -1.0 } else { 1.0 };
        let cx = x + sw / 2.0;
        let cy = h * 0.91;
        let d = 14.0 * h / 720.0;
        unsafe {
            alpha(0.18 + max(0.0, direction * tilt) * 0.45);
            line(
                cx - direction * d,
                cy - d,
                cx + direction * d,
                cy,
                2.0,
                0x6b513bff,
            );
            line(
                cx + direction * d,
                cy,
                cx - direction * d,
                cy + d,
                2.0,
                0x6b513bff,
            );
            alpha(1.0);
            noclip();
        }
    }
}
