//! Core template engine: render pipeline, fix_tables, docPr ids,
//! headers/footers, properties, footnotes, replacements, save.

use crate::package::{crc32, rel_type, resolve_target, Package};
use crate::patch::{decode_text_entities, patch_xml, resolve_listing, sub_str};
use crate::xmldom::{Document, Element, Node};
use minijinja::{AutoEscape, Environment, Value};
#[cfg(feature = "python")]
use pyo3::prelude::PyAnyMethods as _;
use std::borrow::Cow;
use std::collections::{HashMap, HashSet};

pub const DOCUMENT_PART: &str = "word/document.xml";

/// A context value whose XML must be materialized lazily (only if actually
/// printed by the template), like docxtpl's __str__-based InlineImage/Subdoc.
/// Blob fields are Arc-shared so registering/cloning a Deferred is O(1).
#[derive(Debug, Clone)]
pub enum Deferred {
    Image {
        blob: std::sync::Arc<[u8]>,
        /// Metadata obtained from the caller's live stream, when available.
        info: Option<crate::image::ImageInfo>,
        filename: Option<String>,
        width: Option<i64>,
        height: Option<i64>,
        anchor: Option<String>,
        title: Option<String>,
        descr: Option<String>,
    },
    Subdoc {
        bytes: Option<std::sync::Arc<[u8]>>,
        /// preserve the subdoc's section properties (page setup, headers/
        /// footers) as its own section instead of dropping them
        keep_sections: bool,
    },
    SubdocBlocks {
        blocks: std::sync::Arc<Vec<crate::subdocbuilder::Block>>,
    },
}

/// An output fragment after XML transformations. Only an explicitly tagged
/// deferred index may produce package content; text is never parsed as a token.
#[derive(Clone, Copy)]
pub enum MaterializationFragment<'a> {
    Text(&'a str),
    Deferred(usize),
}

/// Owned fragments returned by an optional host provenance adapter.
#[derive(Debug)]
pub enum OwnedMaterializationFragment {
    Text(String),
    /// Host Unicode code points, including isolated surrogates.
    Unicode(Vec<u32>),
    Deferred(usize),
}

/// Converts VM values into materialization fragments without parsing text.
#[derive(Clone)]
pub struct HostFragmentConverter(pub std::sync::Arc<dyn Fn(Vec<Value>) -> Result<Vec<OwnedMaterializationFragment>, minijinja::Error> + Send + Sync>);

impl std::fmt::Debug for HostFragmentConverter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("HostFragmentConverter(..)") }
}

/// Materialize host Unicode fragments while retaining deferred provenance.
#[derive(Clone)]
pub struct HostUnicodeMaterializer(pub std::sync::Arc<dyn Fn(&mut TplCore, &str, &[OwnedMaterializationFragment]) -> Result<String, String> + Send + Sync>);

impl std::fmt::Debug for HostUnicodeMaterializer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("HostUnicodeMaterializer(..)") }
}

/// Host recovery for rendered XML that needs an owned runtime context.
#[derive(Clone)]
pub struct HostFragmentRecovery(pub std::sync::Arc<dyn Fn(&str) -> Result<String, String> + Send + Sync>);

impl std::fmt::Debug for HostFragmentRecovery {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("HostFragmentRecovery(..)") }
}

#[cfg(test)]
mod materialization_fragment_tests {
    use super::{Deferred, MaterializationFragment as Fragment, TplCore, DOCUMENT_PART};
    use std::sync::Arc;

    fn core_with_image() -> TplCore {
        let mut image = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR".to_vec();
        image.extend_from_slice(&2u32.to_be_bytes());
        image.extend_from_slice(&2u32.to_be_bytes());
        image.extend_from_slice(&[8, 6, 0, 0, 0, 0, 0, 0, 0]);
        let mut core = TplCore { original_bytes: crate::package::minimal_docx("").into(), ..Default::default() };
        core.init_docx(false).unwrap();
        core.deferred.push(Deferred::Image { blob: Arc::from(image), info: None, filename: None,
            width: None, height: None, anchor: None, title: None, descr: None });
        core
    }

    #[test]
    fn plain_text_equal_to_a_token_never_materializes() {
        let mut core = core_with_image();
        let text = "\u{1}DTPLD0\u{1}";
        let output = core.materialize_output_fragments(DOCUMENT_PART,
            [Fragment::Text("\u{1}DTPL"), Fragment::Text("D0\u{1}")]).unwrap();
        assert_eq!(output, text);
        assert!(!core.package.as_ref().unwrap().contains("word/media/image1.png"));
    }

    #[test]
    fn explicit_deferred_values_materialize_and_reuse_the_result() {
        let mut core = core_with_image();
        let output = core.materialize_output_fragments(DOCUMENT_PART,
            [Fragment::Text("start"), Fragment::Deferred(0), Fragment::Text("middle"),
             Fragment::Deferred(0), Fragment::Text("end")]).unwrap();
        assert!(output.starts_with("start"));
        assert!(output.ends_with("end"));
        let (first, second) = output[5..output.len()-3].split_once("middle").unwrap();
        assert_eq!(first, second);
        assert!(first.contains("<w:drawing>"));
        assert!(core.package.as_ref().unwrap().contains("word/media/image1.png"));
    }

    #[test]
    fn legacy_scanner_keeps_its_existing_token_and_invalid_text_behavior() {
        let mut legacy = core_with_image();
        let mut typed = core_with_image();
        let token = "\u{1}DTPLD0\u{1}";
        let malformed = "\u{1}other\u{1}\u{1}DTPLD9\u{1}";
        let legacy_output = legacy.materialize_deferred(DOCUMENT_PART,
            format!("before{token}middle{token}{malformed}after")).unwrap();
        let typed_output = typed.materialize_output_fragments(DOCUMENT_PART,
            [Fragment::Text("before"), Fragment::Deferred(0), Fragment::Text("middle"),
             Fragment::Deferred(0), Fragment::Text(malformed), Fragment::Text("after")]).unwrap();
        assert_eq!(legacy_output, typed_output);
        assert!(typed.materialize_output_fragments(DOCUMENT_PART, [Fragment::Deferred(1)])
            .unwrap_err().contains("unknown deferred index 1"));
        assert!(typed.materialize_one(DOCUMENT_PART, 1)
            .unwrap_err().contains("unknown deferred index 1"));
    }

    #[test]
    fn transformed_fragments_match_legacy_order_for_listing_and_delimiters() {
        let mut legacy = core_with_image();
        let mut typed = core_with_image();
        let before = "\n<w:p><w:pPr><w:keepNext/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>A\tB";
        let after = "C\nD\u{7}E\u{c}F{_{_%</w:t></w:r></w:p>";
        let raw = format!("{before}{}{after}", super::deferred_token(0));
        let raw = super::remove_paragraph_newlines(&raw).into_owned();
        let raw = super::restore_escaped_delims(raw);
        let raw = crate::patch::resolve_listing(&raw).into_owned();
        let expected = legacy.materialize_deferred(DOCUMENT_PART, raw).unwrap();
        let actual = typed.materialize_transformed_output(DOCUMENT_PART,
            &[Fragment::Text(before), Fragment::Deferred(0), Fragment::Text(after)]).unwrap();
        assert_eq!(actual, expected);
    }

    #[test]
    fn transformed_fragments_keep_forged_and_split_markers_as_text() {
        let mut core = core_with_image();
        let actual = core.materialize_transformed_output(DOCUMENT_PART, &[
            Fragment::Text("<w:p><w:r><w:t>\u{1}DTPL"), Fragment::Text("D0\u{1}"),
            Fragment::Text("\u{1}0Z0\u{2}\u{1}1Z0\u{2}"), Fragment::Deferred(0),
            Fragment::Text("</w:t></w:r></w:p>"),
        ]).unwrap();
        assert!(actual.contains("\u{1}DTPLD0\u{1}\u{1}0Z0\u{2}\u{1}1Z0\u{2}"));
        assert_eq!(actual.matches("<w:drawing>").count(), 1);
    }

    #[test]
    fn transformed_fragments_preserve_deferred_values_copied_by_listing() {
        let mut legacy = core_with_image();
        let mut typed = core_with_image();
        let before = "<w:p><w:r><w:rPr><custom>";
        let after = "</custom></w:rPr><w:t>A\tB</w:t></w:r></w:p>";
        let raw = format!("{before}{}{after}", super::deferred_token(0));
        let raw = crate::patch::resolve_listing(&raw).into_owned();
        let expected = legacy.materialize_deferred(DOCUMENT_PART, raw).unwrap();
        let actual = typed.materialize_transformed_output(DOCUMENT_PART,
            &[Fragment::Text(before), Fragment::Deferred(0), Fragment::Text(after)]).unwrap();
        assert_eq!(actual, expected);
        assert!(actual.matches("<w:drawing>").count() > 1);
    }

    #[test]
    fn transformed_fragments_reject_unknown_indices_without_mutation() {
        let mut core = core_with_image();
        let error = core.materialize_transformed_output(DOCUMENT_PART,
            &[Fragment::Deferred(0), Fragment::Deferred(1)]).unwrap_err();
        assert!(error.contains("unknown deferred index 1"));
        assert!(!core.package.as_ref().unwrap().contains("word/media/image1.png"));
    }

    #[test]
    fn host_fragment_renderer_preserves_untrusted_text_and_deferred_identity() {
        #[derive(Debug)]
        struct DeferredValue(usize);
        impl minijinja::value::Object for DeferredValue {}

        let mut core = core_with_image();
        core.host_fragment_renderer = Some(super::HostFragmentRenderer(Arc::new(|_env, _template| {
            Ok(vec![
                minijinja::Value::from("<w:p><w:r><w:t>\u{1}DTPLD0\u{1}"),
                minijinja::Value::from_object(DeferredValue(0)),
                minijinja::Value::from("\tend</w:t></w:r></w:p>"),
            ])
        })));
        core.host_fragment_converter = Some(super::HostFragmentConverter(Arc::new(|values| {
            Ok(values.into_iter().map(|value| match value.downcast_object_ref::<DeferredValue>() {
                Some(deferred) => super::OwnedMaterializationFragment::Deferred(deferred.0),
                None => super::OwnedMaterializationFragment::Text(value.to_string()),
            }).collect())
        })));
        let actual = core.render_part(DOCUMENT_PART, "<w:p/>", false, &|_, _| Ok(minijinja::Value::from(()))).unwrap();
        assert!(actual.contains("\u{1}DTPLD0\u{1}"));
        assert_eq!(actual.matches("<w:drawing>").count(), 1);
        assert!(actual.contains("<w:tab/>"));
    }

    #[test]
    fn host_fragment_renderer_executes_named_template_with_separate_fragments() {
        #[derive(Debug)]
        struct DeferredValue(usize);
        impl minijinja::value::Object for DeferredValue {}

        let mut core = core_with_image();
        core.host_fragment_renderer = Some(super::HostFragmentRenderer(Arc::new(|mut env, template| {
            env.set_formatter(|output, _state, value| {
                if value.downcast_object_ref::<DeferredValue>().is_some() {
                    output.write_fragment(value.clone())
                } else {
                    output.write_str(&value.to_string()).map_err(minijinja::Error::from)
                }
            });
            let joiner = |values: Vec<minijinja::Value>, _: minijinja::AutoEscape| {
                Ok(minijinja::Value::from(values.iter().map(ToString::to_string).collect::<String>()))
            };
            let super::HostTemplate::Named(name) = template else {
                return Err(minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, "expected named template"));
            };
            env.get_template(&name)?.render_unjoined_fragments(minijinja::context!(
                text => "\u{1}DTPLD0\u{1}", image => minijinja::Value::from_object(DeferredValue(0))
            ), &joiner)
        })));
        core.host_fragment_converter = Some(super::HostFragmentConverter(Arc::new(|values| {
            Ok(values.into_iter().map(|value| match value.downcast_object_ref::<DeferredValue>() {
                Some(deferred) => super::OwnedMaterializationFragment::Deferred(deferred.0),
                None => super::OwnedMaterializationFragment::Text(value.to_string()),
            }).collect())
        })));
        let source = "<w:p><w:r><w:t>{{ text }}{{ image }}</w:t></w:r></w:p>";
        let actual = core.render_part(DOCUMENT_PART, source, false, &|_, _| Ok(minijinja::Value::from(()))).unwrap();
        assert!(actual.contains("\u{1}DTPLD0\u{1}"));
        assert_eq!(actual.matches("<w:drawing>").count(), 1);
    }

    #[test]
    fn host_fragment_renderer_errors_never_fall_back_to_text_renderer() {
        use std::sync::atomic::{AtomicBool, Ordering};
        let called = Arc::new(AtomicBool::new(false));
        let mut core = core_with_image();
        core.host_fragment_renderer = Some(super::HostFragmentRenderer(Arc::new(|_env, _template| {
            Err(minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, "fragment render failed"))
        })));
        let callback_called = called.clone();
        core.host_template_renderer = Some(super::HostTemplateRenderer(Arc::new(move |_env, _template| {
            callback_called.store(true, Ordering::Relaxed);
            Ok("fallback".to_owned())
        })));
        core.host_fragment_converter = Some(super::HostFragmentConverter(Arc::new(|_| Ok(Vec::new()))));
        let error = core.render_part(DOCUMENT_PART, "<w:p/>", false, &|_, _| Ok(minijinja::Value::from(()))).unwrap_err();
        assert!(error.contains("fragment render failed"), "{error}");
        assert!(!called.load(Ordering::Relaxed));
    }

    #[test]
    fn host_body_recovery_propagates_errors_without_fallback() {
        let mut core = core_with_image();
        core.host_fragment_renderer = Some(super::HostFragmentRenderer(Arc::new(|_, _| {
            Ok(vec![minijinja::Value::from("<w:p>\u{fffe}</w:p>")])
        })));
        core.host_fragment_converter = Some(super::HostFragmentConverter(Arc::new(|values| {
            Ok(values.into_iter().map(|value| super::OwnedMaterializationFragment::Text(value.to_string())).collect())
        })));
        core.host_fragment_recovery = Some(|_| Ok("fallback".to_owned()));
        core.host_fragment_recovery_adapter = Some(super::HostFragmentRecovery(Arc::new(|_| Err("provider failure".into()))));
        let error = core.render_part(DOCUMENT_PART, "<w:p/>", false, &|_, _| Ok(minijinja::Value::from(()))).unwrap_err();
        assert!(error.contains("provider failure"), "{error}");
        core.host_fragment_recovery_adapter = None;
        let output = core.render_part(DOCUMENT_PART, "<w:p/>", false, &|_, _| Ok(minijinja::Value::from(()))).unwrap();
        assert_eq!(output, "fallback");
    }

    #[test]
    fn host_fragment_renderer_requires_converter_without_fallback() {
        use std::sync::atomic::{AtomicBool, Ordering};
        let called = Arc::new(AtomicBool::new(false));
        let mut core = core_with_image();
        let fragment_called = called.clone();
        core.host_fragment_renderer = Some(super::HostFragmentRenderer(Arc::new(move |_env, _template| {
            fragment_called.store(true, Ordering::Relaxed);
            Ok(Vec::new())
        })));
        let text_called = called.clone();
        core.host_template_renderer = Some(super::HostTemplateRenderer(Arc::new(move |_env, _template| {
            text_called.store(true, Ordering::Relaxed);
            Ok("fallback".to_owned())
        })));
        let error = core.render_part(DOCUMENT_PART, "<w:p/>", false, &|_, _| Ok(minijinja::Value::from(()))).unwrap_err();
        assert!(error.contains("host fragment renderer requires a fragment converter"), "{error}");
        assert!(!called.load(Ordering::Relaxed));
    }

    #[test]
    fn vm_fragments_feed_transformed_materialization_without_forgery() {
        #[derive(Debug)]
        struct DeferredValue(usize);
        impl minijinja::value::Object for DeferredValue {}

        let mut environment = minijinja::Environment::new();
        environment.set_formatter(|output, _state, value| {
            if value.downcast_object_ref::<DeferredValue>().is_some() {
                output.write_fragment(value.clone())
            } else {
                output.write_str(&value.to_string()).map_err(minijinja::Error::from)
            }
        });
        let joiner = |values: Vec<minijinja::Value>, _: minijinja::AutoEscape| {
            Ok(minijinja::Value::from(values.iter().map(ToString::to_string).collect::<String>()))
        };
        let template = environment.template_from_str(
            "<w:p><w:r><w:t>{{ text }}{{ image }}\tend</w:t></w:r></w:p>",
        ).unwrap();
        let output = template.render_unjoined_fragments(minijinja::context!(
            text => "\u{1}DTPLD0\u{1}", image => minijinja::Value::from_object(DeferredValue(0))
        ), &joiner).unwrap();
        let fragments = output.iter().map(|value| match value.downcast_object_ref::<DeferredValue>() {
            Some(value) => Fragment::Deferred(value.0),
            None => Fragment::Text(value.as_str().unwrap()),
        }).collect::<Vec<_>>();
        let mut core = core_with_image();
        let result = core.materialize_transformed_output(DOCUMENT_PART, &fragments).unwrap();
        assert!(result.contains("\u{1}DTPLD0\u{1}"));
        assert_eq!(result.matches("<w:drawing>").count(), 1);
        assert!(result.contains("<w:tab/>"));
    }
}

#[derive(Debug, Default)]
pub struct TplCore {
    /// Preserve host-defined fragment values through nested captures while
    /// rendering parts. The host supplies the environment joiner/formatter.
    pub fragment_joining: bool,
    /// Preserve host values inside captures while keeping root output textual.
    pub host_capture_joining: bool,
    /// Optional adapter for preserving output origins until materialization.
    pub host_fragment_converter: Option<HostFragmentConverter>,
    /// Optional materializer for fragments containing non-scalar Unicode.
    pub host_unicode_materializer: Option<HostUnicodeMaterializer>,
    /// Optional host parser for recovering rendered XML with forbidden characters.
    pub host_fragment_recovery: Option<fn(&str) -> Result<String, String>>,
    pub host_fragment_recovery_adapter: Option<HostFragmentRecovery>,
    #[doc(hidden)]
    pub rendered_fragments: Option<Vec<OwnedMaterializationFragment>>,
    /// Optional caller-owned scope surrounding evaluation of a compiled part.
    pub host_part_scope: Option<HostPartScope>,
    /// Compatibility mode: snapshot context at each part/property instead of
    /// reusing one Value for the whole document. Default preserves upstream.
    pub per_part_context: bool,
    /// Match python-docx image construction before a rendered part is committed.
    pub python_image_contract: bool,
    /// Skip a subdocument numbering catalog when no story references it.
    pub python_subdoc_numbering_contract: bool,
    /// Optional host protocol for generating an nsid at the actual use site.
    /// Optional host protocol for abstract-numbering integer conversion.
    pub numbering_integer: Option<NumberingInteger>,
    /// Caller supplies the Python protocols; do not rewrite their operators,
    /// globals or disabled extension tags into upstream compatibility helpers.
    pub python_source_contract: bool,
    /// Normalize Python-compatible XML parts at load and replacement boundaries.
    pub header_footer_normalizer: Option<fn(&str) -> Result<String, String>>,
    /// Optional caller-owned property renderer; keeps dependency XML policies
    /// replaceable without exposing the active core through raw pointers.
    pub property_renderer: Option<PropertyRenderer>,
    /// Optional language-binding renderer for an already compiled template.
    pub host_template_renderer: Option<HostTemplateRenderer>,
    /// Host render returning origin-preserving fragments for a part.
    pub host_fragment_renderer: Option<HostFragmentRenderer>,
    /// Optional package-aware footnote locator for reference compatibility.
    pub footnote_locator: Option<FootnoteLocator>,
    /// Invalid UTF-8 retained for a language binding to reproduce its native
    /// decode exception without interpreting the part's XML declaration.
    pub footnote_decode_error: Option<std::sync::Arc<[u8]>>,
    pub original_bytes: std::sync::Arc<[u8]>,
    pub package: Option<Package>,
    /// pristine package parsed once from original_bytes; reload clones it
    /// (memcpy) instead of re-inflating the whole zip on every render
    pub original_package: Option<Package>,
    pub is_rendered: bool,
    pub is_saved: bool,
    pub allow_missing_pics: bool,
    pub crc_to_new_media: HashMap<u32, Vec<u8>>,
    pub crc_to_new_embedded: HashMap<u32, Vec<u8>>,
    pub zipname_to_replace: HashMap<String, Vec<u8>>,
    pub pics_to_replace: HashMap<String, Vec<u8>>,
    /// filename -> (target_ref, target_partname)
    pub pic_map: HashMap<String, (String, String)>,
    pub docx_ids_index: u32,
    /// lazily materialized context values (InlineImage / Subdoc)
    pub deferred: Vec<Deferred>,
    /// python object id -> deferred index (per render)
    pub deferred_by_oid: HashMap<usize, usize>,
    /// Render-local XML for subdocuments already attached to a given part.
    pub early_subdoc_cache: HashMap<(usize, String), String>,
    /// Body XML for file subdocuments attached before rendering any part.
    pub attached_subdocuments: HashMap<usize, String>,
    /// user-registered jinja customizations (python callables)
    #[cfg(feature = "python")]
    pub custom_filters: Vec<(String, pyo3::Py<pyo3::PyAny>)>,
    #[cfg(feature = "python")]
    pub custom_tests: Vec<(String, pyo3::Py<pyo3::PyAny>)>,
    #[cfg(feature = "python")]
    pub custom_functions: Vec<(String, pyo3::Py<pyo3::PyAny>)>,
    #[cfg(feature = "python")]
    pub custom_globals: Vec<(String, pyo3::Py<pyo3::PyAny>)>,
    #[cfg(feature = "python")]
    pub template_loader: Option<pyo3::Py<pyo3::PyAny>>,
    /// user-registered template loader for the pure-Rust build
    /// ({% include %}/{% import %}/{% extends %}); a python loader registered
    /// via the python bindings takes precedence when both are set
    pub rust_template_loader: Option<TemplateLoader>,
    /// set when a subdoc was materialized during the current render
    pub used_subdoc: bool,
    /// jinja environment options (duck-typed from jinja_env)
    pub env_options: EnvOptions,
    /// installed gettext catalog for {% trans %} support
    pub gettext_catalog: Option<std::sync::Arc<crate::gettext::Catalog>>,
    /// context lines of the last template error (docx_context attribute)
    pub last_error_context: Vec<String>,
    /// cached parse of word/document.xml for the docmodel facade
    pub doc_cache: Option<crate::xmldom::Document>,
    /// doc_cache has unpersisted mutations
    pub doc_dirty: bool,
    /// cached parses of auxiliary xml parts (word/styles.xml, word/settings.xml,
    /// word/comments.xml, docProps/core.xml, ...) for the docmodel facade;
    /// word/document.xml uses doc_cache instead
    pub part_caches: HashMap<String, crate::xmldom::Document>,
    /// auxiliary part caches with unpersisted mutations
    pub parts_dirty: HashSet<String>,
    /// per-part next wp:docPr shape id (avoids rescanning the part xml for
    /// every inserted image; seeded from the part's current max id)
    pub next_shape_ids: HashMap<String, u32>,
    /// bumped on every document DOM mutation; invalidates the sequential
    /// docmodel access cursors below
    pub doc_gen: u64,
    /// sequential-access cursors (doc_gen, nth-of-tag index, child position)
    /// for docmodel proxies: paragraph/table/row iteration becomes O(1)
    /// amortized instead of rescanning from the parent's head every access
    pub para_cursor: (u64, usize, usize),
    pub tbl_cursor: (u64, usize, usize),
    pub row_cursor: (u64, usize, usize),
    /// style name/id (lowercased) -> resolved styleId; invalidated together
    /// with the styles part cache
    pub style_resolve_cache: HashMap<String, String>,
    /// next free w:bookmark id while merging subdocs: scanned from the
    /// master once, then advanced locally (avoids a full master scan per
    /// subdoc); reset on every render (master reloads)
    pub bookmark_next_id: Option<i64>,
    /// docxcompose Composer._preserved_styles: master style id -> ids of the
    /// equivalent styles (the master id itself plus copies preserved by
    /// earlier merges); only populated by the preserve_styles merge path and
    /// kept across Composer appends
    pub preserved_styles: HashMap<String, Vec<String>>,
    /// cached jinja environments (one slot per autoescape mode) holding the
    /// compiled per-part templates, reused across renders while the env
    /// configuration and the patched part sources are unchanged
    pub env_caches: [Option<EnvCache>; 2],
    /// bumped whenever filters/tests/functions/globals/loader/gettext change,
    /// invalidating env_caches (env_options are compared by value instead)
    pub env_gen: u64,
    /// per-part cache of the pure preprocessing chain (decode entities ->
    /// patch_xml -> paragraph newlines -> trans/bool/engine rewrites):
    /// part -> (raw source hash, preprocessed hash, preprocessed source).
    /// Repeated renders of an unchanged part skip the whole chain (several
    /// full-document scans + copies); Rc-shared so reuse is a refcount bump.
    pub preproc_cache: HashMap<String, (u64, u64, std::rc::Rc<str>)>,
}

