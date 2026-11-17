//! Direct libxml2 serialization of a caller-owned tree. There is no Python
//! dependency here. The binding keeps the owner alive during this operation.
//!
//! A tree must always use the libxml2 instance that allocated it. In particular,
//! a host extension may bundle a different libxml2 from this crate's linked one.
//! Namespace/tail rules follow lxml 6 serializer.pxi and proxy.pxi (BSD-3-Clause).
use libxml::bindings::{xmlBuffer, xmlDoc, xmlNode, xmlNs};
use std::ffi::{OsStr, c_int, c_uchar};

pub struct Serializer {
    _library: libloading::Library,
    buffer_create: unsafe extern "C" fn() -> *mut xmlBuffer,
    buffer_free: unsafe extern "C" fn(*mut xmlBuffer),
    buffer_content: unsafe extern "C" fn(*const xmlBuffer) -> *const c_uchar,
    buffer_length: unsafe extern "C" fn(*const xmlBuffer) -> c_int,
    buffer_add: unsafe extern "C" fn(*mut xmlBuffer, *const c_uchar, c_int) -> c_int,
    copy_node: unsafe extern "C" fn(*mut xmlNode, c_int) -> *mut xmlNode,
    free_node: unsafe extern "C" fn(*mut xmlNode),
    new_ns: unsafe extern "C" fn(*mut xmlNode, *const c_uchar, *const c_uchar) -> *mut xmlNs,
    node_dump:
        unsafe extern "C" fn(*mut xmlBuffer, *mut xmlDoc, *mut xmlNode, c_int, c_int) -> c_int,
}

impl Serializer {
    /// Obtain an ABI entry from the same retained library as the serializer.
    ///
    /// # Safety
    /// `T` must describe the exact ABI/type of this exported symbol. A returned
    /// pointer must not outlive this serializer.
    pub unsafe fn symbol<T: Copy>(&self, name: &[u8]) -> Result<T, String> {
        unsafe { self._library.get::<T>(name) }
            .map(|symbol| *symbol)
            .map_err(|e| e.to_string())
    }
    /// Load the already trusted native library that owns the supplied trees.
    ///
    /// # Safety
    /// Loading a library executes its initialization code. The caller must
    /// trust this path and supply libxml2 2.x nodes from this same library.
    pub unsafe fn load(path: impl AsRef<OsStr>) -> Result<Self, String> {
        let library = unsafe { libloading::Library::new(path) }.map_err(|e| e.to_string())?;
        // Resolve every function from the SAME handle. Never fall back to the
        // process-global symbols or free a foreign allocation with our libxml.
        unsafe {
            Ok(Self {
                buffer_create: *library
                    .get(b"xmlBufferCreate\0")
                    .map_err(|e| e.to_string())?,
                buffer_free: *library.get(b"xmlBufferFree\0").map_err(|e| e.to_string())?,
                buffer_content: *library
                    .get(b"xmlBufferContent\0")
                    .map_err(|e| e.to_string())?,
                buffer_length: *library
                    .get(b"xmlBufferLength\0")
                    .map_err(|e| e.to_string())?,
                buffer_add: *library.get(b"xmlBufferAdd\0").map_err(|e| e.to_string())?,
                copy_node: *library.get(b"xmlCopyNode\0").map_err(|e| e.to_string())?,
                free_node: *library.get(b"xmlFreeNode\0").map_err(|e| e.to_string())?,
                new_ns: *library.get(b"xmlNewNs\0").map_err(|e| e.to_string())?,
                node_dump: *library.get(b"xmlNodeDump\0").map_err(|e| e.to_string())?,
                _library: library,
            })
        }
    }

