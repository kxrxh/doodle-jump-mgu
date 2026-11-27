#[derive(Clone, Copy, Debug)]
pub struct Placement {
    pub w: f64,
    pub h: f64,
    pub scale: f64,
    pub ox: f64,
    pub oy: f64,
    pub full: bool,
}

#[derive(Clone, Copy, PartialEq)]
enum Finger {
    None,
    Game,
    Steer,
}

pub struct Controls {
    cursor: [f64; 2],
    tilt: f64,
    direction: f64,
    horizontal: f64,
    pressed: bool,
    finger: Finger,
    until: f64,
    ring: f64,
    step: f64,
    ring_until: f64,
    position: Option<u16>,
    touches: [[f64; 3]; 16],
    touch_count: usize,
}

impl Default for Controls {
    fn default() -> Self {
        Self::new()
    }
}

impl Controls {
    pub fn new() -> Self {
        Self {
            cursor: [180.0, 295.0],
            tilt: 0.0,
            direction: 0.0,
            horizontal: 0.0,
            pressed: false,
            finger: Finger::None,
            until: 0.0,
            ring: 0.0,
            step: 0.0,
            ring_until: 0.0,
            position: None,
            touches: [[0.0; 3]; 16],
            touch_count: 0,
        }
    }
    pub fn reset(&mut self) {
        let (cursor, position) = (self.cursor, self.position);
        *self = Self::new();
        self.cursor = cursor;
        self.position = position;
    }
    pub fn observe(&mut self, position: f64) {
        if position.is_finite() && position.fract() == 0.0 {
            self.position = Some(((position % 65536.0 + 65536.0) % 65536.0) as u16);
        }
    }
    pub fn point(&mut self, phase: f64, x: f64, y: f64) {
        let point = [phase, x, y];
        if self.touch_count > 0 && phase == 1.0 && self.touches[self.touch_count - 1][0] == 1.0 {
            self.touches[self.touch_count - 1] = point;
        } else if self.touch_count < self.touches.len() {
            self.touches[self.touch_count] = point;
            self.touch_count += 1;
        } else if phase == 2.0 {
            self.touches[15] = point;
        }
    }
    pub fn tap(&mut self, x: f64, y: f64) {
        if self.touch_count <= 14 {
            self.point(0.0, x, y);
            self.point(2.0, x, y);
        }
    }
    pub fn select(&mut self, playing: bool) {
        if playing {
            self.tap(272.0, 240.0);
        } else {
            self.tap(self.cursor[0], self.cursor[1]);
        }
    }
    fn turn(&mut self, delta: f64, now_ms: f64) {
        if now_ms >= self.ring_until || sign(delta) != sign(self.ring) {
            self.ring = 0.0;
        }
        self.ring = bounded(self.ring + delta * 0.75, -6.0, 6.0);
        self.step = js_min(1.0, self.ring.abs() / 3.0);
        self.ring_until = now_ms + 250.0;
    }
    pub fn steer(&mut self, direction: f64, now_ms: f64) {
        self.ring = 0.0;
        self.tilt = direction;
        self.until = now_ms + 220.0;
    }
    pub fn raw(
        &mut self,
        deflection: f64,
        push: f64,
        operation: f64,
        playing: bool,
        ring_sensitivity: f64,
        now_ms: f64,
    ) {
        self.direction = if deflection == 1.0 || deflection == 2.0 {
            -1.0
        } else if deflection == 7.0 || deflection == 8.0 {
            1.0
        } else {
            0.0
        };
        self.horizontal = if deflection == 4.0 || deflection == 5.0 {
            1.0
        } else if deflection == 10.0 || deflection == 11.0 {
            -1.0
        } else {
            0.0
        };
        if self.finger != Finger::Steer {
            self.tilt = self.horizontal;
            self.until = 0.0;
        }
        if self.tilt != 0.0 {
            self.ring = 0.0;
        }
        let previous = self.position;
        self.observe(operation);
        let delta = previous.zip(self.position).map_or(0, |(before, after)| {
            ((i32::from(after) - i32::from(before) + 32768) & 0xffff) - 32768
        });
        if delta != 0 && delta.abs() <= 64 {
            if !playing {
                self.cursor[0] = bounded(self.cursor[0] + f64::from(delta) * 16.0, 0.0, 543.0);
            } else if self.tilt == 0.0 {
                self.turn(f64::from(delta) * ring_sensitivity, now_ms);
            }
        }
        let down = push == 1.0 || push == 2.0;
        if down && !self.pressed {
            self.select(playing);
        }
        self.pressed = down;
    }
    pub fn touch(
        &mut self,
        phase: u32,
        x: f64,
        y: f64,
        place: Placement,
        playing: bool,
        _now_ms: f64,
    ) {
        let (sx, sy) = (x * place.scale + place.ox, y * place.scale + place.oy);
        let gw = place.h * 544.0 / 960.0;
        let gx = (place.w - gw) / 2.0;
        if playing && (sx < gx || sx > gx + gw) {
            if phase == 3 {
                self.tilt = 0.0;
                self.finger = Finger::None;
            } else {
                self.ring = 0.0;
                self.finger = Finger::Steer;
                self.tilt = if sx < gx { -1.0 } else { 1.0 };
                self.until = 0.0;
            }
            return;
        }
        if self.finger == Finger::Steer {
            if phase == 3 {
                self.tilt = 0.0;
                self.finger = Finger::None;
            }
            return;
        }
        let px = bounded((sx - gx) * 544.0 / gw, 0.0, 544.0);
        let py = bounded(sy * 960.0 / place.h, 0.0, 960.0);
        if phase == 1 && sx >= gx && sx <= gx + gw {
            self.finger = Finger::Game;
            self.point(0.0, px, py);
        }
        if self.finger == Finger::Game {
            self.cursor = [px, py];
        }
        if phase == 2 && self.finger == Finger::Game {
            self.point(1.0, px, py);
        }
        if phase == 3 && self.finger == Finger::Game {
            self.point(2.0, px, py);
        }
        if phase == 3 {
            self.finger = Finger::None;
        }
    }
    pub fn move_cursor(&mut self, playing: bool, dt: f64) {
        if !playing {
            self.cursor[0] = bounded(self.cursor[0] + self.horizontal * 240.0 * dt, 0.0, 543.0);
            self.cursor[1] = bounded(self.cursor[1] + self.direction * 240.0 * dt, 0.0, 959.0);
        }
    }
    pub fn steering(&mut self, playing: bool, now_ms: f64) -> f64 {
        if self.until != 0.0 && now_ms >= self.until {
            self.tilt = 0.0;
            self.until = 0.0;
        }
        if self.ring != 0.0 && (!playing || now_ms >= self.ring_until) {
            self.ring = 0.0;
        }
        if self.tilt != 0.0 {
            self.tilt
        } else {
            sign(self.ring) * js_min(self.ring.abs(), self.step)
        }
    }
    pub fn take(&mut self, playing: bool, now_ms: f64) -> (f64, Vec<[f64; 4]>) {
        let value = self.steering(playing, now_ms);
        if self.ring != 0.0 {
            self.ring -= value;
            self.ring_until = now_ms + 250.0;
        }
        let mut touches = vec![];
        if self.touch_count > 0 {
            let [phase, x, y] = self.touches[0];
            touches.push([0.0, phase, x, y]);
            self.touches.copy_within(1..self.touch_count, 0);
            self.touch_count -= 1;
        }
        (value, touches)
    }
    pub fn cursor(&self) -> [f64; 2] {
        self.cursor
    }
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
fn js_min(a: f64, b: f64) -> f64 {
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
fn js_max(a: f64, b: f64) -> f64 {
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
    js_max(low, js_min(high, value))
}

#[cfg(test)]
#[path = "../tests/unit/input.rs"]
mod tests;