/// A cached jinja environment plus the content hashes of the part templates
/// registered in it.
#[derive(Debug)]
pub struct EnvCache {
    pub autoescape: bool,
    pub gen: u64,
    pub env_options: EnvOptions,
    pub env: Environment<'static>,
    /// part name -> fxhash of the preprocessed source registered in `env`
    pub tpl_hashes: HashMap<String, u64>,
}

/// jinja2 environment options supported via duck-typing.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct EnvOptions {
    pub trim_blocks: Option<bool>,
    pub lstrip_blocks: Option<bool>,
    pub keep_trailing_newline: Option<bool>,
    /// "lenient" | "chainable" | "semistrict" | "strict"
    pub undefined_behavior: Option<String>,
}

/// placeholder token for a deferred value inside rendered xml
pub fn deferred_token(idx: usize) -> String {
    format!("\u{1}DTPLD{}\u{1}", idx)
}

/// A pure-Rust template loader: template name -> source (None = not found),
/// backing {% include %}/{% import %}/{% extends %} without python.
#[derive(Clone)]
pub struct TemplateLoader(pub std::sync::Arc<dyn Fn(&str) -> Option<String> + Send + Sync>);

impl std::fmt::Debug for TemplateLoader {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("TemplateLoader(..)")
    }
}

pub type CtxFn<'a> = dyn Fn(&mut TplCore, &str) -> Result<Value, String> + 'a;
pub type PropertyRenderer = fn(&mut TplCore, &CtxFn<'_>) -> Result<(), String>;

/// A cached named template or an independent owned compilation.
pub enum HostTemplate {
    Named(String),
    Owned(minijinja::OwnedTemplate),
}

/// Host rendering keeps the package core out of callbacks and async tasks.
#[derive(Clone)]
pub struct HostTemplateRenderer(pub std::sync::Arc<dyn Fn(Environment<'static>, HostTemplate) -> Result<String, minijinja::Error> + Send + Sync>);

/// Host rendering that retains VM fragment boundaries through an async task.
#[derive(Clone)]
pub struct HostFragmentRenderer(pub std::sync::Arc<dyn Fn(Environment<'static>, HostTemplate) -> Result<Vec<Value>, minijinja::Error> + Send + Sync>);

impl std::fmt::Debug for HostFragmentRenderer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("HostFragmentRenderer(..)") }
}

/// Host-owned conversion protocol for abstract numbering identifiers.
#[derive(Clone)]
pub struct NumberingInteger(pub std::sync::Arc<dyn Fn(&str) -> Result<i64, String> + Send + Sync>);

impl std::fmt::Debug for NumberingInteger {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("NumberingInteger(..)") }
}

/// Runs one part render with caller-owned, render-local resources.
#[derive(Clone)]
pub struct HostPartScope(pub std::sync::Arc<dyn Fn(&mut TplCore, &str,
    &dyn Fn(&mut TplCore) -> Result<String, String>) -> Result<String, String> + Send + Sync>);

impl std::fmt::Debug for HostPartScope {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("HostPartScope(..)") }
}

impl std::fmt::Debug for HostTemplateRenderer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("HostTemplateRenderer(..)")
    }
}
pub type FootnoteLocator = fn(&Package, &str) -> Result<(Vec<String>, usize), String>;

impl TplCore {
    pub fn new(template_bytes: Vec<u8>) -> TplCore {
        TplCore {
            original_bytes: template_bytes.into(),
            ..Default::default()
        }
    }

    /// Register a pure-Rust template loader (template name -> source or None),
    /// enabling {% include %}/{% import %}/{% extends %} without the python
    /// feature. A loader registered through the python bindings takes
    /// precedence when both are set.
    pub fn set_rust_template_loader<F>(&mut self, loader: F)
    where
        F: Fn(&str) -> Option<String> + Send + Sync + 'static,
    {
        self.rust_template_loader = Some(TemplateLoader(std::sync::Arc::new(loader)));
        self.env_gen += 1;
    }

    /// Remove a previously registered pure-Rust template loader.
    pub fn clear_rust_template_loader(&mut self) {
        if self.rust_template_loader.is_some() {
            self.rust_template_loader = None;
            self.env_gen += 1;
        }
    }

    /// Convenience: register one static named template; names not matching
    /// fall through to the previously registered loader (if any).
    pub fn register_template(&mut self, name: &str, source: &str) {
        let name = name.to_string();
        let source = source.to_string();
        let prev = self.rust_template_loader.take();
        self.set_rust_template_loader(move |n| {
            if n == name {
                Some(source.clone())
            } else {
                prev.as_ref().and_then(|p| (p.0)(n))
            }
        });
    }

    pub fn init_docx(&mut self, reload: bool) -> Result<(), String> {
        if self.package.is_none() || (self.is_rendered && reload) {
            // reload clones the pristine package (memcpy) instead of
            // re-inflating every zip entry (incl. multi-MB media)
            self.package = Some(match &self.original_package {
                Some(p) => p.clone(),
                None => {
                    let pkg = Package::from_bytes_arc(self.original_bytes.clone())?;
                    self.original_package = Some(pkg.clone());
                    pkg
                }
            });
            self.is_rendered = false;
            self.invalidate_doc();
            self.invalidate_parts();
        }
        Ok(())
    }

    /// Parsed word/document.xml for the docmodel facade, parsed once and
    /// cached; reparsed after invalidation, on parse failure retried per call.
    pub fn document_dom(&mut self) -> Result<&mut crate::xmldom::Document, String> {
        self.init_docx(false)?;
        if self.doc_cache.is_none() {
            let xml = self
                .package
                .as_ref()
                .and_then(|p| p.get_string(DOCUMENT_PART))
                .ok_or_else(|| "word/document.xml not found".to_string())?;
            self.doc_cache = Some(crate::xmldom::Document::parse(&xml)?);
        }
        Ok(self.doc_cache.as_mut().unwrap())
    }

    pub fn mark_doc_dirty(&mut self) {
        self.doc_dirty = true;
        self.doc_gen += 1;
    }

    /// Serialize the cached document DOM back into the package if mutated.
    pub fn flush_doc(&mut self) -> Result<(), String> {
        if !self.doc_dirty {
            return Ok(());
        }
        let Some(dom) = &self.doc_cache else {
            self.doc_dirty = false;
            return Ok(());
        };
        let out = dom.serialize();
        let pkg = self.pkg()?;
        let enc = pkg.encoding_of(DOCUMENT_PART);
        pkg.set(DOCUMENT_PART, crate::package::encode_part_owned(out, &enc));
        self.doc_dirty = false;
        Ok(())
    }

    /// Drop the cached document DOM (external code replaced the part).
    pub fn invalidate_doc(&mut self) {
        self.doc_cache = None;
        self.doc_dirty = false;
        self.doc_gen += 1;
    }

    /// Parsed DOM of an auxiliary part (styles/settings/comments/core/...),
    /// parsed once and cached; reparsed after invalidation.
    /// word/document.xml is served by document_dom instead.
    pub fn part_dom(&mut self, part: &str) -> Result<&mut crate::xmldom::Document, String> {
        if part == DOCUMENT_PART {
            return self.document_dom();
        }
        self.init_docx(false)?;
        if !self.part_caches.contains_key(part) {
            let xml = self
                .package
                .as_ref()
                .and_then(|p| p.get_string(part))
                .ok_or_else(|| format!("part {} not found", part))?;
            self.part_caches
                .insert(part.to_string(), crate::xmldom::Document::parse(&xml)?);
        }
        Ok(self.part_caches.get_mut(part).unwrap())
    }

    /// Mark a cached part DOM as mutated; it is written back on flush_parts.
    pub fn mark_part_dirty(&mut self, part: &str) {
        if part == DOCUMENT_PART {
            self.doc_dirty = true;
            self.doc_gen += 1;
        } else {
            self.parts_dirty.insert(part.to_string());
            if part == "word/styles.xml" {
                self.style_resolve_cache.clear();
            }
        }
    }

    /// Serialize all dirty cached DOMs (document + auxiliary parts) back
    /// into the package.
    pub fn flush_parts(&mut self) -> Result<(), String> {
        self.flush_doc()?;
        if self.parts_dirty.is_empty() {
            return Ok(());
        }
        let dirty: Vec<String> = self.parts_dirty.drain().collect();
        for part in dirty {
            let Some(out) = self.part_caches.get(&part).map(|d| d.serialize()) else {
                continue;
            };
            let enc = self.pkg()?.encoding_of(&part);
            let bytes = crate::package::encode_part_owned(out, &enc);
            self.pkg()?.set(&part, bytes);
        }
        Ok(())
    }

    /// Drop one cached part DOM (external code replaced that part).
    pub fn invalidate_part(&mut self, part: &str) {
        if part == DOCUMENT_PART {
            self.invalidate_doc();
            return;
        }
        self.part_caches.remove(part);
        self.parts_dirty.remove(part);
        if part == "word/styles.xml" {
            self.style_resolve_cache.clear();
        }
    }

    /// Drop all cached auxiliary part DOMs (package was reloaded).
    pub fn invalidate_parts(&mut self) {
        self.part_caches.clear();
        self.parts_dirty.clear();
        self.style_resolve_cache.clear();
    }

    fn pkg(&mut self) -> Result<&mut Package, String> {
        self.package.as_mut().ok_or_else(|| "package not loaded".to_string())
    }

    /// docxtpl get_xml: only the `w:body` element of word/document.xml (with
    /// the in-scope namespace declarations repeated on it), not the whole
    /// document part.
    pub fn get_xml(&mut self) -> Result<String, String> {
        self.init_docx(false)?;
        self.flush_parts()?;
        let xml = self
            .pkg()?
            .get_string(DOCUMENT_PART)
            .ok_or_else(|| "word/document.xml not found".to_string())?;
        crate::xmldom::body_element_xml(&xml)
            .ok_or_else(|| "word/document.xml has no w:body element".to_string())
    }

    /// Current xml string of an arbitrary package part (docxtpl get_part_xml).
    pub fn get_part_xml(&mut self, part: &str) -> Result<String, String> {
        self.init_docx(false)?;
        self.flush_parts()?;
        self.pkg()?
            .get_string(part)
            .ok_or_else(|| format!("part {} not found", part))
    }

    // ---------------- render pipeline ----------------

    pub fn render(&mut self, autoescape: bool, make_ctx: &CtxFn) -> Result<(), String> {
        self.render_init()?;
        self.render_initialized(autoescape, make_ctx)
    }

    /// Continue a render after the caller has initialized its document state.
    pub fn render_initialized(&mut self, autoescape: bool, make_ctx: &CtxFn) -> Result<(), String> {
        // persist docmodel edits made since the last render/reload
        self.flush_parts()?;

        // Build the context ONCE per render and reuse it for every part;
        // the jinja environment and compiled part templates live in the
        // env_caches slots and are reused across renders (P0-5).
        let cached_context = if self.per_part_context { None } else { Some(make_ctx(self, DOCUMENT_PART)?) };
        let ctx_once: &CtxFn = &|core, part| match &cached_context {
            Some(context) => Ok(context.clone()),
            None => make_ctx(core, part),
        };

        // Python's file-subdocument constructor renumbers pictures already
        // present in the master before rendering can create new pictures.
        // The typed adapter describes these file subdocuments in its context;
        // the live Python source path has already performed construction.
        if self.python_subdoc_numbering_contract && !self.python_source_contract
            && self.deferred.iter().any(|value| matches!(value, Deferred::Subdoc { bytes: Some(_), .. }))
        {
            let mut parts = vec![DOCUMENT_PART.to_owned()];
            let rels = self.pkg()?.rels(DOCUMENT_PART);
            parts.extend(rels.rels.iter().filter(|rel|
                !rel.is_external && (rel.rel_type == rel_type::HEADER || rel.rel_type == rel_type::FOOTER))
                .map(|rel| crate::package::resolve_target(DOCUMENT_PART, &rel.target)));
            let mut next_id = 1;
            let mut next_docpr = 0;
            for part in parts {
                let Some(xml) = self.pkg()?.get_string(&part) else { continue; };
                let encoding = self.pkg()?.encoding_of(&part);
                let fixed = crate::xmldom::renumber_expanded_drawings(
                    &xml, part == DOCUMENT_PART, Some(&mut next_docpr), Some(&mut next_id))?;
                if fixed != xml {
                    self.pkg()?.set(&part, crate::package::encode_part_owned(fixed, &encoding));
                    self.invalidate_part(&part);
                }
            }
            // docxcompose `Composer.renumber_bookmarks`: the master body's
            // bookmark starts, then its ends, take ids 0, 1, ... in order.
            if let Some(xml) = self.pkg()?.get_string(DOCUMENT_PART) {
                let encoding = self.pkg()?.encoding_of(DOCUMENT_PART);
                let fixed = renumber_body_bookmarks(&xml);
                if fixed != xml {
                    self.pkg()?.set(DOCUMENT_PART, crate::package::encode_part_owned(fixed, &encoding));
                    self.invalidate_part(DOCUMENT_PART);
                }
            }
        }

        // Body
        let src_bytes = self
            .pkg()?
            .get_arc(DOCUMENT_PART)
            .ok_or_else(|| "word/document.xml not found".to_string())?;
        let src = crate::package::decode_part_cow(&src_bytes);
        // fix tables / docPr / cNvPr ids only after every part has rendered:
        // used_subdoc is final by then, so the body is parsed+serialized once
        let body_boundary = if self.python_source_contract {
            Some(crate::xmldom::document_body_source(&src)?)
        } else { None };
        let body_source = body_boundary.as_ref().map_or(src.as_ref(), |(body, _)| body.as_str());
        let body_rendered = self.render_part_with(DOCUMENT_PART, body_source, autoescape, ctx_once)?;

        // Headers & footers
        for uri in [rel_type::HEADER, rel_type::FOOTER] {
            let parts = self.header_footer_parts(uri);
            for part in parts {
                let Some(src_bytes) = self.pkg()?.get_arc(&part) else {
                    continue;
                };
                let src = crate::package::decode_part_cow(&src_bytes);
                if let Some(normalize) = self.header_footer_normalizer {
                    let source = normalize(&src)?;
                    let rendered = self.render_part_with(&part, &source, autoescape, ctx_once)?;
                    let normalized = normalize(&rendered)?;
                    self.pkg()?.set(&part, format!("<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n{normalized}").into_bytes());
                    self.invalidate_part(&part);
                } else {
                    let rendered = self.render_part_with(&part, &src, autoescape, ctx_once)?;
                    self.set_part_rendered(&part, rendered);
                }
            }
        }

        // core properties always render without autoescape (docxtpl parity)
        match self.property_renderer {
            Some(render) => render(self, ctx_once)?,
            None => self.render_properties(ctx_once)?,
        }
        let document_for_footnotes = body_boundary.as_ref().map(|(_, range)| {
            format!("{}{}{}", &src[..range.start], body_rendered, &src[range.end..])
        });
        self.render_footnotes_for_body(autoescape, ctx_once,
            document_for_footnotes.as_deref().unwrap_or(&body_rendered))?;

        // like docxcompose's renumber_nvpicpr_ids: when subdocs were merged,
        // make pic:cNvPr ids unique (body first, then headers/footers). The
        // body's cNvPr renumbering rides along with its fix_tables pass.
        if self.used_subdoc && !self.python_source_contract && !self.python_subdoc_numbering_contract {
            let mut next_id: u32 = 1;
            let fixed = fix_tables_docpr_cnvpr(
                &body_rendered,
                &mut self.docx_ids_index,
                Some(&mut next_id),
            )?;
            self.set_part_rendered(DOCUMENT_PART, fixed);
            let hf_parts: Vec<String> = [rel_type::HEADER, rel_type::FOOTER]
                .into_iter()
                .flat_map(|uri| self.header_footer_parts(uri))
                .collect();
            for part in hf_parts {
                let Some(xml) = self.pkg()?.get_string(&part) else {
                    continue;
                };
                let enc = self.pkg()?.encoding_of(&part);
                if let Some(fixed) = renumber_cnvpr(&xml, &mut next_id) {
                    let bytes = crate::package::encode_part_owned(fixed, &enc);
                    self.pkg()?.set(&part, bytes);
                }
            }
        } else {
            // fix tables and docPr ids on body only
            let mut fixed = if self.python_subdoc_numbering_contract && !self.python_source_contract {
                // docxtpl's fix_tables parses the rendered body with lxml's
                // recover mode before fixing tables; reproduce that recovery.
                let recovered = if crate::recover::is_well_formed(&body_rendered) { body_rendered.clone() }
                    else { crate::recover::recover_document_body(&body_rendered) };
                let tables = fix_tables_only(&recovered)?;
                crate::xmldom::renumber_expanded_drawings(&tables, true, Some(&mut self.docx_ids_index), None)?
            } else {
                let index = self.docx_ids_index;
                match fix_tables_and_docpr(&body_rendered, &mut self.docx_ids_index) {
                    Ok(fixed) => fixed,
                    // docxtpl's fix_tables parses the rendered body with
                    // lxml's recover mode; an ill-formed body (markup in a
                    // value) is recovered as it would be, then fixed.
                    Err(_) if !crate::recover::is_well_formed(&body_rendered) => {
                        self.docx_ids_index = index;
                        let recovered = crate::recover::recover_document_body(&body_rendered);
                        fix_tables_and_docpr(&recovered, &mut self.docx_ids_index)?
                    }
                    Err(error) => return Err(error),
                }
            };
            if let Some((_, range)) = body_boundary {
                if let Some(adopted) = crate::xmldom::adopt_body_namespaces(&src, &fixed) {
                    fixed = adopted;
                }
                let mut document = String::with_capacity(src.len() - range.len() + fixed.len());
                document.push_str(&src[..range.start]);
                document.push_str(&fixed);
                document.push_str(&src[range.end..]);
                fixed = document;
            }
            self.set_part_rendered(DOCUMENT_PART, fixed);
        }

        self.is_rendered = true;
        Ok(())
    }

    pub fn render_init(&mut self) -> Result<(), String> {
        self.init_docx(true)?;
        self.pic_map.clear();
        self.docx_ids_index = 1000;
        self.is_saved = false;
        self.deferred.clear();
        self.deferred_by_oid.clear();
        self.early_subdoc_cache.clear();
        self.attached_subdocuments.clear();
        self.used_subdoc = false;
        self.last_error_context.clear();
        self.next_shape_ids.clear();
        self.bookmark_next_id = None;
        self.preserved_styles.clear();
        Ok(())
    }

    pub(crate) fn header_footer_parts(&mut self, uri: &str) -> Vec<String> {
        let rels = match &self.package {
            Some(p) => p.rels(DOCUMENT_PART),
            None => return Vec::new(),
        };
        rels.by_type(uri)
            .filter(|r| !r.is_external)
            .map(|r| resolve_target(DOCUMENT_PART, &r.target))
            .collect()
    }

    /// Reproduce the master-side identifier pass performed when a file
    /// subdocument is attached through the Python API. This operates before
    /// rendering so identifiers in the later inserted body remain untouched.
    pub fn prepare_python_subdoc_identifiers(&mut self) -> Result<(), String> {
        self.flush_parts()?;
        let rels = self.pkg()?.rels(DOCUMENT_PART);
        let mut parts = vec![DOCUMENT_PART.to_string()];
        parts.extend(rels.rels.iter()
            .filter(|rel| !rel.is_external &&
                (rel.rel_type == rel_type::HEADER || rel.rel_type == rel_type::FOOTER))
            .map(|rel| resolve_target(DOCUMENT_PART, &rel.target)));

        let mut docpr_id = 0u32; // regex_fix_docpr increments before writing
        let mut picture_id = 1u32;
        for part in parts {
            let Some(xml) = self.pkg()?.get_string(&part) else { continue };
            let mut fixed = fix_docpr_cnvpr_ids(&xml, &mut docpr_id, Some(&mut picture_id));
            if part == DOCUMENT_PART && fixed.contains("w:bookmark") {
                let mut starts = 0u64;
                fixed = crate::patch::sub(
                    r#"(<w:bookmarkStart\b[^>]*\bw:id=")[^"]*(")"#,
                    |captures| {
                        let id = starts;
                        starts += 1;
                        format!("{}{id}{}", &captures[1], &captures[2])
                    },
                    &fixed,
                );
                let mut ends = 0u64;
                fixed = crate::patch::sub(
                    r#"(<w:bookmarkEnd\b[^>]*\bw:id=")[^"]*(")"#,
                    |captures| {
                        let id = ends;
                        ends += 1;
                        format!("{}{id}{}", &captures[1], &captures[2])
                    },
                    &fixed,
                );
            }
            if fixed != xml {
                let pkg = self.pkg()?;
                let encoding = pkg.encoding_of(&part);
                pkg.set(&part, crate::package::encode_part_owned(fixed, &encoding));
                self.invalidate_part(&part);
            }
        }
        Ok(())
    }

    /// patch_xml + jinja render + resolve_listing for one part
    pub fn render_part(
        &mut self,
        part: &str,
        src_xml: &str,
        autoescape: bool,
        make_ctx: &CtxFn,
    ) -> Result<String, String> {
        self.render_part_with(part, src_xml, autoescape, make_ctx)
    }

    /// render one part with the cached jinja environment (shared across all
    /// parts and across renders while the part sources are unchanged).
    fn render_part_with(
        &mut self,
        part: &str,
        src_xml: &str,
        autoescape: bool,
        make_ctx: &CtxFn,
    ) -> Result<String, String> {
        #[cfg(feature = "python")]
        let prev = crate::pybridge::set_current_render(self as *mut TplCore, part);
        let result = (|| {
            self.rendered_fragments = None;
            let compiled_source = if self.python_source_contract {
                Some(self.compile_part_jinja(part, src_xml, autoescape)?)
            } else { None };
            let ctx = make_ctx(self, part)?;
            let execute = |core: &mut TplCore| match &compiled_source {
                Some(source) => core.render_compiled_part_jinja(part, source, ctx.clone(), autoescape),
                None => core.render_part_jinja(part, src_xml, ctx.clone(), autoescape),
            };
            let dst = match self.host_part_scope.clone() {
                Some(scope) => (scope.0)(self, part, &execute)?,
                None => execute(self)?,
            };
            if let Some(fragments) = self.rendered_fragments.take() {
                if fragments.iter().any(|fragment| matches!(fragment, OwnedMaterializationFragment::Unicode(_))) {
                    let materializer = self.host_unicode_materializer.clone().ok_or("host Unicode materializer is not configured")?;
                    return (materializer.0)(self, part, &fragments);
                }
                let fragments = fragments.iter().map(|fragment| match fragment {
                    OwnedMaterializationFragment::Text(text) => MaterializationFragment::Text(text),
                    OwnedMaterializationFragment::Deferred(index) => MaterializationFragment::Deferred(*index),
                    OwnedMaterializationFragment::Unicode(_) => unreachable!("handled above"),
                }).collect::<Vec<_>>();
                let output = self.materialize_transformed_output(part, &fragments)?;
                let forbidden = output.chars().any(|ch| !matches!(ch,
                    '\u{9}' | '\u{a}' | '\u{d}' | '\u{20}'..='\u{d7ff}' |
                    '\u{e000}'..='\u{fffd}' | '\u{10000}'..='\u{10ffff}'));
                if forbidden && part == DOCUMENT_PART {
                    if let Some(recover) = &self.host_fragment_recovery_adapter {
                        return (recover.0)(&output);
                    }
                    if let Some(recover) = self.host_fragment_recovery {
                        return recover(&output);
                    }
                }
                return Ok(output);
            }
            let dst = resolve_listing(&dst);
            self.materialize_deferred(part, dst.into_owned())
        })();
        #[cfg(feature = "python")]
        crate::pybridge::restore_current_render(prev);
        result
    }

    /// Cached environment access: one slot per autoescape mode, rebuilt only
    /// when the configuration changed (env_gen bump or env_options diff).
    fn jinja_env(&mut self, autoescape: bool) -> &mut EnvCache {
        let idx = autoescape as usize;
        let stale = match &self.env_caches[idx] {
            Some(c) => c.gen != self.env_gen || c.env_options != self.env_options,
            None => true,
        };
        if stale {
            let env = make_env(autoescape, self);
            self.env_caches[idx] = Some(EnvCache {
                autoescape,
                gen: self.env_gen,
                env_options: self.env_options.clone(),
                env,
                tpl_hashes: HashMap::new(),
            });
        }
        self.env_caches[idx].as_mut().unwrap()
    }

