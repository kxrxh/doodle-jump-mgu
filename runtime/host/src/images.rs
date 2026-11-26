//! Original bitmap assets and host-generated text/scenery pixels.
use crate::Result;
use serde_json::{Value, json};
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::fs;
use std::io::Cursor;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug)]
pub struct Image {
    pub width: u32,
    pub height: u32,
    pub pixels: Vec<u8>,
}

impl Image {
    fn blank(width: u32, height: u32) -> Result<Self> {
        dimensions(width, height)?;
        Ok(Self {
            width,
            height,
            pixels: vec![0; width as usize * height as usize * 4],
        })
    }

    fn crop(&self, left: u32, top: u32, width: u32, height: u32) -> Result<Self> {
        dimensions(self.width, self.height)?;
        if self.pixels.len() != self.width as usize * self.height as usize * 4 {
            return Err("Invalid image pixel storage".into());
        }
        if left
            .checked_add(width)
            .is_none_or(|right| right > self.width)
            || top
                .checked_add(height)
                .is_none_or(|bottom| bottom > self.height)
        {
            return Err("Image crop exceeds its source".into());
        }
        let mut image = Self::blank(width, height)?;
        for y in 0..height as usize {
            let source = ((top as usize + y) * self.width as usize + left as usize) * 4;
            let target = y * width as usize * 4;
            image.pixels[target..target + width as usize * 4]
                .copy_from_slice(&self.pixels[source..source + width as usize * 4]);
        }
        Ok(image)
    }

    fn premultiplied(mut self) -> Self {
        for pixel in self.pixels.as_chunks_mut::<4>().0 {
            premultiply(pixel);
        }
        self
    }
}

fn dimensions(width: u32, height: u32) -> Result<()> {
    if width == 0 || height == 0 || width > 4096 || height > 4096 {
        return Err("Invalid game image dimensions".into());
    }
    Ok(())
}

pub fn decode(path: &Path) -> Result<Image> {
    let metadata = fs::metadata(path).map_err(|e| format!("{}: {e}", path.display()))?;
    if metadata.len() > 64 * 1024 * 1024 {
        return Err("PNG exceeds the engine asset size limit".into());
    }
    decode_bytes(&fs::read(path).map_err(|e| e.to_string())?)
}

pub fn decode_bytes(bytes: &[u8]) -> Result<Image> {
    if bytes.len() > 64 * 1024 * 1024 {
        return Err("PNG exceeds the engine asset size limit".into());
    }
    let mut decoder = png::Decoder::new(Cursor::new(bytes));
    decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    decoder.set_limits(png::Limits {
        bytes: 64 * 1024 * 1024,
    });
    let mut reader = decoder
        .read_info()
        .map_err(|e| format!("Invalid PNG: {e}"))?;
    dimensions(reader.info().width, reader.info().height)?;
    let mut source = vec![0; reader.output_buffer_size()];
    let info = reader
        .next_frame(&mut source)
        .map_err(|e| format!("Invalid PNG: {e}"))?;
    let source = &source[..info.buffer_size()];
    let mut image = Image::blank(info.width, info.height)?;
    for (pixel, source) in image
        .pixels
        .as_chunks_mut::<4>()
        .0
        .iter_mut()
        .zip(source.chunks_exact(info.color_type.samples()))
    {
        match info.color_type {
            png::ColorType::Rgba => pixel.copy_from_slice(source),
            png::ColorType::Rgb => pixel.copy_from_slice(&[source[0], source[1], source[2], 255]),
            png::ColorType::Grayscale => {
                pixel.copy_from_slice(&[source[0], source[0], source[0], 255])
            }
            png::ColorType::GrayscaleAlpha => {
                pixel.copy_from_slice(&[source[0], source[0], source[0], source[1]])
            }
            png::ColorType::Indexed => return Err("PNG palette was not expanded".into()),
        }
    }
    Ok(image)
}

pub fn png_dimensions(path: &Path) -> Result<(u32, u32)> {
    let file = fs::File::open(path).map_err(|e| e.to_string())?;
    let reader = png::Decoder::new(file)
        .read_info()
        .map_err(|e| format!("Invalid PNG: {e}"))?;
    let info = reader.info();
    dimensions(info.width, info.height)?;
    Ok((info.width, info.height))
}

