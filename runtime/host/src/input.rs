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
    pub fn turn(&mut self, delta: f64, now_ms: f64) {
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
    pub fn raw(&mut self, deflection: f64, push: f64, operation: f64, playing: bool, now_ms: f64) {
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
                self.turn(f64::from(delta), now_ms);
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
mod tests {
    use super::*;

    const PLACE: Placement = Placement {
        w: 1920.0,
        h: 720.0,
        scale: 1.0,
        ox: 0.0,
        oy: 0.0,
        full: true,
    };

    #[test]
    fn one_ring_detent_has_the_same_small_travel_at_each_frame_rate() {
        let mut distances = vec![];
        for fps in [30, 50, 60] {
            let mut c = Controls::new();
            c.observe(65535.0);
            c.raw(0.0, 0.0, 0.0, true, 1000.0);
            let mut total = 0.0;
            for frame in 0..fps {
                total += c
                    .take(true, 1000.0 + f64::from(frame) * 1000.0 / f64::from(fps))
                    .0;
            }
            distances.push(total);
        }
        assert_eq!(distances, vec![0.75; 3]);
    }

    #[test]
    fn touch_release_and_cursor_position_reach_separate_frames() {
        let mut c = Controls::new();
        c.touch(1, 906.0, 150.0, PLACE, false, 0.0);
        c.touch(2, 981.0, 300.0, PLACE, false, 0.0);
        c.touch(3, 1011.0, 330.0, PLACE, false, 0.0);
        assert_eq!(c.take(false, 0.0).1, vec![[0.0, 0.0, 200.0, 200.0]]);
        assert_eq!(c.take(false, 0.0).1, vec![[0.0, 1.0, 300.0, 400.0]]);
        assert_eq!(c.take(false, 0.0).1, vec![[0.0, 2.0, 340.0, 440.0]]);
        assert_eq!(c.cursor(), [340.0, 440.0]);
        c.select(false);
        assert_eq!(c.take(false, 0.0).1, vec![[0.0, 0.0, 340.0, 440.0]]);
    }

    #[test]
    fn menu_transition_and_pause_clear_queued_ring_steering() {
        let mut c = Controls::new();
        c.observe(0.0);
        c.raw(0.0, 0.0, 4.0, true, 1000.0);
        assert!(c.steering(true, 1000.0) > 0.0);
        assert_eq!(c.take(false, 1000.0).0, 0.0);
        assert_eq!(c.take(true, 1000.0).0, 0.0);
        c.raw(0.0, 0.0, 8.0, true, 1000.0);
        c.reset();
        assert_eq!(c.take(true, 1000.0).0, 0.0);
    }

    #[test]
    fn direct_and_held_touch_steering_override_ring_input() {
        let mut c = Controls::new();
        c.observe(0.0);
        c.raw(10.0, 0.0, 4.0, true, 1000.0);
        assert_eq!(c.take(true, 1000.0).0, -1.0);
        c.raw(0.0, 0.0, 4.0, true, 1000.0);
        assert_eq!(c.take(true, 1000.0).0, 0.0);
        c.touch(1, 20.0, 300.0, PLACE, true, 1000.0);
        c.raw(0.0, 0.0, 5.0, true, 1000.0);
        assert_eq!(c.take(true, 1000.0).0, -1.0);
        c.touch(3, 20.0, 300.0, PLACE, true, 1000.0);
        assert_eq!(c.take(true, 1000.0).0, 0.0);
    }

    #[test]
    fn reversing_wrap_discontinuities_and_stalls_do_not_leave_a_long_spin() {
        let mut c = Controls::new();
        c.observe(0.0);
        c.raw(0.0, 0.0, 8.0, true, 1000.0);
        assert_eq!(c.take(true, 1000.0).0, 1.0);
        c.raw(0.0, 0.0, 7.0, true, 1000.0);
        assert_eq!(c.take(true, 1000.0).0, -0.25);
        c.reset();
        c.observe(0.0);
        c.raw(0.0, 0.0, 100.0, true, 1000.0);
        assert_eq!(c.take(true, 1000.0).0, 0.0);
        c.observe(0.0);
        c.raw(0.0, 0.0, 64.0, true, 1000.0);
        let total: f64 = (0..60)
            .map(|i| c.take(true, 1000.0 + f64::from(i) * 1000.0 / 60.0).0)
            .sum();
        assert_eq!(total, 6.0);
        c.observe(0.0);
        c.raw(0.0, 0.0, 4.0, true, 2000.0);
        assert_eq!(c.take(true, 2500.0).0, 0.0);
    }

    #[test]
    fn queued_motion_coalesces_but_a_full_queue_keeps_the_release() {
        let mut c = Controls::new();
        c.point(0.0, 1.0, 2.0);
        c.point(1.0, 3.0, 4.0);
        c.point(1.0, 5.0, 6.0);
        assert_eq!(c.take(false, 0.0).1, vec![[0.0, 0.0, 1.0, 2.0]]);
        assert_eq!(c.take(false, 0.0).1, vec![[0.0, 1.0, 5.0, 6.0]]);
        assert!(c.take(false, 0.0).1.is_empty());
        for _ in 0..16 {
            c.point(0.0, 1.0, 2.0);
        }
        c.point(2.0, 9.0, 8.0);
        for _ in 0..15 {
            c.take(false, 0.0);
        }
        assert_eq!(c.take(false, 0.0).1, vec![[0.0, 2.0, 9.0, 8.0]]);
        assert!(c.take(false, 0.0).1.is_empty());
        for _ in 0..15 {
            c.point(0.0, 1.0, 2.0);
        }
        c.tap(10.0, 20.0);
        for _ in 0..15 {
            c.take(false, 0.0);
        }
        assert!(
            c.take(false, 0.0).1.is_empty(),
            "a tap must not enqueue half a click"
        );
    }

    #[test]
    fn temporary_steering_expires_and_shooting_is_edge_triggered() {
        let mut c = Controls::new();
        c.steer(1.0, 1000.0);
        assert_eq!(c.take(true, 1219.0).0, 1.0);
        assert_eq!(c.take(true, 1220.0).0, 0.0);
        c.raw(0.0, 1.0, f64::NAN, true, 2000.0);
        c.raw(0.0, 2.0, f64::NAN, true, 2000.0);
        c.raw(0.0, 0.0, f64::NAN, true, 2000.0);
        c.raw(0.0, 1.0, f64::NAN, true, 2000.0);
        for phase in [0.0, 2.0, 0.0, 2.0] {
            assert_eq!(c.take(true, 2000.0).1, vec![[0.0, phase, 272.0, 240.0]]);
        }
        assert!(c.take(true, 2000.0).1.is_empty());
    }

    #[test]
    fn menu_joystick_motion_uses_elapsed_time_and_observation_survives_reset() {
        let mut c = Controls::new();
        c.raw(4.0, 0.0, f64::NAN, false, 0.0);
        for _ in 0..60 {
            c.move_cursor(false, 1.0 / 60.0);
        }
        assert_eq!(c.cursor(), [420.0, 295.0]);
        c.raw(7.0, 0.0, f64::NAN, false, 0.0);
        for _ in 0..60 {
            c.move_cursor(false, 1.0 / 60.0);
        }
        assert_eq!(c.cursor(), [420.0, 535.0]);
        c.observe(65535.0);
        c.reset();
        c.observe(f64::NAN);
        c.raw(0.0, 0.0, 0.0, false, 0.0);
        assert_eq!(c.cursor(), [436.0, 535.0]);
        c.raw(4.0, 0.0, 64.0, false, 0.0);
        c.move_cursor(false, 10.0);
        assert_eq!(c.cursor(), [543.0, 535.0]);
        c.move_cursor(true, 10.0);
        assert_eq!(c.cursor(), [543.0, 535.0]);
    }
}
