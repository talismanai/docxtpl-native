//! Pictures as Pillow 12 sees them: format, size and resolution
//! (`Image.open`, `image.info["dpi"]`), whether Pillow can write them as PNG,
//! decoding to pixels, PNG encoding and `Image.rotate(angle, expand=True)`.
//!
//! PNG, JPEG, GIF, BMP, WebP and TIFF are read. Animated GIF and WebP decode
//! to their first frame, as `Image.open` followed by a single-frame save.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format { Png, Jpeg, Gif, Bmp, Webp, Tiff }

#[derive(Debug, Clone, PartialEq)]
pub struct Info {
    pub format: Format,
    pub width: u32,
    pub height: u32,
    /// Pillow `image.info["dpi"]`, when the format records one.
    pub dpi: Option<(f64, f64)>,
    /// Pillow can save the picture's mode as PNG (not CMYK, LAB, RGBX or
    /// floating point).
    pub png_writable: bool,
}

fn be16(b: &[u8], at: usize) -> Option<u16> { b.get(at..at + 2).map(|s| u16::from_be_bytes([s[0], s[1]])) }
fn be32(b: &[u8], at: usize) -> Option<u32> { b.get(at..at + 4).map(|s| u32::from_be_bytes([s[0], s[1], s[2], s[3]])) }
fn le16(b: &[u8], at: usize) -> Option<u16> { b.get(at..at + 2).map(|s| u16::from_le_bytes([s[0], s[1]])) }
fn le32(b: &[u8], at: usize) -> Option<u32> { b.get(at..at + 4).map(|s| u32::from_le_bytes([s[0], s[1], s[2], s[3]])) }

/// Identify a picture as Pillow would open it, or `None` if it cannot.
pub fn inspect(bytes: &[u8]) -> Option<Info> {
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") { return png_info(bytes); }
    if bytes.starts_with(&[0xff, 0xd8]) { return jpeg_info(bytes); }
    if bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a") {
        return Some(Info { format: Format::Gif, width: u32::from(le16(bytes, 6)?), height: u32::from(le16(bytes, 8)?), dpi: None, png_writable: true });
    }
    if bytes.starts_with(b"BM") { return bmp_info(bytes); }
    if bytes.starts_with(b"RIFF") && bytes.get(8..12) == Some(b"WEBP") { return webp_info(bytes); }
    if bytes.starts_with(b"II*\0") || bytes.starts_with(b"MM\0*") { return tiff_info(bytes); }
    None
}

fn png_info(bytes: &[u8]) -> Option<Info> {
    let width = be32(bytes, 16)?;
    let height = be32(bytes, 20)?;
    let mut dpi = None;
    let mut at = 8;
    while at + 8 <= bytes.len() {
        let length = be32(bytes, at)? as usize;
        let kind = bytes.get(at + 4..at + 8)?;
        if kind == b"IDAT" || kind == b"IEND" { break; }
        if kind == b"pHYs" && length >= 9 {
            let data = bytes.get(at + 8..at + 8 + length)?;
            if data[8] == 1 {
                dpi = Some((f64::from(be32(data, 0)?) * 0.0254, f64::from(be32(data, 4)?) * 0.0254));
            }
        }
        at += 12 + length;
    }
    Some(Info { format: Format::Png, width, height, dpi, png_writable: true })
}

fn bmp_info(bytes: &[u8]) -> Option<Info> {
    let header = le32(bytes, 14)?;
    let (width, height, dpi) = if header == 12 {
        (u32::from(le16(bytes, 18)?), u32::from(le16(bytes, 20)?), None)
    } else {
        let width = le32(bytes, 18)? as i32;
        let height = le32(bytes, 22)? as i32;
        // `BmpImageFile._bitmap`: signed pixels per meter, set even when zero.
        let ppm = |at: usize| le32(bytes, at).map(|v| f64::from(v as i32) / 39.3701);
        (width.unsigned_abs(), height.unsigned_abs(), Some((ppm(38)?, ppm(42)?)))
    };
    Some(Info { format: Format::Bmp, width, height, dpi, png_writable: true })
}

