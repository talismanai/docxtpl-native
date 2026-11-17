//! Numbering identifiers for list definitions copied into a document.

/// The `w:nsid` of a list definition copied into a document: derived from
/// the definition itself (FNV-1a, 32 bits, as 8 uppercase hex digits) and
/// moved past values `taken` reports, so the same inputs always produce the
/// same document. docxcompose draws it from Python's `random` instead, which
/// makes every render differ; this is an intentional divergence.
pub fn stable_nsid(definition: &[u8], taken: &dyn Fn(&str) -> bool) -> String {
    let mut hash: u32 = 0x811c_9dc5;
    for byte in definition {
        hash ^= u32::from(*byte);
        hash = hash.wrapping_mul(0x0100_0193);
    }
    loop {
        let nsid = format!("{hash:08X}");
        if !taken(&nsid) { return nsid; }
        hash = hash.wrapping_add(1);
    }
}

#[cfg(test)]
mod tests {
    use super::stable_nsid;

    #[test]
    fn same_definition_same_nsid_and_no_collision() {
        let first = stable_nsid(b"<w:abstractNum/>", &|_| false);
        assert_eq!(first, stable_nsid(b"<w:abstractNum/>", &|_| false));
        assert_eq!(first.len(), 8);
        assert_ne!(first, stable_nsid(b"<w:abstractNum w:abstractNumId=\"1\"/>", &|_| false));
        let taken = first.clone();
        let next = stable_nsid(b"<w:abstractNum/>", &|nsid| nsid == taken);
        assert_ne!(next, first);
    }
}