pub fn excluded(name: &str) -> bool {
    let parts: Vec<&str> = name.split('/').collect();
    let stem = parts
        .last()
        .copied()
        .unwrap_or_default()
        .rsplit_once('.')
        .map_or_else(
            || parts.last().copied().unwrap_or_default(),
            |(stem, _)| stem,
        );
    if parts.contains(&"store") || (parts.contains(&"popups") && stem.starts_with("game-over-")) {
        return true;
    }
    matches!(
        stem.split('_')
            .next()
            .unwrap_or_default()
            .to_ascii_lowercase()
            .as_str(),
        "store"
            | "storeon"
            | "storeicon"
            | "storeiconon"
            | "toys"
            | "toyson"
            | "removeads"
            | "removeadson"
            | "buy"
            | "buyon"
            | "restore"
            | "restoreon"
            | "share"
            | "shareon"
            | "sharepopup"
            | "whatsnew"
            | "whatsnewtext"
            | "newspanel"
    )
}

fn premultiply(pixel: &mut [u8]) {
    for channel in 0..3 {
        let product = u32::from(pixel[channel]) * u32::from(pixel[3]) + 128;
        pixel[channel] = ((product + (product >> 8)) >> 8) as u8;
    }
}

// Match Pillow's 22-bit separable bilinear weights and per-pass rounding.
fn coefficients(input: usize, output: usize) -> Vec<(usize, Vec<i32>)> {
    let scale = input as f64 / output as f64;
    let support = scale.max(1.0);
    (0..output)
        .map(|x| {
            let center = (x as f64 + 0.5) * scale;
            let start = ((center - support + 0.5) as i64).max(0) as usize;
            let end = ((center + support + 0.5) as usize).min(input);
            let weights: Vec<f64> = (start..end)
                .map(|i| (1.0 - ((i as f64 - center + 0.5) / support).abs()).max(0.0))
                .collect();
            let total: f64 = weights.iter().sum();
            (
                start,
                weights
                    .iter()
                    .map(|w| (0.5 + w / total * ((1 << 22) as f64)) as i32)
                    .collect(),
            )
        })
        .collect()
}

fn resize_alpha(source: &[u8], sw: usize, sh: usize, width: usize, height: usize) -> Vec<u8> {
    if sw == 0 || sh == 0 {
        return vec![0; width * height];
    }
    let horizontal = coefficients(sw, width);
    let vertical = coefficients(sh, height);
    let mut rows = vec![0; width * sh];
    for y in 0..sh {
        for (x, (start, weights)) in horizontal.iter().enumerate() {
            let sum = weights
                .iter()
                .enumerate()
                .fold(1i64 << 21, |v, (i, weight)| {
                    v + i64::from(source[y * sw + start + i]) * i64::from(*weight)
                });
            rows[y * width + x] = (sum >> 22).clamp(0, 255) as u8;
        }
    }
    let mut result = vec![0; width * height];
    for (y, (start, weights)) in vertical.iter().enumerate() {
        for x in 0..width {
            let sum = weights
                .iter()
                .enumerate()
                .fold(1i64 << 21, |v, (i, weight)| {
                    v + i64::from(rows[(start + i) * width + x]) * i64::from(*weight)
                });
            result[y * width + x] = (sum >> 22).clamp(0, 255) as u8;
        }
    }
    result
}

#[derive(Clone)]
struct Glyph {
    x: u32,
    y: u32,
    width: u32,
    height: u32,
    xadvance: i32,
    xoffset: i32,
    yoffset: i32,
}
struct Font {
    glyphs: BTreeMap<u32, Glyph>,
    atlas: Image,
}

