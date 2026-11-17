//! Exercise the crate from a separate Rust consumer, without Python.
use docxtpl_core::{prepare_shared, process, process_shared, render, render_shared};
use docxtplrs::package::{minimal_docx, Package};
use std::collections::BTreeMap;
use std::sync::Arc;

fn body(bytes: &[u8]) -> String {
    Package::from_bytes(bytes).unwrap().get_string("word/document.xml").unwrap()
}

#[test]
fn typed_rust_context_matches_existing_adapter() {
    let template = minimal_docx(
        "<w:p><w:r><w:t>Hello {{ name }}</w:t></w:r></w:p>");
    let payload = serde_json::json!({
        "context": {"kind":"map","items":[["name",{"kind":"str","value":"Rust"}]]},
        "replacements": {}
    }).to_string();
    let expected = process(template.clone(), &payload, false, true).unwrap();
    let context = BTreeMap::from([("name", "Rust")]);
    let owned = render(template.clone(), &context, false).unwrap();
    let shared: Arc<[u8]> = template.into();
    let prepared = prepare_shared(shared.clone(), &serde_json::Value::Null).unwrap();
    assert!(Arc::ptr_eq(&shared, &prepared.original_bytes));
    let shared_adapter = process_shared(shared.clone(), &payload, false, true).unwrap();
    let reused = render_shared(shared, &context, false).unwrap();
    assert!(body(&expected).contains("Hello Rust"));
    assert_eq!(body(&owned), body(&expected));
    assert_eq!(body(&shared_adapter), body(&expected));
    assert_eq!(body(&reused), body(&expected));
}
