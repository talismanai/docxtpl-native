//! Render-local provenance for values assembled from text and deferred data.
use minijinja::{AutoEscape, Error, ErrorKind, Value};
use std::cell::RefCell;
use std::sync::Mutex;

struct ImageRenderScope {
    core: RefCell<docxtplrs::template::TplCore>,
    part: String,
    owner: Option<usize>,
}

scoped_tls::scoped_thread_local!(static IMAGE_RENDER: ImageRenderScope);

/// Move the package into a same-thread scope while the VM evaluates one part.
/// Restoring it on errors and unwinds preserves already-observable mutations.
pub(crate) fn with_image_render_scope(core: &mut docxtplrs::template::TplCore, part: &str,
    render: &dyn Fn(&mut docxtplrs::template::TplCore) -> Result<String, String>) -> Result<String, String> {
    with_owned_image_render_scope(core, part, render, None)
}

pub(crate) fn with_owned_image_render_scope(core: &mut docxtplrs::template::TplCore, part: &str,
    render: &dyn Fn(&mut docxtplrs::template::TplCore) -> Result<String, String>, owner: Option<usize>) -> Result<String, String> {
    let mut staged = docxtplrs::template::TplCore::default();
    staged.package = core.package.take();
    staged.deferred = core.deferred.clone();
    staged.deferred_by_oid = core.deferred_by_oid.clone();
    staged.early_subdoc_cache = std::mem::take(&mut core.early_subdoc_cache);
    staged.attached_subdocuments = std::mem::take(&mut core.attached_subdocuments);
    staged.next_shape_ids = std::mem::take(&mut core.next_shape_ids);
    staged.python_image_contract = core.python_image_contract;
    staged.python_source_contract = core.python_source_contract;
    staged.python_subdoc_numbering_contract = core.python_subdoc_numbering_contract;
    staged.header_footer_normalizer = core.header_footer_normalizer;
    staged.numbering_integer = core.numbering_integer.clone();
    staged.used_subdoc = core.used_subdoc;
    staged.bookmark_next_id = core.bookmark_next_id.take();
    staged.preserved_styles = std::mem::take(&mut core.preserved_styles);
    staged.doc_cache = core.doc_cache.take();
    staged.doc_dirty = std::mem::take(&mut core.doc_dirty);
    staged.part_caches = std::mem::take(&mut core.part_caches);
    staged.parts_dirty = std::mem::take(&mut core.parts_dirty);
    staged.style_resolve_cache = std::mem::take(&mut core.style_resolve_cache);
    staged.doc_gen = core.doc_gen;
    staged.para_cursor = core.para_cursor;
    staged.tbl_cursor = core.tbl_cursor;
    staged.row_cursor = core.row_cursor;
    let scope = ImageRenderScope { core: RefCell::new(staged), part: part.to_owned(), owner };
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| IMAGE_RENDER.set(&scope, || render(core))));
    let staged = scope.core.into_inner();
    core.package = staged.package;
    core.deferred = staged.deferred;
    core.early_subdoc_cache = staged.early_subdoc_cache;
    core.attached_subdocuments = staged.attached_subdocuments;
    core.next_shape_ids = staged.next_shape_ids;
    core.used_subdoc = staged.used_subdoc;
    core.bookmark_next_id = staged.bookmark_next_id;
    core.preserved_styles = staged.preserved_styles;
    core.doc_cache = staged.doc_cache;
    core.doc_dirty = staged.doc_dirty;
    core.part_caches = staged.part_caches;
    core.parts_dirty = staged.parts_dirty;
    core.style_resolve_cache = staged.style_resolve_cache;
    core.doc_gen = staged.doc_gen;
    core.para_cursor = staged.para_cursor;
    core.tbl_cursor = staged.tbl_cursor;
    core.row_cursor = staged.row_cursor;
    match result { Ok(result) => result, Err(panic) => std::panic::resume_unwind(panic) }
}