    /// Recover a Unicode XML source using this provider's libxml2 and the
    /// caller's Python Unicode storage representation.
    pub fn recover_unicode(&self, codepoints: &[u32], kind: u32, ascii: bool) -> Result<Vec<u8>, String> {
        type Read = unsafe extern "C" fn(*const std::ffi::c_char, c_int,
            *const std::ffi::c_char, *const std::ffi::c_char, c_int) -> *mut xmlDoc;
        type Root = unsafe extern "C" fn(*const xmlDoc) -> *mut xmlNode;
        type Free = unsafe extern "C" fn(*mut xmlDoc);
        let read: Read = unsafe { self.symbol(b"xmlReadMemory\0")? };
        let root: Root = unsafe { self.symbol(b"xmlDocGetRootElement\0")? };
        let free: Free = unsafe { self.symbol(b"xmlFreeDoc\0")? };
        let mut bytes = Vec::new();
        let encoding = match kind {
            1 if ascii => c"UTF-8",
            1 => c"ISO-8859-1",
            2 if cfg!(target_endian = "little") => c"UTF-16LE",
            2 => c"UTF-16BE",
            4 if cfg!(target_endian = "little") => c"UTF-32LE",
            4 => c"UTF-32BE",
            _ => return Err("invalid Python Unicode storage kind".into()),
        };
        for &code in codepoints {
            match kind {
                1 => bytes.push(u8::try_from(code).map_err(|_| "invalid one-byte Unicode code point")?),
                2 => bytes.extend_from_slice(&u16::try_from(code).map_err(|_| "invalid two-byte Unicode code point")?.to_ne_bytes()),
                4 if code <= 0x10ffff => bytes.extend_from_slice(&code.to_ne_bytes()),
                _ => return Err("invalid Unicode code point".into()),
            }
        }
        let length = c_int::try_from(bytes.len()).map_err(|_| "XML input is too large")?;
        // XML_PARSE_RECOVER | XML_PARSE_NONET. All allocations and frees use
        // this provider, never the process-global libxml2 instance.
        let document = unsafe { read(bytes.as_ptr().cast(), length, std::ptr::null(), encoding.as_ptr(), 1 | 2048) };
        if document.is_null() { return Err("invalid rendered XML".into()); }
        struct OwnedDocument(*mut xmlDoc, Free);
        impl Drop for OwnedDocument {
            fn drop(&mut self) { unsafe { (self.1)(self.0) } }
        }
        let _document = OwnedDocument(document, free);
        let node = unsafe { root(document) };
        if node.is_null() { return Err("missing rendered XML root".into()); }
        // SAFETY: node belongs to the live document and the same provider.
        unsafe { self.serialize(node) }
    }

    /// Serialize a node, inherited namespaces and its text/CDATA tail as UTF-8.
    /// The source graph is never changed or freed.
    ///
    /// # Safety
    /// `node` must be a valid, non-null libxml2 2.x node allocated by this
    /// serializer's library. Its entire graph must remain alive and exclusively
    /// borrowed (no concurrent mutation) until this function returns.
    pub unsafe fn serialize(&self, node: *mut xmlNode) -> Result<Vec<u8>, String> {
        unsafe { self.serialize_inner(node, false) }
    }

    /// Serialize a tree context, including its document type and top-level PIs.
    ///
    /// # Safety
    /// The same ownership and library requirements as `serialize` apply.
    pub unsafe fn serialize_document(&self, node: *mut xmlNode) -> Result<Vec<u8>, String> {
        unsafe { self.serialize_inner(node, true) }
    }

    unsafe fn previous(&self, buffer: *mut xmlBuffer, node: *mut xmlNode) -> Result<(), String> {
        unsafe {
            if !(*node).parent.is_null() && (*(*node).parent).type_ == 1 {
                return Ok(());
            }
            let mut current = node;
            while !(*current).prev.is_null() && matches!((*(*current).prev).type_, 7 | 8) {
                current = (*current).prev;
            }
            while current != node {
                if (self.node_dump)(buffer, (*node).doc, current, 0, 0) < 0 {
                    return Err("XML sibling serialization failed".into());
                }
                current = (*current).next;
            }
        }
        Ok(())
    }

