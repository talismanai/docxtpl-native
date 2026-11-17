//! Read `[{"template": ..., "context": {...}}, ...]` on stdin and print one
//! `{"ok": bool, "output": ...}` result per case, in order.
use std::io::Read;

fn main() {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).expect("stdin");
    let cases: Vec<serde_json::Value> = serde_json::from_str(&input).expect("cases JSON");
    let results: Vec<serde_json::Value> = cases.iter().map(|case| {
        let template = case["template"].as_str().expect("template");
        match docxtpl_wasm::render_string(template, &case["context"]) {
            Ok(output) => serde_json::json!({"ok": true, "output": output}),
            Err(error) => serde_json::json!({"ok": false, "output": error}),
        }
    }).collect();
    println!("{}", serde_json::to_string(&results).expect("results"));
}
