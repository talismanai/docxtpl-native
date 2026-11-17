//! Laboratory helper: compress stdin as raw deflate using pinned zlib-rs.
use std::io::{Read, Write};

fn main() {
    let mut args=std::env::args().skip(1);
    let level=args.next().map(|value| value.parse().unwrap()).unwrap_or(-1);
    let mut data=Vec::new();
    std::io::stdin().read_to_end(&mut data).unwrap();
    let chunk=args.next().map(|value| value.parse::<usize>().unwrap()).unwrap_or(data.len().max(1));
    assert!(chunk>0);
    let mut output=vec![0; zlib_rs::compress_bound(data.len())];
    let mut encoder=zlib_rs::Deflate::new(level,false,15);
    for input in data.chunks(chunk) {
        let written=encoder.total_out() as usize;
        encoder.compress(input,&mut output[written..],zlib_rs::DeflateFlush::NoFlush).unwrap();
    }
    assert_eq!(encoder.total_in(),data.len() as u64);
    let written=encoder.total_out() as usize;
    let status=encoder.compress(&[],&mut output[written..],zlib_rs::DeflateFlush::Finish).unwrap();
    assert_eq!(status,zlib_rs::Status::StreamEnd);
    std::io::stdout().write_all(&output[..encoder.total_out() as usize]).unwrap();
}
