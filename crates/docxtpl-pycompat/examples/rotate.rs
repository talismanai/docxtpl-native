//! `rotate IN.png DEGREES OUT.png`: Pillow-compatible `Image.rotate(expand=True)`.
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let pixels = docxtpl_pycompat::images::decode(&std::fs::read(&args[1]).expect("input")).expect("decode");
    let rotated = docxtpl_pycompat::images::rotate(&pixels, args[2].parse().expect("degrees"));
    std::fs::write(&args[3], docxtpl_pycompat::images::encode_png(&rotated).expect("encode")).expect("output");
}