/// Materialize a registered image in the active part without exposing a
/// mutable package pointer to the language binding.
pub(crate) fn is_registered_image(identity: usize, owner: usize) -> bool {
    if !IMAGE_RENDER.is_set() { return false; }
    IMAGE_RENDER.with(|scope| scope.owner == Some(owner) && scope.core.try_borrow().ok().is_some_and(|core| {
        core.deferred_by_oid.get(&identity).is_some_and(|&index|
            matches!(core.deferred.get(index), Some(docxtplrs::template::Deferred::Image { .. })))
    }))
}

/// The active owner can materialize images created while evaluating a part.
pub(crate) fn is_image_owner(owner: usize) -> bool {
    IMAGE_RENDER.is_set() && IMAGE_RENDER.with(|scope| scope.owner == Some(owner))
}

pub(crate) fn has_owned_image_scope() -> bool {
    IMAGE_RENDER.is_set() && IMAGE_RENDER.with(|scope| scope.owner.is_some())
}

pub(crate) fn has_unowned_image_scope() -> bool {
    IMAGE_RENDER.is_set() && IMAGE_RENDER.with(|scope| scope.owner.is_none())
}

pub(crate) fn is_current_registered_image(identity: usize) -> bool {
    IMAGE_RENDER.is_set() && IMAGE_RENDER.with(|scope| scope.core.try_borrow().ok().is_some_and(|core| {
        core.deferred_by_oid.get(&identity).is_some_and(|&index|
            matches!(core.deferred.get(index), Some(docxtplrs::template::Deferred::Image { .. })))
    }))
}

/// Materialize an explicit registered index in the currently rendered part.
pub(crate) fn materialize_current_deferred(index: usize) -> Option<Result<String, String>> {
    if !IMAGE_RENDER.is_set() { return None; }
    IMAGE_RENDER.with(|scope| {
        let mut core = match scope.core.try_borrow_mut() {
            Ok(core) => core,
            Err(_) => return Some(Err("reentrant deferred materialization".to_owned())),
        };
        let is_subdoc = matches!(core.deferred.get(index),
            Some(docxtplrs::template::Deferred::Subdoc { .. } | docxtplrs::template::Deferred::SubdocBlocks { .. }));
        let cache_key = (index, scope.part.clone());
        if is_subdoc {
            if let Some(xml) = core.early_subdoc_cache.get(&cache_key) { return Some(Ok(xml.clone())); }
        }
        let result = core.materialize_one(&scope.part, index);
        if is_subdoc {
            if let Ok(xml) = &result { core.early_subdoc_cache.insert(cache_key, xml.clone()); }
        }
        Some(result)
    })
}

pub(crate) fn materialize_current_registered_image(identity: usize) -> Option<Result<String, String>> {
    if !IMAGE_RENDER.is_set() { return None; }
    IMAGE_RENDER.with(|scope| {
        let mut core = match scope.core.try_borrow_mut() {
            Ok(core) => core,
            Err(_) => return Some(Err("reentrant image materialization".to_owned())),
        };
        let index = core.deferred_by_oid.get(&identity).copied()?;
        if !matches!(core.deferred.get(index), Some(docxtplrs::template::Deferred::Image { .. })) {
            return None;
        }
        Some(core.materialize_one(&scope.part, index))
    })
}