impl Font {
    fn load(assets: &Path) -> Result<Self> {
        let plist =
            fs::read_to_string(assets.join("doodlejumpfont.plist")).map_err(|e| e.to_string())?;
        let (_, characters) = plist
            .split_once("<key>char</key>")
            .ok_or("Missing BMFont character table")?;
        let (characters, _) = characters
            .split_once("</array>")
            .ok_or("Invalid BMFont character table")?;
        let mut glyphs = BTreeMap::new();
        for definition in characters.split("<dict>").skip(1) {
            let (definition, _) = definition
                .split_once("</dict>")
                .ok_or("Invalid BMFont glyph")?;
            let number = |name: &str| -> Result<i32> {
                let marker = format!("<key>-{name}</key>");
                let (_, value) = definition
                    .split_once(&marker)
                    .ok_or_else(|| format!("Missing glyph {name}"))?;
                let (_, value) = value.split_once("<string>").ok_or("Invalid glyph number")?;
                let (value, _) = value
                    .split_once("</string>")
                    .ok_or("Invalid glyph number")?;
                value.parse().map_err(|_| "Invalid glyph number".into())
            };
            let unsigned = |name: &str| -> Result<u32> {
                u32::try_from(number(name)?).map_err(|_| "Negative glyph dimension".into())
            };
            glyphs.insert(
                unsigned("id")?,
                Glyph {
                    x: unsigned("x")?,
                    y: unsigned("y")?,
                    width: unsigned("width")?,
                    height: unsigned("height")?,
                    xadvance: number("xadvance")?,
                    xoffset: number("xoffset")?,
                    yoffset: number("yoffset")?,
                },
            );
        }
        let atlas = decode(&assets.join("fonts/doodlejumpfont_X.png"))?;
        for glyph in glyphs.values() {
            if glyph
                .x
                .checked_add(glyph.width)
                .is_none_or(|x| x > atlas.width)
                || glyph
                    .y
                    .checked_add(glyph.height)
                    .is_none_or(|y| y > atlas.height)
            {
                return Err("BMFont glyph exceeds its atlas".into());
            }
        }
        Ok(Self { glyphs, atlas })
    }

    fn draw(
        &self,
        image: &mut Image,
        text: &str,
        scale: f64,
        x: f64,
        baseline: f64,
        align: f64,
    ) -> Result<()> {
        let cap = self
            .glyphs
            .get(&u32::from('H'))
            .ok_or("BMFont has no capital H")?;
        let glyphs: Vec<&Glyph> = text
            .chars()
            .filter_map(|c| self.glyphs.get(&u32::from(c)))
            .collect();
        let mut pen =
            x - glyphs.iter().map(|g| i64::from(g.xadvance)).sum::<i64>() as f64 * scale * align;
        for glyph in glyphs {
            let width = (f64::from(glyph.width) * scale).round_ties_even().max(1.0);
            let height = (f64::from(glyph.height) * scale).round_ties_even().max(1.0);
            if width > 4096.0 || height > 4096.0 {
                return Err("Generated glyph exceeds its image limit".into());
            }
            let (width, height) = (width as usize, height as usize);
            let mut alpha = Vec::with_capacity(glyph.width as usize * glyph.height as usize);
            for y in 0..glyph.height {
                for x in 0..glyph.width {
                    alpha.push(
                        self.atlas.pixels
                            [((glyph.y + y) * self.atlas.width + glyph.x + x) as usize * 4 + 3],
                    );
                }
            }
            let alpha = resize_alpha(
                &alpha,
                glyph.width as usize,
                glyph.height as usize,
                width,
                height,
            );
            let left = (pen + f64::from(glyph.xoffset) * scale).round_ties_even() as i64;
            let top = (baseline
                - (i64::from(cap.yoffset) + i64::from(cap.height) - i64::from(glyph.yoffset))
                    as f64
                    * scale)
                .round_ties_even() as i64;
            for y in 0..height {
                for x in 0..width {
                    let px = left.saturating_add(x as i64);
                    let py = top.saturating_add(y as i64);
                    if px < 0
                        || py < 0
                        || px >= i64::from(image.width)
                        || py >= i64::from(image.height)
                    {
                        continue;
                    }
                    let sa = u32::from(alpha[y * width + x]);
                    if sa == 0 {
                        continue;
                    }
                    let index = (py as usize * image.width as usize + px as usize) * 4;
                    let dst = &mut image.pixels[index..index + 4];
                    let blend = u32::from(dst[3]) * (255 - sa);
                    let out = sa * 255 + blend;
                    let keep = 255 * 128 - sa * 255 * 255 * 128 / out;
                    for channel in dst.iter_mut().take(3) {
                        let value = u32::from(*channel) * keep + (128 << 7);
                        *channel = ((((value >> 8) + value) >> 8) >> 7) as u8;
                    }
                    let value = out + 128;
                    dst[3] = (((value >> 8) + value) >> 8) as u8;
                }
            }
            pen += f64::from(glyph.xadvance) * scale;
        }
        Ok(())
    }
}

thread_local! { static FONT: RefCell<Option<(PathBuf, Font)>> = const { RefCell::new(None) }; }

