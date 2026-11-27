use super::*;
#[test]
fn truncated_image_storage_is_rejected_before_a_crop() {
    let image = Image {
        width: 2,
        height: 2,
        pixels: vec![0; 3],
    };
    assert!(image.crop(0, 0, 2, 2).is_err());
}
#[test]
fn png_decoder_expands_rgb_and_preserves_alpha() {
    let mut input = Vec::new();
    {
        let mut e = png::Encoder::new(&mut input, 2, 1);
        e.set_color(png::ColorType::Rgb);
        e.set_depth(png::BitDepth::Eight);
        e.write_header()
            .unwrap()
            .write_image_data(&[255, 0, 0, 0, 255, 0])
            .unwrap();
    }
    let decoded = decode_bytes(&input).unwrap();
    assert_eq!(decoded.pixels, [255, 0, 0, 255, 0, 255, 0, 255]);
}
#[test]
fn purchase_exclusions_keep_gameplay_art_visible() {
    assert!(excluded("popups/game-over-promo_L.png"));
    assert!(excluded("x/store/bck_L.png"));
    assert!(excluded("StoreOn_L.png"));
    assert!(!excluded("snow/bck_L.png"));
    assert!(!excluded("gametiles_L.png"));
}