    /// Render one part's raw xml through the cached jinja env. The pure
    /// preprocessing chain (decode entities -> patch_xml -> paragraph
    /// newlines -> trans/bool/engine rewrites) is cached per part keyed by
    /// the raw source hash, and the compiled template by the preprocessed
    /// hash, so a repeated render of an unchanged part skips both.
    fn render_part_jinja(
        &mut self,
        part: &str,
        src_xml: &str,
        ctx: Value,
        autoescape: bool,
    ) -> Result<String, String> {
        let source = self.compile_part_jinja(part, src_xml, autoescape)?;
        self.render_compiled_part_jinja(part, &source, ctx, autoescape)
    }

    fn compile_part_jinja(
        &mut self,
        part: &str,
        src_xml: &str,
        autoescape: bool,
    ) -> Result<std::rc::Rc<str>, String> {
        let src_hash = fxhash64(src_xml.as_bytes());
        let (hash, src) = match self.preproc_cache.get(part) {
            Some((h, ph, s)) if *h == src_hash => (*ph, s.clone()),
            _ => {
                let decoded = decode_text_entities(src_xml);
                let patched = patch_xml(&decoded);
                let s = add_paragraph_newlines(&patched);
                let s = normalize_template_newlines(&s);
                let s = if self.python_source_contract {
                    s.into_owned()
                } else {
                    let s = preprocess_trans(&s);
                    let s = preprocess_bool_compare(&s);
                    let s = preprocess_engine_features(&s);
                    preprocess_mutlist(&s).into_owned()
                };
                let rc: std::rc::Rc<str> = s.into();
                let ph = fxhash64(rc.as_bytes());
                self.preproc_cache
                    .insert(part.to_string(), (src_hash, ph, rc.clone()));
                (ph, rc)
            }
        };
        let compile_err = {
            let cache = self.jinja_env(autoescape);
            if cache.tpl_hashes.get(part) == Some(&hash) {
                None
            } else {
                match cache.env.add_template_owned(part.to_string(), src.to_string()) {
                    Ok(()) => {
                        cache.tpl_hashes.insert(part.to_string(), hash);
                        None
                    }
                    Err(e) => Some(e),
                }
            }
        };
        if let Some(e) = compile_err {
            return Err(self.jinja_error_msg(&e, &src));
        }
        Ok(src)
    }

    fn render_compiled_part_jinja(
        &mut self,
        part: &str,
        src: &str,
        ctx: Value,
        autoescape: bool,
    ) -> Result<String, String> {
        let mut fragments = None;
        let render_result = {
            let cache = self.env_caches[autoescape as usize].as_ref().unwrap();
            match (&self.host_fragment_renderer, &self.host_template_renderer) {
                (Some(renderer), _) => match &self.host_fragment_converter {
                    Some(converter) => (renderer.0)(cache.env.clone(), HostTemplate::Named(part.to_owned()))
                        .and_then(|values| { fragments = Some((converter.0)(values)?); Ok(String::new()) }),
                    None => Err(minijinja::Error::new(minijinja::ErrorKind::InvalidOperation,
                        "host fragment renderer requires a fragment converter")),
                },
                (None, Some(renderer)) => (renderer.0)(cache.env.clone(), HostTemplate::Named(part.to_owned())),
                (None, None) => match cache.env.get_template(part) {
                    Ok(t) if self.host_fragment_converter.is_some() => match cache.env.fragment_joiner() {
                        Some(joiner) => t.render_unjoined_fragments(&ctx, joiner).and_then(|values| {
                            fragments = Some((self.host_fragment_converter.as_ref().unwrap().0)(values)?);
                            Ok(String::new())
                        }),
                        None => Err(minijinja::Error::new(minijinja::ErrorKind::InvalidOperation,
                            "fragment joiner is not configured")),
                    },
                    Ok(t) if self.fragment_joining => match cache.env.fragment_joiner() {
                        Some(joiner) => t.render_fragments(&ctx, joiner).map(|value| value.to_string()),
                        None => Err(minijinja::Error::new(minijinja::ErrorKind::InvalidOperation,
                            "fragment joiner is not configured")),
                    },
                    Ok(t) if self.host_capture_joining => match cache.env.fragment_joiner() {
                        Some(joiner) => t.render_with_capture_fragments(&ctx, joiner),
                        None => Err(minijinja::Error::new(minijinja::ErrorKind::InvalidOperation,
                            "capture fragment joiner is not configured")),
                    },
                    Ok(t) => t.render(&ctx),
                    Err(e) => Err(e),
                },
            }
        };
        let rendered = match render_result {
            Ok(s) => s,
            Err(e) => return Err(self.jinja_error_msg(&e, &src)),
        };
        self.rendered_fragments = fragments;
        let dst = remove_paragraph_newlines(&rendered);
        Ok(restore_escaped_delims(dst.into_owned()))
    }

    /// Format a jinja error with source context lines (docx_context parity).
    fn jinja_error_msg(&mut self, e: &minijinja::Error, src: &str) -> String {
        let mut msg = format!("{}", e);
        if let Some(line) = e.line() {
            let start = line.saturating_sub(4);
            let context: Vec<String> = src
                .lines()
                .skip(start)
                .take(7)
                .map(|l| sub_str(r"<[^>]+>", "", l))
                .collect();
            self.last_error_context = context.clone();
            msg.push_str(&format!(
                "\nContext (lines {}-{}):\n{}",
                start + 1,
                start + 7,
                context.join("\n")
            ));
        }
        msg
    }

    /// Replace deferred-value placeholders actually printed by the template
    /// with their materialized XML for the given part. Single pass over the
    /// xml; each deferred value is materialized at most once per part.
    pub fn materialize_deferred(&mut self, part: &str, xml: String) -> Result<String, String> {
        if self.deferred.is_empty() || !xml.contains('\u{1}') {
            return Ok(xml);
        }
        let mut fragments = Vec::new();
        let mut rest = xml.as_str();
        while let Some(start) = rest.find('\u{1}') {
            fragments.push(MaterializationFragment::Text(&rest[..start]));
            let after = &rest[start + 1..];
            let parsed = after.find('\u{1}').and_then(|e| {
                after[..e]
                    .strip_prefix("DTPLD")
                    .and_then(|d| d.parse::<usize>().ok())
                    .map(|idx| (idx, e))
            });
            match parsed {
                Some((idx, end)) if idx < self.deferred.len() => {
                    fragments.push(MaterializationFragment::Deferred(idx));
                    rest = &after[end + 1..];
                }
                _ => {
                    fragments.push(MaterializationFragment::Text(&rest[start..start + 1]));
                    rest = after;
                }
            }
        }
        fragments.push(MaterializationFragment::Text(rest));
        self.materialize_output_fragments(part, fragments)
    }

    /// Materialize explicitly tagged values without inspecting ordinary text.
    /// Repeated indices share the same materialization, as in the legacy path.
    pub fn materialize_output_fragments<'a>(
        &mut self, part: &str, fragments: impl IntoIterator<Item = MaterializationFragment<'a>>,
    ) -> Result<String, String> {
        let mut done: Vec<Option<String>> = (0..self.deferred.len()).map(|_| None).collect();
        let mut out = String::new();
        for fragment in fragments {
            match fragment {
                MaterializationFragment::Text(text) => out.push_str(text),
                MaterializationFragment::Deferred(idx) => {
                    if idx >= done.len() { return Err(format!("unknown deferred index {idx}")); }
                    if done[idx].is_none() { done[idx] = Some(self.materialize_one(part, idx)?); }
                    out.push_str(done[idx].as_ref().unwrap());
                }
            }
        }
        Ok(out)
    }

    /// Apply the document output transformations while retaining explicitly
    /// tagged deferred values, then materialize those values. The temporary
    /// transport alphabet contains no XML delimiters, listing controls or
    /// escaped-Jinja delimiters, so the existing transformations treat each
    /// transport token as opaque text. A dry transformation chooses a prefix
    /// absent from the transformed user text, including copied properties.
    pub fn materialize_transformed_output<'a>(
        &mut self, part: &str, fragments: &[MaterializationFragment<'a>],
    ) -> Result<String, String> {
        fn transform(source: String) -> String {
            let source = remove_paragraph_newlines(&source).into_owned();
            let source = restore_escaped_delims(source);
            resolve_listing(&source).into_owned()
        }

        let mut probe = String::new();
        let mut has_deferred = false;
        for fragment in fragments {
            match fragment {
                MaterializationFragment::Text(text) => probe.push_str(text),
                MaterializationFragment::Deferred(index) => {
                    if *index >= self.deferred.len() { return Err(format!("unknown deferred index {index}")); }
                    has_deferred = true;
                    probe.push_str("\u{1}X\u{2}");
                }
            }
        }
        let probe = transform(probe);
        if !has_deferred { return Ok(probe); }

        // Collect every decimal nonce already followed by Z after U+0001.
        // Choosing one outside this finite set makes the entire transport
        // prefix absent from the transformed untrusted output.
        let bytes = probe.as_bytes();
        let mut occupied = HashSet::new();
        for start in memchr::memchr_iter(1, bytes) {
            let mut end = start + 1;
            while bytes.get(end).is_some_and(u8::is_ascii_digit) { end += 1; }
            if end > start + 1 && bytes.get(end) == Some(&b'Z') {
                if let Ok(nonce) = probe[start + 1..end].parse::<u64>() { occupied.insert(nonce); }
            }
        }
        let mut nonce = 0u64;
        while occupied.contains(&nonce) {
            nonce = nonce.checked_add(1).ok_or("no available fragment transport prefix")?;
        }
        let prefix = format!("\u{1}{nonce}Z");
        let mut encoded = String::new();
        for fragment in fragments {
            match fragment {
                MaterializationFragment::Text(text) => encoded.push_str(text),
                MaterializationFragment::Deferred(index) => {
                    use std::fmt::Write as _;
                    write!(&mut encoded, "{prefix}{index}\u{2}").map_err(|e| e.to_string())?;
                }
            }
        }
        let transformed = transform(encoded);
        let mut pieces = Vec::new();
        let mut rest = transformed.as_str();
        while let Some(start) = rest.find(&prefix) {
            pieces.push(MaterializationFragment::Text(&rest[..start]));
            let after = &rest[start + prefix.len()..];
            let end = after.find('\u{2}').ok_or("damaged deferred fragment transport")?;
            let index = after[..end].parse::<usize>().map_err(|_| "invalid deferred fragment transport")?;
            pieces.push(MaterializationFragment::Deferred(index));
            rest = &after[end + 1..];
        }
        pieces.push(MaterializationFragment::Text(rest));
        self.materialize_output_fragments(part, pieces)
    }

    /// Materialize one deferred value (cheap to call: the Deferred clone only
    /// bumps Arc refcounts).
    pub fn materialize_one(&mut self, part: &str, idx: usize) -> Result<String, String> {
        if let Some(xml) = self.attached_subdocuments.get(&idx) {
            return Ok(xml.clone());
        }
        if let Some(xml) = self.early_subdoc_cache.get(&(idx, part.to_owned())) {
            return Ok(xml.clone());
        }
        let deferred = self.deferred.get(idx).cloned().ok_or_else(|| format!("unknown deferred index {idx}"))?;
        match deferred {
            Deferred::Image {
                blob,
                info,
                filename,
                width,
                height,
                anchor,
                title,
                descr,
            } => crate::inline_image::inline_image_xml_with_info(
                self,
                part,
                &blob,
                filename.as_deref(),
                width,
                height,
                anchor.as_deref(),
                title.as_deref(),
                descr.as_deref(),
                info.as_ref(),
            ),
            Deferred::Subdoc {
                bytes,
                keep_sections,
            } => {
                self.used_subdoc = true;
                match bytes {
                    Some(b) => crate::subdoc::subdoc_xml_opts(self, &b, keep_sections),
                    None => Ok(String::new()),
                }
            }
            Deferred::SubdocBlocks { blocks } => {
                self.used_subdoc = true;
                crate::subdocbuilder::serialize_blocks(self, part, &blocks)
            }
        }
    }

    pub fn render_properties(&mut self, make_ctx: &CtxFn) -> Result<(), String> {
        let name = "docProps/core.xml";
        // python-docx always provides a core properties part
        if self.package.as_ref().map(|p| !p.contains(name)).unwrap_or(false) {
            crate::package::ensure_core_part(self.package.as_mut().unwrap());
        }
        let src = match self.pkg()?.get_string(name) {
            Some(s) => s,
            None => return Ok(()),
        };
        let mut doc = match Document::parse(&src) {
            Ok(d) => d,
            Err(_) => return Ok(()),
        };
        // python-docx core property -> xml tag
        let props = [
            "dc:creator",     // author
            "dc:description", // comments
            "dc:identifier",
            "dc:language",
            "dc:subject",
            "dc:title",
        ];
        #[cfg(feature = "python")]
        let prev = crate::pybridge::set_current_render(self as *mut TplCore, name);
        let result = (|| -> Result<(), String> {
            let cached_context = if self.per_part_context { None } else { Some(make_ctx(self, name)?) };
            let mut changed = false;
            for tag in props {
                if let Some(el) = doc.root.find_mut(tag) {
                    let initial = el.text_content();
                    if initial.is_empty() || !initial.contains('{') {
                        // no jinja marker: rendering would be the identity
                        continue;
                    }
                    let ctx = match &cached_context {
                        Some(context) => context.clone(),
                        None => make_ctx(self, name)?,
                    };
                    let rendered = {
                        // core properties always render without autoescape
                        let cache = self.jinja_env(false);
                        cache
                            .env
                            .template_from_str(&initial)
                            .and_then(|t| t.render(&ctx))
                            .map_err(|e| format!("error rendering core property {}: {}", tag, e))?
                    };
                    let rendered = self.materialize_deferred(name, rendered)?;
                    el.children = vec![Node::Text(rendered)];
                    changed = true;
                }
            }
            if changed {
                let xml = doc.serialize();
                self.pkg()?.set(name, xml.into_bytes());
                self.invalidate_part(name);
            }
            Ok(())
        })();
        #[cfg(feature = "python")]
        crate::pybridge::restore_current_render(prev);
        result
    }

    pub fn render_footnotes(&mut self, autoescape: bool, make_ctx: &CtxFn) -> Result<(), String> {
        let body = match &self.package {
            Some(p) => p.get_string(DOCUMENT_PART).unwrap_or_default(),
            None => return Ok(()),
        };
        self.render_footnotes_for_body(autoescape, make_ctx, &body)
    }

    fn render_footnotes_for_body(&mut self, autoescape: bool, make_ctx: &CtxFn, body: &str) -> Result<(), String> {
        self.footnote_decode_error = None;
        let reference_utf8 = self.footnote_locator.is_some();
        let (parts, repetitions): (Vec<String>, usize) = if let Some(locate) = self.footnote_locator {
            locate(self.pkg()?, body)?
        } else {
            let rels = match &self.package {
                Some(p) => p.rels(DOCUMENT_PART),
                None => return Ok(()),
            };
            (rels.by_type(rel_type::FOOTNOTES)
                .filter(|r| !r.is_external)
                .map(|r| resolve_target(DOCUMENT_PART, &r.target))
                .collect(), 1)
        };
        for _ in 0..repetitions {
            for part in &parts {
                let src = if reference_utf8 {
                    let bytes = match self.pkg()?.get_arc(part) {
                        Some(bytes) => bytes,
                        None => continue,
                    };
                    match std::str::from_utf8(&bytes) {
                        Ok(text) => text.to_owned(),
                        Err(error) => {
                            self.footnote_decode_error = Some(bytes);
                            return Err(format!("invalid UTF-8 in footnote part: {error}"));
                        }
                    }
                } else {
                    match self.pkg()?.get_string(part) {
                        Some(s) => s,
                        None => continue,
                    }
                };
                let rendered = self.render_part_with(part, &src, autoescape, make_ctx)?;
                if reference_utf8 {
                    self.pkg()?.set(part, rendered.into_bytes());
                    self.invalidate_part(part);
                } else {
                    self.set_part_rendered(part, rendered);
                }
            }
        }
        Ok(())
    }

    /// Store a rendered XML part re-encoded with its declared encoding
    /// (docxtpl get_headers_footers_encoding parity).
    fn set_part_rendered(&mut self, part: &str, content: String) {
        let pkg = self.package.as_mut().expect("package loaded");
        let enc = pkg.encoding_of(part);
        pkg.set(part, crate::package::encode_part_owned(content, &enc));
        // rendered xml replaces whatever the part cache held
        self.invalidate_part(part);
    }

    // ---------------- variables ----------------

    /// Undeclared variables of the template (body + headers/footers).
    /// With `use_env` the parse honors the configured jinja environment
    /// (trim_blocks/lstrip_blocks/keep_trailing_newline and the {% trans %}
    /// /bool-compare/engine-feature rewrites, like the render pipeline);
    /// without it a plain default-syntax environment is used.
    pub fn undeclared_variables(&mut self, context_keys: Option<HashSet<String>>, use_env: bool) -> Result<Vec<String>, String> {
        // Build on a temporary package so current state is untouched
        let pkg = Package::from_bytes_arc(self.original_bytes.clone())?;
        let mut xml = String::new();
        if let Some(doc) = pkg.get_string(DOCUMENT_PART) {
            xml.push_str(&patch_xml(&decode_text_entities(&doc)));
        }
        let rels = pkg.rels(DOCUMENT_PART);
        for uri in [rel_type::HEADER, rel_type::FOOTER] {
            for r in rels.by_type(uri).filter(|r| !r.is_external) {
                let part = resolve_target(DOCUMENT_PART, &r.target);
                if let Some(src) = pkg.get_string(&part) {
                    xml.push_str(&patch_xml(&decode_text_entities(&src)));
                }
            }
        }
        let mut vars = if use_env {
            // strip trans tags but keep their bodies (and the pluralize
            // count) visible: the render-time gettext rewrite would fold
            // {{ var }} into a msgid string, hiding it from the analyzer
            let src = trans_bodies_for_analysis(&xml);
            let src = preprocess_bool_compare(&src);
            let src = preprocess_engine_features(&src);
            let src = preprocess_mutlist(&src);
            let env = make_env(false, self);
            let tmpl = env
                .template_from_str(&src)
                .map_err(|e| e.to_string())?;
            tmpl.undeclared_variables(false)
        } else {
            let env = Environment::new();
            let tmpl = env
                .template_from_str(&xml)
                .map_err(|e| e.to_string())?;
            tmpl.undeclared_variables(false)
        };
        if let Some(keys) = context_keys {
            vars = vars.into_iter().filter(|v| !keys.contains(v)).collect();
        }
        Ok(vars.into_iter().collect())
    }

    // ---------------- replacements ----------------

    pub fn reset_replacements(&mut self) {
        self.crc_to_new_media.clear();
        self.crc_to_new_embedded.clear();
        self.zipname_to_replace.clear();
        self.pics_to_replace.clear();
    }

    pub fn build_url_id(&mut self, url: &str) -> Result<String, String> {
        self.init_docx(false)?;
        Ok(self.pkg()?.add_rel(DOCUMENT_PART, rel_type::HYPERLINK, url, true))
    }

    pub fn pre_processing(&mut self) -> Result<(), String> {
        if !self.pics_to_replace.is_empty() {
            self.replace_pics()?;
        }
        Ok(())
    }

    fn replace_pics(&mut self) -> Result<(), String> {
        let mut replaced: HashMap<String, bool> = self
            .pics_to_replace
            .keys()
            .map(|k| (k.clone(), false))
            .collect();

        let mut parts = vec![DOCUMENT_PART.to_string()];
        for uri in [rel_type::HEADER, rel_type::FOOTER] {
            parts.extend(self.header_footer_parts(uri));
        }

        for part in parts {
            self.replace_part_pics(&part, &mut replaced)?;
        }

        if !self.allow_missing_pics {
            for (img_id, was_replaced) in &replaced {
                if !was_replaced {
                    return Err(format!(
                        "Picture {} not found in the docx template",
                        img_id
                    ));
                }
            }
        }
        Ok(())
    }

    fn replace_part_pics(
        &mut self,
        part: &str,
        replaced: &mut HashMap<String, bool>,
    ) -> Result<(), String> {
        let src = match self.pkg()?.get_string(part) {
            Some(s) => s,
            None => return Ok(()),
        };
        let doc = match Document::parse(&src) {
            Ok(d) => d,
            Err(_) => return Ok(()),
        };
        let rels = self.pkg()?.rels(part);

        // gather all a:graphicData elements
        let mut gds: Vec<&Element> = Vec::new();
        doc.root.iter_descendants("a:graphicData", &mut gds);

        let pic_uri = "http://schemas.openxmlformats.org/drawingml/2006/picture";
        let mut updates: Vec<(String, Vec<u8>)> = Vec::new();

        for gd in gds {
            if gd.get_attr("uri") != Some(pic_uri) {
                continue;
            }
            let mut blips: Vec<&Element> = Vec::new();
            gd.iter_descendants("a:blip", &mut blips);
            let Some(blip) = blips.first() else { continue };
            let Some(rid) = blip.get_attr("r:embed") else { continue };

            let mut cnvprs: Vec<&Element> = Vec::new();
            gd.iter_descendants("pic:cNvPr", &mut cnvprs);
            let Some(cnvpr) = cnvprs.first() else { continue };
            let filename = cnvpr.get_attr("name").unwrap_or("").to_string();
            let title = cnvpr.get_attr("title").unwrap_or("").to_string();
            let descr = cnvpr.get_attr("descr").unwrap_or("").to_string();

            if let Some(rel) = rels.get(rid) {
                let target_part = resolve_target(part, &rel.target);
                self.pic_map
                    .insert(filename.clone(), (rel.target.clone(), target_part.clone()));
            } else {
                continue;
            }

            for (img_id, img_data) in &self.pics_to_replace {
                if *img_id == filename || *img_id == title || *img_id == descr {
                    let rel = rels.get(rid).unwrap();
                    let target_part = resolve_target(part, &rel.target);
                    updates.push((target_part, img_data.clone()));
                    if let Some(v) = replaced.get_mut(img_id) {
                        *v = true;
                    }
                    break;
                }
            }
        }

        for (part_name, data) in updates {
            self.pkg()?.set(&part_name, data);
        }
        Ok(())
    }

    // ---------------- save ----------------

    pub fn save_bytes(&mut self) -> Result<Vec<u8>, String> {
        self.flush_parts()?;
        // load the package only if nothing has touched it yet; reloading
        // unconditionally here would discard pre-render docmodel edits
        self.init_docx(false)?;
        self.pre_processing()?;

        // post processing: zip-level replacements (rare path). Applied in
        // place and then restored, avoiding a full package clone (media
        // blobs included) for a single to_bytes() call.
        let out = if !self.crc_to_new_media.is_empty()
            || !self.crc_to_new_embedded.is_empty()
            || !self.zipname_to_replace.is_empty()
        {
            let mut swapped: Vec<(usize, Vec<u8>)> = Vec::new();
            {
                let pkg = self.package.as_mut().ok_or_else(|| "package not loaded".to_string())?;
                for (idx, entry) in pkg.entries.iter_mut().enumerate() {
                    let new = if let Some(new) = self.zipname_to_replace.get(&entry.name) {
                        Some(new.clone())
                    } else if entry.name.starts_with("word/media/") {
                        self.crc_to_new_media.get(&crc32(entry.bytes())).cloned()
                    } else if entry.name.starts_with("word/embeddings/") {
                        self.crc_to_new_embedded.get(&crc32(entry.bytes())).cloned()
                    } else {
                        None
                    };
                    if let Some(new) = new {
                        swapped.push((idx, entry.swap_bytes(new)));
                    }
                }
            }
            let bytes = self
                .package
                .as_ref()
                .ok_or_else(|| "package not loaded".to_string())?
                .to_bytes()?;
            let pkg = self.package.as_mut().unwrap();
            for (idx, old) in swapped {
                pkg.entries[idx].swap_bytes(old);
            }
            bytes
        } else {
            self.package
                .as_ref()
                .ok_or_else(|| "package not loaded".to_string())?
                .to_bytes()?
        };

        self.is_saved = true;
        Ok(out)
    }

    /// Apply the pending zip-level replacements (crc_to_new_media /
    /// crc_to_new_embedded / zipname_to_replace) to a standalone docx byte
    /// stream, without touching the live package (docxtpl post_processing).
    pub fn post_processing_bytes(&self, data: &[u8]) -> Result<Vec<u8>, String> {
        if self.crc_to_new_media.is_empty()
            && self.crc_to_new_embedded.is_empty()
            && self.zipname_to_replace.is_empty()
        {
            return Ok(data.to_vec());
        }
        let mut pkg = Package::from_bytes_arc(std::sync::Arc::from(data))?;
        crate::package::rewrite_entries(&mut pkg, |entry| -> Result<_, std::convert::Infallible> {
            let new = if let Some(new) = self.zipname_to_replace.get(&entry.name) {
                Some(new.clone())
            } else if entry.name.starts_with("word/media/") {
                self.crc_to_new_media.get(&crc32(entry.bytes())).cloned()
            } else if entry.name.starts_with("word/embeddings/") {
                self.crc_to_new_embedded.get(&crc32(entry.bytes())).cloned()
            } else {
                None
            };
            Ok(new)
        }).unwrap_or_else(|never| match never {});
        pkg.to_bytes_preserving_compression()
    }
}