/// `WebPImageFile`: canvas size of the first chunk; no resolution.
fn webp_info(bytes: &[u8]) -> Option<Info> {
    let (width, height) = match bytes.get(12..16)? {
        b"VP8 " => {
            if bytes.get(23..26)? != [0x9d, 0x01, 0x2a] { return None; }
            (u32::from(le16(bytes, 26)? & 0x3fff), u32::from(le16(bytes, 28)? & 0x3fff))
        }
        b"VP8L" => {
            if *bytes.get(20)? != 0x2f { return None; }
            let bits = le32(bytes, 21)?;
            ((bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1)
        }
        b"VP8X" => {
            let u24 = |at: usize| Some(u32::from_le_bytes([*bytes.get(at)?, *bytes.get(at + 1)?, *bytes.get(at + 2)?, 0]));
            (u24(24)? + 1, u24(27)? + 1)
        }
        _ => return None,
    };
    Some(Info { format: Format::Webp, width, height, dpi: None, png_writable: true })
}

/// `TiffImageFile._setup` for the first image: size, `info["dpi"]` and the
/// modes Pillow cannot save as PNG.
fn tiff_info(bytes: &[u8]) -> Option<Info> {
    let little = bytes.starts_with(b"II");
    let u16_at = |at: usize| if little { le16(bytes, at) } else { be16(bytes, at) };
    let u32_at = |at: usize| if little { le32(bytes, at) } else { be32(bytes, at) };
    let ifd = u32_at(4)? as usize;
    let count = usize::from(u16_at(ifd)?);
    let mut tags: std::collections::HashMap<u16, Vec<f64>> = std::collections::HashMap::new();
    for index in 0..count {
        let entry = ifd + 2 + index * 12;
        let (tag, kind, n) = (u16_at(entry)?, u16_at(entry + 2)?, u32_at(entry + 4)? as usize);
        let size = match kind { 3 => 2, 4 => 4, 5 => 8, 1 => 1, _ => continue };
        let base = if size * n <= 4 { entry + 8 } else { u32_at(entry + 8)? as usize };
        let mut values = Vec::with_capacity(n.min(16));
        for i in 0..n.min(16) {
            let at = base + i * size;
            values.push(match kind {
                1 => f64::from(*bytes.get(at)?),
                3 => f64::from(u16_at(at)?),
                4 => f64::from(u32_at(at)?),
                _ => {
                    let (numerator, denominator) = (u32_at(at)?, u32_at(at + 4)?);
                    if denominator == 0 { f64::NAN } else { f64::from(numerator) / f64::from(denominator) }
                }
            });
        }
        tags.insert(tag, values);
    }
    let first = |tag: u16| tags.get(&tag).and_then(|v| v.first().copied());
    let width = first(256)? as u32;
    let height = first(257)? as u32;
    let (xres, yres) = (first(282).unwrap_or(1.0), first(283).unwrap_or(1.0));
    let dpi = if xres != 0.0 && yres != 0.0 && !xres.is_nan() && !yres.is_nan() {
        match first(296) {
            Some(u) if u == 2.0 => Some((xres, yres)),
            Some(u) if u == 3.0 => Some((xres * 2.54, yres * 2.54)),
            None => Some((xres, yres)),
            Some(_) => None,
        }
    } else { None };
    let photometric = first(262).unwrap_or(1.0) as u32;
    let samples = first(277).unwrap_or(1.0) as u32;
    let float = first(339) == Some(3.0);
    let extra = first(338);
    let compression = first(259).unwrap_or(1.0) as u32;
    let png_writable = !float
        && !matches!(photometric, 5 | 8 | 9 | 10)
        && !(photometric == 6 && compression != 7)
        && !(photometric == 2 && samples == 4 && extra.unwrap_or(0.0) == 0.0);
    Some(Info { format: Format::Tiff, width, height, dpi, png_writable })
}

fn jpeg_info(bytes: &[u8]) -> Option<Info> {
    let mut at = 2;
    let mut dpi = None;
    let mut exif: Option<&[u8]> = None;
    let mut size = None;
    let mut components = 0;
    while at + 4 <= bytes.len() {
        if bytes[at] != 0xff { at += 1; continue; }
        let marker = bytes[at + 1];
        if marker == 0xff { at += 1; continue; }
        if marker == 0xd8 || (0xd0..=0xd7).contains(&marker) || marker == 0x01 { at += 2; continue; }
        if marker == 0xd9 || marker == 0xda { break; }
        let length = usize::from(be16(bytes, at + 2)?);
        let segment = bytes.get(at + 4..at + 2 + length)?;
        match marker {
            0xe0 if segment.starts_with(b"JFIF") && segment.len() >= 12 => {
                let unit = segment[7];
                let density = (f64::from(be16(segment, 8)?), f64::from(be16(segment, 10)?));
                if unit == 1 { dpi = Some(density); }
                else if unit == 2 { dpi = Some((density.0 * 2.54, density.1 * 2.54)); }
            }
            0xe1 if segment.starts_with(b"Exif\0\0") => if exif.is_none() { exif = Some(&segment[6..]) },
            0xc0..=0xcf if !matches!(marker, 0xc4 | 0xc8 | 0xcc) && size.is_none() => {
                size = Some((u32::from(be16(segment, 3)?), u32::from(be16(segment, 1)?)));
                components = segment.get(5).copied().unwrap_or(0);
            }
            _ => {}
        }
        at += 2 + length;
    }
    let (width, height) = size?;
    if dpi.is_none() { if let Some(exif) = exif { dpi = Some(exif_dpi(exif)); } }
    Some(Info { format: Format::Jpeg, width, height, dpi, png_writable: components != 4 })
}

/// `JpegImageFile._read_dpi_from_exif`: IFD0 XResolution with ResolutionUnit,
/// 72 dpi when either is missing or invalid.
fn exif_dpi(tiff: &[u8]) -> (f64, f64) {
    let read = || -> Option<f64> {
        let little = match tiff.get(..2)? { b"II" => true, b"MM" => false, _ => return None };
        let u16_at = |at: usize| if little { le16(tiff, at) } else { be16(tiff, at) };
        let u32_at = |at: usize| if little { le32(tiff, at) } else { be32(tiff, at) };
        let ifd = u32_at(4)? as usize;
        let count = usize::from(u16_at(ifd)?);
        let (mut unit, mut resolution) = (None, None);
        for index in 0..count {
            let entry = ifd + 2 + index * 12;
            let tag = u16_at(entry)?;
            let kind = u16_at(entry + 2)?;
            match tag {
                0x0128 => unit = Some(if kind == 3 { u32::from(u16_at(entry + 8)?) } else { u32_at(entry + 8)? }),
                0x011a if kind == 5 => {
                    let offset = u32_at(entry + 8)? as usize;
                    let (numerator, denominator) = (u32_at(offset)?, u32_at(offset + 4)?);
                    resolution = Some(if denominator == 0 { f64::NAN } else { f64::from(numerator) / f64::from(denominator) });
                }
                _ => {}
            }
        }
        let mut dpi = resolution?;
        if dpi.is_nan() { return None; }
        if unit? == 3 { dpi *= 2.54; }
        Some(dpi)
    };
    read().map_or((72.0, 72.0), |dpi| (dpi, dpi))
}

/// Decoded pixels: `channels` interleaved 8-bit samples per pixel.
pub struct Pixels {
    pub width: u32,
    pub height: u32,
    pub channels: u8,
    pub data: Vec<u8>,
}

pub fn decode(bytes: &[u8]) -> Result<Pixels, String> {
    match inspect(bytes).map(|i| i.format) {
        Some(Format::Png) => {
            let mut decoder = png::Decoder::new(std::io::Cursor::new(bytes));
            decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
            let mut reader = decoder.read_info().map_err(|e| e.to_string())?;
            let mut data = vec![0; reader.output_buffer_size().ok_or("PNG too large")?];
            let frame = reader.next_frame(&mut data).map_err(|e| e.to_string())?;
            data.truncate(frame.buffer_size());
            let channels = match frame.color_type { png::ColorType::Grayscale => 1, png::ColorType::GrayscaleAlpha => 2, png::ColorType::Rgb => 3, _ => 4 };
            Ok(Pixels { width: frame.width, height: frame.height, channels, data })
        }
        Some(Format::Jpeg) => {
            let mut decoder = zune_jpeg::JpegDecoder::new(zune_core::bytestream::ZCursor::new(bytes));
            let data = decoder.decode().map_err(|e| format!("{e:?}"))?;
            let (width, height) = decoder.dimensions().ok_or("JPEG without dimensions")?;
            let channels = decoder.output_colorspace().map_or(3, |c| c.num_components()) as u8;
            Ok(Pixels { width: width as u32, height: height as u32, channels, data })
        }
        Some(format @ (Format::Gif | Format::Bmp | Format::Webp | Format::Tiff)) => {
            let format = match format {
                Format::Gif => image::ImageFormat::Gif,
                Format::Bmp => image::ImageFormat::Bmp,
                Format::Webp => image::ImageFormat::WebP,
                _ => image::ImageFormat::Tiff,
            };
            let picture = match image::load_from_memory_with_format(bytes, format) {
                Ok(picture) => picture,
                // Palette and gray-with-alpha TIFFs, which Pillow opens as P and LA.
                Err(error) if format == image::ImageFormat::Tiff => return decode_tiff(bytes).ok_or_else(|| error.to_string()),
                Err(error) => return Err(error.to_string()),
            };
            let (width, height) = (picture.width(), picture.height());
            // 16-bit samples keep their high byte, as Pillow's `;16` unpackers do.
            let high = |samples: &[u16]| samples.iter().map(|v| (v >> 8) as u8).collect::<Vec<u8>>();
            let (channels, data) = match &picture {
                image::DynamicImage::ImageLuma16(p) => (1, high(p.as_raw())),
                image::DynamicImage::ImageLumaA16(p) => (2, high(p.as_raw())),
                image::DynamicImage::ImageRgb16(p) => (3, high(p.as_raw())),
                image::DynamicImage::ImageRgba16(p) => (4, high(p.as_raw())),
                _ if picture.color().has_alpha() => (4, picture.into_rgba8().into_raw()),
                _ => (3, picture.into_rgb8().into_raw()),
            };
            Ok(Pixels { width, height, channels, data })
        }
        None => Err("unrecognized picture format".into()),
    }
}

/// 8-bit palette (ColorMap entries scaled `// 256`, as Pillow does) and 8-bit
/// gray with alpha TIFF pictures, which the `tiff` crate does not decode.
fn decode_tiff(bytes: &[u8]) -> Option<Pixels> {
    use tiff::decoder::{Decoder, DecodingResult};
    let mut decoder = Decoder::new(std::io::Cursor::new(bytes)).ok()?;
    let (width, height) = decoder.dimensions().ok()?;
    match decoder.colortype() {
        Ok(tiff::ColorType::GrayA(8) | tiff::ColorType::Multiband { bit_depth: 8, num_samples: 2 }) => {
            let DecodingResult::U8(data) = decoder.read_image().ok()? else { return None };
            Some(Pixels { width, height, channels: 2, data })
        }
        _ => {
            // Palette: read the indices as gray samples, then map them.
            let map = decoder.get_tag_u16_vec(tiff::tags::Tag::ColorMap).ok()?;
            if map.len() != 768 { return None; }
            let gray = with_photometric(bytes, 1)?;
            let mut decoder = Decoder::new(std::io::Cursor::new(&gray)).ok()?;
            if decoder.colortype().ok()? != tiff::ColorType::Gray(8) { return None; }
            let DecodingResult::U8(indices) = decoder.read_image().ok()? else { return None };
            let mut data = Vec::with_capacity(indices.len() * 3);
            for &index in &indices {
                let i = usize::from(index);
                data.extend_from_slice(&[(map[i] >> 8) as u8, (map[256 + i] >> 8) as u8, (map[512 + i] >> 8) as u8]);
            }
            Some(Pixels { width, height, channels: 3, data })
        }
    }
}

/// A copy of a TIFF whose first image declares another PhotometricInterpretation.
fn with_photometric(bytes: &[u8], value: u16) -> Option<Vec<u8>> {
    let little = bytes.starts_with(b"II");
    let u16_at = |at: usize| if little { le16(bytes, at) } else { be16(bytes, at) };
    let u32_at = |at: usize| if little { le32(bytes, at) } else { be32(bytes, at) };
    let ifd = u32_at(4)? as usize;
    for index in 0..usize::from(u16_at(ifd)?) {
        let entry = ifd + 2 + index * 12;
        if u16_at(entry)? == 262 && u16_at(entry + 2)? == 3 {
            let mut copy = bytes.to_vec();
            let encoded = if little { value.to_le_bytes() } else { value.to_be_bytes() };
            copy.get_mut(entry + 8..entry + 10)?.copy_from_slice(&encoded);
            return Some(copy);
        }
    }
    None
}

pub fn encode_png(pixels: &Pixels) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    let mut encoder = png::Encoder::new(&mut out, pixels.width, pixels.height);
    encoder.set_color(match pixels.channels { 1 => png::ColorType::Grayscale, 2 => png::ColorType::GrayscaleAlpha, 3 => png::ColorType::Rgb, _ => png::ColorType::Rgba });
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder.write_header().map_err(|e| e.to_string())?;
    writer.write_image_data(&pixels.data).map_err(|e| e.to_string())?;
    writer.finish().map_err(|e| e.to_string())?;
    Ok(out)
}