pub(crate) fn materialize_registered_image(identity: usize, image: docxtplrs::template::Deferred) -> Option<Result<String, String>> {
    if !IMAGE_RENDER.is_set() { return None; }
    IMAGE_RENDER.with(|scope| {
        let mut core = match scope.core.try_borrow_mut() {
            Ok(core) => core,
            Err(_) => return Some(Err("reentrant image materialization".to_owned())),
        };
        let existing = core.deferred_by_oid.get(&identity).copied().filter(|&index|
            matches!(core.deferred.get(index), Some(docxtplrs::template::Deferred::Image { .. })));
        let index = match existing {
            Some(index) => index,
            None if scope.owner.is_some() => {
                let index = core.deferred.len();
                core.deferred.push(image.clone());
                index
            }
            None => return None,
        };
        core.deferred[index] = image;
        Some(core.materialize_one(&scope.part, index))
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Fragment {
    Text(String),
    Deferred(usize),
}

#[derive(Default)]
pub(crate) struct Arena {
    values: Mutex<Vec<Vec<Fragment>>>,
}

impl Arena {
    /// Resolve image-only pieces while the part is being evaluated. Other
    /// deferred kinds continue through their existing path until supported.
    pub(crate) fn resolve_images(&self, value: &Value) -> Result<Option<Value>, Error> {
        let Some(id) = value.string_provenance() else { return Ok(None) };
        if !IMAGE_RENDER.is_set() { return Ok(None); }
        let fragments = self.fragments(id)?;
        IMAGE_RENDER.with(|scope| {
            let mut core = scope.core.try_borrow_mut().map_err(|_| Error::new(
                ErrorKind::InvalidOperation, "reentrant image materialization"))?;
            if fragments.iter().any(|fragment| match fragment {
                Fragment::Deferred(index) => !matches!(core.deferred.get(*index), Some(docxtplrs::template::Deferred::Image { .. })),
                Fragment::Text(_) => false,
            }) { return Ok(None); }
            let mut output = String::new();
            for fragment in fragments {
                match fragment {
                    Fragment::Text(text) => output.push_str(&text),
                    Fragment::Deferred(index) => output.push_str(&core.materialize_one(&scope.part, index)
                        .map_err(|error| Error::new(ErrorKind::InvalidOperation, error))?),
                }
            }
            Ok(Some(if value.is_safe() { Value::from_safe_string(output) } else { Value::from(output) }))
        })
    }

    fn insert(&self, fragments: Vec<Fragment>) -> Result<u32, Error> {
        let mut values = self.values.lock().map_err(|_| Error::new(
            ErrorKind::InvalidOperation, "provenance arena is poisoned"))?;
        let id = u32::try_from(values.len()).map_err(|_| Error::new(
            ErrorKind::InvalidOperation, "provenance arena exceeds u32"))?;
        values.push(fragments);
        Ok(id)
    }

    pub(crate) fn deferred(&self, index: usize) -> Result<u32, Error> {
        self.insert(vec![Fragment::Deferred(index)])
    }

    pub(crate) fn fragments(&self, id: u32) -> Result<Vec<Fragment>, Error> {
        self.values.lock().map_err(|_| Error::new(
            ErrorKind::InvalidOperation, "provenance arena is poisoned"))?
            .get(id as usize).cloned().ok_or_else(|| Error::new(
                ErrorKind::InvalidOperation, "unknown provenance identifier"))
    }

    /// Apply MiniJinja's string-concatenation result type while retaining the
    /// origin of each input. No marker strings are parsed here.
    pub(crate) fn concatenate(&self, values: &[Value]) -> Result<Value, Error> {
        if values.is_empty() { return Ok(Value::default()); }
        if values.len() == 1 { return Ok(values[0].clone()); }
        self.combine(values, false)
    }

    pub(crate) fn capture(&self, values: Vec<Value>, autoescape: AutoEscape) -> Result<Value, Error> {
        self.combine(&values, !matches!(autoescape, AutoEscape::None))
    }

    fn combine(&self, values: &[Value], safe: bool) -> Result<Value, Error> {
        let mut text = String::new();
        let mut fragments = Vec::new();
        let mut has_provenance = false;
        for value in values {
            let rendered = value.to_string();
            text.push_str(&rendered);
            match value.string_provenance() {
                Some(id) => {
                    has_provenance = true;
                    fragments.extend(self.fragments(id)?);
                }
                None => fragments.push(Fragment::Text(rendered)),
            }
        }
        if !has_provenance {
            return Ok(if safe { Value::from_safe_string(text) } else { Value::from(text) });
        }
        let id = self.insert(fragments)?;
        Ok(if safe { Value::from_safe_string_with_provenance(text, id) }
            else { Value::from_string_with_provenance(text, id) })
    }

    pub(crate) fn escape_html(&self, id: u32) -> Result<Value, Error> {
        let mut text = String::new();
        let mut escaped = Vec::new();
        for fragment in self.fragments(id)? {
            match fragment {
                Fragment::Text(value) => {
                    let value = minijinja::HtmlEscape(&value).to_string();
                    text.push_str(&value);
                    escaped.push(Fragment::Text(value));
                }
                Fragment::Deferred(index) => {
                    text.push_str(&docxtplrs::template::deferred_token(index));
                    escaped.push(Fragment::Deferred(index));
                }
            }
        }
        let id = self.insert(escaped)?;
        Ok(Value::from_safe_string_with_provenance(text, id))
    }

    /// Join the typed runtime's values without flattening deferred entries.
    pub(crate) fn join(&self, state: &minijinja::State<'_, '_>, value: &Value,
        separator: Option<&Value>) -> Result<Value, Error> {
        use minijinja::value::StringInput;
        // InlineImage has __html__, but soft_str(image) is an ordinary str;
        // converting the separator happens even if no separator is emitted.
        let resolved_separator = match separator {
            Some(value) => self.resolve_images(value)?,
            None => None,
        };
        let image_separator = resolved_separator.is_some();
        let plain_separator = resolved_separator.as_ref().map(|value| Value::from(value.to_string()));
        let separator = plain_separator.as_ref().or(separator);
        let separator_input = separator.map(|value| StringInput::new(state, value)).transpose()?;
        let iter = value.try_iter().map_err(|error| Error::new(
            ErrorKind::InvalidOperation, format!("cannot join value of type {}", value.kind()))
            .with_source(error))?;
        // As in the original filter, only inspect the whole iterable first
        // when autoescape and an unsafe separator require a safety decision.
        let escaped: bool;
        let items: Box<dyn Iterator<Item = Value> + '_>;
        if !image_separator && !matches!(state.auto_escape(), AutoEscape::None)
            && !separator_input.as_ref().is_some_and(StringInput::is_safe) {
            let collected: Vec<Value> = iter.collect();
            escaped = collected.iter().any(Value::is_safe);
            items = Box::new(collected.into_iter());
        } else {
            escaped = !image_separator && !matches!(state.auto_escape(), AutoEscape::None);
            items = Box::new(iter);
        }
        let separator_text = separator_input.as_ref().map(StringInput::as_str).unwrap_or_default();
        let separator_value = match separator {
            Some(value) if value.string_provenance().is_some() && escaped && !value.is_safe() =>
                self.escape_html(value.string_provenance().unwrap())?,
            Some(value) if value.string_provenance().is_some() => value.clone(),
            _ if escaped => Value::from_safe_string(match separator_input.as_ref() {
                Some(separator) => separator.format(state)?.into_owned(),
                None => String::new(),
            }),
            _ => Value::from(separator_text),
        };
        let mut fragments = Vec::new();
        for (index, item) in items.enumerate() {
            if index > 0 { fragments.push(separator_value.clone()); }
            let item = self.resolve_images(&item)?.unwrap_or(item);
            if escaped && !item.is_safe() {
                fragments.push(match item.string_provenance() {
                    Some(id) => self.escape_html(id)?,
                    None => Value::from_safe_string(state.format(item)?),
                });
            } else if item.string_provenance().is_some() {
                fragments.push(item);
            } else {
                // Match join's conversion timing while iterating, including
                // values supplied by a lazy template iterable.
                fragments.push(Value::from(item.to_string()));
            }
        }
        self.capture(fragments, if escaped { AutoEscape::Html } else { AutoEscape::None })
    }
}

#[cfg(test)]
mod tests {
    use super::{has_owned_image_scope, has_unowned_image_scope, is_image_owner, materialize_current_deferred, materialize_registered_image, with_image_render_scope,
        with_owned_image_render_scope, IMAGE_RENDER};
    use docxtplrs::package::{minimal_docx, Package};
    use docxtplrs::template::TplCore;

    #[test]
    fn image_scope_ownership_tracks_nested_render_and_restores_outer_scope() {
        assert!(!has_owned_image_scope());
        assert!(!has_unowned_image_scope());
        let mut outer = TplCore::default();
        with_image_render_scope(&mut outer, "outer", &|_| {
            assert!(has_unowned_image_scope());
            assert!(!has_owned_image_scope());
            let mut inner = TplCore::default();
            with_owned_image_render_scope(&mut inner, "inner", &|_| {
                assert!(has_owned_image_scope());
                assert!(!has_unowned_image_scope());
                Ok(String::new())
            }, Some(7))?;
            assert!(has_unowned_image_scope());
            assert!(!has_owned_image_scope());
            Ok(String::new())
        }).unwrap();
        assert!(!has_owned_image_scope());
        assert!(!has_unowned_image_scope());
    }

    #[test]
    fn temporary_image_does_not_leave_an_identity_registration() {
        let mut core = TplCore::default();
        core.package = Some(Package::from_bytes(&minimal_docx("<w:p/>")).unwrap());
        with_owned_image_render_scope(&mut core, "word/document.xml", &|_| {
            assert!(is_image_owner(7));
            assert!(!is_image_owner(8));
            let image = docxtplrs::template::Deferred::Image {
                blob: Vec::<u8>::new().into(), info: None, filename: None, width: None,
                height: None, anchor: None, title: None, descr: None,
            };
            assert!(materialize_registered_image(42, image).is_some());
            IMAGE_RENDER.with(|scope| assert!(!scope.core.borrow().deferred_by_oid.contains_key(&42)));
            Ok(String::new())
        }, Some(7)).unwrap();
        assert!(!core.deferred_by_oid.contains_key(&42));
        assert!(!is_image_owner(7));
    }

    #[test]
    fn temporary_image_does_not_replace_an_unrelated_deferred_value() {
        let mut core = TplCore::default();
        core.package = Some(Package::from_bytes(&minimal_docx("<w:p/>")).unwrap());
        core.deferred.push(docxtplrs::template::Deferred::Subdoc { bytes: None, keep_sections: false });
        core.deferred_by_oid.insert(42, 0);
        with_owned_image_render_scope(&mut core, "word/document.xml", &|_| {
            let image = docxtplrs::template::Deferred::Image {
                blob: Vec::<u8>::new().into(), info: None, filename: None, width: None,
                height: None, anchor: None, title: None, descr: None,
            };
            assert!(materialize_registered_image(42, image).is_some());
            Ok(String::new())
        }, Some(7)).unwrap();
        assert!(matches!(core.deferred[0], docxtplrs::template::Deferred::Subdoc { .. }));
        assert_eq!(core.deferred_by_oid.get(&42), Some(&0));
    }

    #[test]
    fn scoped_composition_retains_contract_and_restores_subdoc_state() {
        for unwind in [false, true] {
            let mut core = TplCore::default();
            core.python_source_contract = true;
            core.python_subdoc_numbering_contract = true;
            core.header_footer_normalizer = Some(|xml| Ok(xml.to_owned()));
            core.numbering_integer = Some(docxtplrs::template::NumberingInteger(std::sync::Arc::new(|_| Ok(7))));
            core.bookmark_next_id = Some(12);
            core.preserved_styles.insert("old".into(), vec!["old_copy".into()]);
            core.doc_cache = Some(docxtplrs::xmldom::Document::parse("<document/>").unwrap());
            core.doc_dirty = true;
            core.part_caches.insert("word/styles.xml".into(), docxtplrs::xmldom::Document::parse("<styles/>").unwrap());
            core.parts_dirty.insert("word/styles.xml".into());
            core.style_resolve_cache.insert("existing".into(), "style".into());
            let render = |_core: &mut TplCore| -> Result<String, String> {
                IMAGE_RENDER.with(|scope| {
                    let mut staged = scope.core.borrow_mut();
                    assert!(staged.python_source_contract);
                    assert!(staged.python_subdoc_numbering_contract);
                    assert!(staged.header_footer_normalizer.is_some());
                    assert!(staged.numbering_integer.is_some());
                    assert_eq!(staged.bookmark_next_id, Some(12));
                    assert_eq!(staged.preserved_styles.get("old").unwrap(), &["old_copy"]);
                    assert!(staged.doc_cache.is_some() && staged.doc_dirty);
                    assert!(staged.part_caches.contains_key("word/styles.xml"));
                    assert!(staged.parts_dirty.contains("word/styles.xml"));
                    assert_eq!(staged.style_resolve_cache.get("existing").unwrap(), "style");
                    staged.doc_dirty = false;
                    staged.part_caches.remove("word/styles.xml");
                    staged.parts_dirty.clear();
                    staged.style_resolve_cache.clear();
                    staged.bookmark_next_id = Some(19);
                    staged.preserved_styles.insert("new".into(), vec!["new_copy".into()]);
                    staged.used_subdoc = true;
                });
                if unwind { panic!("scope test") }
                Err("render failed".to_owned())
            };
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(||
                with_image_render_scope(&mut core, "word/document.xml", &render)));
            if unwind { assert!(result.is_err()); }
            else { assert_eq!(result.unwrap().unwrap_err(), "render failed"); }
            assert_eq!(core.bookmark_next_id, Some(19));
            assert_eq!(core.preserved_styles.get("new").unwrap(), &["new_copy"]);
            assert!(core.used_subdoc);
            assert!(core.doc_cache.is_some() && !core.doc_dirty);
            assert!(!core.part_caches.contains_key("word/styles.xml"));
            assert!(core.parts_dirty.is_empty() && core.style_resolve_cache.is_empty());
        }
    }

    #[test]
    fn scoped_deferred_subdocument_cache_is_part_specific() {
        let mut core = TplCore::default();
        core.package = Some(Package::from_bytes(&minimal_docx("<w:p/>")).unwrap());
        core.deferred.push(docxtplrs::template::Deferred::Subdoc {
            bytes: Some(minimal_docx("<w:p><w:r><w:t>child</w:t></w:r></w:p>").into()),
            keep_sections: false,
        });
        for part in ["word/document.xml", "word/header1.xml"] {
            with_image_render_scope(&mut core, part, &|_| {
                let first = materialize_current_deferred(0).unwrap()?;
                let second = materialize_current_deferred(0).unwrap()?;
                assert_eq!(first, second);
                Ok(String::new())
            }).unwrap();
        }
        assert!(core.early_subdoc_cache.contains_key(&(0, "word/document.xml".into())));
        assert!(core.early_subdoc_cache.contains_key(&(0, "word/header1.xml".into())));
        assert_eq!(core.early_subdoc_cache.len(), 2);
    }

    #[test]
    fn early_subdocument_cache_is_cleared_for_a_new_render() {
        let source = minimal_docx("<w:p/>");
        let mut core = TplCore::default();
        core.original_bytes = source.into();
        core.early_subdoc_cache.insert((0, "word/document.xml".into()), "old".into());
        core.render_init().unwrap();
        assert!(core.early_subdoc_cache.is_empty());
    }

    #[test]
    fn attached_subdocument_survives_scope_and_is_cleared_for_next_render() {
        let mut core = TplCore::default();
        core.original_bytes = minimal_docx("<w:p/>").into();
        core.package = Some(Package::from_bytes(&core.original_bytes).unwrap());
        core.deferred.push(docxtplrs::template::Deferred::Subdoc { bytes: None, keep_sections: false });
        core.attached_subdocuments.insert(0, "attached".into());
        for part in ["word/document.xml", "word/header1.xml"] {
            let result = with_image_render_scope(&mut core, part, &|_| {
                assert_eq!(materialize_current_deferred(0).unwrap()?, "attached");
                Err("render failed".into())
            });
            assert_eq!(result.unwrap_err(), "render failed");
            assert_eq!(core.attached_subdocuments.get(&0).unwrap(), "attached");
        }
        core.render_init().unwrap();
        assert!(core.attached_subdocuments.is_empty());
    }

    #[test]
    fn scoped_subdocument_materialization_matches_direct_package() {
        fn new_core() -> TplCore {
            let mut core = TplCore::default();
            core.package = Some(Package::from_bytes(&minimal_docx("<w:p/>")).unwrap());
            core.python_source_contract = true;
            core.python_subdoc_numbering_contract = true;
            core.header_footer_normalizer = Some(|xml| Ok(xml.to_owned()));
            core.deferred.push(docxtplrs::template::Deferred::Subdoc {
                bytes: Some(minimal_docx("<w:p><w:r><w:t>child</w:t></w:r></w:p>").into()),
                keep_sections: false,
            });
            core
        }
        let mut direct = new_core();
        let expected_xml = direct.materialize_one("word/document.xml", 0).unwrap();
        let expected_package = direct.package.as_ref().unwrap().to_bytes().unwrap();
        let mut scoped = new_core();
        let actual_xml = with_image_render_scope(&mut scoped, "word/document.xml", &|_| {
            IMAGE_RENDER.with(|scope| scope.core.borrow_mut().materialize_one("word/document.xml", 0))
        }).unwrap();
        assert_eq!(actual_xml, expected_xml);
        assert!(scoped.used_subdoc);
        assert_eq!(scoped.bookmark_next_id, direct.bookmark_next_id);
        assert_eq!(scoped.preserved_styles, direct.preserved_styles);
        assert_eq!(scoped.package.as_ref().unwrap().to_bytes().unwrap(), expected_package);
    }

    #[test]
    fn image_scope_restores_mutations_after_error_and_unwind() {
        for unwind in [false, true] {
            let mut core = TplCore::default();
            core.package = Some(Package::from_bytes(&minimal_docx("<w:p/> ")).unwrap());
            let render = |_core: &mut TplCore| -> Result<String, String> {
                IMAGE_RENDER.with(|scope| {
                    scope.core.borrow_mut().package.as_mut().unwrap().set("custom.bin", b"changed".to_vec());
                });
                if unwind { panic!("scope test") }
                Err("render failed".to_owned())
            };
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(||
                with_image_render_scope(&mut core, "word/document.xml", &render)));
            if unwind { assert!(result.is_err()); }
            else { assert_eq!(result.unwrap().unwrap_err(), "render failed"); }
            assert_eq!(core.package.as_ref().unwrap().get("custom.bin"), Some(&b"changed"[..]));
            assert!(!IMAGE_RENDER.is_set());
        }
    }

    #[test]
    fn nested_image_scopes_restore_the_outer_scope() {
        let mut outer = TplCore::default();
        outer.package = Some(Package::from_bytes(&minimal_docx("<w:p/>")).unwrap());
        with_image_render_scope(&mut outer, "outer", &|_| {
            assert_eq!(IMAGE_RENDER.with(|scope| scope.part.clone()), "outer");
            let mut inner = TplCore::default();
            inner.package = Some(Package::from_bytes(&minimal_docx("<w:p/>")).unwrap());
            with_image_render_scope(&mut inner, "inner", &|_| {
                assert_eq!(IMAGE_RENDER.with(|scope| scope.part.clone()), "inner");
                IMAGE_RENDER.with(|scope| scope.core.borrow_mut().package.as_mut().unwrap()
                    .set("inner.bin", b"inner".to_vec()));
                Ok(String::new())
            })?;
            assert!(inner.package.as_ref().unwrap().contains("inner.bin"));
            assert_eq!(IMAGE_RENDER.with(|scope| scope.part.clone()), "outer");
            IMAGE_RENDER.with(|scope| scope.core.borrow_mut().package.as_mut().unwrap()
                .set("outer.bin", b"outer".to_vec()));
            Ok(String::new())
        }).unwrap();
        let package = outer.package.unwrap();
        assert!(package.contains("outer.bin"));
        assert!(!package.contains("inner.bin"));
        assert!(!IMAGE_RENDER.is_set());
    }
}
