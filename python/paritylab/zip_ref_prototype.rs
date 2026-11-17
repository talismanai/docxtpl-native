//! Laboratory-only ZIP serializer: incomplete, does not support ZIP64 or
//! directory entries, and hardcodes the Unix creator. Not production code.
use docxtplrs::package::{crc32, Package};
fn u16le(out: &mut Vec<u8>, n: u16) { out.extend_from_slice(&n.to_le_bytes()); }
fn u32le(out: &mut Vec<u8>, n: u32) { out.extend_from_slice(&n.to_le_bytes()); }
struct Entry { name: Vec<u8>, flags: u16, crc: u32, compressed: u32, original: u32, offset: u32 }
pub fn encode(package: &Package) -> Result<Vec<u8>, String> {
    if package.entries.len()>u16::MAX as usize { return Err("prototype does not support ZIP64 entry counts".into()); }
    let mut output=Vec::new();let mut entries=Vec::with_capacity(package.entries.len());
    for file in &package.entries {
        if file.name.ends_with('/') { return Err("prototype does not support directory entries".into()); }
        let name=file.name.as_bytes().to_vec();
        let name_len=u16::try_from(name.len()).map_err(|_|"ZIP name too long")?;
        let flags=if name.is_ascii(){0}else{1<<11};
        let data=file.bytes();let original=u32::try_from(data.len()).map_err(|_|"prototype does not support ZIP64 entry size")?;
        let crc=crc32(data);
        let mut compressed=vec![0;zlib_rs_ref::compress_bound(data.len())];
        let mut encoder=zlib_rs_ref::Deflate::new(-1,false,15);
        encoder.compress(data,&mut compressed,zlib_rs_ref::DeflateFlush::NoFlush).map_err(|e|format!("{e:?}"))?;
        if encoder.total_in()!=data.len() as u64 { return Err("incomplete compression input".into()); }
        let n=encoder.total_out() as usize;
        let status=encoder.compress(&[],&mut compressed[n..],zlib_rs_ref::DeflateFlush::Finish).map_err(|e|format!("{e:?}"))?;
        if status!=zlib_rs_ref::Status::StreamEnd { return Err("incomplete compression output".into()); }
        compressed.truncate(encoder.total_out() as usize);
        let length=u32::try_from(compressed.len()).map_err(|_|"prototype does not support ZIP64 compressed size")?;
        let offset=u32::try_from(output.len()).map_err(|_|"prototype does not support ZIP64 offsets")?;
        u32le(&mut output,0x04034b50);u16le(&mut output,20);u16le(&mut output,flags);u16le(&mut output,8);
        u16le(&mut output,0);u16le(&mut output,33);u32le(&mut output,crc);u32le(&mut output,length);u32le(&mut output,original);
        u16le(&mut output,name_len);u16le(&mut output,0);output.extend_from_slice(&name);output.extend_from_slice(&compressed);
        entries.push(Entry{name,flags,crc,compressed:length,original,offset});
    }
    let central=u32::try_from(output.len()).map_err(|_|"prototype does not support ZIP64 central directory")?;
    for e in &entries {
        u32le(&mut output,0x02014b50);u16le(&mut output,0x0314);u16le(&mut output,20);u16le(&mut output,e.flags);u16le(&mut output,8);
        u16le(&mut output,0);u16le(&mut output,33);u32le(&mut output,e.crc);u32le(&mut output,e.compressed);u32le(&mut output,e.original);
        u16le(&mut output,e.name.len() as u16);u16le(&mut output,0);u16le(&mut output,0);u16le(&mut output,0);u16le(&mut output,0);
        u32le(&mut output,0o600<<16);u32le(&mut output,e.offset);output.extend_from_slice(&e.name);
    }
    let size=u32::try_from(output.len()-central as usize).map_err(|_|"prototype does not support ZIP64 central size")?;
    u32le(&mut output,0x06054b50);u16le(&mut output,0);u16le(&mut output,0);u16le(&mut output,entries.len() as u16);u16le(&mut output,entries.len() as u16);
    u32le(&mut output,size);u32le(&mut output,central);u16le(&mut output,0);Ok(output)
}
