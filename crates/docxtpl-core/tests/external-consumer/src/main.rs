use std::collections::BTreeMap;
use std::io::{Cursor, Read};
use std::sync::Arc;

fn main() {
    let mut args = std::env::args_os().skip(1);
    let template: Arc<[u8]> = match args.next() {
        Some(path) => Arc::from(std::fs::read(path).expect("read input DOCX")),
        None => Arc::from(&include_bytes!("../minimal.docx")[..]),
    };
    let output_path = args.next();
    assert!(
        args.next().is_none(),
        "expected at most input and output paths"
    );
    let context = BTreeMap::from([("name", "Rust")]);
    let result = docxtpl_core::render_shared(template, &context, false)
        .expect("render DOCX from independent workspace");
    if let Some(path) = output_path {
        std::fs::write(path, &result).expect("write output DOCX");
    }
    let mut archive = zip::ZipArchive::new(Cursor::new(result)).expect("valid output package");
    let mut body = String::new();
    archive
        .by_name("word/document.xml")
        .unwrap()
        .read_to_string(&mut body)
        .unwrap();
    assert!(body.contains("Hello Rust"), "{body}");
}