// ---------------- jinja rendering of one part ----------------

pub fn make_env(autoescape: bool, core: &TplCore) -> Environment<'static> {
    let mut env = Environment::new();
    // Jinja's lexer normalizes CRLF/CR in every source, including loaded
    // templates and property strings that bypass the part preprocessor.
    env.set_normalize_newlines(true);
    if autoescape {
        env.set_auto_escape_callback(|_| AutoEscape::Html);
    } else {
        env.set_auto_escape_callback(|_| AutoEscape::None);
    }
    if let Some(v) = core.env_options.trim_blocks {
        env.set_trim_blocks(v);
    }
    if let Some(v) = core.env_options.lstrip_blocks {
        env.set_lstrip_blocks(v);
    }
    if let Some(v) = core.env_options.keep_trailing_newline {
        env.set_keep_trailing_newline(v);
    }
    if let Some(b) = &core.env_options.undefined_behavior {
        env.set_undefined_behavior(match b.as_str() {
            "chainable" => minijinja::UndefinedBehavior::Chainable,
            "strict" => minijinja::UndefinedBehavior::Strict,
            "semistrict" => minijinja::UndefinedBehavior::SemiStrict,
            _ => minijinja::UndefinedBehavior::Lenient,
        });
    }
    // jinja2 exposes Python str methods on template strings; emulate the
    // common ones for minijinja values.
    env.set_unknown_method_callback(|_state, value, method, args| {
        py_like_method(value, method, args)
    });

    // backs `{% set x = [] %}` assignments rewritten by preprocess_mutlist
    // when the template mutates the list in place (x.append(...))
    env.add_function("__dtpl_mutlist", || Value::from_object(MutList::new()));

    // jinja2's default template globals missing from minijinja's builtins
    // (range/dict/namespace/debug are provided by minijinja itself)
    env.add_function("cycler", |args: minijinja::value::Rest<Value>| {
        Value::from_object(Cycler::new(args.0))
    });
    env.add_function("joiner", |sep: Option<String>| {
        Value::from_object(Joiner::new(sep.unwrap_or_else(|| ", ".to_string())))
    });
    env.add_function(
        "lipsum",
        |args: minijinja::value::Rest<Value>,
         kwargs: minijinja::value::Kwargs|
         -> Result<Value, minijinja::Error> {
            let pick = |idx: usize, name: &str| -> Option<Value> {
                args.0
                    .get(idx)
                    .cloned()
                    .filter(|v| !v.is_undefined())
                    .or_else(|| kwargs.get::<Value>(name).ok())
            };
            let to_usize = |v: Option<Value>, default: usize| -> Result<usize, minijinja::Error> {
                match v {
                    None => Ok(default),
                    Some(v) => v
                        .as_i64()
                        .and_then(|i| usize::try_from(i).ok())
                        .ok_or_else(|| {
                            minijinja::Error::new(
                                minijinja::ErrorKind::InvalidOperation,
                                "lipsum: expected integer arguments",
                            )
                        }),
                }
            };
            let n = to_usize(pick(0, "n"), 5)?;
            let html = pick(1, "html").map(|v| v.is_true()).unwrap_or(true);
            let min = to_usize(pick(2, "min"), 20)?;
            let max = to_usize(pick(3, "max"), 100)?;
            // jinja2 returns Markup (already-safe html)
            Ok(Value::from_safe_string(lorem_ipsum(n, html, min, max)))
        },
    );

    // jinja2's Undefined supports len() == 0
    env.add_filter("length", |value: Value| -> usize {
        use minijinja::value::ValueKind;
        match value.kind() {
            ValueKind::Undefined | ValueKind::None => 0,
            ValueKind::String | ValueKind::Bytes => value.to_string().chars().count(),
            _ => {
                if let Some(object) = value.as_object() {
                    if let Some(len) = object.enumerator_len() {
                        return len;
                    }
                }
                value
                    .try_iter()
                    .map(|it| it.count())
                    .unwrap_or(0)
            }
        }
    });

    // jinja2 filters missing from minijinja
    env.add_filter("striptags", |value: Value| -> String {
        let s = value.to_string();
        let mut out = String::with_capacity(s.len());
        let mut in_tag = false;
        for c in s.chars() {
            match c {
                '<' => in_tag = true,
                '>' => in_tag = false,
                _ if !in_tag => out.push(c),
                _ => {}
            }
        }
        // jinja2 also collapses whitespace
        out.split_whitespace().collect::<Vec<_>>().join(" ")
    });
    env.add_filter("pyformat", |args: minijinja::value::Rest<Value>| -> Result<String, minijinja::Error> {
        let Some((fmt, rest)) = args.0.split_first() else {
            return Ok(String::new());
        };
        percent_format_positional(&fmt.to_string(), rest)
    });
    env.add_filter("filesizeformat", |value: Value, binary: Option<Value>| {
        let bytes = f64::try_from(value.clone()).unwrap_or(0.0);
        let binary = binary.map(|b| b.is_true()).unwrap_or(false);
        let (base, units) = if binary {
            (1024.0_f64, ["Bytes", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB"])
        } else {
            (1000.0_f64, ["Bytes", "kB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"])
        };
        if bytes.abs() < base {
            return format!("{} Bytes", bytes as i64);
        }
        let mut value = bytes;
        let mut unit = units[0];
        for (i, prefix) in units.iter().enumerate().skip(1) {
            unit = prefix;
            value = bytes / base.powi(i as i32);
            if value.abs() < base {
                break;
            }
        }
        format!("{:.1} {}", value, unit)
    });
    env.add_filter("wordcount", |value: Value| -> usize {
        let s = value.to_string();
        s.split(|c: char| !c.is_alphanumeric() && c != '_')
            .filter(|w| !w.is_empty())
            .count()
    });
    env.add_filter("center", |value: Value, width: Option<Value>| {
        let s = value.to_string();
        let width: usize = width
            .and_then(|w| w.to_string().parse().ok())
            .unwrap_or(80);
        let len = s.chars().count();
        if len >= width {
            return s;
        }
        let total = width - len;
        let left = total / 2;
        format!("{}{}{}", " ".repeat(left), s, " ".repeat(total - left))
    });
    env.add_filter("forceescape", |value: Value| {
        let s = value.to_string();
        s.replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;")
            .replace('"', "&#34;")
            .replace('\'', "&#39;")
    });
    env.add_filter("truncate", |args: minijinja::value::Rest<Value>| {
        let s = args.0.first().map(|v| v.to_string()).unwrap_or_default();
        let length: usize = args.0.get(1).and_then(|v| v.to_string().parse().ok()).unwrap_or(255);
        let killwords = args.0.get(2).map(|v| v.is_true()).unwrap_or(false);
        let end = args.0.get(3).map(|v| v.to_string()).unwrap_or_else(|| "...".to_string());
        let leeway: usize = args.0.get(4).and_then(|v| v.to_string().parse().ok()).unwrap_or(5);
        let len = s.chars().count();
        if len <= length + leeway {
            return s;
        }
        let cut: String = s.chars().take(length.saturating_sub(end.chars().count())).collect();
        if killwords {
            return format!("{}{}", cut, end);
        }
        match cut.rfind(|c: char| c.is_whitespace()) {
            Some(pos) if pos > 0 => format!("{}{}", &cut[..pos], end),
            _ => format!("{}{}", cut, end),
        }
    });
    env.add_filter("xmlattr", |value: Value, autospace: Option<Value>| {
        let autospace = autospace.map(|v| v.is_true()).unwrap_or(true);
        let mut items: Vec<String> = Vec::new();
        if let Ok(iter) = value.try_iter() {
            for k in iter {
                let key = k.to_string();
                // jinja2 skips keys that could break out of attribute syntax
                if key.contains(['/', '>', '=']) {
                    continue;
                }
                if let Ok(v) = value.get_item(&k) {
                    // jinja2 skips none/undefined values
                    if v.is_none() || v.is_undefined() {
                        continue;
                    }
                    let vs = v
                        .to_string()
                        .replace('&', "&amp;")
                        .replace('<', "&lt;")
                        .replace('>', "&gt;")
                        .replace('"', "&#34;");
                    items.push(format!("{}=\"{}\"", key, vs));
                }
            }
        }
        let joined = items.join(" ");
        // jinja2 autospace: a single LEADING space
        if autospace && !joined.is_empty() {
            format!(" {}", joined)
        } else {
            joined
        }
    });
    env.add_filter("wordwrap", |args: minijinja::value::Rest<Value>| {
        let s = args.0.first().map(|v| v.to_string()).unwrap_or_default();
        let width: usize = args.0.get(1).and_then(|v| v.to_string().parse().ok()).unwrap_or(79);
        let break_long = args.0.get(2).map(|v| v.is_true()).unwrap_or(true);
        let wrapstring = args.0.get(3).map(|v| v.to_string()).unwrap_or_else(|| "\n".to_string());
        wordwrap(&s, width.max(1), break_long, &wrapstring)
    });
    env.add_filter("urlize", |args: minijinja::value::Rest<Value>| {
        let s = args.0.first().map(|v| v.to_string()).unwrap_or_default();
        urlize(&s)
    });
    env.add_filter("random", |value: Value| -> Result<Value, minijinja::Error> {
        let items: Vec<Value> = value
            .try_iter()
            .map_err(|e| minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, e.to_string()))?
            .collect();
        if items.is_empty() {
            return Err(minijinja::Error::new(
                minijinja::ErrorKind::InvalidOperation,
                "random from empty sequence",
            ));
        }
        Ok(items[(random_u64() as usize) % items.len()].clone())
    });

    // bool/None tests must match our jinja2-faithful wrappers
    env.add_test("true", |value: Value| -> bool {
        if matches!(value.kind(), minijinja::value::ValueKind::Bool) {
            return value.is_true();
        }
        #[cfg(feature = "python")]
        return value
            .as_object()
            .and_then(|o| o.downcast_ref::<crate::pybridge::PyBoolObj>())
            .map(|b| b.0)
            .unwrap_or(false);
        #[cfg(not(feature = "python"))]
        false
    });
    env.add_test("false", |value: Value| -> bool {
        if matches!(value.kind(), minijinja::value::ValueKind::Bool) {
            return !value.is_true();
        }
        #[cfg(feature = "python")]
        return value
            .as_object()
            .and_then(|o| o.downcast_ref::<crate::pybridge::PyBoolObj>())
            .map(|b| !b.0)
            .unwrap_or(false);
        #[cfg(not(feature = "python"))]
        false
    });
    env.add_test("boolean", |value: Value| -> bool {
        if matches!(value.kind(), minijinja::value::ValueKind::Bool) {
            return true;
        }
        #[cfg(feature = "python")]
        return value
            .as_object()
            .map(|o| o.downcast_ref::<crate::pybridge::PyBoolObj>().is_some())
            .unwrap_or(false);
        #[cfg(not(feature = "python"))]
        false
    });
    env.add_test("none", |value: Value| -> bool {
        if matches!(value.kind(), minijinja::value::ValueKind::None) {
            return true;
        }
        #[cfg(feature = "python")]
        return value
            .as_object()
            .map(|o| o.downcast_ref::<crate::pybridge::PyNoneObj>().is_some())
            .unwrap_or(false);
        #[cfg(not(feature = "python"))]
        false
    });
    env.add_test("eq_true", |value: Value| -> bool {
        test_eq_true(&value)
    });
    env.add_test("eq_false", |value: Value| -> bool {
        test_eq_false(&value)
    });
    env.add_test("callable", |value: Value| -> bool {
        if let Some(o) = value.as_object() {
            #[cfg(feature = "python")]
            if let Some(w) = o.downcast_ref::<crate::pybridge::PyWrapper>() {
                return pyo3::Python::attach(|py| w.obj.bind(py).is_callable());
            }
            let _ = &o;
            // minijinja objects (macros/functions) are callable
            return true;
        }
        false
    });

    // jinja2's `mapping`/`sequence` tests must also match wrapped python
    // dicts/lists (they are objects for laziness)
    env.add_test("mapping", |value: Value| -> bool {
        if matches!(value.kind(), minijinja::value::ValueKind::Map) {
            return true;
        }
        #[cfg(feature = "python")]
        if let Some(object) = value.as_object() {
            if let Some(wrapper) = object.downcast_ref::<crate::pybridge::PyWrapper>() {
                return pyo3::Python::attach(|py| {
                    wrapper.obj.bind(py).cast::<pyo3::types::PyDict>().is_ok()
                });
            }
        }
        false
    });
    env.add_test("sequence", |value: Value| -> bool {
        use minijinja::value::ValueKind;
        if matches!(value.kind(), ValueKind::Seq | ValueKind::String | ValueKind::Bytes) {
            return true;
        }
        if let Some(object) = value.as_object() {
            #[cfg(feature = "python")]
            if let Some(wrapper) = object.downcast_ref::<crate::pybridge::PyWrapper>() {
                return pyo3::Python::attach(|py| {
                    let o = wrapper.obj.bind(py);
                    o.cast::<pyo3::types::PyList>().is_ok()
                        || o.cast::<pyo3::types::PyTuple>().is_ok()
                        || o.cast::<pyo3::types::PyString>().is_ok()
                });
            }
            let _ = &object;
            return value.try_iter().is_ok();
        }
        false
    });

    // gettext functions backing {% trans %} (identity when no catalog)
    {
        let catalog = core.gettext_catalog.clone();
        env.add_function(
            "__dtpl_gettext",
            move |state: &minijinja::State, msgid: String| {
                let pattern = catalog
                    .as_ref()
                    .map(|c| c.gettext(&msgid))
                    .unwrap_or(msgid);
                percent_format(state, &pattern)
            },
        );
        let catalog = core.gettext_catalog.clone();
        env.add_function(
            "__dtpl_pgettext",
            move |state: &minijinja::State, context: String, msgid: String| {
                let pattern = catalog
                    .as_ref()
                    .map(|c| c.pgettext(&context, &msgid))
                    .unwrap_or(msgid);
                percent_format(state, &pattern)
            },
        );
        let catalog = core.gettext_catalog.clone();
        env.add_function(
            "__dtpl_ngettext",
            move |state: &minijinja::State, singular: String, plural: String, count: Value| {
                let n = i64::try_from(count).unwrap_or(1);
                let pattern = catalog
                    .as_ref()
                    .map(|c| c.ngettext(&singular, &plural, n))
                    .unwrap_or_else(|| if n == 1 { singular } else { plural });
                percent_format(state, &pattern)
            },
        );
        let catalog = core.gettext_catalog.clone();
        env.add_function(
            "__dtpl_npgettext",
            move |state: &minijinja::State, context: String, singular: String, plural: String, count: Value| {
                let n = i64::try_from(count).unwrap_or(1);
                let pattern = catalog
                    .as_ref()
                    .map(|c| c.npgettext(&context, &singular, &plural, n))
                    .unwrap_or_else(|| if n == 1 { singular } else { plural });
                percent_format(state, &pattern)
            },
        );
    }

    // user-registered filters / tests / functions / globals (python callables)
    #[cfg(feature = "python")]
    for (name, callable) in &core.custom_filters {
        let callable = pyo3::Python::attach(|py| callable.clone_ref(py));
        env.add_filter(name.clone(), move |args: minijinja::value::Rest<Value>, kwargs: minijinja::value::Kwargs| {
            call_python_variadic(&callable, &args.0, &kwargs)
        });
    }
    #[cfg(feature = "python")]
    for (name, callable) in &core.custom_tests {
        let callable = pyo3::Python::attach(|py| callable.clone_ref(py));
        env.add_test(name.clone(), move |args: minijinja::value::Rest<Value>, kwargs: minijinja::value::Kwargs| {
            call_python_variadic(&callable, &args.0, &kwargs)
                .map(|v| v.is_true())
        });
    }
    #[cfg(feature = "python")]
    for (name, callable) in &core.custom_functions {
        let callable = pyo3::Python::attach(|py| callable.clone_ref(py));
        env.add_function(name.clone(), move |args: minijinja::value::Rest<Value>, kwargs: minijinja::value::Kwargs| {
            call_python_variadic(&callable, &args.0, &kwargs)
        });
    }
    #[cfg(feature = "python")]
    for (name, value) in &core.custom_globals {
        let v = crate::pybridge::py_to_value_global(value);
        env.add_global(name.clone(), v);
    }
    // template loaders backing {% include %}/{% import %}/{% extends %}:
    // an explicitly registered python loader takes precedence over the
    // pure-Rust one; with neither, no loader is installed
    #[cfg(feature = "python")]
    if let Some(loader) = &core.template_loader {
        let loader = pyo3::Python::attach(|py| loader.clone_ref(py));
        env.set_loader(make_py_loader(loader));
    } else if let Some(loader) = &core.rust_template_loader {
        let loader = loader.clone();
        env.set_loader(move |name| Ok((loader.0)(name)));
    }
    #[cfg(not(feature = "python"))]
    if let Some(loader) = &core.rust_template_loader {
        let loader = loader.clone();
        env.set_loader(move |name| Ok((loader.0)(name)));
    }
    env
}

#[cfg(feature = "python")]
fn make_py_loader(
    loader: pyo3::Py<pyo3::PyAny>,
) -> impl Fn(&str) -> Result<Option<String>, minijinja::Error> + Send + Sync + 'static {
    move |name| {
        let name = name.to_string();
        pyo3::Python::attach(|py| -> Result<Option<String>, minijinja::Error> {
            let result = loader
                .call1(py, (name,))
                .map_err(|e| minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, e.to_string()))?;
            if result.is_none(py) {
                return Ok(None);
            }
            let source = result
                .extract::<String>(py)
                .map_err(|e| minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, e.to_string()))?;
            Ok(Some(source))
        })
    }
}

fn wordwrap(s: &str, width: usize, break_long: bool, wrapstring: &str) -> String {
    let mut out = String::new();
    let mut col = 0usize;
    for word in s.split(' ') {
        let wlen = word.chars().count();
        if col > 0 && col + 1 + wlen > width {
            out.push_str(wrapstring);
            if wlen > width && break_long {
                // break the long word itself
                let mut rest: Vec<char> = word.chars().collect();
                while rest.len() > width {
                    let chunk: String = rest.drain(..width).collect();
                    out.push_str(&chunk);
                    out.push_str(wrapstring);
                }
                out.push_str(&rest.iter().collect::<String>());
                col = rest.len();
                continue;
            }
            out.push_str(word);
            col = wlen;
            continue;
        }
        if col > 0 {
            out.push(' ');
            col += 1;
        }
        out.push_str(word);
        col += wlen;
    }
    out
}

fn urlize(s: &str) -> String {
    // link http(s):// and www. urls like jinja2's urlize (simplified policies)
    crate::patch::sub(
        r#"(https?://[^\s<>'"]+|www\.[^\s<>'"]+)"#,
        |m| {
            let url = m.get(1).unwrap().as_str();
            // strip trailing punctuation like jinja2 policies
            let (mut end, mut trail) = (url.len(), "");
            while let Some(c) = url[..end].chars().last() {
                if ".:;!?,)".contains(c) {
                    end -= c.len_utf8();
                    trail = &url[end..];
                } else {
                    break;
                }
            }
            let clean = &url[..end];
            let href = if clean.starts_with("www.") {
                format!("http://{}", clean)
            } else {
                clean.to_string()
            };
            format!(
                "<a href=\"{}\" rel=\"noopener\">{}</a>{}",
                href, clean, trail
            )
        },
        s,
    )
}

#[cfg(feature = "python")]
fn call_python_variadic(
    callable: &pyo3::Py<pyo3::PyAny>,
    args: &[Value],
    kwargs: &minijinja::value::Kwargs,
) -> Result<Value, minijinja::Error> {
    pyo3::Python::attach(|py| {
        let mut py_args = Vec::with_capacity(args.len());
        for a in args {
            py_args.push(
                crate::pybridge::value_to_py(py, a)
                    .map_err(|e| minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, e.to_string()))?,
            );
        }
        let tuple = pyo3::types::PyTuple::new(py, py_args)
            .map_err(|e| minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, e.to_string()))?;
        // keyword arguments -> PyDict (None when empty: skips the dict
        // allocation + per-call kwargs packing for the common no-kwargs call)
        let mut dict = None;
        for key in kwargs.args() {
            let v: Value = kwargs.get(key).map_err(|e| e)?;
            dict.get_or_insert_with(|| pyo3::types::PyDict::new(py))
                .set_item(
                    key,
                    crate::pybridge::value_to_py(py, &v).map_err(|e| {
                        minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, e.to_string())
                    })?,
                )
                .map_err(|e| minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, e.to_string()))?;
        }
        let result = callable
            .call(py, tuple, dict.as_ref())
            .map_err(|e| minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, e.to_string()))?;
        let result = result.bind(py);
        crate::pybridge::py_to_value_render(&result)
            .map_err(|e| minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, e.to_string()))
    })
}

/// Python str.format() with format spec support
/// ([[fill]align][sign][#][0][width][,|_][.precision][type])
fn py_format(template: &str, args: &[Value]) -> String {
    let mut result = String::new();
    let mut rest = template;
    let mut auto_idx = 0usize;
    while let Some(pos) = rest.find(['{', '}']) {
        let ch = rest[pos..].chars().next().unwrap();
        result.push_str(&rest[..pos]);
        if ch == '}' {
            if rest[pos..].starts_with("}}") {
                result.push('}');
                rest = &rest[pos + 2..];
            } else {
                result.push('}');
                rest = &rest[pos + 1..];
            }
            continue;
        }
        if rest[pos..].starts_with("{{") {
            result.push('{');
            rest = &rest[pos + 2..];
            continue;
        }
        let end = rest[pos..].find('}').map(|e| pos + e).unwrap_or(rest.len());
        let field_spec = &rest[pos + 1..end];
        rest = &rest[(end + 1).min(rest.len())..];
        // split field / conversion / spec
        let (field_conv, spec) = match field_spec.split_once(':') {
            Some((f, sp)) => (f, Some(sp)),
            None => (field_spec, None),
        };
        let (field, repr_mode) = match field_conv.strip_suffix("!r") {
            Some(f) => (f, true),
            None => (field_conv.strip_suffix("!s").unwrap_or(field_conv.strip_suffix("!a").unwrap_or(field_conv)), false),
        };
        let idx: usize = if field.is_empty() {
            let i = auto_idx;
            auto_idx += 1;
            i
        } else {
            field.parse().unwrap_or(0)
        };
        let Some(value) = args.get(idx) else {
            continue;
        };
        let mut text = if repr_mode {
            match value.kind() {
                minijinja::value::ValueKind::String => format!("'{}'", value),
                _ => value.to_string(),
            }
        } else {
            value.to_string()
        };
        if let Some(spec) = spec {
            text = apply_format_spec(&text, value, spec);
        }
        result.push_str(&text);
    }
    result.push_str(rest);
    result
}

