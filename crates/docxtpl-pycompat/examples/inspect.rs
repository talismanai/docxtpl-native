//! `inspect PICTURE [DECODED.png]`: print what Pillow would report for a
//! picture (format, size, `info["dpi"]`, PNG writability) as JSON, and write
//! its decoded pixels as PNG when asked.
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let bytes = std::fs::read(&args[1]).expect("picture");
    let Some(info) = docxtpl_pycompat::images::inspect(&bytes) else {
        println!("null");
        return;
    };
    let decoded = docxtpl_pycompat::images::decode(&bytes);
    if let (Some(out), Ok(pixels)) = (args.get(2), &decoded) {
        std::fs::write(out, docxtpl_pycompat::images::encode_png(pixels).expect("encode")).expect("output");
    }
    let dpi = info.dpi.map_or("null".to_owned(), |(x, y)| format!("[{x}, {y}]"));
    println!(
        "{{\"format\": \"{:?}\", \"size\": [{}, {}], \"dpi\": {dpi}, \"png_writable\": {}, \"decoded\": {}}}",
        info.format, info.width, info.height, info.png_writable,
        decoded.as_ref().map_or("null".to_owned(), |p| format!("[{}, {}]", p.width, p.height))
    );
}
