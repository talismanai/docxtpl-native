//! The raw WebAssembly ABI shared by every export: the host allocates input
//! buffers with `docxtpl_alloc`, calls an export, then reads the output (the
//! result, or a UTF-8 error message) through `docxtpl_output_ptr` and
//! `docxtpl_output_len` before releasing what it allocated with
//! `docxtpl_free`.
//!
//! A crate that embeds this one adds its own exports with
//! [`export_bytes!`](crate::export_bytes), which writes the unsafe pointer
//! handling and reports the result with [`finish`]. An export that returns
//! more than bytes frames them with [`envelope`].

use std::cell::RefCell;

thread_local! {
    static OUTPUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
}

/// Store an export's result as the output: 0 with the bytes, or 1 with the
/// error message.
pub fn finish(result: Result<Vec<u8>, String>) -> u32 {
    let (status, output) = match result { Ok(bytes) => (0, bytes), Err(message) => (1, message.into_bytes()) };
    OUTPUT.with(|slot| *slot.borrow_mut() = output);
    status
}

/// Borrow an input range passed by the host.
///
/// # Safety
/// The range must be readable for `len` bytes for the duration of the call.
pub unsafe fn input<'a>(pointer: *const u8, len: usize) -> &'a [u8] {
    unsafe { std::slice::from_raw_parts(pointer, len) }
}

/// Allocate `len` bytes owned by the caller; release with `docxtpl_free`.
#[unsafe(no_mangle)]
pub extern "C" fn docxtpl_alloc(len: usize) -> *mut u8 {
    let mut buffer = Vec::<u8>::with_capacity(len.max(1));
    let pointer = buffer.as_mut_ptr();
    std::mem::forget(buffer);
    pointer
}

/// Release a buffer obtained from `docxtpl_alloc` with the same length.
///
/// # Safety
/// `pointer` must come from `docxtpl_alloc(len)` and not be freed twice.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn docxtpl_free(pointer: *mut u8, len: usize) {
    drop(unsafe { Vec::from_raw_parts(pointer, 0, len.max(1)) });
}

#[unsafe(no_mangle)]
pub extern "C" fn docxtpl_output_ptr() -> *const u8 {
    OUTPUT.with(|slot| slot.borrow().as_ptr())
}

#[unsafe(no_mangle)]
pub extern "C" fn docxtpl_output_len() -> usize {
    OUTPUT.with(|slot| slot.borrow().len())
}

/// Frame a result that is more than bytes: a 4-byte little-endian length,
/// that many bytes of UTF-8 JSON (`header`), then `body`. A host reads the
/// length, parses the JSON and takes the rest as the body, so one export can
/// return a document together with what the caller must know about it.
pub fn envelope(header: &serde_json::Value, body: &[u8]) -> Vec<u8> {
    let header = header.to_string().into_bytes();
    let mut out = Vec::with_capacity(4 + header.len() + body.len());
    out.extend_from_slice(&(header.len() as u32).to_le_bytes());
    out.extend_from_slice(&header);
    out.extend_from_slice(body);
    out
}

/// Export a function over byte slices to the host.
///
/// The export takes one `(pointer, length)` pair per input and returns 0 with
/// the result, or 1 with the error message, in the output buffer (see
/// [`finish`]). The body receives the inputs as `&[u8]` and returns
/// `Result<Vec<u8>, String>`.
///
/// ```ignore
/// docxtpl_wasm::export_bytes!(
///     /// Render a template with a payload.
///     my_render, |template, payload| render(template, payload)
/// );
/// docxtpl_wasm::export_bytes!(my_convert, |docx| convert(docx));
/// docxtpl_wasm::export_bytes!(my_status, || Ok(status().into_bytes()));
/// ```
#[macro_export]
macro_rules! export_bytes {
    ($(#[$meta:meta])* $name:ident, |$first:ident, $second:ident| $body:expr) => {
        $(#[$meta])*
        ///
        /// Returns 0 with the result, or 1 with the error message.
        ///
        /// # Safety
        /// Both input ranges must be readable for their lengths.
        #[unsafe(no_mangle)]
        pub unsafe extern "C" fn $name($first: *const u8, first_len: usize,
            $second: *const u8, second_len: usize) -> u32 {
            let ($first, $second) = unsafe {
                ($crate::abi::input($first, first_len), $crate::abi::input($second, second_len))
            };
            $crate::abi::finish($body)
        }
    };
    ($(#[$meta:meta])* $name:ident, |$only:ident| $body:expr) => {
        $(#[$meta])*
        ///
        /// Returns 0 with the result, or 1 with the error message.
        ///
        /// # Safety
        /// The input range must be readable for its length.
        #[unsafe(no_mangle)]
        pub unsafe extern "C" fn $name($only: *const u8, only_len: usize) -> u32 {
            let $only = unsafe { $crate::abi::input($only, only_len) };
            $crate::abi::finish($body)
        }
    };
    ($(#[$meta:meta])* $name:ident, || $body:expr) => {
        $(#[$meta])*
        ///
        /// Returns 0 with the result, or 1 with the error message.
        #[unsafe(no_mangle)]
        pub extern "C" fn $name() -> u32 {
            $crate::abi::finish($body)
        }
    };
}