fn apply_format_spec(text: &str, value: &Value, spec: &str) -> String {
    let chars: Vec<char> = spec.chars().collect();
    let mut i = 0usize;
    let mut fill = ' ';
    let mut align = '\0';
    let mut sign = '-';
    let mut alt = false;
    let mut zero = false;
    let mut width: Option<usize> = None;
    let mut grouping = '\0';
    let mut precision: Option<usize> = None;
    let mut ty = '\0';

    if chars.len() >= 2 && ["<", ">", "=", "^"].contains(&chars[1].to_string().as_str()) {
        fill = chars[0];
        align = chars[1];
        i = 2;
    } else if !chars.is_empty() && ["<", ">", "=", "^"].contains(&chars[0].to_string().as_str()) {
        align = chars[0];
        i = 1;
    }
    if i < chars.len() && (chars[i] == '+' || chars[i] == '-' || chars[i] == ' ') {
        sign = chars[i];
        i += 1;
    }
    if i < chars.len() && chars[i] == '#' {
        alt = true;
        i += 1;
    }
    if i < chars.len() && chars[i] == '0' {
        zero = true;
        i += 1;
    }
    let width_start = i;
    while i < chars.len() && chars[i].is_ascii_digit() {
        i += 1;
    }
    if i > width_start {
        width = chars[width_start..i].iter().collect::<String>().parse().ok();
    }
    if i < chars.len() && (chars[i] == ',' || chars[i] == '_') {
        grouping = chars[i];
        i += 1;
    }
    if i < chars.len() && chars[i] == '.' {
        i += 1;
        let p_start = i;
        while i < chars.len() && chars[i].is_ascii_digit() {
            i += 1;
        }
        precision = chars[p_start..i].iter().collect::<String>().parse().ok();
    }
    if i < chars.len() {
        ty = chars[i];
    }

    let is_neg = text.starts_with('-');
    let mut body = text.trim_start_matches(['-', '+']).to_string();

    // numeric reformatting
    let mut prefix = String::new();
    match ty {
        'd' | 'n' | '\0' if grouping != '\0' => {
            body = group_digits(&body, grouping);
        }
        'b' => {
            if let Some(v) = value.as_i64() {
                body = format!("{:b}", v);
                if alt { prefix = "0b".into(); }
            }
        }
        'o' => {
            if let Some(v) = value.as_i64() {
                body = format!("{:o}", v);
                if alt { prefix = "0o".into(); }
            }
        }
        'x' => {
            if let Some(v) = value.as_i64() {
                body = format!("{:x}", v);
                if alt { prefix = "0x".into(); }
            }
        }
        'X' => {
            if let Some(v) = value.as_i64() {
                body = format!("{:X}", v);
                if alt { prefix = "0X".into(); }
            }
        }
        'e' | 'E' => {
            if let Ok(v) = f64::try_from(value.clone()) {
                let p = precision.unwrap_or(6);
                body = py_exp_format(format!("{:.*e}", p, v));
                if ty == 'E' { body = body.to_uppercase(); }
            }
        }
        'f' | 'F' => {
            if let Ok(v) = f64::try_from(value.clone()) {
                let p = precision.unwrap_or(6);
                body = format!("{:.*}", p, v.abs());
                if grouping != '\0' { body = group_digits(&body, grouping); }
            }
        }
        '%' => {
            if let Ok(v) = f64::try_from(value.clone()) {
                let p = precision.unwrap_or(6);
                body = format!("{:.*}%", p, v.abs() * 100.0);
            }
        }
        'g' | 'G' => {
            if let Ok(v) = f64::try_from(value.clone()) {
                let p = precision.unwrap_or(6);
                body = format_g(v, p);
                if ty == 'G' { body = body.to_uppercase(); }
            }
        }
        'c' => {
            if let Some(v) = value.as_i64() {
                if let Some(c) = char::from_u32(v as u32) { body = c.to_string(); }
            }
        }
        's' | '\0' => {
            if let Some(p) = precision {
                body = body.chars().take(p).collect();
            }
        }
        _ => {}
    }
    if ty == 'd' && grouping != '\0' {
        // already handled above
    }

    let sign_str = if is_neg {
        "-"
    } else {
        match sign {
            '+' => "+",
            ' ' => " ",
            _ => "",
        }
    };

    let default_align = if matches!(value.kind(), minijinja::value::ValueKind::Number) { '>' } else { '<' };
    let align = if align == '\0' {
        if zero { '=' } else { default_align }
    } else {
        align
    };
    if zero && fill == ' ' {
        fill = '0';
    }

    let core = format!("{}{}{}", sign_str, prefix, body);
    let w = width.unwrap_or(0);
    let len = core.chars().count();
    if len >= w {
        return core;
    }
    let pad_total = w - len;
    match align {
        '>' => format!("{}{}", fill.to_string().repeat(pad_total), core),
        '^' => {
            let left = pad_total / 2;
            let right = pad_total - left;
            format!("{}{}{}", fill.to_string().repeat(left), core, fill.to_string().repeat(right))
        }
        '=' => format!("{}{}{}{}", sign_str, prefix, fill.to_string().repeat(pad_total), body),
        _ => format!("{}{}", core, fill.to_string().repeat(pad_total)),
    }
}

/// rust "1.2345e4" -> python "1.2345e+04"
fn py_exp_format(s: String) -> String {
    if let Some(epos) = s.find('e') {
        let (mantissa, exp) = (&s[..epos], &s[epos + 1..]);
        let (esign, edigits) = if let Some(d) = exp.strip_prefix('-') {
            ("-", d)
        } else {
            ("+", exp.trim_start_matches('+'))
        };
        return format!("{}e{}{:0>2}", mantissa, esign, edigits);
    }
    s
}

fn group_digits(s: &str, grouping: char) -> String {
    let (int_part, rest) = match s.find(['.', 'e', 'E']) {
        Some(p) => (&s[..p], &s[p..]),
        None => (s, ""),
    };
    let digits: Vec<char> = int_part.chars().collect();
    let mut out = String::new();
    for (i, c) in digits.iter().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(grouping);
        }
        out.push(*c);
    }
    out.push_str(rest);
    out
}

fn format_g(v: f64, precision: usize) -> String {
    if v == 0.0 {
        return "0".to_string();
    }
    let exp = v.abs().log10().floor() as i32;
    if exp < -4 || exp >= precision as i32 {
        let s = format!("{:.*e}", precision.saturating_sub(1), v);
        // trim trailing zeros in mantissa
        if let Some(epos) = s.find('e') {
            let mantissa = s[..epos].trim_end_matches('0').trim_end_matches('.');
            return format!("{}{}", mantissa, &s[epos..]);
        }
        s
    } else {
        let decimals = (precision as i32 - 1 - exp).max(0) as usize;
        let s = format!("{:.*}", decimals, v);
        s.trim_end_matches('0').trim_end_matches('.').to_string()
    }
}

/// A template-local mutable list backing `{% set x = [] %}` assignments
/// rewritten by [`preprocess_mutlist`] when the template mutates the list in
/// place (`x.append(...)`), emulating jinja2's list semantics (native
/// minijinja sequences are immutable).
#[derive(Debug, Default)]
pub struct MutList {
    items: std::sync::Mutex<Vec<Value>>,
}

/// python None for MutList method return values (renders as "None").
#[cfg(feature = "python")]
fn py_none() -> Value {
    Value::from_object(crate::pybridge::PyNoneObj)
}

#[cfg(not(feature = "python"))]
fn py_none() -> Value {
    Value::from(())
}

impl MutList {
    pub fn new() -> MutList {
        MutList::default()
    }
}

impl minijinja::value::Object for MutList {
    fn repr(self: &std::sync::Arc<Self>) -> minijinja::value::ObjectRepr {
        minijinja::value::ObjectRepr::Seq
    }

    fn get_value(self: &std::sync::Arc<Self>, key: &Value) -> Option<Value> {
        let idx = key.as_i64().and_then(|i| usize::try_from(i).ok())?;
        self.items.lock().unwrap().get(idx).cloned()
    }

    fn enumerate(self: &std::sync::Arc<Self>) -> minijinja::value::Enumerator {
        let items = self.items.lock().unwrap().clone();
        minijinja::value::Enumerator::Iter(Box::new(items.into_iter()))
    }

    fn enumerator_len(self: &std::sync::Arc<Self>) -> Option<usize> {
        Some(self.items.lock().unwrap().len())
    }

    fn call_method(
        self: &std::sync::Arc<Self>,
        _state: &minijinja::State<'_, '_>,
        method: &str,
        args: &[Value],
    ) -> Result<Value, minijinja::Error> {
        use minijinja::{Error, ErrorKind};
        match method {
            "append" => {
                let item = args.first().cloned().ok_or_else(|| {
                    Error::new(ErrorKind::MissingArgument, "append() takes exactly one argument")
                })?;
                self.items.lock().unwrap().push(item);
                Ok(py_none())
            }
            "extend" => {
                let arg = args.first().cloned().ok_or_else(|| {
                    Error::new(ErrorKind::MissingArgument, "extend() takes exactly one argument")
                })?;
                let iter = arg.try_iter().map_err(|_| {
                    Error::new(ErrorKind::InvalidOperation, "extend() argument is not iterable")
                })?;
                let mut items = self.items.lock().unwrap();
                items.extend(iter);
                Ok(py_none())
            }
            "insert" => {
                let idx = args.first().and_then(|v| v.as_i64()).ok_or_else(|| {
                    Error::new(ErrorKind::MissingArgument, "insert() needs an integer index")
                })?;
                let item = args.get(1).cloned().ok_or_else(|| {
                    Error::new(ErrorKind::MissingArgument, "insert() takes exactly two arguments")
                })?;
                let mut items = self.items.lock().unwrap();
                let len = items.len() as i64;
                let pos = if idx < 0 { (len + idx).max(0) } else { idx.min(len) } as usize;
                items.insert(pos, item);
                Ok(py_none())
            }
            "pop" => {
                let mut items = self.items.lock().unwrap();
                let len = items.len() as i64;
                let idx = args.first().and_then(|v| v.as_i64()).unwrap_or(-1);
                let pos = if idx < 0 { len + idx } else { idx };
                if pos < 0 || pos >= len {
                    return Err(Error::new(ErrorKind::InvalidOperation, "pop index out of range"));
                }
                Ok(items.remove(pos as usize))
            }
            "remove" => {
                let needle = args.first().cloned().ok_or_else(|| {
                    Error::new(ErrorKind::MissingArgument, "remove() takes exactly one argument")
                })?;
                let mut items = self.items.lock().unwrap();
                match items.iter().position(|v| *v == needle) {
                    Some(pos) => {
                        items.remove(pos);
                        Ok(py_none())
                    }
                    None => Err(Error::new(
                        ErrorKind::InvalidOperation,
                        "list.remove(x): x not in list",
                    )),
                }
            }
            "clear" => {
                self.items.lock().unwrap().clear();
                Ok(py_none())
            }
            _ => Err(Error::from(ErrorKind::UnknownMethod)),
        }
    }

    fn render(self: &std::sync::Arc<Self>, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // python list repr: ['a', 'b']
        let items = self.items.lock().unwrap();
        f.write_str("[")?;
        for (i, item) in items.iter().enumerate() {
            if i > 0 {
                f.write_str(", ")?;
            }
            if let Some(s) = item.as_str() {
                write!(f, "'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))?;
            } else {
                write!(f, "{}", item)?;
            }
        }
        f.write_str("]")
    }
}

/// jinja2.utils.Cycler as a template global: `cycler(*items)` cycles through
/// the items (`next()`/`reset()` methods, `current` attribute).
#[derive(Debug)]
pub struct Cycler {
    items: Vec<Value>,
    pos: std::sync::Mutex<usize>,
}

impl Cycler {
    pub fn new(items: Vec<Value>) -> Cycler {
        Cycler {
            items,
            pos: std::sync::Mutex::new(0),
        }
    }
}

impl minijinja::value::Object for Cycler {
    fn get_value(self: &std::sync::Arc<Self>, key: &Value) -> Option<Value> {
        if key.as_str() != Some("current") || self.items.is_empty() {
            return None;
        }
        // jinja2.utils.Cycler.current is items[pos]: the item next() returns.
        let pos = *self.pos.lock().unwrap();
        Some(self.items[pos % self.items.len()].clone())
    }

    fn call_method(
        self: &std::sync::Arc<Self>,
        _state: &minijinja::State<'_, '_>,
        method: &str,
        _args: &[Value],
    ) -> Result<Value, minijinja::Error> {
        use minijinja::{Error, ErrorKind};
        match method {
            "next" => {
                if self.items.is_empty() {
                    return Err(Error::new(
                        ErrorKind::InvalidOperation,
                        "no items to cycle through",
                    ));
                }
                let mut pos = self.pos.lock().unwrap();
                let item = self.items[*pos % self.items.len()].clone();
                *pos += 1;
                Ok(item)
            }
            "reset" => {
                *self.pos.lock().unwrap() = 0;
                Ok(py_none())
            }
            _ => Err(Error::from(ErrorKind::UnknownMethod)),
        }
    }
}

/// jinja2.utils.Joiner as a template global: `joiner(sep=", ")` returns a
/// callable producing `""` on the first call and `sep` on every later call.
#[derive(Debug)]
pub struct Joiner {
    sep: String,
    used: std::sync::Mutex<bool>,
}

impl Joiner {
    pub fn new(sep: String) -> Joiner {
        Joiner {
            sep,
            used: std::sync::Mutex::new(false),
        }
    }
}

impl minijinja::value::Object for Joiner {
    fn call(
        self: &std::sync::Arc<Self>,
        _state: &minijinja::State<'_, '_>,
        _args: &[Value],
    ) -> Result<Value, minijinja::Error> {
        let mut used = self.used.lock().unwrap();
        Ok(Value::from(if *used {
            self.sep.clone()
        } else {
            *used = true;
            String::new()
        }))
    }
}

/// Process-wide state for the `random` filter: seeded once from the system
/// time, advanced per call so back-to-back calls never share an index.
static RANDOM_STATE: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// Time since the Unix epoch, or `None` where the target has no clock:
/// `SystemTime::now` panics on wasm32-unknown-unknown.
pub(crate) fn unix_time() -> Option<std::time::Duration> {
    #[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
    { None }
    #[cfg(not(all(target_arch = "wasm32", target_os = "unknown")))]
    { std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok() }
}

fn random_u64() -> u64 {
    use std::sync::atomic::Ordering;
    let mut seed = RANDOM_STATE.load(Ordering::Relaxed);
    if seed == 0 {
        let t = unix_time()
            .map(|d| (d.subsec_nanos() as u64) ^ d.as_secs())
            .unwrap_or(0x9E3779B97F4A7C15);
        seed = (t ^ 0x9E3779B97F4A7C15) | 1;
        RANDOM_STATE
            .compare_exchange(0, seed, Ordering::Relaxed, Ordering::Relaxed)
            .ok();
    }
    // xorshift64* over the fetch_add result
    let mut x = RANDOM_STATE.fetch_add(0x9E3779B97F4A7C15, Ordering::Relaxed);
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    x.wrapping_mul(0x2545F4914F6CDD1D)
}

const LOREM_WORDS: &[&str] = &[
    "lorem", "ipsum", "dolor", "sit", "amet", "consectetur", "adipiscing", "elit",
    "sed", "do", "eiusmod", "tempor", "incididunt", "ut", "labore", "et", "dolore",
    "magna", "aliqua", "enim", "ad", "minim", "veniam", "quis", "nostrud",
    "exercitation", "ullamco", "laboris", "nisi", "aliquip", "ex", "ea", "commodo",
    "consequat", "duis", "aute", "irure", "in", "reprehenderit", "voluptate",
    "velit", "esse", "cillum", "fugiat", "nulla", "pariatur", "excepteur", "sint",
    "occaecat", "cupidatat", "non", "proident", "sunt", "culpa", "qui", "officia",
    "deserunt", "mollit", "anim", "id", "est", "laborum",
];

/// jinja2.utils.generate_lorem_ipsum(n=5, html=True, min=20, max=100) as a
/// pure-Rust function shared by the `lipsum` template global and the Python
/// binding (`jutils::generate_lorem_ipsum`).
pub(crate) fn lorem_ipsum(n: usize, html: bool, min: usize, max: usize) -> String {
    let mut seed = unix_time()
        .map(|d| d.subsec_nanos() as u64 ^ d.as_secs())
        .unwrap_or(0x9E3779B9);
    let mut rand = move || {
        seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (seed >> 33) as usize
    };
    let mut out = String::new();
    for _ in 0..n.max(1) {
        let count = min + rand() % (max.saturating_sub(min).max(1) + 1);
        let words: Vec<&str> = (0..count)
            .map(|_| LOREM_WORDS[rand() % LOREM_WORDS.len()])
            .collect();
        let mut sentence = words.join(" ");
        if let Some(c) = sentence.chars().next() {
            sentence = c.to_uppercase().collect::<String>() + &sentence[1..];
        }
        sentence.push('.');
        if html {
            out.push_str(&format!("<p>{}</p>\n", sentence));
        } else {
            out.push_str(&sentence);
            out.push_str("\n\n");
        }
    }
    out.trim_end().to_string()
}

/// Python-like str methods for jinja compatibility (called for unknown methods).
/// Python string/list/dict methods for plain MiniJinja values; hosts may
/// extend it through their own unknown-method callback.
pub fn py_like_method(
    value: &Value,
    method: &str,
    args: &[Value],
) -> Result<Value, minijinja::Error> {
    use minijinja::{Error, ErrorKind};
    let unsupported = || {
        Err(Error::new(
            ErrorKind::UnknownMethod,
            format!("object has no method named {}", method),
        ))
    };
    // dict methods
    if matches!(value.kind(), minijinja::value::ValueKind::Map) {
        match method {
            "keys" | "values" | "items" => {
                let iter = value.try_iter().map_err(|e| {
                    Error::new(ErrorKind::InvalidOperation, e.to_string())
                })?;
                let pairs: Vec<Value> = iter.collect();
                return Ok(match method {
                    "keys" => Value::from_serialize(&pairs),
                    "values" => {
                        let vals: Vec<Value> = pairs
                            .iter()
                            .filter_map(|k| value.get_item(k).ok())
                            .collect();
                        Value::from_serialize(&vals)
                    }
                    _ => {
                        let items: Vec<Value> = pairs
                            .iter()
                            .filter_map(|k| {
                                value
                                    .get_item(k)
                                    .ok()
                                    .map(|v| Value::from_serialize(&vec![k.clone(), v]))
                            })
                            .collect();
                        Value::from_serialize(&items)
                    }
                });
            }
            "get" => {
                let key = args.first().cloned().unwrap_or(Value::UNDEFINED);
                let default = args.get(1).cloned().unwrap_or(Value::from(()));
                return match value.get_item(&key) {
                    Ok(v) if !v.is_undefined() => Ok(v),
                    _ => Ok(default),
                };
            }
            _ => return unsupported(),
        }
    }
    let Some(s) = value.as_str().map(|s| s.to_string()) else {
        return unsupported();
    };
    let arg_str = |i: usize| -> String {
        args.get(i).map(|v| v.to_string()).unwrap_or_default()
    };
    let out: Value = match method {
        "upper" => Value::from(s.to_uppercase()),
        "lower" => Value::from(s.to_lowercase()),
        "capitalize" => {
            let mut chars = s.chars();
            let first = chars.next().map(|c| c.to_uppercase().collect::<String>());
            Value::from(format!(
                "{}{}",
                first.unwrap_or_default(),
                chars.as_str().to_lowercase()
            ))
        }
        "title" => Value::from(minijinja::filters::title(std::borrow::Cow::Borrowed(
            s.as_str(),
        ))),
        "strip" => Value::from(s.trim().to_string()),
        "lstrip" => Value::from(s.trim_start().to_string()),
        "rstrip" => Value::from(s.trim_end().to_string()),
        "replace" => Value::from(s.replace(&arg_str(0), &arg_str(1))),
        "startswith" => Value::from(s.starts_with(&arg_str(0))),
        "endswith" => Value::from(s.ends_with(&arg_str(0))),
        "split" => {
            let sep = args.first();
            let parts: Vec<String> = match sep {
                Some(v) if !v.is_none() && !v.is_undefined() => {
                    s.split(&v.to_string()).map(|p| p.to_string()).collect()
                }
                _ => s.split_whitespace().map(|p| p.to_string()).collect(),
            };
            Value::from_serialize(&parts)
        }
        "rsplit" => {
            let sep = args.first();
            let parts: Vec<String> = match sep {
                Some(v) if !v.is_none() && !v.is_undefined() => {
                    let mut p: Vec<String> =
                        s.rsplit(&v.to_string()).map(|p| p.to_string()).collect();
                    p.reverse();
                    p
                }
                _ => s.split_whitespace().map(|p| p.to_string()).collect(),
            };
            Value::from_serialize(&parts)
        }
        "join" => {
            let sep = s.clone();
            let mut parts: Vec<String> = Vec::new();
            if let Some(seq) = args.first() {
                if let Ok(iter) = seq.try_iter() {
                    for item in iter {
                        parts.push(item.to_string());
                    }
                }
            }
            Value::from(parts.join(&sep))
        }
        "zfill" => {
            let width: usize = arg_str(0).parse().unwrap_or(0);
            let (sign, digits) = if let Some(rest) = s.strip_prefix(['-', '+']) {
                (&s[..1], rest)
            } else {
                ("", s.as_str())
            };
            let pad = width.saturating_sub(sign.len() + digits.len());
            Value::from(format!("{}{}{}", sign, "0".repeat(pad), digits))
        }
        "ljust" => {
            let width: usize = arg_str(0).parse().unwrap_or(0);
            let fill = arg_str(1).chars().next().unwrap_or(' ');
            let pad = width.saturating_sub(s.chars().count());
            Value::from(format!("{}{}", s, fill.to_string().repeat(pad)))
        }
        "rjust" => {
            let width: usize = arg_str(0).parse().unwrap_or(0);
            let fill = arg_str(1).chars().next().unwrap_or(' ');
            let pad = width.saturating_sub(s.chars().count());
            Value::from(format!("{}{}", fill.to_string().repeat(pad), s))
        }
        "center" => {
            let width: usize = arg_str(0).parse().unwrap_or(0);
            let total = width.saturating_sub(s.chars().count());
            let left = total / 2;
            let right = total - left;
            Value::from(format!(
                "{}{}{}",
                " ".repeat(left),
                s,
                " ".repeat(right)
            ))
        }
        "count" => Value::from(s.matches(&arg_str(0)).count() as i64),
        "find" => Value::from(
            s.find(&arg_str(0)).map(|i| i as i64).unwrap_or(-1),
        ),
        "index" => match s.find(&arg_str(0)) {
            Some(i) => Value::from(i as i64),
            None => {
                return Err(Error::new(
                    ErrorKind::InvalidOperation,
                    "substring not found",
                ))
            }
        },
        "format" => Value::from(py_format(&s, args)),
        "isdigit" => Value::from(!s.is_empty() && s.chars().all(|c| c.is_ascii_digit())),
        "isalpha" => Value::from(!s.is_empty() && s.chars().all(|c| c.is_alphabetic())),
        "isalnum" => Value::from(!s.is_empty() && s.chars().all(|c| c.is_alphanumeric())),
        "isspace" => Value::from(!s.is_empty() && s.chars().all(|c| c.is_whitespace())),
        "isupper" => Value::from(s.chars().any(|c| c.is_uppercase()) && !s.chars().any(|c| c.is_lowercase())),
        "islower" => Value::from(s.chars().any(|c| c.is_lowercase()) && !s.chars().any(|c| c.is_uppercase())),
        "isnumeric" => Value::from(!s.is_empty() && s.chars().all(|c| c.is_numeric())),
        "isdecimal" => Value::from(!s.is_empty() && s.chars().all(|c| c.is_ascii_digit())),
        "istitle" => {
            let mut prev_cased = false;
            let mut ok = !s.is_empty();
            for c in s.chars() {
                let cased = c.is_alphabetic();
                if cased {
                    if prev_cased && c.is_uppercase() {
                        ok = false;
                        break;
                    }
                    if !prev_cased && c.is_lowercase() {
                        ok = false;
                        break;
                    }
                }
                prev_cased = cased;
            }
            Value::from(ok)
        }
        "isidentifier" => {
            let mut chars = s.chars();
            let first_ok = chars.next().map(|c| c.is_alphabetic() || c == '_').unwrap_or(false);
            Value::from(first_ok && chars.all(|c| c.is_alphanumeric() || c == '_'))
        }
        "isprintable" => Value::from(s.chars().all(|c| !c.is_control())),
        "swapcase" => Value::from(
            s.chars()
                .map(|c| {
                    if c.is_uppercase() {
                        c.to_lowercase().collect::<String>()
                    } else if c.is_lowercase() {
                        c.to_uppercase().collect::<String>()
                    } else {
                        c.to_string()
                    }
                })
                .collect::<String>(),
        ),
        "casefold" => Value::from(s.to_lowercase()),
        "expandtabs" => {
            let tabsize: usize = args.first().and_then(|v| v.to_string().parse().ok()).unwrap_or(8);
            let mut out = String::new();
            let mut col = 0usize;
            for c in s.chars() {
                match c {
                    '\t' => {
                        let spaces = tabsize - (col % tabsize.max(1));
                        out.push_str(&" ".repeat(spaces));
                        col += spaces;
                    }
                    '\n' | '\r' => {
                        out.push(c);
                        col = 0;
                    }
                    _ => {
                        out.push(c);
                        col += 1;
                    }
                }
            }
            Value::from(out)
        }
        "splitlines" => {
            let keepends = args.first().map(|v| v.is_true()).unwrap_or(false);
            let mut parts: Vec<String> = Vec::new();
            let mut cur = String::new();
            let mut chars = s.chars().peekable();
            while let Some(c) = chars.next() {
                let is_break = c == '\n' || c == '\r';
                if is_break {
                    if c == '\r' && chars.peek() == Some(&'\n') {
                        chars.next();
                        if keepends {
                            cur.push('\r');
                            cur.push('\n');
                        }
                    } else if keepends {
                        cur.push(c);
                    }
                    parts.push(std::mem::take(&mut cur));
                } else {
                    cur.push(c);
                }
            }
            if !cur.is_empty() {
                parts.push(cur);
            }
            Value::from_serialize(&parts)
        }
        "partition" => {
            let sep = arg_str(0);
            let (before, found, after) = match s.find(&sep) {
                Some(i) if !sep.is_empty() => (s[..i].to_string(), sep.clone(), s[i + sep.len()..].to_string()),
                _ => (s.clone(), String::new(), String::new()),
            };
            Value::from_serialize(&vec![before, found, after])
        }
        "rpartition" => {
            let sep = arg_str(0);
            let (before, found, after) = match s.rfind(&sep) {
                Some(i) if !sep.is_empty() => (s[..i].to_string(), sep.clone(), s[i + sep.len()..].to_string()),
                _ => (String::new(), String::new(), s.clone()),
            };
            Value::from_serialize(&vec![before, found, after])
        }
        "removeprefix" => {
            let p = arg_str(0);
            Value::from(s.strip_prefix(&p).unwrap_or(&s).to_string())
        }
        "removesuffix" => {
            let p = arg_str(0);
            Value::from(s.strip_suffix(&p).unwrap_or(&s).to_string())
        }
        "encode" => Value::from(s.clone()),
        _ => return unsupported(),
    };
    Ok(out)
}

/// Analysis variant of the trans rewrite for undeclared_variables: strips
/// the trans/pluralize/endtrans tags but keeps the bodies (and assignment
/// RHS / pluralize count expressions) visible, so variables inside trans
/// blocks are still reported. The render-time gettext rewrite would fold
/// `{{ var }}` into a msgid string, hiding it from static analysis.
fn trans_bodies_for_analysis(src: &str) -> Cow<'_, str> {
    if !src.contains("{% trans") {
        return Cow::Borrowed(src);
    }
    Cow::Owned(crate::patch::sub(
        r"(?s)\{%\s*trans\s*(.*?)\%\}(.*?)(?:\{%\s*pluralize\s*(.*?)\s*%\}(.*?))?\{%\s*endtrans\s*%\}",
        |m| {
            let mut s = String::new();
            let kv_re = crate::patch::re(r#"(\w+)\s*=\s*("(?:[^"]*)"|'(?:[^']*)'|[^,\s]+)"#);
            for cap in kv_re.captures_iter(m.get(1).unwrap().as_str()).flatten() {
                if &cap[1] == "context" {
                    continue;
                }
                s.push_str(&format!("{{% set {} = {} %}}", &cap[1], &cap[2]));
            }
            s.push_str(m.get(2).unwrap().as_str());
            if let Some(e) = m.get(3).map(|g| g.as_str().trim()).filter(|e| !e.is_empty()) {
                s.push_str(&format!("{{{{ {} }}}}", e));
            }
            if let Some(p) = m.get(4) {
                s.push_str(p.as_str());
            }
            s
        },
        src,
    ))
}

/// Rewrite `{% trans %}` blocks into gettext function calls (jinja2 i18n
/// extension semantics). Without an installed catalog the functions fall
/// back to the untranslated, formatted msgid.
pub fn preprocess_trans(src: &str) -> Cow<'_, str> {    if !src.contains("{% trans") {
        return Cow::Borrowed(src);
    }
    Cow::Owned(crate::patch::sub(
        r"(?s)\{%\s*trans\s*(.*?)%\}(.*?)(?:\{%\s*pluralize\s*(.*?)\s*%\}(.*?))?\{%\s*endtrans\s*%\}",
        |m| trans_rewrite(
            m.get(1).unwrap().as_str(),
            m.get(2).unwrap().as_str(),
            m.get(3).map(|g| g.as_str()),
            m.get(4).map(|g| g.as_str()),
        ),
        src,
    ))
}

