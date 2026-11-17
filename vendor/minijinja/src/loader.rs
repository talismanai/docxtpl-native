use std::borrow::Cow;
use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::io;
use std::path::Path;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use crate::vendor::self_cell::self_cell;
use memo_map::MemoMap;

use crate::compiler::instructions::Instructions;
use crate::error::{Error, ErrorKind};
use crate::template::{CompiledTemplate, CompiledTemplateRef};
use crate::template::TemplateConfig;

type LoadFunc = dyn for<'a> Fn(&'a str) -> Result<Option<String>, Error> + Send + Sync;
type SharedLoadedSlot = Arc<Mutex<Option<Arc<LoadedTemplate>>>>;

/// Internal utility for dynamic template loading.
///
/// Because an [`Environment`](crate::Environment) holds a reference to the
/// source lifetime it borrows templates from, it becomes very inconvenient when
/// it is shared. This object provides a solution for such cases. First templates
/// are loaded into the source to decouple the lifetimes from the environment.
#[derive(Clone)]
pub(crate) struct LoaderStore<'source> {
    pub template_config: TemplateConfig,
    loader: Option<Arc<LoadFunc>>,
    owned_templates: MemoMap<Arc<str>, Arc<LoadedTemplate>>,
    shared_loaded: Option<Arc<MemoMap<Arc<str>, SharedLoadedSlot>>>,
    borrowed_templates: BTreeMap<&'source str, Arc<CompiledTemplate<'source>>>,
    // Decides whether a cached loaded template may be reused.
    loaded_validator: Option<Arc<LoadedValidator>>,
    // Modules of imports without context, per loaded template.
    shared_modules: Arc<Mutex<Vec<CachedModule>>>,
}

type LoadedValidator = dyn Fn(&str) -> Result<bool, Error> + Send + Sync;
// A loaded template, the module an import without context made from it, and
// the id of the state that made it.
type CachedModule = (std::sync::Weak<LoadedTemplate>, crate::value::Value, isize);

impl fmt::Debug for LoaderStore<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut l = f.debug_list();
        for key in self.owned_templates.keys() {
            l.entry(key);
        }
        for key in self.borrowed_templates.keys() {
            if !self.owned_templates.contains_key(*key) {
                l.entry(key);
            }
        }
        l.finish()
    }
}

self_cell! {
    pub(crate) struct LoadedTemplate {
        owner: (Arc<str>, Box<str>, Option<Box<dyn crate::template::TemplateAst>>),
        #[covariant]
        dependent: CompiledTemplate,
    }
}

self_cell! {
    pub(crate) struct OwnedInstructions {
        owner: Box<str>,
        #[covariant]
        dependent: Instructions,
    }
}

impl fmt::Debug for LoadedTemplate {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Debug::fmt(&self.borrow_dependent(), f)
    }
}

impl LoadedTemplate {
    pub(crate) fn compile(name: Arc<str>, source: String, config: &TemplateConfig, loaded: bool) -> Result<Arc<Self>, Error> {
        crate::template::with_compilation_origin(loaded, || Self::compile_inner(name, source, config))
    }

    fn compile_inner(name: Arc<str>, source: String, config: &TemplateConfig) -> Result<Arc<Self>, Error> {
        let key = config.compilation_key.as_ref().map(|make| make(&name));
        let callback_name = key.as_deref().unwrap_or(&name);
        let result = (|| {
            if let Some(validate) = &config.source_validator { validate(callback_name, &source)?; }
            let source = if config.normalize_newlines { crate::template::normalize_newlines(source) } else { source };
            let metadata = config.template_metadata.as_ref().map(|capture| capture(callback_name)).transpose()?;
            let tree = match &config.ast_adapter {
                Some(adapt) => adapt(callback_name, &source)?,
                None => None,
            };
            Self::try_new((name.clone(), source.into_boxed_str(), tree), |(name, source, tree)| {
                crate::error::attach_basic_debug_info(
                    CompiledTemplate::from_prepared(name, source, config, tree.as_deref(), metadata, key.clone()), source)
            }).map(Arc::new)
        })();
        let cleanup = config.compilation_cleanup.as_ref().map(|finish| finish(callback_name)).transpose();
        if result.is_ok() { cleanup?; }
        result
    }
}

impl<'source> LoaderStore<'source> {
    pub(crate) fn get_handle(&self, name: &str) -> Result<CompiledTemplateRef<'_, '_>, Error> {
        if self.shared_loaded.is_none() || self.borrowed_templates.contains_key(name) {
            return self.get(name).map(CompiledTemplateRef::Borrowed);
        }
        if let Some(template) = self.owned_templates.get(name) {
            return Ok(CompiledTemplateRef::Adapted(template.clone()));
        }
        self.get_shared(name.into()).map(CompiledTemplateRef::Adapted)
    }

