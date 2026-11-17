use docxtpl_core::opc;
use docxtplrs::package::{minimal_docx, Package};
use std::collections::HashSet;

#[test]
fn only_selected_rendered_parts_preserve_new_blank_nodes() {
    let source = minimal_docx("<w:p>  <w:r/>  </w:p>");
    let mut package = Package::from_bytes(&source).unwrap();
    let types = package.get_string("[Content_Types].xml").unwrap().replace("</Types>",
        "<Override PartName=\"/word/header1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml\"/></Types>");
    package.set("[Content_Types].xml", types.into_bytes());
    package.set("word/header1.xml", b"<w:hdr xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:p>  <w:r/>  </w:p></w:hdr>".to_vec());
    package.set("word/_rels/document.xml.rels", b"<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/header' Target='header1.xml'/></Relationships>".to_vec());
    let selected = HashSet::from(["word/document.xml".to_owned()]);
    let output = Package::from_bytes(&opc::serialize_rendered(&package, &selected).unwrap()).unwrap();
    let document = output.get_string("word/document.xml").unwrap();
    let header = output.get_string("word/header1.xml").unwrap();
    assert!(document.contains("<w:p>  <w:r/>  </w:p>"));
    assert!(header.contains("<w:p><w:r/></w:p>"));
}

#[test]
fn raw_body_is_preserved_without_disabling_other_part_validation() {
    let source = minimal_docx("<w:p><w:r/></w:p>");
    let mut package = Package::from_bytes(&source).unwrap();
    let original = package.get_string("word/document.xml").unwrap();
    let invalid = original.replace("<w:r/>", "<w:r data=\"A\u{fffe}B\"/>");
    assert_ne!(original, invalid);
    package.set("word/document.xml", invalid.as_bytes().to_vec());
    let selected = HashSet::from(["word/document.xml".to_owned()]);
    assert!(opc::serialize_rendered(&package, &selected).is_err());
    let bytes = opc::serialize_rendered_with_raw_body(&package, &selected).unwrap();
    let output = Package::from_bytes(&bytes).unwrap();
    assert_eq!(output.get_arc("word/document.xml").unwrap().as_ref(), invalid.as_bytes());

    let types = package.get_string("[Content_Types].xml").unwrap().replace("</Types>",
        "<Override PartName=\"/word/header1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml\"/></Types>");
    package.set("[Content_Types].xml", types.into_bytes());
    package.set("word/header1.xml", "<w:hdr xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:p data='\u{fffe}'/></w:hdr>".as_bytes().to_vec());
    package.set("word/_rels/document.xml.rels", b"<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/header' Target='header1.xml'/></Relationships>".to_vec());
    assert!(opc::serialize_rendered_with_raw_body(&package, &selected).is_err());
}
