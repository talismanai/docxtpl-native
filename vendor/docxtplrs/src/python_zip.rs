//! Metadata adapter for generated OPC packages. ZipWriter owns compression,
//! CRC, local headers and I/O; this adapter applies Python's ZIP64 thresholds
//! and central-directory representation without recompressing payloads.
use std::io::Cursor;

const ZIP64_LIMIT: u64 = (1 << 31) - 1;

pub(crate) fn needs_large_file(size: u64) -> bool {
    // zipfile._open_to_write reserves ZIP64 space before compression, using
    // the same float estimate even when the uncompressed length is known.
    size as f64 * 1.05 > ZIP64_LIMIT as f64
}

fn read16(bytes: &[u8], offset: usize) -> usize {
    u16::from_le_bytes(bytes[offset..offset + 2].try_into().unwrap()) as usize
}

fn central_record(header: &[u8], size: u64, compressed: u64, offset: u64) -> Result<Vec<u8>, String> {
    if header.len() < 46 || &header[..4] != b"PK\x01\x02" {
        return Err("invalid generated ZIP central header".into());
    }
    let name_end = 46 + read16(header, 28);
    let extra_end = name_end + read16(header, 30);
    let end = extra_end + read16(header, 32);
    if end != header.len() { return Err("truncated generated ZIP central header".into()); }
    let mut fields = Vec::new();
    let large_sizes = size > ZIP64_LIMIT || compressed > ZIP64_LIMIT;
    if large_sizes {
        fields.extend_from_slice(&size.to_le_bytes());
        fields.extend_from_slice(&compressed.to_le_bytes());
    }
    if offset > ZIP64_LIMIT { fields.extend_from_slice(&offset.to_le_bytes()); }
    let mut extra = Vec::new();
    if !fields.is_empty() {
        extra.extend_from_slice(&1u16.to_le_bytes());
        extra.extend_from_slice(&(fields.len() as u16).to_le_bytes());
        extra.extend_from_slice(&fields);
    }
    // Remove the writer's padded ZIP64 field; retain other declared fields.
    let mut position = name_end;
    while position < extra_end {
        if position + 4 > extra_end { return Err("truncated generated ZIP extra field".into()); }
        let length = read16(header, position + 2);
        let next = position + 4 + length;
        if next > extra_end { return Err("truncated generated ZIP extra field".into()); }
        if read16(header, position) != 1 { extra.extend_from_slice(&header[position..next]); }
        position = next;
    }
    let extra_len = u16::try_from(extra.len()).map_err(|_| "generated ZIP extra field too large")?;
    let mut result = header[..name_end].to_vec();
    result[5] = if cfg!(windows) { 0 } else { 3 };
    if !fields.is_empty() {
        result[4] = result[4].max(45);
        let needed = u16::from_le_bytes(result[6..8].try_into().unwrap()).max(45);
        result[6..8].copy_from_slice(&needed.to_le_bytes());
    }
    let compressed32 = if large_sizes { u32::MAX } else { compressed as u32 };
    let size32 = if large_sizes { u32::MAX } else { size as u32 };
    let offset32 = if offset > ZIP64_LIMIT { u32::MAX } else { offset as u32 };
    result[20..24].copy_from_slice(&compressed32.to_le_bytes());
    result[24..28].copy_from_slice(&size32.to_le_bytes());
    result[30..32].copy_from_slice(&extra_len.to_le_bytes());
    result[38..42].copy_from_slice(&(0o600u32 << 16).to_le_bytes());
    result[42..46].copy_from_slice(&offset32.to_le_bytes());
    result.extend_from_slice(&extra);
    result.extend_from_slice(&header[extra_end..end]);
    Ok(result)
}

/// End records shared by ordinary OPC saves and interrupted-write finalization.
pub fn finish_directory(count: u64, central_size: u64, central_start: u64) -> Result<Vec<u8>, String> {
    let mut result = Vec::new();
    if count > u16::MAX as u64 || central_size > ZIP64_LIMIT || central_start > ZIP64_LIMIT {
        let zip64_start = central_start.checked_add(central_size).ok_or("generated ZIP size overflow")?;
        result.extend_from_slice(b"PK\x06\x06");
        result.extend_from_slice(&44u64.to_le_bytes());
        result.extend_from_slice(&45u16.to_le_bytes());
        result.extend_from_slice(&45u16.to_le_bytes());
        result.extend_from_slice(&[0; 8]); // single disk
        result.extend_from_slice(&count.to_le_bytes());
        result.extend_from_slice(&count.to_le_bytes());
        result.extend_from_slice(&central_size.to_le_bytes());
        result.extend_from_slice(&central_start.to_le_bytes());
        result.extend_from_slice(b"PK\x06\x07");
        result.extend_from_slice(&0u32.to_le_bytes());
        result.extend_from_slice(&zip64_start.to_le_bytes());
        result.extend_from_slice(&1u32.to_le_bytes());
    }
    result.extend_from_slice(b"PK\x05\x06");
    result.extend_from_slice(&[0; 4]);
    let count = count.min(u16::MAX as u64) as u16;
    result.extend_from_slice(&count.to_le_bytes());
    result.extend_from_slice(&count.to_le_bytes());
    result.extend_from_slice(&(central_size.min(u32::MAX as u64) as u32).to_le_bytes());
    result.extend_from_slice(&(central_start.min(u32::MAX as u64) as u32).to_le_bytes());
    result.extend_from_slice(&0u16.to_le_bytes());
    Ok(result)
}