/// Pillow `Image.rotate(angle, expand=True)` with nearest-neighbor sampling
/// and black (transparent) fill.
pub fn rotate(pixels: &Pixels, degrees: f64) -> Pixels {
    let angle = degrees.rem_euclid(360.0);
    let (w, h, c) = (pixels.width as usize, pixels.height as usize, pixels.channels as usize);
    let at = |x: usize, y: usize| &pixels.data[(y * w + x) * c..(y * w + x + 1) * c];
    let transpose = |nw: usize, nh: usize, map: &dyn Fn(usize, usize) -> (usize, usize)| {
        let mut data = Vec::with_capacity(nw * nh * c);
        for y in 0..nh { for x in 0..nw { let (sx, sy) = map(x, y); data.extend_from_slice(at(sx, sy)); } }
        Pixels { width: nw as u32, height: nh as u32, channels: pixels.channels, data }
    };
    if angle == 0.0 { return transpose(w, h, &|x, y| (x, y)); }
    if angle == 180.0 { return transpose(w, h, &|x, y| (w - 1 - x, h - 1 - y)); }
    if angle == 90.0 { return transpose(h, w, &|x, y| (w - 1 - y, x)); }
    if angle == 270.0 { return transpose(h, w, &|x, y| (y, h - 1 - x)); }
    let radians = -angle.to_radians();
    let round15 = |v: f64| (v * 1e15).round() / 1e15;
    let mut m = [round15(radians.cos()), round15(radians.sin()), 0.0, round15(-radians.sin()), round15(radians.cos()), 0.0];
    let apply = |m: &[f64; 6], x: f64, y: f64| (m[0] * x + m[1] * y + m[2], m[3] * x + m[4] * y + m[5]);
    let (cx, cy) = (w as f64 / 2.0, h as f64 / 2.0);
    let (tx, ty) = apply(&m, -cx, -cy);
    m[2] = tx + cx;
    m[5] = ty + cy;
    let corners = [(0.0, 0.0), (w as f64, 0.0), (w as f64, h as f64), (0.0, h as f64)].map(|(x, y)| apply(&m, x, y));
    let max_x = corners.iter().map(|p| p.0).fold(f64::MIN, f64::max);
    let min_x = corners.iter().map(|p| p.0).fold(f64::MAX, f64::min);
    let max_y = corners.iter().map(|p| p.1).fold(f64::MIN, f64::max);
    let min_y = corners.iter().map(|p| p.1).fold(f64::MAX, f64::min);
    let nw = (max_x.ceil() - min_x.floor()) as usize;
    let nh = (max_y.ceil() - min_y.floor()) as usize;
    let (tx, ty) = apply(&m, -((nw as f64) - w as f64) / 2.0, -((nh as f64) - h as f64) / 2.0);
    m[2] = tx;
    m[5] = ty;
    // ImagingTransformAffine, nearest filter: 16.16 fixed point from pixel centers.
    let fix = |v: f64| (v * 65536.0 + 0.5).floor() as i64;
    let (a0, a3) = (fix(m[0]), fix(m[3]));
    let mut xo = m[2] + m[1] * 0.5 + m[0] * 0.5;
    let mut yo = m[5] + m[4] * 0.5 + m[3] * 0.5;
    let mut data = vec![0u8; nw * nh * c];
    for y in 0..nh {
        let (mut xx, mut yy) = (fix(xo), fix(yo));
        for x in 0..nw {
            let (sx, sy) = (xx >> 16, yy >> 16);
            if sx >= 0 && (sx as usize) < w && sy >= 0 && (sy as usize) < h {
                data[(y * nw + x) * c..(y * nw + x + 1) * c].copy_from_slice(at(sx as usize, sy as usize));
            }
            xx += a0;
            yy += a3;
        }
        xo += m[1];
        yo += m[4];
    }
    Pixels { width: nw as u32, height: nh as u32, channels: pixels.channels, data }
}
