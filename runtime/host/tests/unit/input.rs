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
fn ring_sensitivity_scales_detent_travel_at_each_frame_rate() {
    for (sensitivity, expected) in [(0.5, 0.375), (1.0, 0.75), (1.25, 0.9375), (1.5, 1.125)] {
        for fps in [30, 50, 60] {
            let mut c = Controls::new();
            c.observe(65535.0);
            c.raw(0.0, 0.0, 0.0, true, sensitivity, 1000.0);
            let total: f64 = (0..fps)
                .map(|frame| {
                    c.take(true, 1000.0 + f64::from(frame) * 1000.0 / f64::from(fps))
                        .0
                })
                .sum();
            assert_eq!(total, expected);
        }
    }
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
fn game_touches_keep_their_release_when_crossing_into_either_side_panel() {
    for (x, edge) in [(700.0, 0.0), (1220.0, 544.0)] {
        let mut c = Controls::new();
        c.touch(1, 960.0, 300.0, PLACE, true, 0.0);
        assert_eq!(c.take(true, 0.0), (0.0, vec![[0.0, 0.0, 272.0, 400.0]]));
        c.touch(2, x, 300.0, PLACE, true, 17.0);
        assert_eq!(c.take(true, 17.0), (0.0, vec![[0.0, 1.0, edge, 400.0]]));
        c.touch(3, x, 300.0, PLACE, true, 34.0);
        assert_eq!(c.take(true, 34.0), (0.0, vec![[0.0, 2.0, edge, 400.0]]));
        c.touch(3, x, 300.0, PLACE, true, 51.0);
        assert!(c.take(true, 51.0).1.is_empty());
    }
}

#[test]
fn side_panel_touches_keep_steering_until_released_inside_the_game() {
    let mut c = Controls::new();
    c.touch(1, 20.0, 300.0, PLACE, true, 0.0);
    assert_eq!(c.take(true, 0.0), (-1.0, vec![]));
    c.touch(2, 960.0, 300.0, PLACE, true, 17.0);
    assert_eq!(c.take(true, 17.0), (-1.0, vec![]));
    c.touch(3, 960.0, 300.0, PLACE, true, 34.0);
    assert_eq!(c.take(true, 34.0), (0.0, vec![]));
}

#[test]
fn menu_transition_and_pause_clear_queued_ring_steering() {
    let mut c = Controls::new();
    c.observe(0.0);
    c.raw(0.0, 0.0, 4.0, true, 1.0, 1000.0);
    assert!(c.steering(true, 1000.0) > 0.0);
    assert_eq!(c.take(false, 1000.0).0, 0.0);
    assert_eq!(c.take(true, 1000.0).0, 0.0);
    c.raw(0.0, 0.0, 8.0, true, 1.0, 1000.0);
    c.reset();
    assert_eq!(c.take(true, 1000.0).0, 0.0);
}

#[test]
fn direct_and_held_touch_steering_override_ring_input() {
    let mut c = Controls::new();
    c.observe(0.0);
    c.raw(10.0, 0.0, 4.0, true, 1.5, 1000.0);
    assert_eq!(c.take(true, 1000.0).0, -1.0);
    c.raw(0.0, 0.0, 4.0, true, 1.5, 1000.0);
    assert_eq!(c.take(true, 1000.0).0, 0.0);
    c.touch(1, 20.0, 300.0, PLACE, true, 1000.0);
    c.raw(0.0, 0.0, 5.0, true, 1.5, 1000.0);
    assert_eq!(c.take(true, 1000.0).0, -1.0);
    c.touch(3, 20.0, 300.0, PLACE, true, 1000.0);
    assert_eq!(c.take(true, 1000.0).0, 0.0);
}

#[test]
fn reversing_wrap_discontinuities_and_stalls_do_not_leave_a_long_spin() {
    let mut c = Controls::new();
    c.observe(0.0);
    c.raw(0.0, 0.0, 8.0, true, 1.0, 1000.0);
    assert_eq!(c.take(true, 1000.0).0, 1.0);
    c.raw(0.0, 0.0, 7.0, true, 1.0, 1000.0);
    assert_eq!(c.take(true, 1000.0).0, -0.25);
    c.reset();
    c.observe(0.0);
    c.raw(0.0, 0.0, 100.0, true, 1.0, 1000.0);
    assert_eq!(c.take(true, 1000.0).0, 0.0);
    c.observe(0.0);
    c.raw(0.0, 0.0, 64.0, true, 1.0, 1000.0);
    let total: f64 = (0..60)
        .map(|i| c.take(true, 1000.0 + f64::from(i) * 1000.0 / 60.0).0)
        .sum();
    assert_eq!(total, 6.0);
    c.observe(0.0);
    c.raw(0.0, 0.0, 4.0, true, 1.0, 2000.0);
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
    c.raw(0.0, 1.0, f64::NAN, true, 1.0, 2000.0);
    c.raw(0.0, 2.0, f64::NAN, true, 1.0, 2000.0);
    c.raw(0.0, 0.0, f64::NAN, true, 1.0, 2000.0);
    c.raw(0.0, 1.0, f64::NAN, true, 1.0, 2000.0);
    for phase in [0.0, 2.0, 0.0, 2.0] {
        assert_eq!(c.take(true, 2000.0).1, vec![[0.0, phase, 272.0, 240.0]]);
    }
    assert!(c.take(true, 2000.0).1.is_empty());
}

#[test]
fn menu_joystick_motion_uses_elapsed_time_and_observation_survives_reset() {
    let mut c = Controls::new();
    c.raw(4.0, 0.0, f64::NAN, false, 1.0, 0.0);
    for _ in 0..60 {
        c.move_cursor(false, 1.0 / 60.0);
    }
    assert_eq!(c.cursor(), [420.0, 295.0]);
    c.raw(7.0, 0.0, f64::NAN, false, 1.0, 0.0);
    for _ in 0..60 {
        c.move_cursor(false, 1.0 / 60.0);
    }
    assert_eq!(c.cursor(), [420.0, 535.0]);
    c.observe(65535.0);
    c.reset();
    c.observe(f64::NAN);
    c.raw(0.0, 0.0, 0.0, false, 1.5, 0.0);
    assert_eq!(c.cursor(), [436.0, 535.0]);
    c.raw(4.0, 0.0, 64.0, false, 1.0, 0.0);
    c.move_cursor(false, 10.0);
    assert_eq!(c.cursor(), [543.0, 535.0]);
    c.move_cursor(true, 10.0);
    assert_eq!(c.cursor(), [543.0, 535.0]);
}