#[expect(
    clippy::too_many_arguments,
    reason = "Matches the original text request fields"
)]
pub fn text_texture(
    assets: &Path,
    width: u32,
    height: u32,
    tm: f64,
    message: i32,
    texts: &[String],
    local_names: &[String],
    recent_names: &[String],
) -> Result<Image> {
    if !tm.is_finite() || tm <= 0.0 {
        return Err("Invalid text texture multiplier".into());
    }
    let mut image = Image::blank(width, height)?;
    FONT.with(|cached| -> Result<()> {
        let mut cached = cached.borrow_mut();
        if cached.as_ref().is_none_or(|(path, _)| path != assets) {
            *cached = Some((assets.to_path_buf(), Font::load(assets)?));
        }
        let Some((_, font)) = cached.as_ref() else {
            return Err("Font cache is empty".into());
        };
        let cap = font
            .glyphs
            .get(&u32::from('H'))
            .ok_or("BMFont has no capital H")?;
        if cap.height == 0 {
            return Err("BMFont capital height is zero".into());
        }
        let size = 12.0 * tm;
        let scale = size * 0.72 / f64::from(cap.height);
        if message == 63 {
            for (i, value) in texts.iter().enumerate() {
                font.draw(
                    &mut image,
                    value,
                    scale,
                    128.0 * tm,
                    tm * (i as f64 * 50.0 + 25.0) + size * 0.72 / 2.0,
                    0.5,
                )?;
            }
        } else {
            for names in [local_names, recent_names] {
                for (i, value) in names.iter().enumerate() {
                    let top =
                        (f64::from(height) - tm * (i as f64 * 24.0 + 7.0)).round_ties_even() as i64;
                    let bottom = (f64::from(height) - tm * (i as f64 * 24.0 + 3.0))
                        .round_ties_even() as i64
                        - 1;
                    let right = (tm * 64.0).round_ties_even() as i64;
                    for y in top.max(0)..=bottom.min(i64::from(height) - 1) {
                        for x in 0..=right.min(i64::from(width) - 1) {
                            let index = (y as usize * width as usize + x as usize) * 4;
                            image.pixels[index..index + 4].copy_from_slice(&[0, 85, 170, 255]);
                        }
                    }
                    let value: String = value.chars().take(8).collect();
                    font.draw(
                        &mut image,
                        &value,
                        scale,
                        tm * 62.0,
                        f64::from(height) - tm * (i as f64 * 24.0 + 7.0),
                        1.0,
                    )?;
                }
            }
            let row = width as usize * 4;
            for y in 0..height as usize / 2 {
                let other = height as usize - y - 1;
                for x in 0..row {
                    image.pixels.swap(y * row + x, other * row + x);
                }
            }
        }
        Ok(())
    })?;
    Ok(image)
}

pub fn scenery_assets(assets: &Path, cursor: &Path) -> Result<(Vec<(u32, Image)>, Value)> {
    let atlas = decode(&assets.join("gametiles_L.png"))?;
    let mut images = Vec::new();
    let mut metadata = serde_json::Map::new();
    for (name, identifier, (left, top, right, bottom)) in [
        ("platform", 10, (0, 0, 90, 24)),
        ("broken", 11, (0, 107, 90, 138)),
        ("bat", 12, (223, 0, 340, 73)),
        ("ufo", 13, (642, 309, 769, 362)),
        ("monster", 14, (101, 277, 216, 358)),
        ("springs", 15, (298, 396, 334, 449)),
    ] {
        let image = atlas
            .crop(left, top, right - left, bottom - top)?
            .premultiplied();
        metadata.insert(
            name.into(),
            json!({"id":identifier,"width":image.width,"height":image.height}),
        );
        images.push((identifier, image));
    }
    let image = decode(cursor)?.premultiplied();
    let reader = png::Decoder::new(fs::File::open(cursor).map_err(|e| e.to_string())?)
        .read_info()
        .map_err(|e| e.to_string())?;
    let hotspot = |name: &str| -> Result<i32> {
        reader
            .info()
            .uncompressed_latin1_text
            .iter()
            .find(|entry| entry.keyword == name)
            .ok_or_else(|| format!("Cursor has no {name}"))?
            .text
            .parse()
            .map_err(|_| format!("Invalid cursor {name}"))
    };
    metadata.insert(
        "cursor".into(),
        json!({"id":20,"width":image.width,"height":image.height,
        "hotspotX":hotspot("hotspot_x")?,"hotspotY":hotspot("hotspot_y")?}),
    );
    images.push((20, image));
    Ok((images, Value::Object(metadata)))
}

#[derive(PartialEq, Eq)]
struct PaperKey {
    identifier: u32,
    revision: u64,
    asset: String,
    tint: [u64; 3],
    flip_x: bool,
    flip_y: bool,
}
#[derive(Default)]
pub struct Paper {
    key: Option<PaperKey>,
}

