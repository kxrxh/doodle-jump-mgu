use serde_json::{Value, json};

#[derive(Default)]
pub struct World {
    phase: f64,
    lean: f64,
}

impl World {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn draw(
        &mut self,
        w: f64,
        h: f64,
        scene: &Value,
        tilt: f64,
        playing: bool,
        dt: f64,
    ) -> Vec<Value> {
        let scale = h / 960.0;
        let gw = 544.0 * scale;
        let gx = (w - gw) / 2.0;
        let sw = max(0.0, gx);
        self.phase += dt * if playing { 8.0 } else { 1.5 };
        self.lean += (tilt * 18.0 - self.lean) * (1.0 - (-dt * 6.0).exp());
        let mut commands = Vec::with_capacity(64);
        if sw == 0.0 || sw.is_nan() {
            return commands;
        }
        let inputs = scene_inputs(scene);
        let margins = [0.0, gx + gw];
        let mut clips = [[0.0; 4]; 2];
        for (i, x) in margins.iter().enumerate() {
            clips[i] = [round(*x), 0.0, round(x + sw) - round(*x), round(h)];
        }
        let tiles = tile_plan(&inputs, scale, gx, &clips, h);
        for side in 0..2 {
            let x = margins[side];
            let c = clips[side];
            commands.push(json!(["clip", c[0], c[1], c[2], c[3]]));
            if let Some(t) = tiles {
                commands.push(json!(["alpha", inputs[7]]));
                let mut j = t.top;
                while j < t.bottom {
                    let mut k = t.columns[side][0];
                    while k < t.columns[side][1] {
                        let tx = t.bx + k * t.pw;
                        let ty = t.by + j * t.ph;
                        let left = max(c[0], round(tx));
                        let right = min(c[0] + c[2], round(t.bx + (k + 1.0) * t.pw));
                        let y = max(0.0, round(ty));
                        let end = min(c[3], round(t.by + (j + 1.0) * t.ph));
                        if right > left && end > y {
                            commands.push(json!(["clip", left, y, right - left, end - y]));
                            commands.push(json!([
                                "image",
                                2,
                                tx - t.pw / inputs[1],
                                ty - t.ph / inputs[2],
                                t.iw,
                                t.ih
                            ]));
                        }
                        k += 1.0;
                    }
                    j += 1.0;
                }
                commands.push(json!(["clip", c[0], c[1], c[2], c[3]]));
            }
            commands.push(json!(["alpha", 1.0]));
            for item in if side == 0 { LEFT } else { RIGHT } {
                let s = 8 + item.sprite * 4;
                if inputs[s] == 0.0 {
                    continue;
                }
                let width = item.size * h / 720.0;
                let height = width * inputs[s + 3] / inputs[s + 2];
                let y = ((item.y * h + self.phase * item.depth + 120.0) % (h + 240.0)) - 120.0;
                commands.push(json!(["alpha", item.opacity]));
                commands.push(json!([
                    "image",
                    inputs[s + 1] as u32,
                    x + item.x * sw - width / 2.0 - self.lean * item.depth,
                    y - height / 2.0,
                    width,
                    height
                ]));
            }
            let direction = if side == 0 { -1.0 } else { 1.0 };
            let cx = x + sw / 2.0;
            let cy = h * 0.91;
            let d = 14.0 * h / 720.0;
            commands.push(json!(["alpha", 0.18 + max(0.0, direction * tilt) * 0.45]));
            commands.push(json!([
                "line",
                cx - direction * d,
                cy - d,
                cx + direction * d,
                cy,
                2.0,
                [107, 81, 59, 255]
            ]));
            commands.push(json!([
                "line",
                cx + direction * d,
                cy,
                cx - direction * d,
                cy + d,
                2.0,
                [107, 81, 59, 255]
            ]));
            commands.push(json!(["alpha", 1.0]));
            commands.push(json!(["noclip"]));
        }
        commands
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(v) => v.as_f64().is_some_and(|n| n != 0.0),
        Value::String(v) => !v.is_empty(),
        _ => true,
    }
}
fn scene_inputs(scene: &Value) -> [f64; 32] {
    let mut inputs = [0.0; 32];
    let paper = &scene["paper"];
    inputs[0] = f64::from(truthy(paper));
    for (i, name) in ["width", "height", "u", "v", "du", "dv", "alpha"]
        .iter()
        .enumerate()
    {
        inputs[1 + i] = paper[name].as_f64().unwrap_or(f64::NAN);
    }
    for (i, name) in ["ufo", "platform", "broken", "monster", "bat", "springs"]
        .iter()
        .enumerate()
    {
        let sprite = &scene["sprites"][name];
        let at = 8 + i * 4;
        inputs[at] = f64::from(truthy(sprite));
        for (j, field) in ["id", "width", "height"].iter().enumerate() {
            inputs[at + 1 + j] = sprite[field].as_f64().unwrap_or(f64::NAN);
        }
    }
    inputs
}

fn round(value: f64) -> f64 {
    if !value.is_finite() || value == 0.0 {
        return value;
    }
    let floor = value.floor();
    let result = if value - floor < 0.5 {
        floor
    } else {
        floor + 1.0
    };
    if result == 0.0 && value.is_sign_negative() {
        -0.0
    } else {
        result
    }
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
        || p[1].fract() != 0.0
        || p[2].fract() != 0.0
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
    let top = (-by / ph).floor();
    let bottom = ((round(h) - by) / ph).ceil();
    let mut columns = [[0.0; 2]; 2];
    for (i, clip) in clips.iter().enumerate() {
        columns[i] = [
            ((clip[0] - bx) / pw).floor(),
            ((clip[0] + clip[2] - bx) / pw).ceil(),
        ];
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

#[cfg(test)]
mod tests {
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
            let scene =
                json!({"paper":paper,"sprites":{"platform":{"id":80,"width":90,"height":24}}});
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
                        .filter(|(c, _)| {
                            x >= c[0] && x < c[0] + c[2] && y >= c[1] && y < c[1] + c[3]
                        })
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
                    let eu = paper["u"].as_f64().unwrap()
                        + (x - gx) * paper["du"].as_f64().unwrap() / scale;
                    let ev =
                        paper["v"].as_f64().unwrap() + y * paper["dv"].as_f64().unwrap() / scale;
                    assert!((u - (eu - eu.floor())).abs() < 1e-8);
                    assert!((v - (ev - ev.floor())).abs() < 1e-8);
                }
            }
        }
    }
}
