use super::*;
use std::{io::Read, os::unix::net::UnixListener};

fn session(now: Instant) -> Session {
    Session::new(
        json!({"sprites":{},"paper":null}),
        Worker::new(PathBuf::from("/nonexistent/session-test.sock")),
        now,
    )
}

#[test]
fn frame_deadlines_keep_sixty_hertz_under_small_wakeup_delays() {
    let start = Instant::now();
    let mut clock = Clock::new(start);
    for i in 1..=120 {
        let now = start + PERIOD * i + Duration::from_millis(2);
        clock.submit(now);
        assert_eq!(clock.next, start + PERIOD * (i + 1));
    }
}

#[test]
fn resuming_discards_time_spent_hidden() {
    let start = Instant::now();
    let mut clock = Clock::new(start);
    clock.submit(start + PERIOD);
    let resume = start + Duration::from_secs(120);
    clock.restart(resume);
    assert!((clock.submit(resume + PERIOD) - 1. / 60.).abs() < 0.000001);
}

#[test]
fn focus_visibility_driving_dialogs_and_stale_parent_each_stop_the_game() {
    let now = Instant::now();
    let mut s = session(now);
    s.ui.shown = true;
    s.ui.focused = true;
    assert!(s.wants_run(now));
    s.ui.shown = false;
    assert!(!s.wants_run(now));
    s.ui.shown = true;
    s.ui.focused = false;
    assert!(!s.wants_run(now));
    s.ui.focused = true;
    s.speed = Some(4.);
    assert!(!s.wants_run(now));
    s.ui.parked_only = false;
    assert!(s.wants_run(now));
    s.dialogs.insert(1);
    assert!(!s.wants_run(now));
    s.dialogs.clear();
    assert!(!s.wants_run(now + Duration::from_secs(2)));
    s.ui_at = now + Duration::from_secs(2);
    assert!(s.wants_run(now + Duration::from_secs(2)));
    s.failure = Some("stopped".into());
    assert!(!s.wants_run(now + Duration::from_secs(2)));
}

#[test]
fn inactive_store_surface_draws_in_the_hmi_canvas_dimensions() {
    let now = Instant::now();
    let mut s = session(now);
    s.ui.place = Placement {
        w: 1920.,
        h: 720.,
        full: true,
        scale: 1.164,
        ox: 71.,
        oy: -38.,
    };
    s.ui.active = false;
    let mut out = vec![];
    s.draw(&mut out, now).unwrap();
    let draw: Value = serde_json::from_slice(&out[8..]).unwrap();
    assert_eq!(
        (
            draw["w"].as_f64(),
            draw["h"].as_f64(),
            draw["full"].as_bool()
        ),
        (Some(1600.), Some(520.), Some(false))
    );
}

#[test]
fn an_inactive_native_renderer_can_bootstrap_with_cpu_frames() {
    let now = Instant::now();
    let directory =
        std::env::temp_dir().join(format!("doodle-session-gate-{}", std::process::id()));
    std::fs::create_dir_all(&directory).unwrap();
    let path = directory.join("worker.sock");
    let _listener = UnixListener::bind(&path).unwrap();
    let mut worker = Worker::new(path);
    worker.poll(now);
    assert!(worker.ready());
    let mut s = Session::new(json!({"sprites":{},"paper":null}), worker, now);
    s.running = true;
    s.ui.renderer.ready = true;
    s.ui.renderer.native = true;
    s.ui.renderer.video = true;
    s.ui.renderer.path = "/tmp/renderer.sock".into();
    s.ui.active = false;
    assert!(
        s.can_frame(),
        "an unavailable Store surface must not deadlock CPU fallback"
    );
    std::fs::remove_dir_all(directory).unwrap();
}

#[test]
fn an_attempt_does_not_make_a_previous_native_binding_current() {
    let now = Instant::now();
    let directory =
        std::env::temp_dir().join(format!("doodle-session-bound-{}", std::process::id()));
    std::fs::create_dir_all(&directory).unwrap();
    let path = directory.join("worker.sock");
    let listener = UnixListener::bind(&path).unwrap();
    let mut worker = Worker::new(path);
    worker.poll(now);
    assert!(worker.ready());
    let (mut peer, _) = listener.accept().unwrap();
    assert!(
        worker
            .bind("/tmp/old-renderer.sock", [-9000, -9000, 1920, 720], now)
            .unwrap()
    );
    worker.poll(now);
    let mut header = [0; 8];
    peer.read_exact(&mut header).unwrap();
    let mut payload = vec![0; u32::from_le_bytes(header[4..8].try_into().unwrap()) as usize];
    peer.read_exact(&mut payload).unwrap();
    peer.write_all(b"{\"event\":\"bind\",\"native\":true}\n")
        .unwrap();
    worker.poll(now);
    assert!(worker.native());
    let mut s = Session::new(json!({"sprites":{},"paper":null}), worker, now);
    s.ui.renderer = Renderer {
        ready: true,
        native: true,
        video: true,
        visible: true,
        on_screen: true,
        source: 2,
        socket: 2,
        path: "/tmp/new-renderer.sock".into(),
        ..Renderer::default()
    };
    s.attempted = s.ui.surface(&s.worker);
    assert!(
        !s.bound(),
        "a new challenge must not adopt the previous renderer's native surface"
    );
    std::fs::remove_dir_all(directory).unwrap();
}

#[test]
fn untimed_frames_do_not_make_old_measurements_fresh() {
    let now = Instant::now();
    let mut s = session(now);
    s.sample.timing = Some((now, 2., 1., 4.));
    s.completed(
        now + Duration::from_millis(50),
        now + Duration::from_millis(60),
    );
    assert_eq!(s.sample.timing, Some((now, 2., 1., 4.)));
}

#[test]
fn the_fps_display_reports_zero_when_no_frame_is_completed() {
    let now = Instant::now();
    let mut s = session(now);
    s.running = true;
    s.ui.show_fps = true;
    s.frames = 60;
    s.sample.frames = 60;
    s.sample.rate = Some(60);
    s.mode = Some(Mode::Native);
    let mut out = vec![];
    s.draw(&mut out, now + Duration::from_secs(2)).unwrap();
    let draw: Value = serde_json::from_slice(&out[8..]).unwrap();
    assert!(
        draw["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| command[0] == "text" && command[1] == "Native | Presented 0 FPS")
    );
}

#[test]
fn invalid_layout_and_socket_inputs_are_rejected() {
    for ui in [
        json!({"place":{"w":0}}),
        json!({"place":{"x":1.5}}),
        json!({"renderer":{"path":"relative.sock"}}),
        json!({"focused":1}),
    ] {
        assert!(Ui::parse(&ui).is_err());
    }
}