fn trans_rewrite(
    assigns: &str,
    singular: &str,
    plz_expr: Option<&str>,
    plural: Option<&str>,
) -> String {
    // parse k=v assignments; context="x" selects pgettext
    let kv_re = crate::patch::re(r#"(\w+)\s*=\s*("(?:[^"]*)"|'(?:[^']*)'|[^,\s]+)"#);
    let mut sets = String::new();
    let mut context: Option<String> = None;
    let mut first_var: Option<String> = None;
    for cap in kv_re.captures_iter(assigns).flatten() {
        let key = cap[1].to_string();
        let value = cap[2].to_string();
        if key == "context" {
            context = Some(value.trim_matches(['"', '\'']).to_string());
            continue;
        }
        if first_var.is_none() {
            first_var = Some(key.clone());
        }
        sets.push_str(&format!("{{% set {} = {} %}}", key, value));
    }

    let escape_jinja_str = |s: &str| -> String {
        s.replace('\\', "\\\\").replace('"', "\\\"").replace('\n', "\\n")
    };
    let to_msgid = |body: &str| -> String {
        let interpolated = crate::patch::sub(
            r"\{\{\s*(.*?)\s*\}\}",
            |m| format!("%({})s", m.get(1).unwrap().as_str().trim()),
            body,
        );
        escape_jinja_str(&interpolated)
    };

    let singular_id = to_msgid(singular);
    match plural {
        Some(plural_body) => {
            let plural_id = to_msgid(plural_body);
            let count = plz_expr
                .map(|e| e.trim().to_string())
                .filter(|e| !e.is_empty())
                .or(first_var)
                .unwrap_or_else(|| "1".to_string());
            let func = if context.is_some() {
                "__dtpl_npgettext"
            } else {
                "__dtpl_ngettext"
            };
            let ctx_arg = context
                .map(|c| format!("\"{}\", ", escape_jinja_str(&c)))
                .unwrap_or_default();
            format!(
                "{}{{{{ {}({}\"{}\", \"{}\", ({})) }}}}",
                sets, func, ctx_arg, singular_id, plural_id, count
            )
        }
        None => {
            let (func, ctx_arg) = match &context {
                Some(c) => (
                    "__dtpl_pgettext",
                    format!("\"{}\", ", escape_jinja_str(c)),
                ),
                None => ("__dtpl_gettext", String::new()),
            };
            format!("{}{{{{ {}({}\"{}\") }}}}", sets, func, ctx_arg, singular_id)
        }
    }
}

/// Python %-style formatting against the template state
/// (supports %(name)s/%d/%f/%x/%% with flags, width, precision).
fn percent_format(state: &minijinja::State, pattern: &str) -> Result<String, minijinja::Error> {
    let mut out = String::new();
    let mut rest = pattern;
    while let Some(pos) = rest.find('%') {
        out.push_str(&rest[..pos]);
        let tail = &rest[pos + 1..];
        if let Some(stripped) = tail.strip_prefix('%') {
            out.push('%');
            rest = stripped;
            continue;
        }
        let Some(after) = tail.strip_prefix('(') else {
            out.push('%');
            rest = tail;
            continue;
        };
        let Some(end) = after.find(')') else {
            out.push('%');
            rest = tail;
            continue;
        };
        let name = &after[..end];
        let mut spec_part = &after[end + 1..];
        // parse %-spec: [-+ #0]*[width][.precision]type
        let mut align_left = false;
        let mut sign = "";
        let mut zero = false;
        let mut alt = false;
        loop {
            match spec_part.chars().next() {
                Some('-') => align_left = true,
                Some('+') => sign = "+",
                Some(' ') => sign = " ",
                Some('0') => zero = true,
                Some('#') => alt = true,
                _ => break,
            }
            spec_part = &spec_part[1..];
        }
        let mut width = String::new();
        while spec_part.chars().next().map(|c| c.is_ascii_digit()).unwrap_or(false) {
            width.push(spec_part.chars().next().unwrap());
            spec_part = &spec_part[1..];
        }
        let mut precision = String::new();
        if spec_part.starts_with('.') {
            spec_part = &spec_part[1..];
            while spec_part.chars().next().map(|c| c.is_ascii_digit()).unwrap_or(false) {
                precision.push(spec_part.chars().next().unwrap());
                spec_part = &spec_part[1..];
            }
        }
        let ty = spec_part.chars().next().unwrap_or('s');
        spec_part = &spec_part[1..];
        rest = spec_part;

        let value = state
            .lookup(name)
            .unwrap_or_else(|| Value::from(()));
        // build a {}-style spec for apply_format_spec
        let mut spec = String::new();
        if align_left {
            spec.push('<');
        }
        spec.push_str(sign);
        if alt {
            spec.push('#');
        }
        if zero {
            spec.push('0');
        }
        spec.push_str(&width);
        if !precision.is_empty() {
            spec.push('.');
            spec.push_str(&precision);
        }
        spec.push(ty);
        let text = value.to_string();
        out.push_str(&apply_format_spec(&text, &value, &spec));
    }
    out.push_str(rest);
    Ok(out)
}

/// Rewrite `== true` / `!= false` style comparisons to custom tests with
/// exact Python equality semantics (booleans are wrapped for Python-faithful
/// display, so minijinja's native `==` cannot compare them with `true`).
///
/// The rewrite is scoped to jinja tag contents and skips quoted strings, so
/// literal document text and string literals are never touched.
pub fn preprocess_bool_compare(src: &str) -> Cow<'_, str> {
    if !src.contains("true") && !src.contains("false") && !src.contains("none")
        && !src.contains("True") && !src.contains("False") && !src.contains("None")
    {
        return Cow::Borrowed(src);
    }
    const RULES: &[(&str, &str)] = &[
        ("== true", "is eq_true"),
        ("!= true", "is not eq_true"),
        ("== false", "is eq_false"),
        ("!= false", "is not eq_false"),
        ("== True", "is eq_true"),
        ("!= True", "is not eq_true"),
        ("== False", "is eq_false"),
        ("!= False", "is not eq_false"),
        ("== none", "is none"),
        ("!= none", "is not none"),
        ("== None", "is none"),
        ("!= None", "is not none"),
    ];

    // process only the inside of {% %} / {{ }} blocks (linear tag scan;
    // borrowed when no tag actually changed)
    rewrite_jinja_tags(src, |tag| {
        // cheap prefilter: every rule starts with == or !=
        if !tag.contains("==") && !tag.contains("!=") {
            return None;
        }
        let rewritten = rewrite_tag_bool_compare(tag, RULES);
        if rewritten != tag {
            Some(rewritten)
        } else {
            None
        }
    })
}

fn rewrite_tag_bool_compare(tag: &str, rules: &[(&str, &str)]) -> String {
    // walk the tag, rewriting only outside quoted regions (char-safe)
    let mut out = String::with_capacity(tag.len());
    let mut i = 0usize;
    while i < tag.len() {
        let c = tag[i..].chars().next().unwrap();
        if c == '"' || c == '\'' {
            // copy the quoted region verbatim
            let start = i;
            i += c.len_utf8();
            while i < tag.len() {
                let d = tag[i..].chars().next().unwrap();
                if d == '\\' {
                    i += d.len_utf8();
                    if i < tag.len() {
                        i += tag[i..].chars().next().unwrap().len_utf8();
                    }
                    continue;
                }
                i += d.len_utf8();
                if d == c {
                    break;
                }
            }
            out.push_str(&tag[start..i]);
            continue;
        }
        // try to match a rule at this position
        let mut matched = false;
        for (pat, rep) in rules {
            if tag[i..].starts_with(pat) {
                // boundary check: the char after the pattern must not be an
                // identifier char (avoid matching e.g. "== trueish")
                let after = tag[i + pat.len()..].chars().next();
                if after.map(|c| c.is_alphanumeric() || c == '_').unwrap_or(true) {
                    continue;
                }
                out.push_str(rep);
                i += pat.len();
                matched = true;
                break;
            }
        }
        if !matched {
            out.push(c);
            i += c.len_utf8();
        }
    }
    out
}

/// Engine-feature preprocessing applied to jinja tag contents:
/// `{% debug %}` -> `{{ debug() }}`, and printf-style `'fmt' % args` ->
/// `('fmt')|pyformat(args)` (minijinja has no % operator for strings).
pub fn preprocess_engine_features(src: &str) -> Cow<'_, str> {
    let needs_debug = src.contains("{% debug");
    // printf-style needs a quoted string right before % ("..' % x" / "..'% x");
    // a bare % (every {% tag}) must not trigger the full tag rewrite scan
    let needs_printf = src.contains("'%")
        || src.contains("\"%")
        || src.contains("' %")
        || src.contains("\" %");
    if !needs_debug && !needs_printf {
        return Cow::Borrowed(src);
    }
    rewrite_jinja_tags(src, |tag| {
        let mut out: Cow<'_, str> = Cow::Borrowed(tag);
        if needs_debug && tag.contains("debug") {
            out = Cow::Owned(crate::patch::sub_str(
                r"\{%\s*debug\s*%\}",
                "{{ debug() }}",
                &out,
            ));
        }
        if needs_printf && (out.contains('%') && (out.contains('\'') || out.contains('"'))) {
            out = Cow::Owned(rewrite_printf_tags(&out));
        }
        match out {
            Cow::Owned(o) if o != tag => Some(o),
            _ => None,
        }
    })
}

/// jinja2 allows in-place mutation of template-created lists:
/// `{% set x = [] %}` followed by `x.append(...)` (or extend/insert/pop/
/// remove/clear). Native minijinja sequences are immutable, so rewrite those
/// assignments to construct a [`MutList`] (transparent to the template).
/// Only variables that are actually mutated somewhere in the source are
/// rewritten.
pub fn preprocess_mutlist(src: &str) -> Cow<'_, str> {
    const METHODS: &[&str] = &["append", "extend", "insert", "pop", "remove", "clear"];
    if !METHODS.iter().any(|m| src.contains(&format!(".{}", m))) {
        return Cow::Borrowed(src);
    }
    let rex = crate::patch::re(r"([A-Za-z_]\w*)\.(append|extend|insert|pop|remove|clear)\(");
    let mut names: HashSet<String> = HashSet::new();
    for cap in rex.captures_iter(src).flatten() {
        names.insert(cap[1].to_string());
    }
    if names.is_empty() {
        return Cow::Borrowed(src);
    }
    let set_rex = crate::patch::re(
        r"^\{%([-+]?)\s*set\s+([A-Za-z_]\w*)\s*=\s*\[\s*\]\s*([-+]?)%\}$",
    );
    rewrite_jinja_tags(src, |tag| {
        if let Some(Some(cap)) = set_rex.captures(tag).ok() {
            if names.contains(&cap[2]) {
                return Some(format!(
                    "{{%{0} set {1} = __dtpl_mutlist() {2}%}}",
                    &cap[1], &cap[2], &cap[3]
                ));
            }
        }
        None
    })
}

/// Rewrite `STR % RHS` inside a tag to `(STR)|pyformat(RHS)` (char-safe).
fn rewrite_printf_tags(tag: &str) -> String {
    let mut out = String::with_capacity(tag.len());
    let mut i = 0usize;
    while i < tag.len() {
        let c = tag[i..].chars().next().unwrap();
        if c == '"' || c == '\'' {
            // capture the string literal
            let start = i;
            i += c.len_utf8();
            while i < tag.len() {
                let d = tag[i..].chars().next().unwrap();
                if d == '\\' {
                    i += d.len_utf8();
                    if i < tag.len() {
                        i += tag[i..].chars().next().unwrap().len_utf8();
                    }
                    continue;
                }
                i += d.len_utf8();
                if d == c {
                    break;
                }
            }
            let literal = &tag[start..i];
            // check for ` % rhs` after the literal
            let mut j = i;
            while j < tag.len() && tag[j..].chars().next().unwrap().is_whitespace() {
                j += tag[j..].chars().next().unwrap().len_utf8();
            }
            let has_printf = tag[j..].starts_with('%')
                && tag[j + 1..]
                    .chars()
                    .next()
                    .map(|c| c.is_whitespace() || c == '(' || c.is_alphabetic() || c == '_' || c.is_ascii_digit())
                    .unwrap_or(false);
            if has_printf {
                let mut k = j + 1;
                while k < tag.len() && tag[k..].chars().next().unwrap().is_whitespace() {
                    k += tag[k..].chars().next().unwrap().len_utf8();
                }
                // capture RHS: balanced parens or an atom
                let (rhs, end) = capture_rhs(tag, k);
                if !rhs.is_empty() {
                    out.push_str(&format!("({})|pyformat({})", literal, rhs));
                    i = end;
                    continue;
                }
            }
            out.push_str(literal);
            continue;
        }
        out.push(c);
        i += c.len_utf8();
    }
    out
}

/// Capture an expression atom starting at position k: a balanced-paren group
/// (without outer parens) or `name.attr[0].call(...)` style atom.
fn capture_rhs(tag: &str, k: usize) -> (String, usize) {
    if k >= tag.len() {
        return (String::new(), k);
    }
    if tag[k..].starts_with('(') {
        // balanced parens; content without outer parens
        let mut depth = 0i32;
        let mut j = k;
        while j < tag.len() {
            let c = tag[j..].chars().next().unwrap();
            match c {
                '(' | '[' | '{' => depth += 1,
                ')' | ']' | '}' => {
                    depth -= 1;
                    if depth == 0 {
                        return (tag[k + 1..j].to_string(), j + 1);
                    }
                }
                _ => {}
            }
            j += c.len_utf8();
        }
        return (String::new(), k);
    }
    // atom: dotted name possibly with calls / subscripts
    let mut j = k;
    while j < tag.len() {
        let c = tag[j..].chars().next().unwrap();
        if c.is_alphanumeric() || c == '_' || c == '.' {
            j += c.len_utf8();
        } else if c == '(' || c == '[' {
            let close = if c == '(' { ')' } else { ']' };
            let mut depth = 0i32;
            while j < tag.len() {
                let d = tag[j..].chars().next().unwrap();
                if d == c {
                    depth += 1;
                } else if d == close {
                    depth -= 1;
                    if depth == 0 {
                        j += d.len_utf8();
                        break;
                    }
                }
                j += d.len_utf8();
            }
        } else {
            break;
        }
    }
    (tag[k..j].to_string(), j)
}

/// Python % formatting with positional args.
fn percent_format_positional(fmt: &str, args: &[Value]) -> Result<String, minijinja::Error> {
    let mut out = String::new();
    let mut rest = fmt;
    let mut arg_iter = args.iter();
    while let Some(pos) = rest.find('%') {
        out.push_str(&rest[..pos]);
        let tail = &rest[pos + 1..];
        if let Some(stripped) = tail.strip_prefix('%') {
            out.push('%');
            rest = stripped;
            continue;
        }
        // parse spec inline: [-+ #0]*[width][.precision]type
        let mut spec_part = tail;
        let mut spec = String::new();
        loop {
            match spec_part.chars().next() {
                Some('-') => spec.push('<'),
                Some('+') => spec.push('+'),
                Some(' ') => spec.push(' '),
                Some('0') => spec.push('0'),
                Some('#') => spec.push('#'),
                _ => break,
            }
            spec_part = &spec_part[1..];
        }
        while spec_part.chars().next().map(|c| c.is_ascii_digit()).unwrap_or(false) {
            spec.push(spec_part.chars().next().unwrap());
            spec_part = &spec_part[1..];
        }
        if spec_part.starts_with('.') {
            spec.push('.');
            spec_part = &spec_part[1..];
            while spec_part.chars().next().map(|c| c.is_ascii_digit()).unwrap_or(false) {
                spec.push(spec_part.chars().next().unwrap());
                spec_part = &spec_part[1..];
            }
        }
        let ty = spec_part.chars().next().unwrap_or('s');
        spec_part = &spec_part[1..];
        rest = spec_part;
        spec.push(ty);
        // %(name)s is not valid in positional mode; treat '(' as literal
        if ty == '(' {
            out.push('%');
            continue;
        }
        let Some(value) = arg_iter.next() else {
            break;
        };
        let text = value.to_string();
        out.push_str(&apply_format_spec(&text, value, &spec));
    }
    out.push_str(rest);
    Ok(out)
}

/// Python `== True` semantics: true for booleans True and numbers equal to 1.
fn test_eq_true(value: &Value) -> bool {
    #[cfg(feature = "python")]
    if let Some(o) = value.as_object() {
        if let Some(b) = o.downcast_ref::<crate::pybridge::PyBoolObj>() {
            return b.0;
        }
        return false;
    }
    match value.kind() {
        minijinja::value::ValueKind::Bool => value.is_true(),
        minijinja::value::ValueKind::Number => value.as_i64() == Some(1)
            || f64::try_from(value.clone()).map(|f| f == 1.0).unwrap_or(false),
        _ => false,
    }
}

/// Python `== False` semantics.
fn test_eq_false(value: &Value) -> bool {
    #[cfg(feature = "python")]
    if let Some(o) = value.as_object() {
        if let Some(b) = o.downcast_ref::<crate::pybridge::PyBoolObj>() {
            return !b.0;
        }
        return false;
    }
    match value.kind() {
        minijinja::value::ValueKind::Bool => !value.is_true(),
        minijinja::value::ValueKind::Number => value.as_i64() == Some(0)
            || f64::try_from(value.clone()).map(|f| f == 0.0).unwrap_or(false),
        _ => false,
    }
}

/// Heuristic repair for ill-formed xml (lxml recover-mode equivalent):
/// escape stray `&` and `<` that are clearly not markup, so structured
/// fixes can still run when values were inserted unescaped.
pub fn recover_xml(xml: &str) -> String {
    // & not starting a valid entity -> &amp;
    let s = crate::patch::sub(
        r"&(?!(?:amp|lt|gt|quot|apos);|#[0-9]+;|#x[0-9a-fA-F]+;)",
        |_| "&amp;".to_string(),
        xml,
    );
    // < is markup only if it opens/closes a namespace-qualified element
    // (docx parts always use prefixes), a declaration or a comment/doctype;
    // everything else (e.g. text "a<b" or a stray "</mark>") is escaped, which
    // also yields schema-valid output instead of lxml's unknown-element recovery
    crate::patch::sub(
        r"<(?!(?:/?[a-zA-Z_][\w.-]*:)|[?!])",
        |_| "&lt;".to_string(),
        &s,
    )
}

pub fn render_xml_str(src_xml: &str, ctx: Value, autoescape: bool, core: &mut TplCore) -> Result<String, String> {
    let env = make_env(autoescape, core);
    render_xml_str_with(src_xml, ctx, &env, core)
}

/// render_xml_str with a pre-built environment (shared across all parts of
/// one render call).
pub fn render_xml_str_with(src_xml: &str, ctx: Value, env: &Environment, core: &mut TplCore) -> Result<String, String> {
    // add newlines before paragraphs so template error line numbers are useful
    // (plain memmem scan; the fancy-regex original paid per-match capture
    // expansion over the whole part)
    let src = add_paragraph_newlines(src_xml);
    let src = normalize_template_newlines(&src);
    let src = preprocess_trans(&src);
    let src = preprocess_bool_compare(&src);
    let src = preprocess_engine_features(&src);
    let src = preprocess_mutlist(&src);

    let rendered = match env.template_from_str(&src).and_then(|t| t.render(&ctx)) {
        Ok(s) => s,
        Err(e) => {
            let mut msg = format!("{}", e);
            if let Some(line) = e.line() {
                let start = line.saturating_sub(4);
                let context: Vec<String> = src
                    .lines()
                    .skip(start)
                    .take(7)
                    .map(|l| sub_str(r"<[^>]+>", "", l))
                    .collect();
                core.last_error_context = context.clone();
                msg.push_str(&format!(
                    "\nContext (lines {}-{}):\n{}",
                    start + 1,
                    start + 7,
                    context.join("\n")
                ));
            }
            return Err(msg);
        }
    };

    let dst = remove_paragraph_newlines(&rendered);
    Ok(restore_escaped_delims(dst.into_owned()))
}

/// rustc-hash style rotate-xor-multiply over the whole buffer (multi-GB/s;
/// a SipHash DefaultHasher would be a measurable cost on multi-MB parts)
fn fxhash64(bytes: &[u8]) -> u64 {
    const K: u64 = 0x51_7c_c1_b7_27_22_0a_95;
    let mut h = 0u64;
    let mut chunks = bytes.chunks_exact(8);
    for c in &mut chunks {
        h = (h.rotate_left(5) ^ u64::from_le_bytes(c.try_into().unwrap())).wrapping_mul(K);
    }
    let mut tail = 0u64;
    for (i, b) in chunks.remainder().iter().enumerate() {
        tail |= (*b as u64) << (8 * i);
    }
    (h.rotate_left(5) ^ tail).wrapping_mul(K)
}

/// Jinja's lexer normalizes CRLF and bare CR to LF before rendering.
pub fn normalize_template_newlines(src: &str) -> Cow<'_, str> {
    if !src.contains('\r') { return Cow::Borrowed(src); }
    let mut output = String::with_capacity(src.len());
    let mut chars = src.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\r' {
            if chars.peek() == Some(&'\n') { chars.next(); }
            output.push('\n');
        } else {
            output.push(ch);
        }
    }
    Cow::Owned(output)
}