impl Paper {
    pub fn update(
        &mut self,
        texture: &Image,
        content: (u32, u32),
        vertices: &[[f64; 8]],
        identifier: u32,
        revision: u64,
        asset: &str,
    ) -> Result<(Value, Option<Image>)> {
        let (width, height) = content;
        if width == 0 || height == 0 || width > 2046 || height > 2046 || vertices.len() < 3 {
            return Err("Invalid paper dimensions".into());
        }
        if !vertices.iter().flatten().all(|n| n.is_finite()) {
            return Err("Invalid paper projection".into());
        }
        let left = vertices.iter().map(|v| v[0]).fold(f64::INFINITY, f64::min);
        let top = vertices.iter().map(|v| v[1]).fold(f64::INFINITY, f64::min);
        let right = vertices
            .iter()
            .map(|v| v[0])
            .fold(f64::NEG_INFINITY, f64::max);
        let bottom = vertices
            .iter()
            .map(|v| v[1])
            .fold(f64::NEG_INFINITY, f64::max);
        if right <= left || bottom <= top {
            return Err("Invalid paper geometry".into());
        }
        let corner = |x: f64, y: f64| -> Result<&[f64; 8]> {
            vertices
                .iter()
                .min_by(|a, b| {
                    ((a[0] - x).powi(2) + (a[1] - y).powi(2))
                        .total_cmp(&((b[0] - x).powi(2) + (b[1] - y).powi(2)))
                })
                .ok_or_else(|| "Paper has no vertices".into())
        };
        let (tl, tr, bl) = (
            corner(left, top)?,
            corner(right, top)?,
            corner(left, bottom)?,
        );
        let du = (tr[2] - tl[2]) / (right - left) * f64::from(texture.width) / f64::from(width);
        let dv = (bl[3] - tl[3]) / (bottom - top) * f64::from(texture.height) / f64::from(height);
        let u = tl[2] * f64::from(texture.width) / f64::from(width) - left * du;
        let v = tl[3] * f64::from(texture.height) / f64::from(height) - top * dv;
        if ![u, v, du, dv].iter().all(|n| n.is_finite()) || du == 0.0 || dv == 0.0 {
            return Err("Invalid paper projection".into());
        }
        let phase = |x: f64| {
            let x = x.rem_euclid(1.0);
            if x == 0.0 { 0.0 } else { x }
        };
        let projection = json!({"width":width,"height":height,"u":phase(if du>0.0 {u}else{-u}),
            "v":phase(if dv>0.0 {v}else{-v}),"du":du.abs(),"dv":dv.abs(),"alpha":tl[7].clamp(0.0,1.0)});
        let key = PaperKey {
            identifier,
            revision,
            asset: asset.into(),
            tint: [tl[4].to_bits(), tl[5].to_bits(), tl[6].to_bits()],
            flip_x: du < 0.0,
            flip_y: dv < 0.0,
        };
        if self.key.as_ref() == Some(&key) {
            return Ok((projection, None));
        }
        let mut image = Image::blank(width + 2, height + 2)?;
        let source = texture.crop(0, 0, width, height)?;
        for y in 0..height + 2 {
            for x in 0..width + 2 {
                let sx = (x + width - 1) % width;
                let sy = (y + height - 1) % height;
                let sx = if key.flip_x { width - sx - 1 } else { sx };
                let sy = if key.flip_y { height - sy - 1 } else { sy };
                let target = (y * (width + 2) + x) as usize * 4;
                let origin = (sy * width + sx) as usize * 4;
                image.pixels[target..target + 4]
                    .copy_from_slice(&source.pixels[origin..origin + 4]);
                for channel in 0..3 {
                    image.pixels[target + channel] =
                        (f64::from(image.pixels[target + channel]) * tl[4 + channel] + 0.5)
                            .clamp(0.0, 255.0) as u8;
                }
                premultiply(&mut image.pixels[target..target + 4]);
            }
        }
        self.key = Some(key);
        Ok((projection, Some(image)))
    }
}
#[cfg(test)]
mod tests {
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
            let values =
                |key: &str| serde_json::from_value::<Vec<String>>(entry[key].clone()).unwrap();
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
            let vertices: Vec<[f64; 8]> =
                serde_json::from_value(entry["vertices"].clone()).unwrap();
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
}

#[cfg(test)]
mod parity_tests {
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
}
