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
    /// Jinja2 3.1 with docxtpl's `RichText('Keep')` as `r` and `RichText()` as
    /// `e`: (autoescape, template, Some(output) or None for an error).
    const RICHTEXT_REFERENCE: &[(bool, &str, Option<&str>)] = &[
        (false, r#"{{ r is string }}"#, Some(r#"False"#)),
        (false, r#"{{ r is mapping }}"#, Some(r#"False"#)),
        (false, r#"{{ r is sequence }}"#, Some(r#"False"#)),
        (false, r#"{{ r is iterable }}"#, Some(r#"False"#)),
        (false, r#"{{ r is escaped }}"#, Some(r#"True"#)),
        (false, r#"{{ r is defined }}"#, Some(r#"True"#)),
        (false, r#"{{ r is none }}"#, Some(r#"False"#)),
        (false, r#"{{ r is number }}"#, Some(r#"False"#)),
        (false, r#"{{ r.xml is defined }}"#, Some(r#"True"#)),
        (false, r#"{{ r.xml is string }}"#, Some(r#"True"#)),
        (false, r#"{{ r.xml }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (false, r#"{{ r }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (false, r#"{{ r ~ '|' }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>|"#)),
        (false, r#"{{ r == 'Keep' }}"#, Some(r#"False"#)),
        (false, r#"{{ r != 'Keep' }}"#, Some(r#"True"#)),
        (false, r#"{% if r %}T{% else %}F{% endif %}"#, Some(r#"T"#)),
        (false, r#"{% if e %}T{% else %}F{% endif %}"#, Some(r#"T"#)),
        (false, r#"[{{ e }}]"#, Some(r#"[]"#)),
        (false, r#"{{ e is string }}"#, Some(r#"False"#)),
        (false, r#"{{ r|e }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (false, r#"{{ r|escape }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (false, r#"{{ r|safe }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (false, r#"{{ r|string }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (false, r#"{{ r|forceescape }}"#, Some(r#"&lt;w:r&gt;&lt;w:t xml:space=&#34;preserve&#34;&gt;Keep&lt;/w:t&gt;&lt;/w:r&gt;"#)),
        (false, r#"{{ r|trim }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (false, r#"{{ r|upper }}"#, Some(r#"<W:R><W:T XML:SPACE="PRESERVE">KEEP</W:T></W:R>"#)),
        (false, r#"{{ r|replace('Keep', 'Drop') }}"#, Some(r#"<w:r><w:t xml:space="preserve">Drop</w:t></w:r>"#)),
        (false, r#"{{ r|striptags }}"#, Some(r#"Keep"#)),
        (false, r#"{{ 'Keep' in r|e }}"#, Some(r#"True"#)),
        (false, r#"{{ 'Keep' in r|string }}"#, Some(r#"True"#)),
        (false, r#"{{ [r, r]|join(',') }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>,<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (false, r#"{{ r|int }}"#, Some(r#"0"#)),
        (false, r#"{{ r|float }}"#, Some(r#"0.0"#)),
        (false, r#"{{ r|default('x') }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (false, r#"{{ r.missing is defined }}"#, Some(r#"False"#)),
        (false, r#"{{ '%s!'|format(r) }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>!"#)),
        (false, r#"{{ '%s!' % r }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>!"#)),
        (false, r#"{{ r + 'x' }}"#, None),
        (false, r#"{{ r|list }}"#, None),
        (false, r#"{{ r|tojson }}"#, None),
        (false, r#"{{ r|first }}"#, None),
        (true, r#"{{ r is string }}"#, Some(r#"False"#)),
        (true, r#"{{ r is mapping }}"#, Some(r#"False"#)),
        (true, r#"{{ r is sequence }}"#, Some(r#"False"#)),
        (true, r#"{{ r is iterable }}"#, Some(r#"False"#)),
        (true, r#"{{ r is escaped }}"#, Some(r#"True"#)),
        (true, r#"{{ r is defined }}"#, Some(r#"True"#)),
        (true, r#"{{ r is none }}"#, Some(r#"False"#)),
        (true, r#"{{ r is number }}"#, Some(r#"False"#)),
        (true, r#"{{ r.xml is defined }}"#, Some(r#"True"#)),
        (true, r#"{{ r.xml is string }}"#, Some(r#"True"#)),
        (true, r#"{{ r.xml }}"#, Some(r#"&lt;w:r&gt;&lt;w:t xml:space=&#34;preserve&#34;&gt;Keep&lt;/w:t&gt;&lt;/w:r&gt;"#)),
        (true, r#"{{ r }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (true, r#"{{ r ~ '|' }}"#, Some(r#"&lt;w:r&gt;&lt;w:t xml:space=&#34;preserve&#34;&gt;Keep&lt;/w:t&gt;&lt;/w:r&gt;|"#)),
        (true, r#"{{ r == 'Keep' }}"#, Some(r#"False"#)),
        (true, r#"{{ r != 'Keep' }}"#, Some(r#"True"#)),
        (true, r#"{% if r %}T{% else %}F{% endif %}"#, Some(r#"T"#)),
        (true, r#"{% if e %}T{% else %}F{% endif %}"#, Some(r#"T"#)),
        (true, r#"[{{ e }}]"#, Some(r#"[]"#)),
        (true, r#"{{ e is string }}"#, Some(r#"False"#)),
        (true, r#"{{ r|e }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (true, r#"{{ r|escape }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (true, r#"{{ r|safe }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (true, r#"{{ r|string }}"#, Some(r#"&lt;w:r&gt;&lt;w:t xml:space=&#34;preserve&#34;&gt;Keep&lt;/w:t&gt;&lt;/w:r&gt;"#)),
        (true, r#"{{ r|forceescape }}"#, Some(r#"&lt;w:r&gt;&lt;w:t xml:space=&#34;preserve&#34;&gt;Keep&lt;/w:t&gt;&lt;/w:r&gt;"#)),
        (true, r#"{{ r|trim }}"#, Some(r#"&lt;w:r&gt;&lt;w:t xml:space=&#34;preserve&#34;&gt;Keep&lt;/w:t&gt;&lt;/w:r&gt;"#)),
        (true, r#"{{ r|upper }}"#, Some(r#"&lt;W:R&gt;&lt;W:T XML:SPACE=&#34;PRESERVE&#34;&gt;KEEP&lt;/W:T&gt;&lt;/W:R&gt;"#)),
        (true, r#"{{ r|replace('Keep', 'Drop') }}"#, Some(r#"&lt;w:r&gt;&lt;w:t xml:space=&#34;preserve&#34;&gt;Drop&lt;/w:t&gt;&lt;/w:r&gt;"#)),
        (true, r#"{{ r|striptags }}"#, Some(r#"Keep"#)),
        (true, r#"{{ 'Keep' in r|e }}"#, Some(r#"True"#)),
        (true, r#"{{ 'Keep' in r|string }}"#, Some(r#"True"#)),
        (true, r#"{{ [r, r]|join(',') }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>,<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (true, r#"{{ r|int }}"#, Some(r#"0"#)),
        (true, r#"{{ r|float }}"#, Some(r#"0.0"#)),
        (true, r#"{{ r|default('x') }}"#, Some(r#"<w:r><w:t xml:space="preserve">Keep</w:t></w:r>"#)),
        (true, r#"{{ r.missing is defined }}"#, Some(r#"False"#)),
        (true, r#"{{ '%s!'|format(r) }}"#, Some(r#"&lt;w:r&gt;&lt;w:t xml:space=&#34;preserve&#34;&gt;Keep&lt;/w:t&gt;&lt;/w:r&gt;!"#)),
        (true, r#"{{ '%s!' % r }}"#, Some(r#"&lt;w:r&gt;&lt;w:t xml:space=&#34;preserve&#34;&gt;Keep&lt;/w:t&gt;&lt;/w:r&gt;!"#)),
        (true, r#"{{ r + 'x' }}"#, None),
        (true, r#"{{ r|list }}"#, None),
        (true, r#"{{ r|tojson }}"#, None),
        (true, r#"{{ r|first }}"#, None),
    ];

    /// `RichText('Keep')` and `RichText()` rendered with the document
    /// environment.
    fn render_rich(source: &str, autoescape: bool) -> Result<String, minijinja::Error> {
        let env = super::environment(&docxtplrs::template::TplCore::default(), autoescape);
        let context = minijinja::context! {
            r => crate::value::RichText::value(&rich_run("Keep", &RunStyle::default())),
            e => crate::value::RichText::value(""),
        };
        env.render_str(source, context)
    }

    /// A RichText behaves as docxtpl's Python object does under Jinja2: it is
    /// not a string, has its run XML as `xml`, prints that XML unescaped and
    /// is true even when empty. Errors are compared by occurrence only.
    #[test]
    fn richtext_behaves_like_python() {
        let mismatches: Vec<String> = RICHTEXT_REFERENCE.iter().filter_map(|&(autoescape, source, expected)| {
            let actual = render_rich(source, autoescape);
            let same = match expected {
                Some(expected) => actual.as_deref().ok() == Some(expected),
                None => actual.is_err(),
            };
            (!same).then(|| format!("autoescape={autoescape} {source}: expected {expected:?}, got {actual:?}"))
        }).collect();
        assert!(mismatches.is_empty(), "{}", mismatches.join("\n"));
    }

    /// A prepared RichText reaches the template as RichText, not as a string.
    #[test]
    fn documents_receive_richtext_objects() {
        let template = docxtplrs::package::minimal_docx(
            "<w:p><w:r><w:t>[{{ r is string }}][{{ r.xml is defined }}][{% if e %}T{% endif %}]</w:t></w:r></w:p><w:p>{{r r }}</w:p>");
        let context = Ctx::Map(vec![
            ("r".into(), Ctx::Rich(rich_run("Keep", &RunStyle::default()))),
            ("e".into(), Ctx::Rich(String::new())),
        ]);
        let output = render_document(&template, &context, &DocumentOptions::default(), &|_, _| {}).unwrap();
        let body = docxtplrs::package::Package::from_bytes(&output).unwrap().get_string("word/document.xml").unwrap();
        assert!(body.contains("[False][True][T]"), "{body}");
        assert!(body.contains("<w:t xml:space=\"preserve\">Keep</w:t>"), "{body}");
    }

    /// Where Jinja2 raises TypeError, `|length` gives 0 (as for any value
    /// that cannot be iterated) and `in` gives false (as for any plain
    /// object). No render that succeeds under Jinja2 depends on either.
    #[test]
    fn richtext_differences_where_jinja2_raises() {
        for autoescape in [false, true] {
            assert_eq!(render_rich("{{ r|length }}", autoescape).unwrap(), "0");
            assert_eq!(render_rich("{{ 'Keep' in r }}", autoescape).unwrap(), "False");
        }
    }

    /// Templates read a value's text through `xml` when it is not a string;
    /// with RichText as a string the macro returned the run XML and the
    /// comparison was false.
    #[test]
    fn richtext_text_reads_through_xml() {
        let source = concat!(
            "{% macro plain(v) %}{% if v is string %}{{ v }}",
            "{% elif v is not mapping and v.xml is defined and v.xml is string %}",
            "{% set rt = namespace(text='') %}",
            "{% for piece in v.xml.split('<w:t xml:space=\"preserve\">')[1:] %}",
            "{% set rt.text = rt.text ~ piece.split('</w:t>')[0] %}{% endfor %}{{ rt.text }}",
            "{% endif %}{% endmacro %}",
            "{% set d = plain(r) if r is defined and r is not string and r is not mapping and r.xml is defined and r.xml is string else r %}",
            "[{{ plain(r) }}][{{ d == 'Keep' }}][{{ d is string }}]",
        );
        for autoescape in [false, true] {
            assert_eq!(render_rich(source, autoescape).unwrap(), "[Keep][True][True]");
        }
    }
}