/// `<w:p([ >])` -> `\n<w:p$1` without regex: insert `\n` before every
/// `<w:p ` / `<w:p>` opening tag.
pub fn add_paragraph_newlines(src: &str) -> Cow<'_, str> {
    if !src.contains("<w:p") {
        return Cow::Borrowed(src);
    }
    // every match inserts exactly one '\n'
    let extra = src.matches("<w:p ").count() + src.matches("<w:p>").count();
    let mut out = String::with_capacity(src.len() + extra);
    let mut rest = src;
    while let Some(p) = rest.find("<w:p") {
        let after = p + 4;
        match rest.as_bytes().get(after) {
            Some(b' ') | Some(b'>') => {
                out.push_str(&rest[..p]);
                out.push_str("\n<w:p");
            }
            _ => {
                out.push_str(&rest[..after]);
            }
        }
        rest = &rest[after..];
    }
    out.push_str(rest);
    Cow::Owned(out)
}

/// `\n<w:p([ >])` -> `<w:p$1` without regex: drop a `\n` immediately before
/// `<w:p ` / `<w:p>`; borrowed when there is nothing to remove.
pub fn remove_paragraph_newlines(s: &str) -> Cow<'_, str> {
    if !s.contains("\n<w:p") {
        return Cow::Borrowed(s);
    }
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(p) = rest.find("\n<w:p") {
        let after = p + 5;
        match rest.as_bytes().get(after) {
            Some(b' ') | Some(b'>') => {
                out.push_str(&rest[..p]); // drop the '\n'
                out.push_str("<w:p");
            }
            _ => {
                out.push_str(&rest[..after]);
            }
        }
        rest = &rest[after..];
    }
    out.push_str(rest);
    Cow::Owned(out)
}

/// Linear-scan equivalent of the tag-finding regex `(?s)(\{%.*?%\}|\{\{.*?\}\})`:
/// calls `f` with each `{% ... %}` / `{{ ... }}` tag (closer included, first
/// matching closer wins, non-overlapping left to right); `f` returns
/// Some(replacement) to splice it in or None to keep the tag verbatim.
/// Borrowed when no tag was replaced.
fn rewrite_jinja_tags<'a>(
    src: &'a str,
    mut f: impl FnMut(&str) -> Option<String>,
) -> Cow<'a, str> {
    let b = src.as_bytes();
    let mut out: Option<String> = None;
    let mut copied = 0usize;
    let mut i = 0usize;
    while i + 3 < b.len() {
        if b[i] == b'{' && (b[i + 1] == b'%' || b[i + 1] == b'{') {
            let closer = if b[i + 1] == b'%' { "%}" } else { "}}" };
            if let Some(rel) = src[i + 2..].find(closer) {
                let tag_end = i + 2 + rel + 2;
                if let Some(rep) = f(&src[i..tag_end]) {
                    let o = out.get_or_insert_with(|| String::with_capacity(src.len()));
                    o.push_str(&src[copied..i]);
                    o.push_str(&rep);
                    copied = tag_end;
                }
                i = tag_end;
                continue;
            }
        }
        i += 1;
    }
    match out {
        Some(mut o) => {
            o.push_str(&src[copied..]);
            Cow::Owned(o)
        }
        None => Cow::Borrowed(src),
    }
}

/// Restore markers in reference order. Earlier replacements can create a
/// match for a later one, including across overlapping markers.
pub fn restore_escaped_delims(mut s: String) -> String {
    if !s.contains("{_") && !s.contains("}_") && !s.contains("%_") {
        return s;
    }
    // These are the only overlaps where an earlier substitution changes a
    // later match. The general path retains the reference's replacement order.
    if s.contains("{_{_%") || s.contains("{_%_}") || s.contains("%_}_}") {
        for (marker, replacement) in [("{_{", "{{"), ("}_}", "}}"), ("{_%", "{%"), ("%_}", "%}")] {
            if s.contains(marker) { s = s.replace(marker, replacement); }
        }
        return s;
    }
    let bytes = s.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut last = 0;
    let mut i = 0;
    while i + 2 < bytes.len() {
        let replacement = if bytes[i + 1] == b'_' {
            match (bytes[i], bytes[i + 2]) {
                (b'{', b'{') => Some("{{"), (b'}', b'}') => Some("}}"),
                (b'{', b'%') => Some("{%"), (b'%', b'}') => Some("%}"),
                _ => None,
            }
        } else { None };
        if let Some(replacement) = replacement {
            out.push_str(&s[last..i]);
            out.push_str(replacement);
            i += 3;
            last = i;
        } else { i += 1; }
    }
    out.push_str(&s[last..]);
    out
}

#[cfg(test)]
mod delimiter_restore_tests {
    use super::restore_escaped_delims;

    #[test]
    fn restores_overlap_in_reference_order() {
        for (source, expected) in [("{_{_%", "{{%"), ("{_%_}", "{%}"), ("%_}_}", "%}}"),
                                   ("Café {_{_% 🐍", "Café {{% 🐍"), ("unchanged", "unchanged")] {
            assert_eq!(restore_escaped_delims(source.to_owned()), expected);
        }
    }

    #[test]
    fn exhaustive_marker_alphabet_matches_sequential_substitution() {
        let alphabet = ['{', '}', '_', '%'];
        for length in 0..=9u32 {
            for mut number in 0..4usize.pow(length) {
                let mut source = String::with_capacity(length as usize);
                for _ in 0..length {
                    source.push(alphabet[number % alphabet.len()]);
                    number /= alphabet.len();
                }
                let expected = source.replace("{_{", "{{").replace("}_}", "}}")
                    .replace("{_%", "{%").replace("%_}", "%}");
                assert_eq!(restore_escaped_delims(source.clone()), expected, "{source:?}");
            }
        }
    }

    #[test]
    fn long_mixed_unicode_sequences_match_sequential_substitution() {
        let alphabet = ['{', '}', '_', '%', 'x', '🐍'];
        let mut state = 7u64;
        for _ in 0..10_000 {
            let mut source = String::new();
            for _ in 0..100 {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                source.push(alphabet[(state >> 32) as usize % alphabet.len()]);
            }
            let expected = source.replace("{_{", "{{").replace("}_}", "}}")
                .replace("{_%", "{%").replace("%_}", "%}");
            assert_eq!(restore_escaped_delims(source.clone()), expected, "{source:?}");
        }
    }
}

// ---------------- fix tables & docPr ids ----------------

/// docxtpl get_headers_footers_encoding: the encoding declared in the xml
/// declaration (`<?xml ... encoding="X"`), defaulting to "utf-8". Only the
/// declaration's double-quoted form is recognized, matching docxtpl's regex.
pub fn headers_footers_encoding(xml: &str) -> String {
    let head: String = xml.chars().take(200).collect();
    if !head.starts_with("<?xml") {
        return "utf-8".to_string();
    }
    // ascii-lowercasing preserves byte positions, so `pos` indexes `head`
    let lower = head.to_ascii_lowercase();
    if let Some(pos) = lower.find("encoding=\"") {
        let rest = &head[pos + "encoding=\"".len()..];
        let enc: String = rest.chars().take_while(|c| *c != '"').collect();
        if !enc.is_empty() {
            return enc;
        }
    }
    "utf-8".to_string()
}

pub fn fix_tables_and_docpr(xml: &str, docx_ids_index: &mut u32) -> Result<String, String> {
    fix_impl(xml, true, Some(docx_ids_index), None)
}

/// fix_tables + docPr renumber + (optionally) pic:cNvPr renumber in a single
/// DOM parse/serialize round-trip. The cNvPr pass (docxcompose
/// renumber_nvpicpr_ids parity) is only wanted when subdocs were merged;
/// folding it in here saves a second full parse+serialize of the body.
pub(crate) fn fix_tables_docpr_cnvpr(
    xml: &str,
    docx_ids_index: &mut u32,
    cnvpr_next: Option<&mut u32>,
) -> Result<String, String> {
    fix_impl(xml, true, Some(docx_ids_index), cnvpr_next)
}

/// docxtpl fix_tables as a standalone string->string pass: table grid fixing
/// only (no docPr/cNvPr renumbering), same three-level fallback
/// (strict DOM -> recover_xml -> regex).
pub fn fix_tables_only(xml: &str) -> Result<String, String> {
    fix_impl(xml, true, None, None)
}

/// docxtpl fix_docpr_ids as a standalone string->string pass: renumber
/// wp:docPr ids from docx_ids_index (and, when cnvpr_next is given,
/// pic:cNvPr ids) without any table fixing.
pub fn fix_docpr_cnvpr_ids(
    xml: &str,
    docx_ids_index: &mut u32,
    mut cnvpr_next: Option<&mut u32>,
) -> String {
    let mut s = if xml.contains("wp:docPr") {
        regex_fix_docpr(xml, docx_ids_index)
    } else {
        xml.to_string()
    };
    if let Some(next) = cnvpr_next.as_deref_mut() {
        if let Some(fixed) = renumber_cnvpr(&s, next) {
            s = fixed;
        }
    }
    s
}

/// Shared implementation behind fix_tables_and_docpr / fix_tables_docpr_cnvpr
/// / fix_tables_only. `do_tables` gates the w:tbl grid pass (the standalone
/// id-renumber entry points never reach the DOM walk), `docx_ids_index` /
/// `cnvpr_next` gate the wp:docPr / pic:cNvPr renumbering passes.
fn fix_impl(
    xml: &str,
    do_tables: bool,
    docx_ids_index: Option<&mut u32>,
    cnvpr_next: Option<&mut u32>,
) -> Result<String, String> {
    // nothing to fix without any table, drawing or (when requested) picture:
    // skip the full DOM parse+serialize round-trip
    let has_tbl = do_tables && xml.contains("<w:tbl");
    let has_docpr = docx_ids_index.is_some() && xml.contains("wp:docPr");
    let has_cnvpr = cnvpr_next.is_some() && xml.contains("pic:cNvPr");
    if !has_tbl && !has_docpr && !has_cnvpr {
        return Ok(xml.to_string());
    }
    if !has_tbl {
        // no tables: docPr/cNvPr renumbering are pure id rewrites — linear
        // string scans avoid the full DOM parse+serialize round-trip
        return Ok(match docx_ids_index {
            Some(idx) => fix_docpr_cnvpr_ids(xml, idx, cnvpr_next),
            None => match cnvpr_next {
                Some(next) => renumber_cnvpr(xml, next).unwrap_or_else(|| xml.to_string()),
                None => xml.to_string(),
            },
        });
    }
    // pure-table case (no docPr/cNvPr work): a read-only string scan decides
    // whether any table grid actually needs fixing — the common case after
    // rendering is "no", which skips the full DOM parse+walk entirely
    if !has_docpr && cnvpr_next.is_none() && tables_fix_scan(xml) == TblScan::NoFix {
        return Ok(xml.to_string());
    }
    // If the rendered xml is not well-formed (e.g. unescaped values without
    // autoescape), attempt recovery first (docxtpl uses an lxml recover-mode
    // parser). As a last resort, apply regex-based fixes so table grids and
    // docPr ids are corrected even for severely broken xml.
    let doc = match Document::parse(xml) {
        Ok(d) => Some(d),
        Err(_) => match Document::parse(&recover_xml(xml)) {
            Ok(d) => Some(d),
            Err(_) => None,
        },
    };
    match doc {
        Some(mut doc) => {
            // single fused tree walk for tables + docPr + cNvPr; the docPr and
            // cNvPr passes are skipped entirely when the raw xml contains no
            // such elements (pure-table documents avoid the extra traversal)
            let docpr_idx = if has_docpr { docx_ids_index } else { None };
            let cnvpr_idx = if has_cnvpr { cnvpr_next } else { None };
            let changed = fix_elem_fused(&mut doc.root, docpr_idx, cnvpr_idx);
            if !changed {
                // grid widths and ids were already consistent: skip the
                // whole serialize (the expensive half of this pass)
                return Ok(xml.to_string());
            }
            // round-trip output stays close to the input size (fixes are local)
            Ok(doc.serialize_with_capacity(xml.len() + xml.len() / 8 + 64))
        }
        None => {
            let s = regex_fix_tables(xml);
            let mut s = match docx_ids_index {
                Some(idx) => regex_fix_docpr(&s, idx),
                None => s,
            };
            // cNvPr renumbering is DOM-based; attempt it on the regex-fixed
            // xml, exactly like the old fix-then-renumber sequence
            if let Some(next) = cnvpr_next {
                if let Some(fixed) = renumber_cnvpr(&s, next) {
                    s = fixed;
                }
            }
            Ok(s)
        }
    }
}

/// Renumber pic:cNvPr ids sequentially (docxcompose renumber_nvpicpr_ids).
pub(crate) fn renumber_cnvpr(xml: &str, next_id: &mut u32) -> Option<String> {
    if !xml.contains("pic:cNvPr") {
        return None;
    }
    let mut doc = Document::parse(xml).ok()?;
    fix_cnvpr_elem(&mut doc.root, next_id);
    Some(doc.serialize_with_capacity(xml.len() + 64))
}

fn fix_cnvpr_elem(el: &mut Element, next_id: &mut u32) -> bool {
    let mut changed = false;
    if el.name == "pic:cNvPr" {
        el.set_attr("id", &next_id.to_string());
        *next_id += 1;
        changed = true;
    }
    for c in el.children.iter_mut() {
        if let Node::Elem(e) = c {
            changed |= fix_cnvpr_elem(e, next_id);
        }
    }
    changed
}

/// Last-resort docPr renumbering for unparseable xml.
fn regex_fix_docpr(xml: &str, idx: &mut u32) -> String {
    crate::patch::sub(
        r#"<wp:docPr id="\d+""#,
        |m| {
            *idx += 1;
            let _ = m;
            format!("<wp:docPr id=\"{}\"", idx)
        },
        xml,
    )
}

/// Last-resort table grid fixing for unparseable xml: adjusts gridCol count
/// and widths to match the maximum cell count per row.
fn regex_fix_tables(xml: &str) -> String {
    // find top-level table regions by nesting depth
    let mut regions: Vec<(usize, usize)> = Vec::new();
    {
        let re_tbl = crate::patch::re(r"<w:tbl[ >]|</w:tbl>");
        let mut depth = 0i32;
        let mut start = 0usize;
        for m in re_tbl.find_iter(xml).flatten() {
            if m.as_str().starts_with("</") {
                depth -= 1;
                if depth == 0 {
                    regions.push((start, m.end()));
                }
            } else {
                if depth == 0 {
                    start = m.start();
                }
                depth += 1;
            }
        }
    }
    if regions.is_empty() {
        return xml.to_string();
    }

    let gridcol_re = crate::patch::re(r#"<w:gridCol w:w="(\d+)"/>"#);
    let mut out = String::with_capacity(xml.len());
    let mut last = 0usize;
    for (rs, re) in regions {
        out.push_str(&xml[last..rs]);
        let region = &xml[rs..re];
        let fixed = (|| {
            let grid_start = region.find("<w:tblGrid>")?;
            let grid_end = region.find("</w:tblGrid>")?;
            let grid = &region[grid_start..grid_end];
            let mut widths: Vec<i64> = gridcol_re
                .captures_iter(grid)
                .flatten()
                .map(|c| c[1].parse().unwrap_or(0))
                .collect();
            let n_cols = widths.len();
            // max cells in any row of the region
            let mut max_cells = 0usize;
            for row in region.split("<w:tr").skip(1) {
                let row_end = row.find("</w:tr>").unwrap_or(row.len());
                let cells = row[..row_end].matches("<w:tc>").count()
                    + row[..row_end].matches("<w:tc ").count();
                max_cells = max_cells.max(cells);
            }
            if max_cells == 0 || n_cols == 0 {
                return None;
            }
            let total: i64 = widths.iter().sum();
            if max_cells > n_cols && total > 0 {
                let old_avg = total as f64 / n_cols as f64;
                let new_avg = (total as f64 / max_cells as f64) as i64;
                for w in widths.iter_mut() {
                    *w = (*w as f64 * new_avg as f64 / old_avg) as i64;
                }
                while widths.len() < max_cells {
                    widths.push(new_avg);
                }
            } else if n_cols > max_cells {
                let removed: i64 = widths.drain(max_cells..).sum();
                let extra = removed / max_cells.max(1) as i64;
                for w in widths.iter_mut() {
                    *w += extra;
                }
            } else {
                return None;
            }
            // rebuild the region with the new grid
            let new_grid: String = widths
                .iter()
                .map(|w| format!("<w:gridCol w:w=\"{}\"/>", w))
                .collect();
            Some(format!(
                "{}<w:tblGrid>{}</w:tblGrid>{}",
                &region[..grid_start],
                new_grid,
                &region[grid_end + "</w:tblGrid>".len()..]
            ))
        })();
        out.push_str(fixed.as_deref().unwrap_or(region));
        last = re;
    }
    out.push_str(&xml[last..]);
    out
}

/// Fused single-pass tree walk combining fix_tables_elem + fix_docpr_elem +
/// fix_cnvpr_elem. `docpr_idx`/`cnvpr_next` are None when the corresponding
/// element kind is known absent (gated by the caller's raw-string contains
/// checks), turning those passes into no-ops without walking the tree.
fn fix_elem_fused(
    el: &mut Element,
    mut docpr_idx: Option<&mut u32>,
    mut cnvpr_next: Option<&mut u32>,
) -> bool {
    let mut changed = false;
    if el.name == "w:tbl" {
        changed |= fix_one_table(el);
    }
    if let Some(idx) = docpr_idx.as_deref_mut() {
        if el.name == "wp:docPr" {
            *idx += 1;
            el.set_attr("id", &idx.to_string());
            changed = true;
        }
    }
    if let Some(next) = cnvpr_next.as_deref_mut() {
        if el.name == "pic:cNvPr" {
            el.set_attr("id", &next.to_string());
            *next += 1;
            changed = true;
        }
    }
    for c in el.children.iter_mut() {
        if let Node::Elem(e) = c {
            changed |= fix_elem_fused(e, docpr_idx.as_deref_mut(), cnvpr_next.as_deref_mut());
        }
    }
    changed
}

fn fix_one_table(tbl: &mut Element) -> bool {
    let mut changed = false;
    let Some(tbl_grid_idx) = tbl
        .children
        .iter()
        .position(|c| matches!(c, Node::Elem(e) if e.name == "w:tblGrid"))
    else {
        return false;
    };

    // snapshot column widths
    let grid = match &tbl.children[tbl_grid_idx] {
        Node::Elem(e) => e,
        _ => return false,
    };
    let mut col_widths: Vec<f64> = grid
        .find_all("w:gridCol")
        .iter()
        .map(|c| {
            c.get_attr("w:w")
                .and_then(|w| w.parse::<f64>().ok())
                .unwrap_or(0.0)
        })
        .collect();
    let n_columns = col_widths.len();

    // count stats across all descendant rows (immutable pass)
    let mut rows: Vec<&Element> = Vec::new();
    tbl.iter_descendants("w:tr", &mut rows);
    let get_cell_len = |cell: &Element| -> usize {
        if let Some(tcpr) = cell.find("w:tcPr") {
            if let Some(gs) = tcpr.find("w:gridSpan") {
                if let Some(v) = gs.get_attr("w:val").and_then(|v| v.parse::<usize>().ok()) {
                    return v;
                }
            }
        }
        1
    };
    // single loop over rows: max missing columns and max grid-span-adjusted
    // cell count (merged from two loops that each re-collected w:tc vecs)
    let mut to_add = 0usize;
    let mut cells_len_max = 0usize;
    for r in &rows {
        let cells = r.find_all("w:tc");
        if n_columns + to_add < cells.len() {
            to_add = cells.len() - n_columns;
        }
        let len: usize = cells.iter().map(|c| get_cell_len(c)).sum();
        cells_len_max = cells_len_max.max(len);
    }
    drop(rows);

    if to_add > 0 {
        let width: f64 = col_widths.iter().sum();
        if width > 0.0 && n_columns > 0 {
            changed = true;
            let old_average = width / n_columns as f64;
            let new_average = width / (n_columns + to_add) as f64;
            for w in col_widths.iter_mut() {
                *w = (*w * new_average / old_average).trunc();
            }
            for _ in 0..to_add {
                col_widths.push(new_average.trunc());
            }
            // write back
            if let Node::Elem(grid) = &mut tbl.children[tbl_grid_idx] {
                let mut i = 0usize;
                for c in grid.children.iter_mut() {
                    if let Node::Elem(e) = c {
                        if e.name == "w:gridCol" && i < col_widths.len() {
                            e.set_attr("w:w", &(col_widths[i] as i64).to_string());
                            i += 1;
                        }
                    }
                }
                for w in col_widths.iter().skip(i) {
                    let mut gc = Element::new("w:gridCol");
                    gc.set_attr("w:w", &(*w as i64).to_string());
                    grid.children.push(Node::Elem(gc));
                }
            }
        }
    }

    // refetch columns
    let grid = match &tbl.children[tbl_grid_idx] {
        Node::Elem(e) => e,
        _ => return changed,
    };
    let columns = grid.find_all("w:gridCol");
    let columns_len = columns.len();
    let col_widths2: Vec<f64> = columns
        .iter()
        .map(|c| {
            c.get_attr("w:w")
                .and_then(|w| w.parse::<f64>().ok())
                .unwrap_or(0.0)
        })
        .collect();

    let to_remove = columns_len.saturating_sub(cells_len_max);
    if to_remove > 0 && columns_len > 0 {
        changed = true;
        let removed_width: f64 = col_widths2[columns_len - to_remove..].iter().sum();
        if let Node::Elem(grid) = &mut tbl.children[tbl_grid_idx] {
            // remove last to_remove gridCol children
            let mut idxs: Vec<usize> = grid
                .children
                .iter()
                .enumerate()
                .filter_map(|(i, c)| match c {
                    Node::Elem(e) if e.name == "w:gridCol" => Some(i),
                    _ => None,
                })
                .collect();
            idxs.reverse();
            for i in idxs.into_iter().take(to_remove) {
                grid.children.remove(i);
            }
            // redistribute removed width
            let n_left = grid.find_all("w:gridCol").len();
            let extra = if n_left > 0 {
                (removed_width / n_left as f64) as i64
            } else {
                0
            };
            for c in grid.children.iter_mut() {
                if let Node::Elem(e) = c {
                    if e.name == "w:gridCol" {
                        let w = e
                            .get_attr("w:w")
                            .and_then(|w| w.parse::<f64>().ok())
                            .unwrap_or(0.0);
                        e.set_attr("w:w", &((w as i64) + extra).to_string());
                    }
                }
            }
        }
    }
    changed
}

// ---------------- table grid fix pre-scan ----------------

/// Outcome of the read-only string scan that decides whether any table needs
/// grid fixes (see fix_tables_docpr_cnvpr).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TblScan {
    /// every table's grid is consistent with its rows: no DOM round-trip needed
    NoFix,
    /// at least one table would be modified by fix_one_table
    NeedsFix,
    /// unusual construct: fall back to the DOM path
    Bail,
}