    fn get_shared(&self, name: Arc<str>) -> Result<Arc<LoadedTemplate>, Error> {
        let shared = self.shared_loaded.as_ref().expect("shared cache enabled");
        if let Some(compiled) = shared.get(&name).and_then(|slot| slot.lock().unwrap().clone()) {
            let valid = match &self.loaded_validator { Some(validate) => validate(&name)?, None => true };
            if valid { return Ok(compiled); }
        }
        // A miss owns its compilation; compilation and destructor callbacks run
        // outside the cache lock. Later completed compilations replace the slot.
        let source = match self.loader {
            Some(ref loader) => loader(&name)?,
            None => None,
        }.ok_or_else(|| Error::new_not_found(&name))?;
        let compiled = self.make_owned_template(name.clone(), source)?;
        let slot = shared.get_or_insert_owned(name, || Arc::new(Mutex::new(None)));
        let previous = slot.lock().unwrap().replace(compiled.clone());
        drop(previous);
        Ok(compiled)
    }

    pub fn new(template_config: TemplateConfig) -> LoaderStore<'source> {
        LoaderStore {
            template_config,
            loader: None,
            owned_templates: MemoMap::default(),
            shared_loaded: None,
            borrowed_templates: BTreeMap::default(),
            loaded_validator: None,
            shared_modules: Arc::default(),
        }
    }

    pub fn insert(&mut self, name: &'source str, source: &'source str) -> Result<(), Error> {
        self.insert_cow(Cow::Borrowed(name), Cow::Borrowed(source))
    }

    pub fn insert_cow(
        &mut self,
        name: Cow<'source, str>,
        source: Cow<'source, str>,
    ) -> Result<(), Error> {
        // Adapted trees own names and literal data absent from the source.
        // Keep their storage next to the source for the instruction lifetime.
        let source = if self.template_config.ast_adapter.is_some()
            || self.template_config.rewrites_source(&source) {
            Cow::Owned(source.into_owned())
        } else { source };
        match (source, name) {
            (Cow::Borrowed(source), Cow::Borrowed(name)) => {
                self.owned_templates.remove(name);
                self.borrowed_templates.insert(
                    name,
                    Arc::new(ok!(CompiledTemplate::new(
                        name,
                        source,
                        &self.template_config
                    ))),
                );
            }
            (source, name) => {
                self.borrowed_templates.remove(&name as &str);
                let name: Arc<str> = name.into();
                self.owned_templates.replace(
                    name.clone(),
                    ok!(self.make_owned_template(name, source.to_string())),
                );
            }
        }

        Ok(())
    }

    pub fn remove(&mut self, name: &str) {
        self.borrowed_templates.remove(name);
        self.owned_templates.remove(name);
        if let Some(shared) = &mut self.shared_loaded {
            Arc::make_mut(shared).remove(name);
        }
    }

    pub fn clear(&mut self) {
        self.borrowed_templates.clear();
        self.owned_templates.clear();
        if let Some(shared) = &mut self.shared_loaded {
            *shared = Arc::new(MemoMap::default());
        }
    }

    pub fn get(&self, name: &str) -> Result<&CompiledTemplate<'_>, Error> {
        if let Some(rv) = self.borrowed_templates.get(name) {
            Ok(&**rv)
        } else {
            let name: Arc<str> = name.into();
            if let Some(local) = self.owned_templates.get(&name) {
                return Ok(local.borrow_dependent());
            }
            if self.shared_loaded.is_some() {
                let compiled = self.get_shared(name.clone())?;
                return Ok(self.owned_templates.get_or_insert_owned(name, || compiled).borrow_dependent());
            }
            self.owned_templates
                .get_or_try_insert(&name.clone(), || -> Result<_, Error> {
                    let loader_result = match self.loader {
                        Some(ref loader) => ok!(loader(&name)),
                        None => None,
                    }
                    .ok_or_else(|| Error::new_not_found(&name));
                    self.make_owned_template(name, ok!(loader_result))
                })
                .map(|x| x.borrow_dependent())
        }
    }

    pub fn set_loaded_validator(&mut self, validator: Arc<LoadedValidator>) {
        self.loaded_validator = Some(validator);
    }

    /// The module an import without context created from this template, and
    /// the id of the state that created it.
    pub(crate) fn cached_module(&self, template: &Arc<LoadedTemplate>) -> Option<(crate::value::Value, isize)> {
        let mut modules = self.shared_modules.lock().unwrap();
        modules.retain(|(owner, _, _)| owner.strong_count() > 0);
        modules.iter().find(|(owner, _, _)| std::ptr::eq(owner.as_ptr(), Arc::as_ptr(template)))
            .map(|(_, module, state)| (module.clone(), *state))
    }

    pub(crate) fn store_module(&self, template: &Arc<LoadedTemplate>, module: crate::value::Value, state: isize) {
        let mut modules = self.shared_modules.lock().unwrap();
        modules.retain(|(owner, _, _)| owner.strong_count() > 0 && !std::ptr::eq(owner.as_ptr(), Arc::as_ptr(template)));
        modules.push((Arc::downgrade(template), module, state));
    }

    pub fn enable_shared_loaded_cache(&mut self) {
        self.shared_loaded = Some(Arc::new(MemoMap::default()));
    }

    /// Whether every template this store hands out is reference counted, so
    /// a state can keep its instructions alive without borrowing the store.
    pub(crate) fn hands_out_owned_templates(&self) -> bool {
        self.shared_loaded.is_some() && self.borrowed_templates.is_empty()
    }

    pub fn set_loader<F>(&mut self, f: F)
    where
        F: Fn(&str) -> Result<Option<String>, Error> + Send + Sync + 'static,
    {
        self.loader = Some(Arc::new(f));
    }

    fn make_owned_template(
        &self,
        name: Arc<str>,
        source: String,
    ) -> Result<Arc<LoadedTemplate>, Error> {
        LoadedTemplate::compile(name, source, &self.template_config, true)
    }

    pub fn iter(&self) -> impl Iterator<Item = (&str, CompiledTemplateRef<'_, '_>)> {
        let borrowed = self
            .borrowed_templates
            .iter()
            .map(|(name, template)| (*name, CompiledTemplateRef::Borrowed(&**template)));

        let owned = self
            .owned_templates
            .iter()
            .map(|(name, template)| (&**name, CompiledTemplateRef::Borrowed(template.borrow_dependent())));

        let shared = self.shared_loaded.iter().flat_map(|cache| cache.iter())
            .filter(|(name, _)| !self.borrowed_templates.contains_key(&***name)
                && !self.owned_templates.contains_key(&***name))
            .filter_map(|(name, slot)| slot.lock().unwrap().clone()
                .map(|template| (&**name, CompiledTemplateRef::Adapted(template))));

        borrowed.chain(owned).chain(shared)
    }
}

