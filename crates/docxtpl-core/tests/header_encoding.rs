use docxtpl_core::{headers_footers_encoding, headers_footers_encoding_with_unicode, UnicodeWordVersion};

#[test]
fn recognizes_declarations_without_a_python_runtime() {
    for (xml, expected) in [
        ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>", "UTF-8"),
        ("<?XML ·encodİng=\"LATIN1\"?>", "LATIN1"),
        ("<?xml \u{345}encoding=\"x\"?>", "x"),
        ("<?xml éencoding=\"x\"?>", "utf-8"),
        ("<?xml encoding=\"first\" encoding=\"second\"?>", "second"),
        ("<?xml? encoding=\"ignored\"?>", "utf-8"),
    ] {
        assert_eq!(headers_footers_encoding(xml), expected, "{xml:?}");
    }
}

#[test]
fn reference_versions_keep_their_own_unicode_boundaries() {
    let value = "<?xml \u{11f02}encoding=\"x\"?>";
    assert_eq!(headers_footers_encoding_with_unicode(value, UnicodeWordVersion::Python311), "x");
    assert_eq!(headers_footers_encoding_with_unicode(value, UnicodeWordVersion::Python312), "utf-8");
    assert_eq!(headers_footers_encoding("<?xml \u{8d4}encoding=\"x\"?>"), "x");
    assert_eq!(headers_footers_encoding("<?xml \u{88f}encoding=\"x\"?>"), "x");
}
