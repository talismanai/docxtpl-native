//! Read a JSON list of XML strings on stdin; print the recovered strings.
use std::io::Read;
fn main() {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).expect("stdin");
    let cases: Vec<String> = serde_json::from_str(&input).expect("cases");
    let out: Vec<String> = cases.iter().map(|c| docxtplrs::recover::recover(c)).collect();
    println!("{}", serde_json::to_string(&out).expect("json"));
}