/// Safely joins two paths.
pub fn safe_join(base: &Path, template: &str) -> Option<PathBuf> {
    let mut rv = base.to_path_buf();
    for segment in template.split('/') {
        if segment.starts_with('.') || segment.contains('\\') {
            return None;
        }
        rv.push(segment);
    }
    Some(rv)
}

/// Helper to load templates from a given directory.
///
/// This creates a dynamic loader which looks up templates in the
/// given directory.  Templates that start with a dot (`.`) or are contained in
/// a folder starting with a dot cannot be loaded.
///
/// # Example
///
/// ```rust
/// # use minijinja::{path_loader, Environment};
/// fn create_env() -> Environment<'static> {
///     let mut env = Environment::new();
///     env.set_loader(path_loader("path/to/templates"));
///     env
/// }
/// ```
pub fn path_loader<'x, P: AsRef<Path> + 'x>(
    dir: P,
) -> impl for<'a> Fn(&'a str) -> Result<Option<String>, Error> + Send + Sync + 'static {
    let dir = dir.as_ref().to_path_buf();
    move |name| {
        let Some(path) = safe_join(&dir, name) else {
            return Ok(None);
        };
        match fs::read_to_string(path) {
            Ok(result) => Ok(Some(result)),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(err) => Err(
                Error::new(ErrorKind::InvalidOperation, "could not read template").with_source(err),
            ),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use similar_asserts::assert_eq;

    #[test]
    fn test_safe_join() {
        assert_eq!(
            safe_join(Path::new("foo"), "bar/baz"),
            Some(PathBuf::from("foo").join("bar").join("baz"))
        );
        assert_eq!(safe_join(Path::new("foo"), ".bar/baz"), None);
        assert_eq!(safe_join(Path::new("foo"), "bar/.baz"), None);
        assert_eq!(safe_join(Path::new("foo"), "bar/../baz"), None);
    }

    #[cfg(feature = "unstable_machinery")]
    #[test]
    fn adapted_ast_owns_data_across_loaders_clones_and_captured_state() {
        use crate::Environment;
        use crate::compiler::{ast as a, tokens::Span};
        use crate::template::TemplateAst;
        struct Generated { name: String, text: String }
        impl TemplateAst for Generated {
            fn lower(&self) -> Result<a::Stmt<'_>, Error> {
                let sp = Span::default();
                Ok(a::Stmt::Template(a::Spanned::new(a::Template { children: vec![
                    a::Stmt::EmitRaw(a::Spanned::new(a::EmitRaw { raw: &self.text }, sp)),
                    a::Stmt::EmitExpr(a::Spanned::new(a::EmitExpr {
                        expr: a::Expr::Var(a::Spanned::new(a::Var { id: &self.name }, sp)), constant_id: None,
                    }, sp)),
                ] }, sp)))
            }
        }
        let mut env = Environment::new();
        env.set_ast_adapter(|_, _| Ok(Some(Box::new(Generated { name: "generated".into(), text: "owned:".into() }))));
        env.add_template("borrowed", "syntax supplied by the adapter").unwrap();
        env.set_loader(|_| Ok(Some("loaded custom syntax".into())));
        let clone = env.clone();
        env.remove_template("borrowed");
        for name in ["borrowed", "loaded"] {
            let template = clone.get_template(name).unwrap();
            assert_eq!(template.render(context!(generated => "value")).unwrap(), "owned:value");
            assert!(template.undeclared_variables(false).contains("generated"));
        }
        let captured = clone.template_from_str("one shot custom syntax").unwrap()
            .render_captured(context!(generated => "captured")).unwrap();
        assert_eq!(captured.output(), "owned:captured");
    }
}
