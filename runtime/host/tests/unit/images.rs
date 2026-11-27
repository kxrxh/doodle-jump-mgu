use super::*;
#[test]
fn alpha_resize_keeps_constant_coverage_and_interpolates_edges() {
    assert_eq!(resize_alpha(&[0, 255], 2, 1, 3, 1), vec![0, 128, 255]);
    assert_eq!(resize_alpha(&[127; 9], 3, 3, 1, 1), vec![127]);
}
#[test]
fn rgba_premultiplication_keeps_pillow_rounding() {
    let mut pixel = [255, 128, 1, 128];
    premultiply(&mut pixel);
    assert_eq!(pixel, [128, 64, 1, 128]);
}
#[test]
#[ignore = "requires the Pillow reference fixture generator"]
fn matches_pillow_fixtures() {
    use sha2::{Digest, Sha256};
    let root = std::path::PathBuf::from(std::env::var("DOODLE_IMAGE_FIXTURES").unwrap());
    let fixtures: serde_json::Value =
        serde_json::from_slice(&std::fs::read(root.join("fixtures.json")).unwrap()).unwrap();
    for entry in fixtures["pngs"].as_array().unwrap() {
        let image = decode(Path::new(entry["path"].as_str().unwrap())).unwrap();
        assert_eq!(image.width as u64, entry["width"].as_u64().unwrap());
        assert_eq!(image.height as u64, entry["height"].as_u64().unwrap());
        assert_eq!(
            format!("{:x}", Sha256::digest(&image.pixels)),
            entry["sha256"].as_str().unwrap(),
            "PNG {}",
            entry["path"]
        );
    }
    for path in fixtures["invalid_pngs"].as_array().unwrap() {
        assert!(decode(Path::new(path.as_str().unwrap())).is_err());
    }
    for entry in fixtures["resizes"].as_array().unwrap() {
        let n = |key: &str| entry[key].as_u64().unwrap() as usize;
        let source: Vec<u8> = serde_json::from_value(entry["source"].clone()).unwrap();
        let pixels: Vec<u8> = serde_json::from_value(entry["pixels"].clone()).unwrap();
        assert_eq!(
            resize_alpha(&source, n("sw"), n("sh"), n("width"), n("height")),
            pixels,
            "resize {entry}"
        );
    }
    for entry in fixtures["texts"].as_array().unwrap() {
        let values = |key: &str| serde_json::from_value::<Vec<String>>(entry[key].clone()).unwrap();
        let image = text_texture(
            Path::new(fixtures["assets"].as_str().unwrap()),
            entry["width"].as_u64().unwrap() as u32,
            entry["height"].as_u64().unwrap() as u32,
            entry["tm"].as_f64().unwrap(),
            entry["message"].as_i64().unwrap() as i32,
            &values("texts"),
            &values("local"),
            &values("recent"),
        )
        .unwrap();
        let expected = std::fs::read(root.join(entry["pixels"].as_str().unwrap())).unwrap();
        assert_eq!(image.pixels, expected, "text {entry}");
    }
    let (sprites, metadata) = scenery_assets(
        Path::new(fixtures["assets"].as_str().unwrap()),
        Path::new(fixtures["cursor"].as_str().unwrap()),
    )
    .unwrap();
    assert_eq!(metadata, fixtures["sprites"]);
    for ((identifier, image), entry) in sprites
        .iter()
        .zip(fixtures["sprite_images"].as_array().unwrap())
    {
        assert_eq!(*identifier as u64, entry["id"].as_u64().unwrap());
        assert_eq!(
            image.pixels,
            std::fs::read(root.join(entry["pixels"].as_str().unwrap())).unwrap()
        );
    }
    for entry in fixtures["papers"].as_array().unwrap() {
        let texture = Image {
            width: entry["width"].as_u64().unwrap() as u32,
            height: entry["height"].as_u64().unwrap() as u32,
            pixels: serde_json::from_value(entry["texture"].clone()).unwrap(),
        };
        let vertices: Vec<[f64; 8]> = serde_json::from_value(entry["vertices"].clone()).unwrap();
        let content: (u32, u32) = serde_json::from_value(entry["content"].clone()).unwrap();
        let mut paper = Paper::default();
        let (projection, image) = paper
            .update(&texture, content, &vertices, 7, 1, "paper.png")
            .unwrap();
        assert_eq!(projection, entry["projection"]);
        assert_eq!(
            image.unwrap().pixels,
            std::fs::read(root.join(entry["pixels"].as_str().unwrap())).unwrap()
        );
        assert!(
            paper
                .update(&texture, content, &vertices, 7, 1, "paper.png")
                .unwrap()
                .1
                .is_none()
        );
        assert!(
            paper
                .update(&texture, content, &vertices, 7, 2, "paper.png")
                .unwrap()
                .1
                .is_some()
        );
    }
}