pub(crate) fn normalize(mut bytes: Vec<u8>) -> Result<Vec<u8>, String> {
    let (start, central, end) = {
        let mut archive = zip::ZipArchive::new(Cursor::new(&bytes)).map_err(|e| e.to_string())?;
        let mut central = Vec::new();
        let mut start = bytes.len();
        for index in 0..archive.len() {
            let entry = archive.by_index_raw(index).map_err(|e| e.to_string())?;
            let at = usize::try_from(entry.central_header_start()).map_err(|e| e.to_string())?;
            let fixed = bytes.get(at..at.saturating_add(46)).ok_or("invalid generated ZIP central offset")?;
            let end = at + 46 + read16(fixed, 28) + read16(fixed, 30) + read16(fixed, 32);
            let header = bytes.get(at..end).ok_or("invalid generated ZIP central length")?;
            central.extend_from_slice(&central_record(header, entry.size(), entry.compressed_size(), entry.header_start())?);
            start = start.min(at);
        }
        if archive.is_empty() { start = 0; }
        let end = finish_directory(archive.len() as u64, central.len() as u64, start as u64)?;
        (start, central, end)
    };
    bytes.truncate(start);
    bytes.extend_from_slice(&central);
    bytes.extend_from_slice(&end);
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    #[test]
    fn forced_zip64_drops_central_padding_and_preserves_local_header() {
        let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
        writer.start_file("a", zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated).large_file(true)).unwrap();
        writer.write_all(b"a").unwrap();
        let original = writer.finish().unwrap().into_inner();
        let start = zip::ZipArchive::new(Cursor::new(&original)).unwrap().by_index_raw(0).unwrap().central_header_start() as usize;
        let output = normalize(original.clone()).unwrap();
        assert_eq!(&output[..start], &original[..start]);
        assert_eq!(output.len() + 20, original.len());
        let mut archive = zip::ZipArchive::new(Cursor::new(&output)).unwrap();
        let mut entry = archive.by_index(0).unwrap();
        assert_eq!(entry.extra_data(), Some(&[][..]));
        assert_eq!(entry.unix_mode(), Some(0o600));
        let mut data = Vec::new(); entry.read_to_end(&mut data).unwrap();
        assert_eq!(data, b"a");
        assert_eq!(output[start + 6], 45);
    }

    #[test]
    fn zip64_metadata_uses_python_thresholds() {
        let mut header = vec![0u8; 46]; header[..4].copy_from_slice(b"PK\x01\x02");
        header[4] = 20; header[6] = 20;
        let normal = central_record(&header, ZIP64_LIMIT, 4, ZIP64_LIMIT).unwrap();
        assert_eq!(normal.len(), 46);
        let large = central_record(&header, ZIP64_LIMIT + 1, 4, ZIP64_LIMIT + 1).unwrap();
        assert_eq!(large[4], 45); assert_eq!(large[6], 45);
        assert_eq!(&large[20..28], &[255; 8]);
        assert_eq!(&large[42..46], &[255; 4]);
        assert_eq!(read16(&large, 30), 28);
        assert_eq!(&large[50..58], &(ZIP64_LIMIT + 1).to_le_bytes());
        assert_eq!(&large[58..66], &4u64.to_le_bytes());
        assert_eq!(&large[66..74], &(ZIP64_LIMIT + 1).to_le_bytes());
        assert_eq!(finish_directory(65535, 100, 200).unwrap().len(), 22);
        let count64 = finish_directory(65536, 100, 200).unwrap();
        assert_eq!(count64.len(), 98); assert_eq!(&count64[12..16], &[45, 0, 45, 0]);
        assert_eq!(finish_directory(1, ZIP64_LIMIT + 1, 0).unwrap().len(), 98);
        assert_eq!(finish_directory(1, 1, ZIP64_LIMIT + 1).unwrap().len(), 98);
        assert!(finish_directory(65536, u64::MAX, 1).is_err());
        assert!(!needs_large_file(0)); assert!(needs_large_file(ZIP64_LIMIT));
    }

    #[test]
    fn malformed_archive_and_central_fields_fail() {
        assert!(normalize(b"not a zip".to_vec()).is_err());
        let mut header = vec![0; 46]; header[..4].copy_from_slice(b"PK\x01\x02");
        header[30] = 2; header.extend_from_slice(&[1, 0]);
        assert!(central_record(&header, 0, 0, 0).is_err());
    }
}
