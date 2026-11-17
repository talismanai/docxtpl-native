//! The exports the macro writes, and the envelope an export frames a result
//! with.

use docxtpl_wasm::abi;

docxtpl_wasm::export_bytes!(
    /// Hand an input straight back.
    test_echo, |input| Ok(input.to_vec())
);
docxtpl_wasm::export_bytes!(
    /// Join two inputs.
    test_join, |left, right| Ok([left, right].concat())
);
docxtpl_wasm::export_bytes!(
    /// Always fail, with a message the host reads as the output.
    test_refuse, |input| Err::<Vec<u8>, String>(format!("refused {} bytes", input.len()))
);
docxtpl_wasm::export_bytes!(
    /// Answer without an input.
    test_report, || Ok(b"ready".to_vec())
);

/// What the host reads after an export returns.
fn output() -> Vec<u8> {
    let (pointer, len) = (abi::docxtpl_output_ptr(), abi::docxtpl_output_len());
    unsafe { std::slice::from_raw_parts(pointer, len) }.to_vec()
}

#[test]
fn an_export_reports_a_result_through_the_output_buffer() {
    let input = b"document";
    let status = unsafe { test_echo(input.as_ptr(), input.len()) };
    assert_eq!((status, output()), (0, input.to_vec()));
}

#[test]
fn an_export_takes_two_inputs() {
    let (left, right) = (b"tem".as_slice(), b"plate".as_slice());
    let status = unsafe { test_join(left.as_ptr(), left.len(), right.as_ptr(), right.len()) };
    assert_eq!((status, output()), (0, b"template".to_vec()));
}

#[test]
fn an_export_reports_a_failure_as_the_message() {
    let input = b"abc";
    let status = unsafe { test_refuse(input.as_ptr(), input.len()) };
    assert_eq!(status, 1);
    assert_eq!(String::from_utf8(output()).unwrap(), "refused 3 bytes");
}

#[test]
fn an_export_can_take_no_input_at_all() {
    assert_eq!((test_report(), output()), (0, b"ready".to_vec()));
}

#[test]
fn the_host_allocates_and_releases_its_own_buffers() {
    let pointer = abi::docxtpl_alloc(4);
    assert!(!pointer.is_null());
    unsafe { abi::docxtpl_free(pointer, 4) };
    // A zero-length request still gives a pointer the host may free.
    let empty = abi::docxtpl_alloc(0);
    unsafe { abi::docxtpl_free(empty, 0) };
}

#[test]
fn an_envelope_puts_the_header_length_first() {
    let header = serde_json::json!({"pages": 2});
    let framed = abi::envelope(&header, b"%PDF-1.7");
    let length = u32::from_le_bytes(framed[..4].try_into().unwrap()) as usize;
    let json: serde_json::Value = serde_json::from_slice(&framed[4..4 + length]).unwrap();
    assert_eq!(json, header);
    assert_eq!(&framed[4 + length..], b"%PDF-1.7");
}

#[test]
fn an_envelope_carries_an_empty_body() {
    let framed = abi::envelope(&serde_json::json!({}), b"");
    let length = u32::from_le_bytes(framed[..4].try_into().unwrap()) as usize;
    assert_eq!(framed.len(), 4 + length);
}
