//! Render DOCX templates without Python, for browsers (wasm32) and other
//! embedders, over the native Rust core.
//!
//! - [`render`] renders a template with a JSON context;
//! - [`environment`] is the Jinja environment every part uses: the core's
//!   environment with Jinja2 builtins;
//! - [`value`] and [`document`] let a host prepare contexts with pictures,
//!   rich text and subdocuments, add filters, and choose docxtpl/python-docx
//!   compatibility;
//! - [`abi`] is the raw WebAssembly ABI (`docxtpl_render`,
//!   `docxtpl_render_string`), which embedders extend with their own exports.

use docxtplrs::template::{TplCore, make_env};
use minijinja::Value;

pub mod abi;
mod builtins;
pub mod document;
pub mod value;

/// Render `template` (DOCX bytes) with `context` (a JSON object).
pub fn render(template: &[u8], context: &[u8], autoescape: bool) -> Result<Vec<u8>, String> {
    let context: serde_json::Value = serde_json::from_slice(context).map_err(|error| format!("invalid context JSON: {error}"))?;
    if !context.is_object() {
        return Err("context must be a JSON object".to_owned());
    }
    let context = Value::from_serialize(&context);
    let mut core = TplCore::new(template.to_vec());
    let env = environment(&core, autoescape);
    core.env_caches[usize::from(autoescape)] = Some(docxtplrs::template::EnvCache {
        autoescape, r#gen: core.env_gen, env_options: core.env_options.clone(), env, tpl_hashes: Default::default(),
    });
    core.render(autoescape, &|_core, _part| Ok(context.clone()))?;
    core.save_bytes()
}

/// The Jinja environment used for every part: the core's environment with
/// Jinja2 compatibility and Jinja2 builtins.
pub fn environment(core: &TplCore, autoescape: bool) -> minijinja::Environment<'static> {
    let mut env = make_env(autoescape, core);
    jinja::add_jinja2_compat(&mut env);
    builtins::register(&mut env);
    env
}

/// Python `str(value)` for template values.
pub fn py_str(value: &Value) -> String { builtins::py_str(value) }

/// Render a bare Jinja source with the document environment (conformance tests).
pub fn render_string(source: &str, context: &serde_json::Value) -> Result<String, String> {
    let env = environment(&TplCore::default(), false);
    env.render_str(source, Value::from_serialize(context)).map_err(|error| format!("{:?}: {error}", error.kind()))
}

/// Render a template with a JSON context. Returns 0 with the DOCX, or 1 with
/// the error message (see [`abi`]).
///
/// # Safety
/// Both input ranges must be readable for their lengths.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn docxtpl_render(template: *const u8, template_len: usize,
    context: *const u8, context_len: usize, autoescape: u32) -> u32 {
    let (template, context) = unsafe { (abi::input(template, template_len), abi::input(context, context_len)) };
    abi::finish(render(template, context, autoescape != 0))
}

/// Render a bare Jinja source with a JSON context (conformance checks).
/// Returns 0 with the text, or 1 with the error message.
///
/// # Safety
/// Both input ranges must be readable for their lengths.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn docxtpl_render_string(source: *const u8, source_len: usize,
    context: *const u8, context_len: usize) -> u32 {
    let (source, context) = unsafe { (abi::input(source, source_len), abi::input(context, context_len)) };
    let result = std::str::from_utf8(source).map_err(|error| error.to_string()).and_then(|source| {
        let context: serde_json::Value = serde_json::from_slice(context).map_err(|error| error.to_string())?;
        render_string(source, &context)
    });
    abi::finish(result.map(String::into_bytes))
}

#[cfg(test)]
mod tests {
    use crate::document::{DocumentOptions, render_document, render_subdocument};
    use crate::value::{Ctx, Image, RunStyle, rich_run};

    const MINIMAL: &[u8] = include_bytes!("../../docxtpl-core/tests/external-consumer/minimal.docx");

    #[test]
    fn renders_and_reports_errors_as_messages() {
        let output = super::render(MINIMAL, br#"{"name": "Ana"}"#, false).unwrap();
        assert_eq!(&output[..2], b"PK");
        assert!(super::render(MINIMAL, b"[]", false).unwrap_err().contains("JSON object"));
        assert!(super::render(MINIMAL, b"{", false).unwrap_err().contains("invalid context JSON"));
        assert!(super::render(b"not a zip", b"{}", false).is_err());
    }

    #[test]
    fn renders_prepared_contexts_with_filters() {
        let context = Ctx::Map(vec![
            ("name".into(), Ctx::Rich(rich_run("Ana & Bia", &RunStyle::default()))),
            ("picture".into(), Ctx::Image(Image { bytes: Vec::new().into(), width: None, height: None })),
        ]);
        let options = DocumentOptions { self_reference: Some("_context".into()), python_numbering: true, ..Default::default() };
        let output = render_document(MINIMAL, &context, &options, &|env, pictures| {
            let pictures = pictures.clone();
            env.add_filter("known", move |value: minijinja::Value| pictures.get(&value).is_some());
        }).unwrap();
        assert_eq!(&output[..2], b"PK");
        assert_eq!(&render_subdocument(MINIMAL, &context, &options).unwrap()[..2], b"PK");
    }

    /// docxtpl 0.17 leaves footnote tags unrendered; they always render here.
    #[test]
    fn renders_footnotes() {
        const FOOTNOTES: &str = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml";
        const RELATIONSHIP: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes";
        let mut package = docxtplrs::package::Package::from_bytes(&docxtplrs::package::minimal_docx(
            "<w:p><w:r><w:t>body</w:t></w:r><w:r><w:footnoteReference w:id=\"1\"/></w:r></w:p>")).unwrap();
        package.set("word/footnotes.xml", format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n<w:footnotes xmlns:w=\"{}\"><w:footnote w:id=\"1\"><w:p><w:r><w:t>{{{{ name }}}}</w:t></w:r></w:p></w:footnote></w:footnotes>",
            "http://schemas.openxmlformats.org/wordprocessingml/2006/main").into_bytes());
        let types = package.get_string("[Content_Types].xml").unwrap().replace("</Types>",
            &format!("<Override PartName=\"/word/footnotes.xml\" ContentType=\"{FOOTNOTES}\"/></Types>"));
        package.set("[Content_Types].xml", types.into_bytes());
        let mut rels = (*package.rels("word/document.xml")).clone();
        rels.add(RELATIONSHIP, "footnotes.xml", false);
        package.save_rels("word/document.xml", &rels);
        let template = package.to_bytes().unwrap();
        let context = Ctx::Map(vec![("name".into(), Ctx::from_json(&serde_json::json!("Ana")))]);
        let output = render_document(&template, &context, &DocumentOptions::default(), &|_, _| {}).unwrap();
        let notes = docxtplrs::package::Package::from_bytes(&output).unwrap().get_string("word/footnotes.xml").unwrap();
        assert!(notes.contains(">Ana</w:t>") && !notes.contains("{{"), "{notes}");
    }

