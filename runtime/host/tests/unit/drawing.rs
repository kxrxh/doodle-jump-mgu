use super::*;
use serde_json::json;

#[test]
fn paper_tiles_cover_each_margin_once_and_restore_the_clip() {
    let scene = json!({"sprites":{},"paper":{"width":544,"height":960,"u":0,"v":0,"du":1.0/544.0,"dv":1.0/960.0,"alpha":1}});
    let commands = World::new().draw(1920.0, 720.0, &scene, 0.0, false, 0.0);
    assert_eq!(commands.iter().filter(|c| c[0] == "image").count(), 4);
    assert_eq!(commands.last(), Some(&json!(["noclip"])));
}

#[test]
fn invalid_paper_does_not_drop_decorations() {
    let scene = json!({"sprites":{"platform":{"id":80,"width":90,"height":24}},"paper":{"width":0,"height":960,"u":0,"v":0,"du":1.0/544.0,"dv":1.0/960.0,"alpha":1}});
    let commands = World::new().draw(1920.0, 720.0, &scene, 0.0, false, 0.0);
    assert_eq!(
        commands
            .iter()
            .filter(|c| c[0] == "image" && c[1] == 80)
            .count(),
        4
    );
    assert!(!commands.iter().any(|c| c[0] == "image" && c[1] == 2));
}

#[test]
fn negative_half_rounding_matches_the_store_pixel_boundary() {
    assert_eq!(round(-0.5).to_bits(), (-0.0_f64).to_bits());
    assert_eq!(round(-1.5), -1.0);
    assert_eq!(round(1.5), 2.0);
    assert_eq!(round(4503599627370497.0), 4503599627370497.0);
}

#[test]
fn malformed_or_excessive_paper_plans_leave_all_decorations_intact() {
    let normal =
        json!({"width":544,"height":960,"u":0,"v":0,"du":1.0/544.0,"dv":1.0/960.0,"alpha":1});
    for change in [
        json!({"width":0}),
        json!({"width":1.5}),
        json!({"height":2047}),
        json!({"height":null}),
        json!({"du":0}),
        json!({"du":1e-300}),
        json!({"dv":null}),
        json!({"u":-0.1}),
        json!({"v":1}),
        json!({"alpha":null}),
        json!({"du":1e6,"dv":1e6}),
    ] {
        let mut paper = normal.clone();
        for (key, value) in change.as_object().unwrap() {
            paper[key] = value.clone();
        }
        let scene = json!({"paper":paper,"sprites":{"platform":{"id":80,"width":90,"height":24}}});
        let commands = World::new().draw(1920.0, 720.0, &scene, 0.0, false, 0.0);
        assert!(!commands.iter().any(|c| c[0] == "image" && c[1] == 2));
        assert_eq!(
            commands
                .iter()
                .filter(|c| c[0] == "image" && c[1] == 80)
                .count(),
            4
        );
    }
}

#[test]
fn scrolling_paper_samples_match_the_original_repeat_phase_without_overdraw() {
    for (w, h, paper) in [
        (
            1920.0,
            720.0,
            json!({"width":544,"height":960,"u":0,"v":0,"du":1.0/544.0,"dv":1.0/960.0,"alpha":1}),
        ),
        (
            1920.0,
            720.0,
            json!({"width":544,"height":960,"u":0.37,"v":0.995,"du":1.0/544.0,"dv":1.0/960.0,"alpha":0.6}),
        ),
        (
            1600.0,
            520.0,
            json!({"width":544,"height":960,"u":0.137,"v":0.271,"du":1.0/544.0,"dv":1.0/960.0,"alpha":0.5}),
        ),
        (
            1280.0,
            480.0,
            json!({"width":100,"height":80,"u":0.125,"v":0.875,"du":1.0/240.0,"dv":1.0/320.0,"alpha":0.25}),
        ),
    ] {
        let commands =
            World::new().draw(w, h, &json!({"paper":paper,"sprites":{}}), 0.0, false, 0.0);
        let mut clip = [0.0; 4];
        let mut alpha = 1.0;
        let mut tiles = vec![];
        for c in commands {
            match c[0].as_str().unwrap() {
                "alpha" => alpha = c[1].as_f64().unwrap(),
                "clip" => {
                    for (i, v) in clip.iter_mut().enumerate() {
                        *v = c[i + 1].as_f64().unwrap();
                    }
                }
                "image" => {
                    assert_eq!(c[1], 2);
                    assert_eq!(alpha, paper["alpha"].as_f64().unwrap());
                    tiles.push((
                        clip,
                        [
                            c[2].as_f64().unwrap(),
                            c[3].as_f64().unwrap(),
                            c[4].as_f64().unwrap(),
                            c[5].as_f64().unwrap(),
                        ],
                    ));
                }
                _ => {}
            }
        }
        let scale = h / 960.0;
        let gx = (w - 544.0 * scale) / 2.0;
        let left = round(gx);
        let right = round(gx + 544.0 * scale);
        assert_eq!(
            tiles.iter().map(|(c, _)| c[2] * c[3]).sum::<f64>(),
            (left + w - right) * h
        );
        for y in (0..h as usize).step_by(39).map(|v| v as f64 + 0.5) {
            for x in (0..w as usize).step_by(31).map(|v| v as f64 + 0.5) {
                if x >= left && x < right {
                    continue;
                }
                let hits = tiles
                    .iter()
                    .filter(|(c, _)| x >= c[0] && x < c[0] + c[2] && y >= c[1] && y < c[1] + c[3])
                    .collect::<Vec<_>>();
                assert_eq!(
                    hits.len(),
                    1,
                    "every margin pixel has exactly one paper tile"
                );
                let image = hits[0].1;
                let u = ((x - image[0]) / image[2] * (paper["width"].as_f64().unwrap() + 2.0)
                    - 1.0)
                    / paper["width"].as_f64().unwrap();
                let v = ((y - image[1]) / image[3] * (paper["height"].as_f64().unwrap() + 2.0)
                    - 1.0)
                    / paper["height"].as_f64().unwrap();
                let eu =
                    paper["u"].as_f64().unwrap() + (x - gx) * paper["du"].as_f64().unwrap() / scale;
                let ev = paper["v"].as_f64().unwrap() + y * paper["dv"].as_f64().unwrap() / scale;
                assert!((u - (eu - eu.floor())).abs() < 1e-8);
                assert!((v - (ev - ev.floor())).abs() < 1e-8);
            }
        }
    }
}