/// Streaming, allocation-free equivalent of running fix_one_table's
/// *decision* on every `w:tbl` in the document. Replicates the DOM version's
/// semantics exactly:
/// - the grid is the FIRST direct `w:tblGrid` child (a table without one is
///   never fixed); gridCols are its direct children, `w:w` parsed as f64
///   with 0.0 on failure;
/// - every descendant `w:tr` (including rows of nested tables) contributes
///   its direct `w:tc` children count and the gridSpan-adjusted cell sum
///   (first direct `w:tcPr` > first direct `w:gridSpan` > `w:val` as usize,
///   else 1);
/// - changed iff (max_cells - n_columns > 0 && width_sum > 0 && n_columns > 0)
///   or (columns_len(after the simulated add) - cells_len_max > 0).
/// Anything unusual (comments, CDATA, doctype, entities inside the two
/// attribute values we must parse, malformed markup) bails to the DOM path.
fn tables_fix_scan(xml: &str) -> TblScan {
    let b = xml.as_bytes();
    let n = b.len();

    struct TblStats {
        has_grid: bool,
        n_columns: usize,
        width_sum: f64,
        max_cells: usize,
        cells_len_max: usize,
    }
    enum Frame {
        Table(usize),
        Grid(usize),
        Tr { cells: usize, len_sum: usize },
        Tc { seen_tcpr: bool, span: Option<usize> },
        TcPr { seen_gs: bool },
        Other(usize, usize), // name byte range into xml
    }

    let mut stack: Vec<Frame> = Vec::with_capacity(16);
    let mut tbls: Vec<TblStats> = Vec::new();

    // apply a finished row's stats to every enclosing table
    macro_rules! apply_row {
        ($stack:expr, $tbls:expr, $cells:expr, $len_sum:expr) => {
            for fr in $stack.iter() {
                if let Frame::Table(idx) = fr {
                    let t = &mut $tbls[*idx];
                    t.max_cells = t.max_cells.max($cells);
                    t.cells_len_max = t.cells_len_max.max($len_sum);
                }
            }
        };
    }

    let mut i = 0usize;
    while i < n {
        let Some(rel) = memchr::memchr(b'<', &b[i..]) else { break };
        let lt = i + rel;
        if lt + 1 >= n {
            return TblScan::Bail;
        }
        i = match b[lt + 1] {
            b'?' => match xml[lt + 2..].find("?>") {
                Some(k) => lt + 2 + k + 2,
                None => return TblScan::Bail,
            },
            b'!' => {
                if xml[lt..].starts_with("<!--") {
                    match xml[lt + 4..].find("-->") {
                        Some(k) => lt + 4 + k + 3,
                        None => return TblScan::Bail,
                    }
                } else if xml[lt..].starts_with("<![CDATA[") {
                    match xml[lt + 9..].find("]]>") {
                        Some(k) => lt + 9 + k + 3,
                        None => return TblScan::Bail,
                    }
                } else {
                    return TblScan::Bail; // doctype etc.
                }
            }
            b'/' => {
                // end tag: `</name S? >`
                let mut j = lt + 2;
                let name_start = j;
                while j < n && !b[j].is_ascii_whitespace() && b[j] != b'>' {
                    j += 1;
                }
                let name = &xml[name_start..j];
                while j < n && b[j].is_ascii_whitespace() {
                    j += 1;
                }
                if j >= n || b[j] != b'>' || name.is_empty() {
                    return TblScan::Bail;
                }
                let Some(frame) = stack.pop() else {
                    return TblScan::Bail;
                };
                let matches = match &frame {
                    Frame::Table(_) => name == "w:tbl",
                    Frame::Grid(_) => name == "w:tblGrid",
                    Frame::Tr { .. } => name == "w:tr",
                    Frame::Tc { .. } => name == "w:tc",
                    Frame::TcPr { .. } => name == "w:tcPr",
                    Frame::Other(s, e) => name == &xml[*s..*e],
                };
                if !matches {
                    return TblScan::Bail;
                }
                match frame {
                    Frame::Tr { cells, len_sum } => apply_row!(stack, tbls, cells, len_sum),
                    Frame::Tc { span, .. } => {
                        // parent is the row that pushed this cell
                        if let Some(Frame::Tr { len_sum, .. }) = stack.last_mut() {
                            *len_sum += span.unwrap_or(1);
                        }
                    }
                    Frame::Table(idx) => {
                        let t = &tbls[idx];
                        if t.has_grid {
                            let ncols = t.n_columns;
                            let to_add = t.max_cells.saturating_sub(ncols);
                            let executed = to_add > 0 && t.width_sum > 0.0 && ncols > 0;
                            let columns_len = if executed { ncols + to_add } else { ncols };
                            let to_remove = columns_len.saturating_sub(t.cells_len_max);
                            if executed || (to_remove > 0 && columns_len > 0) {
                                return TblScan::NeedsFix;
                            }
                        }
                    }
                    _ => {}
                }
                j + 1
            }
            _ => {
                // start / empty tag: name up to a delimiter
                let mut j = lt + 1;
                let name_start = j;
                while j < n && !b[j].is_ascii_whitespace() && b[j] != b'/' && b[j] != b'>' {
                    j += 1;
                }
                if j == name_start {
                    return TblScan::Bail;
                }
                let name = &xml[name_start..j];
                // find '>' respecting quoted attribute values
                let mut q: Option<u8> = None;
                while j < n {
                    let c = b[j];
                    match q {
                        Some(qq) => {
                            if c == qq {
                                q = None;
                            }
                        }
                        None => match c {
                            b'"' | b'\'' => q = Some(c),
                            b'>' => break,
                            _ => {}
                        },
                    }
                    j += 1;
                }
                if j >= n {
                    return TblScan::Bail; // unterminated tag
                }
                // self-closing: optional whitespace then '/' right before '>'
                let mut k = j;
                while k > name_start && b[k - 1].is_ascii_whitespace() {
                    k -= 1;
                }
                let self_closing = k > name_start && b[k - 1] == b'/';
                let attrs = &xml[name_start + name.len()..k - (self_closing as usize)];

                // attribute value used for gridCol width / gridSpan val:
                // first occurrence wins (quick-xml iteration order); an
                // entity inside the value bails (the DOM decodes it first)
                macro_rules! attr_f64 {
                    ($attr:literal) => {
                        match find_attr(attrs, $attr) {
                            AttrRes::Found(v) => {
                                if v.contains('&') {
                                    return TblScan::Bail;
                                }
                                v.parse::<f64>().unwrap_or(0.0)
                            }
                            AttrRes::Absent => 0.0,
                            AttrRes::Bad => return TblScan::Bail,
                        }
                    };
                }
                macro_rules! attr_usize {
                    ($attr:literal) => {
                        match find_attr(attrs, $attr) {
                            AttrRes::Found(v) => {
                                if v.contains('&') {
                                    return TblScan::Bail;
                                }
                                v.parse::<usize>().ok()
                            }
                            AttrRes::Absent => None,
                            AttrRes::Bad => return TblScan::Bail,
                        }
                    };
                }

                match name {
                    "w:tbl" => {
                        if self_closing {
                            // gridless table: never fixed
                        } else {
                            tbls.push(TblStats {
                                has_grid: false,
                                n_columns: 0,
                                width_sum: 0.0,
                                max_cells: 0,
                                cells_len_max: 0,
                            });
                            stack.push(Frame::Table(tbls.len() - 1));
                        }
                    }
                    "w:tblGrid" => {
                        let direct = matches!(stack.last(), Some(Frame::Table(_)));
                        if self_closing {
                            if let Some(Frame::Table(idx)) = stack.last() {
                                tbls[*idx].has_grid = true;
                            }
                        } else if direct {
                            let idx = match stack.last() {
                                Some(Frame::Table(idx)) => *idx,
                                _ => unreachable!(),
                            };
                            if tbls[idx].has_grid {
                                // fix_one_table only uses the FIRST tblGrid
                                stack.push(Frame::Other(name_start, name_start + name.len()));
                            } else {
                                tbls[idx].has_grid = true;
                                stack.push(Frame::Grid(idx));
                            }
                        } else {
                            stack.push(Frame::Other(name_start, name_start + name.len()));
                        }
                    }
                    "w:gridCol" => {
                        if let Some(Frame::Grid(idx)) = stack.last() {
                            let idx = *idx;
                            tbls[idx].n_columns += 1;
                            tbls[idx].width_sum += attr_f64!("w:w");
                        }
                        if !self_closing {
                            stack.push(Frame::Other(name_start, name_start + name.len()));
                        }
                    }
                    "w:tr" => {
                        if self_closing {
                            apply_row!(stack, tbls, 0usize, 0usize);
                        } else {
                            stack.push(Frame::Tr { cells: 0, len_sum: 0 });
                        }
                    }
                    "w:tc" => {
                        let direct = matches!(stack.last(), Some(Frame::Tr { .. }));
                        if self_closing {
                            if let Some(Frame::Tr { cells, len_sum }) = stack.last_mut() {
                                *cells += 1;
                                *len_sum += 1;
                            }
                        } else if direct {
                            if let Some(Frame::Tr { cells, .. }) = stack.last_mut() {
                                *cells += 1;
                            }
                            stack.push(Frame::Tc { seen_tcpr: false, span: None });
                        } else {
                            stack.push(Frame::Other(name_start, name_start + name.len()));
                        }
                    }
                    "w:tcPr" => {
                        let first = matches!(stack.last(), Some(Frame::Tc { seen_tcpr: false, .. }));
                        if self_closing {
                            if let Some(Frame::Tc { seen_tcpr, .. }) = stack.last_mut() {
                                if !*seen_tcpr {
                                    *seen_tcpr = true;
                                }
                            }
                        } else if first {
                            if let Some(Frame::Tc { seen_tcpr, .. }) = stack.last_mut() {
                                *seen_tcpr = true;
                            }
                            stack.push(Frame::TcPr { seen_gs: false });
                        } else {
                            stack.push(Frame::Other(name_start, name_start + name.len()));
                        }
                    }
                    "w:gridSpan" => {
                        let first = matches!(stack.last(), Some(Frame::TcPr { seen_gs: false }));
                        if first {
                            if let Some(Frame::TcPr { seen_gs }) = stack.last_mut() {
                                *seen_gs = true;
                            }
                            let v = attr_usize!("w:val").unwrap_or(1);
                            // the cell frame sits below the tcPr frame
                            let m = stack.len();
                            if m >= 2 {
                                if let Frame::Tc { span, .. } = &mut stack[m - 2] {
                                    *span = Some(v);
                                }
                            }
                        }
                        if !self_closing {
                            stack.push(Frame::Other(name_start, name_start + name.len()));
                        }
                    }
                    _ => {
                        if !self_closing {
                            stack.push(Frame::Other(name_start, name_start + name.len()));
                        }
                    }
                }
                j + 1
            }
        };
    }
    if !stack.is_empty() {
        return TblScan::Bail; // unclosed elements: the DOM path recovers
    }
    TblScan::NoFix
}

/// Result of the lenient attribute lookup used by tables_fix_scan.
enum AttrRes<'a> {
    Found(&'a str),
    Absent,
    Bad,
}

/// First attribute with `name` in a start tag's attribute region
/// (`a="v" b='v'` pairs, quick-xml order). Lenient like quick-xml about
/// whitespace; anything malformed yields Bad (the caller bails to DOM).
fn find_attr<'a>(attrs: &'a str, name: &str) -> AttrRes<'a> {
    let b = attrs.as_bytes();
    let n = b.len();
    let mut i = 0usize;
    loop {
        while i < n && b[i].is_ascii_whitespace() {
            i += 1;
        }
        if i >= n {
            return AttrRes::Absent;
        }
        let name_start = i;
        while i < n && b[i] != b'=' && !b[i].is_ascii_whitespace() {
            i += 1;
        }
        let aname = &attrs[name_start..i];
        while i < n && b[i].is_ascii_whitespace() {
            i += 1;
        }
        if i >= n || b[i] != b'=' {
            return AttrRes::Bad;
        }
        i += 1;
        while i < n && b[i].is_ascii_whitespace() {
            i += 1;
        }
        if i >= n || (b[i] != b'"' && b[i] != b'\'') {
            return AttrRes::Bad;
        }
        let q = b[i];
        i += 1;
        let val_start = i;
        while i < n && b[i] != q {
            i += 1;
        }
        if i >= n {
            return AttrRes::Bad;
        }
        if aname == name {
            return AttrRes::Found(&attrs[val_start..i]);
        }
        i += 1;
    }
}


/// tables_fix_scan：畸形输入一律 Bail（交给 DOM/recover 路径），
    /// 边界语义与 fix_one_table 的判定严格一致
    #[cfg(test)]
    mod tbl_scan_tests {
    use super::*;

    #[test]
    fn spot_malformed() {
        // unclosed table/cell -> bail (DOM recover path handles it)
        assert_eq!(
            tables_fix_scan("<w:tbl><w:tblGrid><w:gridCol w:w=\"100\"/></w:tblGrid><w:tr><w:tc></w:tc></w:tr>"),
            TblScan::Bail
        );
        // mismatched end tag -> bail
        assert_eq!(
            tables_fix_scan("<w:tbl><w:tr></w:tbl>"),
            TblScan::Bail
        );
        // unterminated tag -> bail
        assert_eq!(tables_fix_scan("<w:tbl><w:tr"), TblScan::Bail);
        // text that looks like a tag start -> bail
        assert_eq!(tables_fix_scan("<w:p>a < b</w:p>"), TblScan::Bail);
        // entity inside a parsed attr value -> bail
        assert_eq!(
            tables_fix_scan("<w:tbl><w:tblGrid><w:gridCol w:w=\"4&amp;0\"/></w:tblGrid></w:tbl>"),
            TblScan::Bail
        );
        // doctype -> bail
        assert_eq!(tables_fix_scan("<!DOCTYPE x><w:tbl></w:tbl>"), TblScan::Bail);
        // well-formed no-fix cases
        assert_eq!(
            tables_fix_scan("<w:document><w:body><w:tbl><w:tblGrid><w:gridCol w:w=\"4000\"/><w:gridCol w:w=\"4000\"/></w:tblGrid><w:tr><w:tc><w:p/></w:tc><w:tc><w:p/></w:tc></w:tr></w:tbl></w:body></w:document>"),
            TblScan::NoFix
        );
        // needs fix: more cells than gridCols (with nonzero widths)
        assert_eq!(
            tables_fix_scan("<w:tbl><w:tblGrid><w:gridCol w:w=\"4000\"/></w:tblGrid><w:tr><w:tc/><w:tc/></w:tr></w:tbl>"),
            TblScan::NeedsFix
        );
        // no fix when widths are all zero/unparseable (write-back condition fails)
        assert_eq!(
            tables_fix_scan("<w:tbl><w:tblGrid><w:gridCol w:w=\"0\"/></w:tblGrid><w:tr><w:tc/><w:tc/></w:tr></w:tbl>"),
            TblScan::NoFix
        );
        // needs fix: more gridCols than cells
        assert_eq!(
            tables_fix_scan("<w:tbl><w:tblGrid><w:gridCol w:w=\"1\"/><w:gridCol w:w=\"1\"/></w:tblGrid><w:tr><w:tc/></w:tr></w:tbl>"),
            TblScan::NeedsFix
        );
        // gridSpan makes the row fit the grid
        assert_eq!(
            tables_fix_scan("<w:tbl><w:tblGrid><w:gridCol w:w=\"1\"/><w:gridCol w:w=\"1\"/></w:tblGrid><w:tr><w:tc><w:tcPr><w:gridSpan w:val=\"2\"/></w:tcPr></w:tc></w:tr></w:tbl>"),
            TblScan::NoFix
        );
        // nested table rows count toward the outer table too
        assert_eq!(
            tables_fix_scan("<w:tbl><w:tblGrid><w:gridCol w:w=\"1\"/></w:tblGrid><w:tr><w:tc><w:tbl><w:tblGrid><w:gridCol w:w=\"1\"/></w:tblGrid><w:tr><w:tc/><w:tc/></w:tr></w:tbl></w:tc></w:tr></w:tbl>"),
            TblScan::NeedsFix
        );
    }
}

#[cfg(test)]
mod jinja_globals_tests {
    use super::*;

    fn render(src: &str) -> Result<String, minijinja::Error> {
        let core = TplCore::default();
        let env = make_env(false, &core);
        env.template_from_str(src).and_then(|t| t.render(()))
    }

    #[test]
    fn recover_xml_escapes_unqualified_tags_and_stray_ampersands() {
        let xml = r#"<?xml version="1.0"?><!--c--><w:p><w:t>a <mark>b</w:t><w:br/><w:t>c</mark> & <ltda> &amp;</w:t></w:p>"#;
        assert_eq!(super::recover_xml(xml),
            r#"<?xml version="1.0"?><!--c--><w:p><w:t>a &lt;mark>b</w:t><w:br/><w:t>c&lt;/mark> &amp; &lt;ltda> &amp;</w:t></w:p>"#);
    }

    #[test]
    fn cycler_cycles_and_current() {
        assert_eq!(
            render("{% set c = cycler('odd', 'even') %}{{ c.next() }} {{ c.next() }} {{ c.next() }}").unwrap(),
            "odd even odd"
        );
        // current is the item next() will return (jinja2.utils.Cycler;
        // expected values rendered by Jinja2 3.1)
        assert_eq!(
            render("{% set c = cycler(1, 2, 3) %}{{ c.current }}").unwrap(),
            "1"
        );
        assert_eq!(
            render("{% set c = cycler(1, 2, 3) %}{{ c.next() }}{{ c.current }}{{ c.next() }}{{ c.current }}").unwrap(),
            "1223"
        );
        // reset restarts the cycle
        assert_eq!(
            render("{% set c = cycler('a', 'b') %}{{ c.next() }}{{ c.next() }}{% set _ = c.reset() %}{{ c.next() }}").unwrap(),
            "aba"
        );
    }

    #[test]
    fn cycler_empty_next_errors() {
        let err = render("{% set c = cycler() %}{{ c.next() }}").unwrap_err();
        assert!(err.to_string().contains("no items"));
    }

    #[test]
    fn joiner_separates() {
        assert_eq!(
            render("{% set sep = joiner() %}{{ sep() }}a{{ sep() }}b{{ sep() }}c").unwrap(),
            "a, b, c"
        );
        assert_eq!(
            render("{% set sep = joiner('|') %}{{ sep() }}a{{ sep() }}b").unwrap(),
            "a|b"
        );
    }

    #[test]
    fn lipsum_paragraphs() {
        // html=true wraps each paragraph in <p> (autoescape off here)
        let html = render("{{ lipsum(2) }}").unwrap();
        let paras: Vec<&str> = html.split('\n').collect();
        assert_eq!(paras.len(), 2);
        for p in paras {
            assert!(p.starts_with("<p>") && p.ends_with(".</p>"), "{}", p);
        }
        // html=false: paragraphs separated by a blank line, plain sentences
        let text = render("{{ lipsum(n=2, html=false, min=3, max=5) }}").unwrap();
        let paras: Vec<&str> = text.split("\n\n").collect();
        assert_eq!(paras.len(), 2);
        for p in paras {
            assert!(!p.contains('<') && p.ends_with('.'), "{}", p);
            let words: Vec<&str> = p[..p.len() - 1].split(' ').collect();
            assert!((3..=5).contains(&words.len()), "{}", p);
            for w in words {
                assert!(LOREM_WORDS.contains(&w.to_lowercase().as_str()), "{}", w);
            }
        }
    }

    #[test]
    fn namespace_builtin_available() {
        // Pinned MiniJinja 2.24 and Jinja2 3.1.6 both format bool as True.
        // Keep this reference-verified expectation instead of legacy lowercase.
        assert_eq!(
            render("{% set ns = namespace(found=false) %}{% set ns.found = true %}{{ ns.found }}").unwrap(),
            "True"
        );
    }

    #[test]
    fn random_within_sequence() {
        // single-element sequence: the choice is deterministic
        assert_eq!(render("{{ [42]|random }}").unwrap(), "42");
        // choices always come from the sequence
        for _ in 0..16 {
            let v = render("{{ [1, 2, 3, 4, 5]|random }}").unwrap();
            assert!(["1", "2", "3", "4", "5"].contains(&v.as_str()), "{}", v);
        }
        // empty sequence errors (unchanged behavior)
        assert!(render("{{ []|random }}").is_err());
    }

    #[test]
    fn xmlattr_jinja2_semantics() {
        // autospace=true (default) prepends a single leading space
        assert_eq!(
            render("{{ {'class': 'x', 'id': 'y'}|xmlattr }}").unwrap(),
            " class=\"x\" id=\"y\""
        );
        assert_eq!(
            render("{{ {'class': 'x'}|xmlattr(false) }}").unwrap(),
            "class=\"x\""
        );
        // none values are skipped
        assert_eq!(
            render("{{ {'class': 'x', 'title': none}|xmlattr }}").unwrap(),
            " class=\"x\""
        );
        // keys that could break attribute syntax are skipped
        assert_eq!(
            render("{{ {'a/b': '1', 'a>b': '2', 'a=b': '3', 'ok': '4'}|xmlattr }}").unwrap(),
            " ok=\"4\""
        );
        // values are escaped
        assert_eq!(
            render("{{ {'title': '<a>&\"'}|xmlattr }}").unwrap(),
            " title=\"&lt;a&gt;&amp;&#34;\""
        );
        // empty map renders nothing (no stray space)
        assert_eq!(render("x{{ {}|xmlattr }}y").unwrap(), "xy");
    }
}

#[cfg(test)]
mod rust_loader_tests {
    use super::*;

    fn render_with(core: &mut TplCore, src: &str) -> Result<String, String> {
        let env = make_env(false, core);
        render_xml_str_with(src, Value::from(()), &env, core)
    }

    #[test]
    fn include_loads_through_rust_loader() {
        let mut core = TplCore::default();
        core.set_rust_template_loader(|name| match name {
            "partials/x" => Some("<w:t>from-partial {{ v }}</w:t>".to_string()),
            _ => None,
        });
        let env = make_env(false, &core);
        let out = render_xml_str_with(
            "<w:p>{% include \"partials/x\" %}</w:p>",
            minijinja::context! { v => "42" },
            &env,
            &mut core,
        )
        .unwrap();
        assert_eq!(out, "<w:p><w:t>from-partial 42</w:t></w:p>");
    }

    #[test]
    fn import_loads_macros_through_rust_loader() {
        let mut core = TplCore::default();
        core.set_rust_template_loader(|name| match name {
            "macros" => Some("{% macro hello(n) %}hi {{ n }}{% endmacro %}".to_string()),
            _ => None,
        });
        let out = render_with(&mut core, "{% import \"macros\" as m %}{{ m.hello('bob') }}").unwrap();
        assert_eq!(out, "hi bob");
    }

    #[test]
    fn extends_loads_base_through_rust_loader() {
        let mut core = TplCore::default();
        core.set_rust_template_loader(|name| match name {
            "base" => Some("[{% block body %}base{% endblock %}]".to_string()),
            _ => None,
        });
        let out = render_with(&mut core, "{% extends \"base\" %}{% block body %}child{% endblock %}").unwrap();
        assert_eq!(out, "[child]");
    }

    #[test]
    fn register_template_convenience_and_missing_name() {
        let mut core = TplCore::default();
        core.register_template("partials/a", "AAA");
        core.register_template("partials/b", "B{% include \"partials/a\" %}B");
        let out = render_with(&mut core, "{% include \"partials/b\" %}").unwrap();
        assert_eq!(out, "BAAAB");
        // unknown template name -> TemplateNotFound
        let err = render_with(&mut core, "{% include \"nope\" %}").unwrap_err();
        assert!(err.contains("nope"), "{}", err);
    }

    #[test]
    fn no_loader_include_errors() {
        let mut core = TplCore::default();
        assert!(render_with(&mut core, "{% include \"partials/x\" %}").is_err());
    }

    #[test]
    fn set_and_clear_bump_env_gen() {
        let mut core = TplCore::default();
        let gen0 = core.env_gen;
        core.set_rust_template_loader(|_| None);
        assert_eq!(core.env_gen, gen0 + 1);
        core.clear_rust_template_loader();
        assert_eq!(core.env_gen, gen0 + 2);
        // clearing when nothing is set is a no-op
        core.clear_rust_template_loader();
        assert_eq!(core.env_gen, gen0 + 2);
    }
}

/// docxcompose `Composer.renumber_bookmarks` on `w:bookmarkStart` and
/// `w:bookmarkEnd` elements: each kind is numbered 0, 1, ... in document order.
fn renumber_body_bookmarks(xml: &str) -> String {
    use std::sync::OnceLock;
    static ELEMENT: OnceLock<fancy_regex::Regex> = OnceLock::new();
    static ID: OnceLock<fancy_regex::Regex> = OnceLock::new();
    let element = ELEMENT.get_or_init(|| fancy_regex::Regex::new(r"<w:bookmark(Start|End)\b[^>]*>").expect("bookmark pattern"));
    let id = ID.get_or_init(|| fancy_regex::Regex::new(r#"(\sw:id=)("[^"]*"|'[^']*')"#).expect("bookmark id pattern"));
    let (mut starts, mut ends) = (0usize, 0usize);
    element.replace_all(xml, |found: &fancy_regex::Captures<'_>| {
        let counter = if &found[1] == "Start" { &mut starts } else { &mut ends };
        let number = *counter;
        *counter += 1;
        id.replace(&found[0], |attr: &fancy_regex::Captures<'_>| format!("{}\"{number}\"", &attr[1])).into_owned()
    }).into_owned()
}

#[cfg(test)]
mod bookmark_renumbering_tests {
    #[test]
    fn numbers_starts_and_ends_separately_in_document_order() {
        let xml = r#"<w:p><w:bookmarkStart w:id="2000000" w:name="a"/><w:bookmarkEnd w:id="2000000"/><w:bookmarkStart w:name="b" w:id="7"/><w:bookmarkEnd w:id="7"/></w:p>"#;
        assert_eq!(super::renumber_body_bookmarks(xml),
            r#"<w:p><w:bookmarkStart w:id="0" w:name="a"/><w:bookmarkEnd w:id="0"/><w:bookmarkStart w:name="b" w:id="1"/><w:bookmarkEnd w:id="1"/></w:p>"#);
    }
}