    /// A 1x1 PNG.
    const PIXEL: &[u8] = &[
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4,
        0x89, 0x00, 0x00, 0x00, 0x0a, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9c, 0x63, 0x00, 0x01, 0x00, 0x00,
        0x05, 0x00, 0x01, 0x0d, 0x0a, 0x2d, 0xb4, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44, 0xae,
        0x42, 0x60, 0x82,
    ];

    /// The body of `body` rendered with a `picture`; filters from `configure`.
    fn render_picture_body(body: &str, options: &DocumentOptions,
        configure: &dyn Fn(&mut minijinja::Environment<'static>, &crate::document::Pictures)) -> (String, String) {
        let template = docxtplrs::package::minimal_docx(&format!("<w:p><w:r><w:t>{body}</w:t></w:r></w:p>"));
        let context = Ctx::Map(vec![("picture".into(), Ctx::Image(Image { bytes: PIXEL.into(), width: None, height: None }))]);
        let output = render_document(&template, &context, options, configure).unwrap();
        let package = docxtplrs::package::Package::from_bytes(&output).unwrap();
        (package.get_string("word/document.xml").unwrap(), package.get_string("word/_rels/document.xml.rels").unwrap_or_default())
    }

    /// `str(InlineImage)` is the picture's run XML, so a template can test or
    /// edit a picture's markup while the part renders.
    #[test]
    fn stringifies_pictures_as_their_markup() {
        for python_numbering in [false, true] {
            let options = DocumentOptions { python_numbering, ..Default::default() };
            let (body, rels) = render_picture_body(
                "{% set piece = picture|string %}[{{ 'pic:pic' in piece }}]{{ piece|replace('</pic:spPr>', '<a:ln/></pic:spPr>')|safe }}{{ picture }}",
                &options, &|_, _| {});
            assert!(body.contains("[True]") || body.contains("[true]"), "{body}");
            assert_eq!(body.matches("<w:drawing>").count(), 2, "{body}");
            assert_eq!(body.matches("<a:ln/></pic:spPr>").count(), 1, "{body}");
            assert!(!body.contains("\u{1}DTPLD"), "{body}");
            let embeds: Vec<&str> = body.split("r:embed=\"").skip(1).map(|rest| &rest[..rest.find('"').unwrap()]).collect();
            assert_eq!(embeds.len(), 2, "{body}");
            assert!(embeds.iter().all(|id| rels.contains(&format!("Id=\"{id}\""))), "{rels}");
            if !python_numbering {
                let ids: Vec<&str> = body.split("<wp:docPr id=\"").skip(1).map(|rest| &rest[..rest.find('"').unwrap()]).collect();
                assert_eq!(ids.len(), 2, "{body}");
                assert_ne!(ids[0], ids[1], "{body}");
            }
        }
    }

    /// A picture a filter creates while the part renders stringifies too.
    #[test]
    fn stringifies_pictures_created_by_filters() {
        let (body, _) = render_picture_body("[{{ 'pic:pic' in (picture|copy|string) }}]", &DocumentOptions::default(), &|env, pictures| {
            let pictures = pictures.clone();
            env.add_filter("copy", move |value: minijinja::Value| match pictures.get(&value) {
                Some(image) => pictures.create(image),
                None => value,
            });
        });
        assert!(body.contains("[True]") || body.contains("[true]"), "{body}");
    }

    /// Text without pictures stringifies as before.
    #[test]
    fn stringifies_other_values_unchanged() {
        let (body, _) = render_picture_body("[{{ 12|string }}][{{ ('a' ~ 'b')|string }}]", &DocumentOptions::default(), &|_, _| {});
        assert!(body.contains("[12][ab]"), "{body}");
    }

    #[test]
    fn compares_like_python() {
        let value = Ctx::from_json(&serde_json::json!({"a": [true, 1.0]}));
        assert!(value.py_eq(&serde_json::json!({"a": [1, 1]})));
        assert!(!value.py_eq(&serde_json::json!({"a": [1]})));
    }
}
