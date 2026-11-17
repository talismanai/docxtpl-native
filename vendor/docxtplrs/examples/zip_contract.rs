//! Differential laboratory driver. Production saves use Package::to_bytes_opc.
use std::io::{Cursor, Read, Seek, SeekFrom, Write};
use docxtplrs::package::Package;
#[path = "../src/python_zip.rs"]
mod python_zip;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args[1] == "--offset" {
        // Real ZIP64 offsets; keep the on-disk prefix sparse. This is a writer
        // and metadata-adapter test, not a multi-gigabyte DOCX graph fixture.
        let offset = (1usize << 31) + 127;
        let mut cursor = Cursor::new(vec![0u8; offset]);
        cursor.set_position(offset as u64);
        let mut writer = zip::ZipWriter::new(cursor);
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated).compression_level(Some(6))
            .last_modified_time(zip::DateTime::default());
        writer.start_file("a", options)?; writer.write_all(b"a")?;
        let cursor = writer.finish()?;
        let end = cursor.position() as usize;
        let mut bytes = cursor.into_inner(); bytes.truncate(end);
        let bytes = python_zip::normalize(bytes)?;
        let mut file = std::fs::File::create(&args[2])?;
        file.set_len(bytes.len() as u64)?;
        file.seek(SeekFrom::Start(offset as u64))?;
        file.write_all(&bytes[offset..])?;
        return Ok(());
    }
    let source = std::fs::read(&args[1])?;
    let bytes = if args.get(3).map(String::as_str) == Some("force64") {
        // Exercise reserved local ZIP64 space using the same writer/adapter
        // without allocating a multi-gigabyte document.
        let mut input = zip::ZipArchive::new(Cursor::new(source))?;
        let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated).compression_level(Some(6))
            .last_modified_time(zip::DateTime::default()).large_file(true);
        for index in 0..input.len() {
            let mut entry = input.by_index(index)?;
            let mut data = Vec::new(); entry.read_to_end(&mut data)?;
            writer.start_file(entry.name(), options)?;
            writer.write_all(&data)?;
        }
        python_zip::normalize(writer.finish()?.into_inner())?
    } else {
        let mut package = Package::from_bytes(&source)?;
        for entry in &mut package.entries { entry.mtime = Some(zip::DateTime::default()); }
        package.to_bytes_opc()?
    };
    std::fs::write(&args[2], bytes)?;
    Ok(())
}
