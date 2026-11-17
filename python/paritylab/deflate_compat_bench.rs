//! Laboratory benchmark: current zlib-ng backend versus the experimental Rust port.
use std::io::{Read, Write};
use std::time::Instant;

fn compatible(input: &[u8]) -> Vec<u8> {
    let mut config=zlib_rs::DeflateConfig::default();
    config.window_bits=-15;
    let mut output=vec![0;zlib_rs::compress_bound(input.len())];
    let (result,status)=zlib_rs::compress_slice(&mut output,input,config);
    assert_eq!(status,zlib_rs::ReturnCode::Ok);
    let size=result.len();output.truncate(size);output
}

fn current(input: &[u8]) -> Vec<u8> {
    let mut encoder=flate2::write::DeflateEncoder::new(Vec::new(),flate2::Compression::default());
    encoder.write_all(input).unwrap();encoder.finish().unwrap()
}

fn measure(input: &[u8],iterations: usize, function: fn(&[u8])->Vec<u8>) -> u128 {
    let start=Instant::now();
    for _ in 0..iterations { std::hint::black_box(function(input)); }
    start.elapsed().as_nanos()/iterations as u128
}

fn main() {
    let iterations:usize=std::env::args().nth(1).unwrap().parse().unwrap();assert!(iterations>0);
    let mut input=Vec::new();std::io::stdin().read_to_end(&mut input).unwrap();
    for _ in 0..3 { std::hint::black_box(current(&input));std::hint::black_box(compatible(&input)); }
    let mut samples=Vec::new();
    for index in 0..9 {
        let (a,b)=if index%2==0 {
            (measure(&input,iterations,current),measure(&input,iterations,compatible))
        } else {
            let b=measure(&input,iterations,compatible);(measure(&input,iterations,current),b)
        };
        samples.push(format!("{{\"current_ns\":{a},\"compat_ns\":{b}}}"));
    }
    println!("{{\"current_bytes\":{},\"compat_bytes\":{},\"samples\":[{}]}}",
        current(&input).len(),compatible(&input).len(),samples.join(","));
}
