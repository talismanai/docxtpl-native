//! Image header parsing over caller-owned seek/read protocols.
//!
//! This parser consumes the bytes returned by each read; it does not infer
//! stream effects from an earlier snapshot. The callback image binding uses
//! it for file-like sources. The traversal follows python-docx's image readers
//! (MIT; see vendor/notices) for the supported formats.

use docxtplrs::image::ImageInfo;

/// A seekable source whose errors remain owned by the caller.
pub trait ImageStream {
    type Error;

    fn seek(&mut self, offset: u64) -> Result<(), Self::Error>;
    fn read(&mut self, count: usize) -> Result<Vec<u8>, Self::Error>;
    fn tell(&mut self) -> Result<u64, Self::Error>;
}

/// An error from the source protocol or from interpreting its bytes.
#[derive(Debug, PartialEq, Eq)]
pub enum ImageStreamError<E> {
    Source(E),
    UnexpectedEnd,
    Unrecognized,
    Invalid(&'static str),
}

fn read_at<S: ImageStream>(stream: &mut S, offset: u64, count: usize) -> Result<Vec<u8>, ImageStreamError<S::Error>> {
    stream.seek(offset).map_err(ImageStreamError::Source)?;
    let bytes = stream.read(count).map_err(ImageStreamError::Source)?;
    if bytes.len() != count { return Err(ImageStreamError::UnexpectedEnd); }
    Ok(bytes)
}

fn u32_at<S: ImageStream>(stream: &mut S, offset: u64, big_endian: bool) -> Result<u32, ImageStreamError<S::Error>> {
    let bytes = read_at(stream, offset, 4)?;
    let bytes = [bytes[0], bytes[1], bytes[2], bytes[3]];
    Ok(if big_endian { u32::from_be_bytes(bytes) } else { u32::from_le_bytes(bytes) })
}

fn u16_at<S: ImageStream>(stream: &mut S, offset: u64, big_endian: bool) -> Result<u16, ImageStreamError<S::Error>> {
    let bytes = read_at(stream, offset, 2)?;
    Ok(if big_endian { u16::from_be_bytes([bytes[0], bytes[1]]) } else { u16::from_le_bytes([bytes[0], bytes[1]]) })
}

/// Parse a header by executing the source's seek/read operations. The caller
/// performs the earlier whole-blob read; this starts with the 32-byte probe.
pub fn parse_header<S: ImageStream>(stream: &mut S) -> Result<ImageInfo, ImageStreamError<S::Error>> {
    stream.seek(0).map_err(ImageStreamError::Source)?;
    let header = stream.read(32).map_err(ImageStreamError::Source)?;
    if header.starts_with(b"\x89PNG\r\n\x1a\n") { return png(stream); }
    if header.get(6..10) == Some(b"JFIF") { return jpeg(stream, false); }
    if header.get(6..10) == Some(b"Exif") { return jpeg(stream, true); }
    if header.starts_with(b"GIF87a") || header.starts_with(b"GIF89a") { return gif(stream); }
    if header.starts_with(b"II*\0") || header.starts_with(b"MM\0*") { return tiff(stream); }
    if header.starts_with(b"BM") { return bmp(stream); }
    Err(ImageStreamError::Unrecognized)
}

fn png<S: ImageStream>(stream: &mut S) -> Result<ImageInfo, ImageStreamError<S::Error>> {
    let mut offset = 8u64;
    let mut dimensions = None;
    let mut resolution = None;
    loop {
        let length = u64::from(u32_at(stream, offset, true)?);
        let kind = read_at(stream, offset.checked_add(4).ok_or(ImageStreamError::Invalid("PNG offset overflow"))?, 4)?;
        std::str::from_utf8(&kind).map_err(|_| ImageStreamError::Invalid("invalid PNG chunk type"))?;
        let data = offset.checked_add(8).ok_or(ImageStreamError::Invalid("PNG offset overflow"))?;
        if kind == b"IHDR" {
            let width = u32_at(stream, data, true)?;
            let height = u32_at(stream, data.checked_add(4).ok_or(ImageStreamError::Invalid("PNG offset overflow"))?, true)?;
            if dimensions.is_none() { dimensions = Some((width, height)); }
        } else if kind == b"pHYs" {
            let x = u32_at(stream, data, true)?;
            let y = u32_at(stream, data.checked_add(4).ok_or(ImageStreamError::Invalid("PNG offset overflow"))?, true)?;
            let unit = read_at(stream, data.checked_add(8).ok_or(ImageStreamError::Invalid("PNG offset overflow"))?, 1)?[0];
            if resolution.is_none() { resolution = Some((x, y, unit)); }
        }
        if kind == b"IEND" { break; }
        offset = offset.checked_add(length).and_then(|v| v.checked_add(12))
            .ok_or(ImageStreamError::Invalid("PNG offset overflow"))?;
    }
    let (px_width, px_height) = dimensions.ok_or(ImageStreamError::Invalid("PNG has no IHDR"))?;
    let dpi = |value: u32, unit: u8| if unit == 1 && value != 0 {
        (f64::from(value) * 0.0254).round_ties_even() as u32
    } else { 72 };
    let (horz_dpi, vert_dpi) = resolution.map_or((72, 72), |(x, y, unit)| (dpi(x, unit), dpi(y, unit)));
    Ok(ImageInfo { px_width, px_height, horz_dpi, vert_dpi, content_type: "image/png", default_ext: "png" })
}

fn gif<S: ImageStream>(stream: &mut S) -> Result<ImageInfo, ImageStreamError<S::Error>> {
    let bytes = read_at(stream, 6, 4)?;
    Ok(ImageInfo { px_width: u32::from(u16::from_le_bytes([bytes[0], bytes[1]])),
        px_height: u32::from(u16::from_le_bytes([bytes[2], bytes[3]])), horz_dpi: 72, vert_dpi: 72,
        content_type: "image/gif", default_ext: "gif" })
}

fn bmp<S: ImageStream>(stream: &mut S) -> Result<ImageInfo, ImageStreamError<S::Error>> {
    let px_width = u32_at(stream, 0x12, false)?;
    let px_height = u32_at(stream, 0x16, false)?;
    let x = u32_at(stream, 0x26, false)?;
    let y = u32_at(stream, 0x2a, false)?;
    let dpi = |value: u32| if value == 0 { 96 } else { (f64::from(value) * 0.0254).round_ties_even() as u32 };
    Ok(ImageInfo { px_width, px_height, horz_dpi: dpi(x), vert_dpi: dpi(y),
        content_type: "image/bmp", default_ext: "bmp" })
}

fn tiff_data<S: ImageStream>(stream: &mut S) -> Result<(u32, u32, u32, u32), ImageStreamError<S::Error>> {
    stream.seek(0).map_err(ImageStreamError::Source)?;
    let endian = stream.read(2).map_err(ImageStreamError::Source)?;
    let big = endian == b"MM";
    let base = u64::from(u32_at(stream, 4, big)?);
    let count = u16_at(stream, base, big)?;
    let mut width = None;
    let mut height = None;
    let mut xres = None;
    let mut yres = None;
    let mut unit = 2u32;
    for index in 0..u64::from(count) {
        let offset = base.checked_add(2).and_then(|v| v.checked_add(index * 12))
            .ok_or(ImageStreamError::Invalid("TIFF offset overflow"))?;
        let field = u16_at(stream, offset + 2, big)?;
        let tag = u16_at(stream, offset, big)?;
        let length = u32_at(stream, offset + 4, big)?;
        let value_offset = u32_at(stream, offset + 8, big)?;
        let numeric = match field {
            2 => {
                let count = length.checked_sub(1).ok_or(ImageStreamError::Invalid("invalid TIFF ASCII length"))?;
                let bytes = read_at(stream, u64::from(value_offset), count as usize)?;
                std::str::from_utf8(&bytes).map_err(|_| ImageStreamError::Invalid("invalid TIFF ASCII"))?;
                None
            }
            3 if length == 1 => Some(f64::from(u16_at(stream, offset + 8, big)?)),
            4 if length == 1 => Some(f64::from(u32_at(stream, offset + 8, big)?)),
            5 if length == 1 => {
                let numerator = u32_at(stream, u64::from(value_offset), big)?;
                let denominator = u32_at(stream, u64::from(value_offset) + 4, big)?;
                if denominator == 0 { return Err(ImageStreamError::Invalid("zero TIFF denominator")); }
                Some(f64::from(numerator) / f64::from(denominator))
            }
            _ => None,
        };
        if let Some(value) = numeric {
            match tag { 256 => width = Some(value as u32), 257 => height = Some(value as u32),
                282 => xres = Some(value), 283 => yres = Some(value), 296 => unit = value as u32, _ => {} }
        }
    }
    let dpi = |resolution: Option<f64>| match (resolution, unit) {
        (Some(value), 2) => value.round_ties_even() as u32,
        (Some(value), 3) => (value * 2.54).round_ties_even() as u32,
        _ => 72,
    };
    Ok((width.unwrap_or(0), height.unwrap_or(0), dpi(xres), dpi(yres)))
}

fn tiff<S: ImageStream>(stream: &mut S) -> Result<ImageInfo, ImageStreamError<S::Error>> {
    let (px_width, px_height, horz_dpi, vert_dpi) = tiff_data(stream)?;
    Ok(ImageInfo { px_width, px_height, horz_dpi, vert_dpi, content_type: "image/tiff", default_ext: "tiff" })
}

fn jpeg<S: ImageStream>(stream: &mut S, exif: bool) -> Result<ImageInfo, ImageStreamError<S::Error>> {
    let mut start = 0u64;
    let mut dimensions = None;
    let mut app0 = None;
    let mut app1 = None;
    loop {
        let mut position = start;
        let (code, offset) = loop {
            stream.seek(position).map_err(ImageStreamError::Source)?;
            loop {
                let byte = stream.read(1).map_err(ImageStreamError::Source)?;
                if byte.is_empty() { return Err(ImageStreamError::UnexpectedEnd); }
                if byte == [0xff] { break; }
            }
            let found = stream.tell().map_err(ImageStreamError::Source)?.checked_sub(1)
                .ok_or(ImageStreamError::Invalid("invalid JPEG position"))?;
            stream.seek(found + 1).map_err(ImageStreamError::Source)?;
            let code = loop {
                let byte = stream.read(1).map_err(ImageStreamError::Source)?;
                if byte.len() != 1 { return Err(ImageStreamError::UnexpectedEnd); }
                if byte[0] != 0xff { break byte[0]; }
            };
            position = stream.tell().map_err(ImageStreamError::Source)?.checked_sub(1)
                .ok_or(ImageStreamError::Invalid("invalid JPEG position"))?;
            if code != 0 { break (code, position + 1); }
        };
        let standalone = code == 0x01 || (0xd0..=0xd9).contains(&code);
        let length = if standalone { 0 } else { u64::from(u16_at(stream, offset, true)?) };
        match code {
            0xe0 => {
                let unit = read_at(stream, offset + 9, 1)?[0];
                let x = u16_at(stream, offset + 10, true)?;
                let y = u16_at(stream, offset + 12, true)?;
                if app0.is_none() { app0 = Some((unit, x, y)); }
            }
            0xe1 => {
                stream.seek(offset + 2).map_err(ImageStreamError::Source)?;
                let signature = stream.read(6).map_err(ImageStreamError::Source)?;
                let dpi = if signature == b"Exif\0\0" {
                    stream.seek(offset + 8).map_err(ImageStreamError::Source)?;
                    let size = length.checked_sub(8).ok_or(ImageStreamError::Invalid("invalid EXIF length"))?;
                    let bytes = stream.read(size as usize).map_err(ImageStreamError::Source)?;
                    let mut embedded = MemoryStream { data: bytes, position: 0 };
                    let (_, _, x, y) = tiff_data(&mut embedded).map_err(|_| ImageStreamError::Invalid("invalid EXIF TIFF"))?;
                    (x, y)
                } else { (72, 72) };
                if app1.is_none() { app1 = Some(dpi); }
            }
            0xc0..=0xc3 | 0xc5..=0xc7 | 0xc9..=0xcb | 0xcd..=0xcf => {
                let height = u16_at(stream, offset + 3, true)?;
                let width = u16_at(stream, offset + 5, true)?;
                if dimensions.is_none() { dimensions = Some((u32::from(width), u32::from(height))); }
            }
            _ => {}
        }
        if code == 0xda || code == 0xd9 { break; }
        start = offset.checked_add(length).ok_or(ImageStreamError::Invalid("JPEG offset overflow"))?;
    }
    let (px_width, px_height) = dimensions.ok_or(ImageStreamError::Invalid("JPEG has no SOF"))?;
    let (horz_dpi, vert_dpi) = if exif { app1.ok_or(ImageStreamError::Invalid("JPEG has no APP1"))? }
    else if let Some((unit, x, y)) = app0 {
        let dpi = |value: u16| match unit { 1 => u32::from(value), 2 => (f64::from(value) * 2.54).round_ties_even() as u32, _ => 72 };
        (dpi(x), dpi(y))
    } else { return Err(ImageStreamError::Invalid("JPEG has no APP0")); };
    Ok(ImageInfo { px_width, px_height, horz_dpi, vert_dpi, content_type: "image/jpeg", default_ext: "jpg" })
}

struct MemoryStream { data: Vec<u8>, position: usize }
impl ImageStream for MemoryStream {
    type Error = std::convert::Infallible;
    fn seek(&mut self, offset: u64) -> Result<(), Self::Error> { self.position = offset as usize; Ok(()) }
    fn read(&mut self, count: usize) -> Result<Vec<u8>, Self::Error> {
        let start = self.position.min(self.data.len());
        let end = start.saturating_add(count).min(self.data.len());
        self.position = end;
        Ok(self.data[start..end].to_vec())
    }
    fn tell(&mut self) -> Result<u64, Self::Error> { Ok(self.position as u64) }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Stream { bytes: Vec<u8>, position: usize, calls: Vec<(&'static str, u64)> }
    impl ImageStream for Stream {
        type Error = ();
        fn seek(&mut self, offset: u64) -> Result<(), ()> {
            self.calls.push(("seek", offset));
            self.position = offset as usize;
            Ok(())
        }
        fn read(&mut self, count: usize) -> Result<Vec<u8>, ()> {
            self.calls.push(("read", count as u64));
            let start = self.position.min(self.bytes.len());
            let end = start.saturating_add(count).min(self.bytes.len());
            self.position = end;
            Ok(self.bytes[start..end].to_vec())
        }
        fn tell(&mut self) -> Result<u64, ()> { Ok(self.position as u64) }
    }

    #[test]
    fn png_reads_through_iend_and_uses_stream_values() {
        let mut bytes = b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR\0\0\0\x02\0\0\0\x03\x08\x02\0\0\0\0\0\0\0".to_vec();
        bytes.extend_from_slice(b"\0\0\0\0IEND\0\0\0\0");
        let mut stream = Stream { bytes, position: 0, calls: Vec::new() };
        let info = parse_header(&mut stream).unwrap();
        assert_eq!((info.px_width, info.px_height, info.horz_dpi), (2, 3, 72));
        assert_eq!(stream.calls, [("seek", 0), ("read", 32), ("seek", 8), ("read", 4),
            ("seek", 12), ("read", 4), ("seek", 16), ("read", 4), ("seek", 20), ("read", 4),
            ("seek", 33), ("read", 4), ("seek", 37), ("read", 4)]);
    }

    #[test]
    fn gif_and_bmp_follow_format_specific_offsets() {
        let mut gif = Stream { bytes: b"GIF89a\x02\0\x03\0".to_vec(), position: 0, calls: Vec::new() };
        let info = parse_header(&mut gif).unwrap();
        assert_eq!((info.px_width, info.px_height), (2, 3));
        assert_eq!(gif.calls, [("seek", 0), ("read", 32), ("seek", 6), ("read", 4)]);
        let mut bytes = vec![0; 46];
        bytes[..2].copy_from_slice(b"BM");
        bytes[18..22].copy_from_slice(&2u32.to_le_bytes());
        bytes[22..26].copy_from_slice(&3u32.to_le_bytes());
        let mut bmp = Stream { bytes, position: 0, calls: Vec::new() };
        let info = parse_header(&mut bmp).unwrap();
        assert_eq!((info.px_width, info.px_height, info.horz_dpi), (2, 3, 96));
        assert_eq!(bmp.calls, [("seek", 0), ("read", 32), ("seek", 18), ("read", 4),
            ("seek", 22), ("read", 4), ("seek", 38), ("read", 4), ("seek", 42), ("read", 4)]);
    }
}