    unsafe fn serialize_inner(
        &self,
        node: *mut xmlNode,
        complete: bool,
    ) -> Result<Vec<u8>, String> {
        if node.is_null() {
            return Err("missing XML node".into());
        }
        let buffer = unsafe { (self.buffer_create)() };
        if buffer.is_null() {
            return Err("cannot allocate XML output buffer".into());
        }
        struct Buffer<'a>(&'a Serializer, *mut xmlBuffer);
        impl Drop for Buffer<'_> {
            fn drop(&mut self) {
                unsafe {
                    (self.0.buffer_free)(self.1);
                }
            }
        }
        let _buffer = Buffer(self, buffer);
        // xmlNode's public common prefix and namespace fields are part of the
        // libxml2 2.x ABI; allocations are always owned by the supplying library.
        let document = unsafe { (*node).doc };
        if complete && !document.is_null() {
            unsafe {
                let dtd = (*document).intSubset;
                if !dtd.is_null() {
                    self.previous(buffer, dtd.cast())?;
                    if !(*dtd).name.is_null()
                        && !(*node).name.is_null()
                        && std::ffi::CStr::from_ptr((*dtd).name.cast())
                            == std::ffi::CStr::from_ptr((*node).name.cast())
                    {
                        if (self.node_dump)(buffer, document, dtd.cast(), 0, 0) < 0
                            || (self.buffer_add)(buffer, b"\n".as_ptr(), 1) < 0
                        {
                            return Err("XML doctype serialization failed".into());
                        }
                    }
                }
                self.previous(buffer, node)?;
            }
        }
        let parent = unsafe { (*node).parent };
        let copied = parent.is_null() || unsafe { (*parent).type_ } != 9;
        let target = if copied {
            unsafe { (self.copy_node)(node, 2) }
        } else {
            node
        };
        if target.is_null() {
            return Err("cannot allocate XML namespace copy".into());
        }
        struct Copy<'a>(&'a Serializer, *mut xmlNode, bool);
        impl Drop for Copy<'_> {
            fn drop(&mut self) {
                if self.2 {
                    unsafe {
                        // The descendants belong to the original graph.
                        (*self.1).children = std::ptr::null_mut();
                        (*self.1).last = std::ptr::null_mut();
                        (self.0.free_node)(self.1);
                    }
                }
            }
        }
        let _copy = Copy(self, target, copied);
        if copied {
            unsafe {
                let mut ancestor = parent;
                while !ancestor.is_null() && matches!((*ancestor).type_, 1 | 9 | 19 | 20) {
                    let mut namespace = (*ancestor).nsDef;
                    while !namespace.is_null() {
                        (self.new_ns)(target, (*namespace).href, (*namespace).prefix);
                        namespace = (*namespace).next;
                    }
                    ancestor = (*ancestor).parent;
                }
                (*target).parent = parent;
                (*target).children = (*node).children;
                (*target).last = (*node).last;
            }
        }
        if unsafe { (self.node_dump)(buffer, document, target, 0, 0) } < 0 {
            return Err("libxml2 XML serialization failed".into());
        }
        let mut tail = unsafe { (*node).next };
        while !tail.is_null() && matches!(unsafe { (*tail).type_ }, 3 | 4) {
            if unsafe { (self.node_dump)(buffer, (*tail).doc, tail, 0, 0) } < 0 {
                return Err("libxml2 XML tail serialization failed".into());
            }
            tail = unsafe { (*tail).next };
        }
        if complete && (parent.is_null() || unsafe { (*parent).type_ } != 1) {
            unsafe {
                let mut sibling = (*node).next;
                while !sibling.is_null() && matches!((*sibling).type_, 7 | 8) {
                    if (self.node_dump)(buffer, document, sibling, 0, 0) < 0 {
                        return Err("XML sibling serialization failed".into());
                    }
                    sibling = (*sibling).next;
                }
            }
        }
        let length = unsafe { (self.buffer_length)(buffer) };
        let content = unsafe { (self.buffer_content)(buffer) };
        if length < 0 || (length > 0 && content.is_null()) {
            return Err("invalid XML output buffer".into());
        }
        if length == 0 {
            Ok(Vec::new())
        } else {
            Ok(unsafe { std::slice::from_raw_parts(content, length as usize) }.to_vec())
        }
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;

    #[test]
    fn serializes_a_foreign_subtree_without_mutating_its_owner() {
        let document = libxml::parser::Parser::default()
            .parse_string(b"<r xmlns:a='urn:a'><a:x/>tail<y/></r>")
            .unwrap();
        let root = document.get_root_element().unwrap();
        let child = root.get_child_elements().remove(0);
        let before = document.to_string();
        // The Rust parser and this handle use the same linked libxml2 SONAME.
        let serializer = unsafe { Serializer::load("libxml2.so.2") }.unwrap();
        for _ in 0..100 {
            let actual = unsafe { serializer.serialize(child.node_ptr()) }.unwrap();
            assert_eq!(actual, b"<a:x xmlns:a=\"urn:a\"/>tail");
        }
        assert_eq!(document.to_string(), before);
    }

    #[test]
    fn document_serialization_and_invalid_inputs_need_no_python() {
        let document = libxml::parser::Parser::default()
            .parse_string(b"<?before x?><!DOCTYPE r SYSTEM 'example.dtd'><r/><!--after-->")
            .unwrap();
        let node = document.get_root_element().unwrap();
        let serializer = unsafe { Serializer::load("libxml2.so.2") }.unwrap();
        let actual = unsafe { serializer.serialize_document(node.node_ptr()) }.unwrap();
        assert_eq!(
            actual,
            b"<?before x?><!DOCTYPE r SYSTEM \"example.dtd\">\n<r/><!--after-->"
        );
        assert!(unsafe { serializer.serialize(std::ptr::null_mut()) }.is_err());
        assert!(unsafe { serializer.symbol::<*mut ()>(b"no_such_xml_symbol\0") }.is_err());
    }
}
