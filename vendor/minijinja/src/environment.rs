use std::borrow::Cow;
use std::collections::BTreeMap;
use std::fmt;
use std::sync::{Arc, OnceLock};

use serde::Serialize;

use crate::compiler::codegen::CodeGenerator;
use crate::compiler::instructions::Instructions;
use crate::compiler::parser::parse_expr;
use crate::error::{attach_basic_debug_info, Error, ErrorKind};
use crate::expression::Expression;
use crate::output::Output;
use crate::template::{
    AutoEscapeFunc, CompiledTemplate, CompiledTemplateRef, Template, TemplateConfig,
};
use crate::utils::{write_escaped, AutoEscape, BTreeMapKeysDebug, UndefinedBehavior};
use crate::value::{FunctionArgs, FunctionResult, UndefinedType, Value, ValueRepr};
use crate::vm::State;
use crate::{defaults, functions};

type FormatterFunc = dyn Fn(&mut Output, &State, &Value) -> Result<(), Error> + Sync + Send;
type OutputPreparer = dyn Fn(&State) -> Result<Value, Error> + Sync + Send;
type PreparedFormatter = dyn Fn(&mut Output, &State, &Value, &Value) -> Result<(), Error> + Sync + Send;
type PathJoinFunc = dyn for<'s> Fn(&'s str, &'s str) -> Cow<'s, str> + Sync + Send;
type UnknownMethodFunc =
    dyn Fn(&State, &Value, &str, &[Value]) -> Result<Value, Error> + Sync + Send;
type RuntimeMacroCall = dyn Fn(&State, &Value, &[Value]) -> Result<Value, Error> + Send + Sync;
type RuntimeMacroFactory = dyn Fn(&State, &Value) -> Result<Value, Error> + Send + Sync;
type RuntimeBlocksLoaded = dyn Fn(&State, &[(&str, usize)]) -> Result<(), Error> + Send + Sync;
type RuntimeModuleCreated = dyn Fn(&State, &Value) -> Result<(), Error> + Send + Sync;
type RuntimeTemplateNotFound = dyn Fn(&State, &[Value], bool) -> Error + Send + Sync;
type RuntimeHostError = dyn Fn(&State, &str, &str) -> Error + Send + Sync;
type RuntimeIncludeForeign = dyn Fn(&State, &Value, bool) -> Result<Option<String>, Error> + Send + Sync;
type RuntimeLoopRecursion = dyn Fn(&State, &Value, &[Value], bool) -> Result<Value, Error> + Send + Sync;
type RuntimeLoopHandoff = dyn Fn(&State, &Value, &str) -> Result<(), Error> + Send + Sync;
type RuntimeLoopConstructed = dyn Fn(&State) -> Result<bool, Error> + Send + Sync;
type RuntimeDeferredSuper = dyn for<'t, 'e> Fn(&mut State<'t, 'e>,
    &mut dyn FnMut(&mut State<'t, 'e>) -> Result<Value, Error>) -> Result<Value, Error> + Send + Sync;
type RuntimeBlockDispatch = dyn Fn(&State, &str, usize, &mut Output) -> Result<bool, Error> + Send + Sync;
type RuntimeLoopIterator = dyn Fn(&State, &Value, bool, bool) -> Option<Result<Box<dyn crate::value::LoopIterator>, Error>> + Send + Sync;
type RuntimeLoopNext = dyn Fn(Vec<crate::vm::RuntimeRenderFrame>, usize,
    &mut dyn FnMut() -> Result<Option<Value>, Error>) -> Result<Option<Value>, Error> + Send + Sync;

/// An owned, compiled source that does not occupy a loader-cache name.
/// Render it with the environment that compiled it so its configuration and
/// callbacks remain consistent with compilation.
#[derive(Clone)]
pub struct OwnedTemplate(Arc<crate::loader::LoadedTemplate>);

fn default_auto_escape_callback() -> Arc<AutoEscapeFunc> {
    static DEFAULT_AUTO_ESCAPE: OnceLock<Arc<AutoEscapeFunc>> = OnceLock::new();
    DEFAULT_AUTO_ESCAPE
        .get_or_init(|| Arc::new(defaults::default_auto_escape_callback))
        .clone()
}

fn no_auto_escape_callback() -> Arc<AutoEscapeFunc> {
    static NO_AUTO_ESCAPE: OnceLock<Arc<AutoEscapeFunc>> = OnceLock::new();
    NO_AUTO_ESCAPE
        .get_or_init(|| Arc::new(defaults::no_auto_escape))
        .clone()
}

fn default_formatter() -> Arc<FormatterFunc> {
    static FORMATTER: OnceLock<Arc<FormatterFunc>> = OnceLock::new();
    FORMATTER
        .get_or_init(|| Arc::new(defaults::escape_formatter))
        .clone()
}

/// The maximum recursion in the VM.  Normally each stack frame
/// adds one to this counter (e.g., every time a frame is added).
/// However in some situations more depth is pushed if the cost
/// of the stack frame is higher.  Raising this limit
/// requires enabling the `stacker` feature.
const MAX_RECURSION: usize = 500;

/// Container construction delegated to a foreign runtime when configured.
pub enum RuntimeContainer {
    /// A fresh mutable list literal or argument accumulator.
    List(Vec<Value>),
    /// An immutable argument tuple.
    Tuple(Vec<Value>),
    /// A mutable keyword mapping, retaining insertion order.
    Map(Vec<(Value, Value)>),
    /// Keyword mapping sources retained until the call is bound by the host.
    KeywordSources(Vec<Value>),
}

/// Lexical ownership of explicit assignments in a foreign context.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RuntimeScope {
    /// Template root, where assignments are exported.
    Root,
    /// Loop body, whose assignments may derive callback contexts.
    Loop,
    /// Block body, whose assignments may derive callback contexts.
    Block,
    /// Local scope, including macros and with statements.
    Local,
}

/// Activation and publication events of the existing VM.
pub enum RuntimeContextEvent {
    /// Attach the root context prepared before execution.
    Root,
    /// Construct the special template reference before root symbol resolution.
    SelfReference,
    /// Construct a fresh context for the currently loaded include.
    Include,
    /// Obtain the lookup namespace of the newly attached include context.
    Namespace,
    /// Publish a complete assignment after unpacking succeeds.
    Publish(Vec<(String, Value)>),
    /// Bind import targets without exporting them from their own module.
    PublishImports(Vec<(String, Value)>),
    /// Copy the completed module's public exports, including host mutations.
    ModuleExports(Vec<(String, Value)>),
    /// Save the foreign evaluation context, then install the raw value.
    EnterAutoEscape(Value),
    /// Restore the saved foreign evaluation context.
    LeaveAutoEscape(Value),
    /// A scoped block is about to run with these visible template variables;
    /// a host may return the context the block receives (undefined keeps it).
    ScopedBlock(Vec<(String, Value)>),
}
type RuntimeContextHandler = dyn Fn(&State, RuntimeContextEvent) -> Result<Value, Error> + Send + Sync;

/// Result of unpacking values in a foreign runtime.
pub enum RuntimeUnpack {
    /// Successfully obtained exactly the requested values.
    Values(Vec<Value>),
    /// The iterator ended before the requested number of values.
    TooFew(usize),
    /// The iterator produced at least one extra value.
    TooMany,
}

/// An abstraction that holds the engine configuration.
///
/// This object holds the central configuration state for templates.  It is also
/// the container for all loaded templates.
///
/// The environment holds references to the source the templates were created from.
/// This makes it very inconvenient to pass around unless the templates are static
/// strings.
///
/// There are generally two ways to construct an environment:
///
/// * [`Environment::new`] creates an environment preconfigured with sensible
///   defaults.  It will contain all built-in filters, tests and globals as well
///   as a callback for auto escaping based on file extension.
/// * [`Environment::empty`] creates a completely blank environment.
#[derive(Clone)]
pub struct Environment<'source> {
    templates: TemplateStore<'source>,
    filters: Arc<BTreeMap<Cow<'source, str>, Value>>,
    tests: Arc<BTreeMap<Cow<'source, str>, Value>>,
    globals: Arc<BTreeMap<Cow<'source, str>, Value>>,
    path_join_callback: Option<Arc<PathJoinFunc>>,
    pub(crate) unknown_method_callback: Option<Arc<UnknownMethodFunc>>,
    undefined_behavior: UndefinedBehavior,
    formatter: Arc<FormatterFunc>,
    formatter_is_default: bool,
    runtime_container_factory: Option<Arc<dyn Fn(RuntimeContainer) -> Result<Value, Error> + Send + Sync>>,
    runtime_context_handler: Option<Arc<RuntimeContextHandler>>,
    runtime_prepared_lookup: Option<Arc<dyn Fn(&Value, &str) -> Option<Option<Value>> + Send + Sync>>,
    runtime_prepared_use: Option<Arc<dyn Fn(Value) -> Result<Value, Error> + Send + Sync>>,
    runtime_root_missing: Option<Arc<dyn Fn(&Value, &str) -> Result<Value, Error> + Send + Sync>>,
    runtime_import_missing: Option<Arc<dyn Fn(&State, &Value, &str, u16) -> Result<Value, Error> + Send + Sync>>,
    runtime_error_observer: Option<Arc<dyn Fn(&State, &Error) + Send + Sync>>,
    runtime_error_frame_observer: Option<Arc<dyn Fn(&State, &Error, usize) + Send + Sync>>,
    runtime_macro_call: Option<Arc<RuntimeMacroCall>>,
    runtime_macro_factory: Option<Arc<RuntimeMacroFactory>>,
    runtime_operator: Option<Arc<dyn Fn(&State, &str, &Value, Option<&Value>) -> Result<Value, Error> + Send + Sync>>,
    string_format_operator: Option<Arc<dyn Fn(&Value, &Value) -> Result<Value, Error> + Send + Sync>>,
    runtime_loop_changed: Option<Arc<dyn Fn(&[Value], &[Value]) -> Result<bool, Error> + Send + Sync>>,
    runtime_loop_changed_with_state: Option<Arc<dyn Fn(&State, &[Value], &[Value]) -> Result<bool, Error> + Send + Sync>>,
    runtime_loop_changed_scope: Option<Arc<dyn Fn(&State, &mut dyn FnMut() -> Result<Value, Error>) -> Result<Value, Error> + Send + Sync>>,
    runtime_loop_arguments: Option<Arc<dyn Fn(&State, &str, &[Value]) -> Result<Vec<Value>, Error> + Send + Sync>>,
    runtime_loop_recursion: Option<Arc<RuntimeLoopRecursion>>,
    runtime_loop_advance: Option<Arc<dyn Fn(&State, &Value) -> Result<(), Error> + Send + Sync>>,
    runtime_loop_iterator: Option<Arc<RuntimeLoopIterator>>,
    runtime_loop_next: Option<Arc<RuntimeLoopNext>>,
    runtime_filtered_loop_length: Option<Arc<dyn Fn(&State, &Value) -> Result<Option<Value>, Error> + Send + Sync>>,
    runtime_filtered_loop_length_with_input: Option<Arc<dyn Fn(&Value,
        &mut dyn FnMut() -> Result<Option<Value>, Error>) -> Result<Option<Value>, Error> + Send + Sync>>,
    runtime_constant_output_guard: Option<Arc<dyn Fn(&Value) -> Result<(), Error> + Send + Sync>>,
    runtime_capture_wrapper: Option<Arc<crate::output::CaptureWrapper>>,
    runtime_loop_post_lookup: Option<Arc<dyn Fn(&State, &Value, &Value, Option<&str>, Option<&Value>, Value) -> Result<Value, Error> + Send + Sync>>,
    runtime_loop_handoff: Option<Arc<RuntimeLoopHandoff>>,
    runtime_loop_constructed: Option<Arc<RuntimeLoopConstructed>>,
    runtime_module_created: Option<Arc<RuntimeModuleCreated>>,
    runtime_template_not_found: Option<Arc<RuntimeTemplateNotFound>>,
    runtime_host_error: Option<Arc<RuntimeHostError>>,
    runtime_include_foreign: Option<Arc<RuntimeIncludeForeign>>,
    runtime_blocks_loaded: Option<Arc<RuntimeBlocksLoaded>>,
    runtime_loop_item_key: Option<Arc<dyn Fn(&Value, &Value) -> Result<Option<Value>, Error> + Send + Sync>>,
    runtime_nonfinite_constant: Option<Arc<dyn Fn(f64) -> Result<Value, Error> + Send + Sync>>,
    runtime_subscript_tuple: Option<Arc<dyn Fn(&State, Value) -> Result<Value, Error> + Send + Sync>>,
    runtime_unique_constant: Option<Arc<dyn Fn(&State, Value, u32) -> Result<Value, Error> + Send + Sync>>,
    runtime_prepare_call: Option<Arc<dyn Fn(&State, bool) -> Result<Value, Error> + Send + Sync>>,
    runtime_prepare_attribute: Option<Arc<dyn Fn() -> Result<Value, Error> + Send + Sync>>,
    runtime_attribute: Option<Arc<dyn Fn(&State, &Value, &Value, &str) -> Result<Option<Value>, Error> + Send + Sync>>,
    runtime_release_attribute: Option<Arc<dyn Fn(Value) + Send + Sync>>,
    runtime_prepare_item: Option<Arc<dyn Fn() -> Result<Value, Error> + Send + Sync>>,
    runtime_item: Option<Arc<dyn Fn(&State, &Value, &Value, &Value) -> Result<Option<Value>, Error> + Send + Sync>>,
    runtime_release_item: Option<Arc<dyn Fn(Value) + Send + Sync>>,
    runtime_concat: Option<Arc<dyn Fn(&State, &[Value]) -> Result<Value, Error> + Send + Sync>>,
    fragment_joiner: Option<Arc<crate::FragmentJoiner>>,
    runtime_await: Option<Arc<dyn Fn(&State, Value) -> Result<Value, Error> + Send + Sync>>,
    runtime_deferred_super: Option<Arc<RuntimeDeferredSuper>>,
    runtime_block_dispatch: Option<Arc<RuntimeBlockDispatch>>,
    runtime_required_block_check: Option<Arc<dyn Fn(&State, &str, usize) -> Result<bool, Error> + Send + Sync>>,
    runtime_self_block_guard: Option<Arc<dyn Fn(&State, bool) -> Result<(), Error> + Send + Sync>>,
    runtime_self_block_arguments: Option<Arc<dyn Fn(&State, &[Value], bool) -> Result<(), Error> + Send + Sync>>,
    runtime_self_block_prepare: Option<Arc<dyn Fn(&State, &str) -> Result<Option<Value>, Error> + Send + Sync>>,
    runtime_deferred_loop: Option<Arc<RuntimeDeferredSuper>>,
    runtime_prepare_await: Option<Arc<dyn Fn(&State) -> Result<(), Error> + Send + Sync>>,
    unpack_mismatch_handler: Option<Arc<dyn Fn(usize, Option<usize>) -> Error + Send + Sync>>,
    unpack_iterator_error_handler: Option<Arc<dyn Fn(&Value, Error) -> Error + Send + Sync>>,
    runtime_unpack_handler: Option<Arc<dyn Fn(&Value, usize) -> Option<Result<RuntimeUnpack, Error>> + Send + Sync>>,
    preserve_test_results: bool,
    loop_else_on_completed_iteration: bool,
    respect_include_context: bool,
    output_preparer: Option<Arc<OutputPreparer>>,
    prepared_formatter: Option<Arc<PreparedFormatter>>,
    #[cfg(feature = "debug")]
    debug: bool,
    #[cfg(feature = "fuel")]
    fuel: Option<u64>,
    recursion_limit: usize,
}

impl Default for Environment<'_> {
    fn default() -> Self {
        Environment::empty()
    }
}

impl fmt::Debug for Environment<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Environment")
            .field("globals", &self.globals)
            .field("tests", &BTreeMapKeysDebug(&self.tests))
            .field("filters", &BTreeMapKeysDebug(&self.filters))
            .field("templates", &self.templates)
            .finish()
    }
}

impl<'source> Environment<'source> {
    /// Resolve a prepared binding without materializing a host missing value.
    /// `None` delegates to the ordinary lookup; `Some(None)` continues scope lookup.
    pub fn set_runtime_prepared_lookup<F>(&mut self, lookup: F)
    where F: Fn(&Value, &str) -> Option<Option<Value>> + Send + Sync + 'static {
        self.runtime_prepared_lookup = Some(Arc::new(lookup));
    }

    pub(crate) fn runtime_prepared_lookup(&self, value: &Value, name: &str) -> Option<Option<Value>> {
        self.runtime_prepared_lookup.as_ref().and_then(|lookup| lookup(value, name))
    }

    /// Materialize a prepared foreign binding at an expression use.
    pub fn set_runtime_prepared_use<F>(&mut self, materialize: F)
    where F: Fn(Value) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_prepared_use = Some(Arc::new(materialize));
    }

    pub(crate) fn runtime_prepared_use(&self, value: Value) -> Result<Value, Error> {
        match &self.runtime_prepared_use { Some(materialize) => materialize(value), None => Ok(value) }
    }

    /// Enable root symbol initialization and provide a lazy foreign missing value.
    pub fn set_runtime_root_missing<F>(&mut self, missing: F)
    where F: Fn(&Value, &str) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_root_missing = Some(Arc::new(missing));
        self.templates.template_config.root_symbol_prologue = true;
    }

    pub(crate) fn runtime_root_missing(&self, context: &Value, name: &str) -> Result<Value, Error> {
        match &self.runtime_root_missing { Some(missing) => missing(context, name), None => Ok(Value::UNDEFINED) }
    }

    /// Construct a foreign undefined value when an imported export is absent.
    pub fn set_runtime_import_missing<F>(&mut self, missing: F)
    where F: Fn(&State, &Value, &str, u16) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_import_missing = Some(Arc::new(missing));
    }

    pub(crate) fn runtime_import_missing(&self, state: &State, template: &Value, name: &str, line: u16) -> Result<Value, Error> {
        match &self.runtime_import_missing {
            Some(missing) => missing(state, template, name, line),
            None => Ok(Value::UNDEFINED),
        }
    }

    /// Observe a failed root activation while its state and bindings are alive.
    pub fn set_runtime_error_observer<F>(&mut self, observer: F)
    where F: Fn(&State, &Error) + Send + Sync + 'static {
        self.runtime_error_observer = Some(Arc::new(observer));
    }

    pub(crate) fn observe_runtime_error(&self, state: &State, error: &Error) {
        if let Some(observer) = &self.runtime_error_observer { observer(state, error); }
    }

    /// Observe a VM error at its current instruction before that activation unwinds.
    pub fn set_runtime_error_frame_observer<F>(&mut self, observer: F)
    where F: Fn(&State, &Error, usize) + Send + Sync + 'static {
        self.runtime_error_frame_observer = Some(Arc::new(observer));
    }

    pub(crate) fn observe_runtime_error_frame(&self, state: &State, error: &Error, line: usize) {
        if let Some(observer) = &self.runtime_error_frame_observer { observer(state, error, line); }
    }
    /// Capture formatter dependencies before evaluating an output expression.
    pub fn set_output_preparer<F>(&mut self, prepare: F)
    where F: Fn(&State) -> Result<Value, Error> + Send + Sync + 'static {
        self.output_preparer = Some(Arc::new(prepare));
        self.templates.template_config.prepare_output = true;
    }

    /// Format an output with its previously captured dependencies.
    pub fn set_prepared_formatter<F>(&mut self, format: F)
    where F: Fn(&mut Output, &State, &Value, &Value) -> Result<(), Error> + Send + Sync + 'static {
        self.prepared_formatter = Some(Arc::new(format));
    }

    pub(crate) fn prepare_output(&self, state: &State) -> Result<Value, Error> {
        self.output_preparer.as_ref().map_or(Ok(Value::UNDEFINED), |prepare| prepare(state))
    }

    pub(crate) fn format_prepared(&self, output: &mut Output, state: &State, value: &Value, prepared: &Value) -> Result<(), Error> {
        match &self.prepared_formatter {
            Some(format) => format(output, state, value, prepared),
            None => self.format(value, state, output),
        }
    }
    /// Retain original test results; branch instructions perform truth testing.
    pub fn set_preserve_test_results(&mut self, enabled: bool) { self.preserve_test_results = enabled; }

    pub(crate) fn preserve_test_results(&self) -> bool { self.preserve_test_results }
    /// Use Jinja loop-control semantics: else runs unless an iteration reaches its end.
    pub fn set_loop_else_on_completed_iteration(&mut self, enabled: bool) {
        self.loop_else_on_completed_iteration = enabled;
    }

    /// Honor with/without context on includes instead of the legacy shared scope.
    pub fn set_respect_include_context(&mut self, enabled: bool) {
        self.respect_include_context = enabled;
    }

    pub(crate) fn respect_include_context(&self) -> bool { self.respect_include_context }

    pub(crate) fn loop_else_on_completed_iteration(&self) -> bool {
        self.loop_else_on_completed_iteration
    }

    /// Preserve original-runtime constant output effects during compilation.
    pub fn set_constant_output<F>(&mut self, convert: F)
    where F: Fn(&str, &str, usize, usize, Option<bool>) -> Result<Option<String>, Error> + Send + Sync + 'static {
        self.templates.template_config.constant_output = Some(Arc::new(move |name, source, start, end, escape| {
            convert(name, source, start, end, escape).map(|output| output.map(crate::ConstantOutput::Text))
        }));
    }
    /// Preserve non-UTF-8 foreign constants without repeating their evaluation.
    pub fn set_constant_output_values<F>(&mut self, convert: F)
    where F: Fn(&str, &str, usize, usize, Option<bool>) -> Result<Option<crate::ConstantOutput>, Error> + Send + Sync + 'static {
        self.templates.template_config.constant_output = Some(Arc::new(convert));
    }
    /// Fold supported literal filter arguments using a foreign runtime.
    pub fn set_constant_argument<F>(&mut self, convert: F)
    where F: Fn(&str, &str, &Value, Option<bool>) -> Result<Option<crate::template::ConstantArgument>, Error> + Send + Sync + 'static {
        self.templates.template_config.constant_argument = Some(Arc::new(convert));
    }
    /// Fold supported numeric arithmetic through a foreign runtime.
    pub fn set_constant_arithmetic<F>(&mut self, convert: F)
    where F: Fn(&str, &str, &Value, Option<&Value>, Option<bool>, bool, bool, bool) -> Result<Option<(Value, bool)>, Error> + Send + Sync + 'static {
        self.templates.template_config.constant_arithmetic = Some(Arc::new(convert));
    }
    /// Select arithmetic folding after source-language operator decisions.
    pub fn set_arithmetic_codegen_mode<F>(&mut self, mode: F)
    where F: Fn(&str) -> bool + Send + Sync + 'static {
        self.templates.template_config.arithmetic_codegen_mode = Some(Arc::new(mode));
    }
    /// Query foreign optimizer activity and operator interception.
    pub fn set_jinja_arithmetic_policy<F>(&mut self, policy: F)
    where F: Fn(&str, Option<&str>, bool, Option<bool>) -> Result<Option<bool>, Error> + Send + Sync + 'static {
        self.templates.template_config.jinja_arithmetic_policy = Some(Arc::new(policy));
    }
    /// Prepare a foreign constant-attribute lookup.
    pub fn set_jinja_const_attribute_lookup<F>(&mut self, lookup: F)
    where F: Fn() -> Result<Option<Value>, Error> + Send + Sync + 'static {
        self.templates.template_config.jinja_const_attribute_lookup = Some(Arc::new(lookup));
    }
    /// Prepare a foreign constant-item lookup.
    pub fn set_jinja_const_item_lookup<F>(&mut self, lookup: F)
    where F: Fn() -> Result<Option<Value>, Error> + Send + Sync + 'static {
        self.templates.template_config.jinja_const_item_lookup = Some(Arc::new(lookup));
    }
    /// Release a foreign constant-attribute lookup at the matching boundary.
    pub fn set_jinja_const_attribute_release<F>(&mut self, release: F)
    where F: Fn(Value) + Send + Sync + 'static {
        self.templates.template_config.jinja_const_attribute_release = Some(Arc::new(release));
    }
    /// Select foreign call dispatch at the source-language compile boundary.
    pub fn set_call_policy_factory<F>(&mut self, factory: F)
    where F: Fn(&str) -> Result<bool, Error> + Send + Sync + 'static {
        self.templates.template_config.call_policy_factory = Some(Arc::new(factory));
    }
    /// Capture the foreign call dispatcher before evaluating call arguments.
    pub fn set_runtime_prepare_call<F>(&mut self, handler: F)
    where F: Fn(&State, bool) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_prepare_call = Some(Arc::new(handler));
    }

    pub(crate) fn runtime_prepare_call(&self, state: &State, policy: bool) -> Result<Value, Error> {
        match &self.runtime_prepare_call {
            Some(handler) => handler(state, policy),
            None => Ok(Value::UNDEFINED),
        }
    }
    /// Install foreign attribute capture and lookup handlers.
    pub fn set_runtime_attribute_handlers<P, L, R>(&mut self, prepare: P, lookup: L, release: R)
    where P: Fn() -> Result<Value, Error> + Send + Sync + 'static,
          L: Fn(&State, &Value, &Value, &str) -> Result<Option<Value>, Error> + Send + Sync + 'static,
          R: Fn(Value) + Send + Sync + 'static {
        self.runtime_prepare_attribute = Some(Arc::new(prepare));
        self.runtime_attribute = Some(Arc::new(lookup));
        self.runtime_release_attribute = Some(Arc::new(release));
        self.templates.template_config.prepare_attribute = true;
    }
    pub(crate) fn runtime_prepare_attribute(&self) -> Result<Value, Error> {
        self.runtime_prepare_attribute.as_ref().expect("attribute handler missing")()
    }
    pub(crate) fn runtime_attribute(&self, state: &State, method: &Value, value: &Value, name: &str) -> Result<Option<Value>, Error> {
        self.runtime_attribute.as_ref().expect("attribute handler missing")(state, method, value, name)
    }
    pub(crate) fn runtime_release_attribute(&self, method: Value) {
        if let Some(release) = &self.runtime_release_attribute { release(method); }
    }
    /// Install foreign item capture and lookup handlers.
    pub fn set_runtime_item_handlers<P, L, R>(&mut self, prepare: P, lookup: L, release: R)
    where P: Fn() -> Result<Value, Error> + Send + Sync + 'static,
          L: Fn(&State, &Value, &Value, &Value) -> Result<Option<Value>, Error> + Send + Sync + 'static,
          R: Fn(Value) + Send + Sync + 'static {
        self.runtime_prepare_item = Some(Arc::new(prepare));
        self.runtime_item = Some(Arc::new(lookup));
        self.runtime_release_item = Some(Arc::new(release));
        self.templates.template_config.prepare_item = true;
    }
    pub(crate) fn runtime_prepare_item(&self) -> Result<Value, Error> {
        self.runtime_prepare_item.as_ref().expect("item handler missing")()
    }
    pub(crate) fn runtime_item(&self, state: &State, method: &Value, value: &Value, key: &Value) -> Result<Option<Value>, Error> {
        self.runtime_item.as_ref().expect("item handler missing")(state, method, value, key)
    }
    pub(crate) fn runtime_release_item(&self, method: Value) {
        if let Some(release) = &self.runtime_release_item { release(method); }
    }
    /// Normalize CRLF and bare CR to LF like Jinja's lexer.
    ///
    /// The source validator still receives the original text; normalization
    /// applies to the source that is tokenized and compiled afterwards.
    pub fn set_normalize_newlines(&mut self, yes: bool) {
        self.templates.template_config.normalize_newlines = yes;
    }

    /// Validate the source language before compiling native instructions.
    pub fn set_source_validator<F>(&mut self, validate: F)
    where F: Fn(&str, &str) -> Result<(), Error> + Send + Sync + 'static {
        self.templates.template_config.source_validator = Some(Arc::new(validate));
    }

    /// Capture host metadata once for each compiled template.
    pub fn set_template_metadata<F>(&mut self, capture: F)
    where F: Fn(&str) -> Result<Value, Error> + Send + Sync + 'static {
        self.templates.template_config.template_metadata = Some(Arc::new(capture));
    }

    /// Complete per-template host metadata after native compilation succeeds.
    pub fn set_template_finalizer<F>(&mut self, finish: F)
    where F: Fn(Option<&Value>) -> Result<(), Error> + Send + Sync + 'static {
        self.templates.template_config.template_finalizer = Some(Arc::new(finish));
    }

    /// Prepare per-template metadata immediately before native code generation.
    pub fn set_template_prelude<F>(&mut self, prepare: F)
    where F: Fn(Option<&Value>) -> Result<(), Error> + Send + Sync + 'static {
        self.templates.template_config.template_prelude = Some(Arc::new(prepare));
    }

    /// Assign a private key used by compilation callbacks.
    pub fn set_compilation_key<F>(&mut self, make: F)
    where F: Fn(&str) -> String + Send + Sync + 'static {
        self.templates.template_config.compilation_key = Some(Arc::new(make));
    }

    /// Release private callback state after compilation.
    pub fn set_compilation_cleanup<F>(&mut self, finish: F)
    where F: Fn(&str) -> Result<(), Error> + Send + Sync + 'static {
        self.templates.template_config.compilation_cleanup = Some(Arc::new(finish));
    }
    /// Adapt syntax trees before compiling native instructions. Their owned
    /// storage lives as long as the template, including loader and one-shot use.
    #[cfg(feature = "unstable_machinery")]
    pub fn set_ast_adapter<F>(&mut self, adapt: F)
    where F: Fn(&str, &str) -> Result<Option<Box<dyn crate::template::TemplateAst>>, Error> + Send + Sync + 'static {
        self.templates.template_config.ast_adapter = Some(Arc::new(adapt));
    }
    /// Delegate operators while keeping evaluation order in the native VM.
    pub fn set_runtime_operator<F>(&mut self, operator: F)
    where F: Fn(&State, &str, &Value, Option<&Value>) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_operator = Some(Arc::new(operator));
        self.templates.template_config.native_optimizations = false;
    }

    /// Implement `string % arguments` (Python printf formatting) without
    /// taking over the other operators. Only string left operands reach it.
    pub fn set_string_format_operator<F>(&mut self, operator: F)
    where F: Fn(&Value, &Value) -> Result<Value, Error> + Send + Sync + 'static {
        self.string_format_operator = Some(Arc::new(operator));
    }

    pub(crate) fn string_format_operator(&self, left: &Value, right: &Value) -> Option<Result<Value, Error>> {
        if left.kind() != crate::value::ValueKind::String { return None; }
        self.string_format_operator.as_ref().map(|operator| operator(left, right))
    }

    pub(crate) fn runtime_operator(&self, state: &State, op: &str, left: &Value, right: Option<&Value>) -> Option<Result<Value, Error>> {
        self.runtime_operator.as_ref().map(|operator| operator(state, op, left, right))
    }

    /// Inspect emitted raw or constant text containing U+0001, and foreign
    /// constant fragments whose Unicode content may not be valid UTF-8.
    pub fn set_runtime_constant_output_guard<F>(&mut self, guard: F)
    where F: Fn(&Value) -> Result<(), Error> + Send + Sync + 'static {
        self.runtime_constant_output_guard = Some(Arc::new(guard));
    }

    pub(crate) fn runtime_constant_output_guard(&self, value: &Value) -> Result<(), Error> {
        match &self.runtime_constant_output_guard {
            Some(guard) => guard(value),
            None => Ok(()),
        }
    }

    pub(crate) fn has_runtime_constant_output_guard(&self) -> bool {
        self.runtime_constant_output_guard.is_some()
    }

    /// Wrap a marked capture in a host value without changing its contents.
    pub fn set_runtime_capture_wrapper<F>(&mut self, wrapper: F)
    where F: Fn(Value) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_capture_wrapper = Some(Arc::new(wrapper));
    }

    pub(crate) fn runtime_capture_wrapper(&self) -> Option<Arc<crate::output::CaptureWrapper>> {
        self.runtime_capture_wrapper.clone()
    }

    /// Compare the argument tuples retained by a loop's changed method.
    pub fn set_runtime_loop_changed<F>(&mut self, compare: F)
    where F: Fn(&[Value], &[Value]) -> Result<bool, Error> + Send + Sync + 'static {
        self.runtime_loop_changed = Some(Arc::new(compare));
        self.runtime_loop_changed_with_state = None;
    }

    /// Compare loop arguments while exposing the active VM state to the host.
    pub fn set_runtime_loop_changed_with_state<F>(&mut self, compare: F)
    where F: Fn(&State, &[Value], &[Value]) -> Result<bool, Error> + Send + Sync + 'static {
        self.runtime_loop_changed_with_state = Some(Arc::new(compare));
        self.runtime_loop_changed = None;
    }

    /// Scope the full changed operation, including release of replaced values.
    pub fn set_runtime_loop_changed_scope<F>(&mut self, scope: F)
    where F: Fn(&State, &mut dyn FnMut() -> Result<Value, Error>) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_loop_changed_scope = Some(Arc::new(scope));
    }

    pub(crate) fn runtime_loop_changed_scope(&self, state: &State,
        execute: &mut dyn FnMut() -> Result<Value, Error>) -> Result<Value, Error> {
        match &self.runtime_loop_changed_scope {
            Some(scope) => scope(state, execute),
            None => execute(),
        }
    }

    pub(crate) fn runtime_loop_changed(&self, state: &State, previous: &[Value], current: &[Value]) -> Result<bool, Error> {
        if let Some(compare) = &self.runtime_loop_changed_with_state {
            return compare(state, previous, current);
        }
        match &self.runtime_loop_changed {
            Some(compare) => compare(previous, current),
            None => Ok(previous != current),
        }
    }

    /// Adapt arguments for host loop methods without moving loop state out of the VM.
    pub fn set_runtime_loop_arguments<F>(&mut self, bind: F)
    where F: Fn(&State, &str, &[Value]) -> Result<Vec<Value>, Error> + Send + Sync + 'static {
        self.runtime_loop_arguments = Some(Arc::new(bind));
    }

    pub(crate) fn runtime_loop_arguments(&self, state: &State, name: &str, args: &[Value]) -> Option<Result<Vec<Value>, Error>> {
        self.runtime_loop_arguments.as_ref().map(|bind| bind(state, name, args))
    }

    /// Bind an internal loop call before entering native recursive execution.
    /// The hook receives the called loop; returning a
    /// [`LoopCallOutcome`](crate::LoopCallOutcome) completes the call with its
    /// value instead of recursing.
    pub fn set_runtime_loop_recursion<F>(&mut self, bind: F)
    where F: Fn(&State, &Value, &[Value], bool) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_loop_recursion = Some(Arc::new(bind));
    }

    pub(crate) fn runtime_loop_recursion(&self, state: &State, called: &Value, args: &[Value], recursive: bool) -> Option<Result<Value, Error>> {
        self.runtime_loop_recursion.as_ref().map(|bind| bind(state, called, args, recursive))
    }

    /// Allow a host to provide an iterator for a loop activation.
    pub fn set_runtime_loop_iterator<F>(&mut self, iterator: F)
    where F: Fn(&State, &Value, bool, bool) -> Option<Result<Box<dyn crate::value::LoopIterator>, Error>> + Send + Sync + 'static {
        self.runtime_loop_iterator = Some(Arc::new(iterator));
    }

    pub(crate) fn runtime_loop_iterator(&self, state: &State, value: &Value, metadata: bool, filtered: bool) -> Option<Result<Box<dyn crate::value::LoopIterator>, Error>> {
        self.runtime_loop_iterator.as_ref().and_then(|iterator| iterator(state, value, metadata, filtered))
    }

    /// Observe each successful native loop advance without consuming lookahead.
    /// The loop value retains the same identity across iterations, including
    /// after an alias has escaped the loop body. Failed and terminal advances
    /// do not call this hook.
    pub fn set_runtime_loop_advance<F>(&mut self, observe: F)
    where F: Fn(&State, &Value) -> Result<(), Error> + Send + Sync + 'static {
        self.runtime_loop_advance = Some(Arc::new(observe));
    }

    pub(crate) fn has_runtime_loop_advance(&self) -> bool { self.runtime_loop_advance.is_some() }

    pub(crate) fn runtime_loop_advance(&self, state: &State, loop_value: &Value) -> Result<(), Error> {
        if let Some(observe) = &self.runtime_loop_advance {
            observe(state, loop_value)?;
        }
        Ok(())
    }

    /// Wrap iterator advancement with a snapshot of the active VM scopes.
    pub fn set_runtime_loop_next<F>(&mut self, next: F)
    where F: Fn(Vec<crate::vm::RuntimeRenderFrame>, usize,
        &mut dyn FnMut() -> Result<Option<Value>, Error>) -> Result<Option<Value>, Error> + Send + Sync + 'static {
        self.runtime_loop_next = Some(Arc::new(next));
    }

    pub(crate) fn has_runtime_loop_next(&self) -> bool { self.runtime_loop_next.is_some() }

    pub(crate) fn runtime_loop_next(&self, frames: Vec<crate::vm::RuntimeRenderFrame>, depth: usize,
        next: &mut dyn FnMut() -> Result<Option<Value>, Error>) -> Result<Option<Value>, Error> {
        match &self.runtime_loop_next {
            Some(wrap) => wrap(frames, depth, next),
            None => next(),
        }
    }

    /// Optionally supply a filtered loop's length before its remainder is consumed.
    /// `None` declines the request; `Some(Value::from(()))` supplies a none value.
    pub fn set_runtime_filtered_loop_length<F>(&mut self, length: F)
    where F: Fn(&State, &Value) -> Result<Option<Value>, Error> + Send + Sync + 'static {
        self.runtime_filtered_loop_length = Some(Arc::new(length));
    }

    pub(crate) fn has_runtime_filtered_loop_length(&self) -> bool { self.runtime_filtered_loop_length.is_some() }

    pub(crate) fn runtime_filtered_loop_length(&self, state: &State, value: &Value) -> Result<Option<Value>, Error> {
        match &self.runtime_filtered_loop_length {
            Some(length) => length(state, value),
            None => Ok(None),
        }
    }

    /// Supply a filtered loop length while optionally consuming its input.
    /// The advancement callback is valid only during this call and never
    /// advances the loop's index or its separate lookahead item.
    pub fn set_runtime_filtered_loop_length_with_input<F>(&mut self, length: F)
    where F: Fn(&Value, &mut dyn FnMut() -> Result<Option<Value>, Error>) -> Result<Option<Value>, Error>
        + Send + Sync + 'static {
        self.runtime_filtered_loop_length_with_input = Some(Arc::new(length));
    }

    pub(crate) fn runtime_filtered_loop_length_with_input(&self, value: &Value,
        next: &mut dyn FnMut() -> Result<Option<Value>, Error>) -> Result<Option<Value>, Error> {
        match &self.runtime_filtered_loop_length_with_input {
            Some(length) => length(value, next),
            None => Ok(None),
        }
    }

    pub(crate) fn has_runtime_filtered_loop_length_with_input(&self) -> bool {
        self.runtime_filtered_loop_length_with_input.is_some()
    }

    /// Adapt a successful native loop lookup after it can execute user iteration.
    /// The dispatcher and original loop/key stay available for host policies;
    /// a replacement result is permitted. Attribute and item lookups supply
    /// exactly one of `attribute` and `key`.
    pub fn set_runtime_loop_post_lookup<F>(&mut self, check: F)
    where F: Fn(&State, &Value, &Value, Option<&str>, Option<&Value>, Value) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_loop_post_lookup = Some(Arc::new(check));
    }

    /// Observe the parent blocks an `extends` added: each block name with the
    /// depth of its new definition in the block's stack.
    pub fn set_runtime_blocks_loaded<F>(&mut self, loaded: F)
    where F: Fn(&State, &[(&str, usize)]) -> Result<(), Error> + Send + Sync + 'static {
        self.runtime_blocks_loaded = Some(Arc::new(loaded));
    }

    pub(crate) fn runtime_blocks_loaded(&self, state: &State, blocks: &[(&str, usize)]) -> Result<(), Error> {
        match &self.runtime_blocks_loaded { Some(loaded) => loaded(state, blocks), None => Ok(()) }
    }

    /// Observe the module an `import` created (a [`RuntimeModule`](crate::RuntimeModule))
    /// before it is assigned.
    pub fn set_runtime_module_created<F>(&mut self, created: F)
    where F: Fn(&State, &Value) -> Result<(), Error> + Send + Sync + 'static {
        self.runtime_module_created = Some(Arc::new(created));
    }

    pub(crate) fn runtime_module_created(&self, state: &State, module: &Value) -> Result<(), Error> {
        match &self.runtime_module_created { Some(created) => created(state, module), None => Ok(()) }
    }

    /// Build the error of an `include` or `import` that found no template:
    /// the names tried and whether a list of candidates was given (Jinja's
    /// `TemplateNotFound` / `TemplatesNotFound`, including an empty list).
    pub fn set_runtime_template_not_found<F>(&mut self, not_found: F)
    where F: Fn(&State, &[Value], bool) -> Error + Send + Sync + 'static {
        self.runtime_template_not_found = Some(Arc::new(not_found));
    }

    pub(crate) fn runtime_template_not_found(&self, state: &State, tried: &[Value], select: bool) -> Option<Error> {
        self.runtime_template_not_found.as_ref().map(|not_found| not_found(state, tried, select))
    }

    /// Build the host's error for a runtime failure the reference raises as
    /// a typed exception: the exception's class name and message (such as
    /// `TemplateRuntimeError`, `extended multiple times`).
    pub fn set_runtime_host_error<F>(&mut self, host_error: F)
    where F: Fn(&State, &str, &str) -> Error + Send + Sync + 'static {
        self.runtime_host_error = Some(Arc::new(host_error));
    }

    pub(crate) fn runtime_host_error(&self, state: &State, class: &str, message: &str) -> Option<Error> {
        self.runtime_host_error.as_ref().map(|host_error| host_error(state, class, message))
    }

    /// Render an `include` target the host owns (such as a template object
    /// of the host's engine) and return its output, emitted as is; `None`
    /// leaves the target to the VM. The flag tells whether the include passes
    /// its context.
    pub fn set_runtime_include_foreign<F>(&mut self, include: F)
    where F: Fn(&State, &Value, bool) -> Result<Option<String>, Error> + Send + Sync + 'static {
        self.runtime_include_foreign = Some(Arc::new(include));
    }

    pub(crate) fn runtime_include_foreign(&self, state: &State, target: &Value, with_context: bool)
        -> Result<Option<String>, Error> {
        match &self.runtime_include_foreign { Some(include) => include(state, target, with_context), None => Ok(None) }
    }

    /// Let a host take over a filtered loop (see `RuntimeLoop::delegate_with`)
    /// before the VM computes a field whose algorithm consumes its input:
    /// `length`, `revindex`, `revindex0`, `last` or `nextitem` (the field), when
    /// a filtered loop starts (`"start"`), and before any loop's `changed` or
    /// `cycle` method runs. The hook runs with the state
    /// installed (see [`crate::with_host_state`]).
    pub fn set_runtime_loop_handoff<F>(&mut self, handoff: F)
    where F: Fn(&State, &Value, &str) -> Result<(), Error> + Send + Sync + 'static {
        self.runtime_loop_handoff = Some(Arc::new(handoff));
    }

    pub(crate) fn runtime_loop_handoff(&self, state: &State, target: &Value, field: &str) -> Result<(), Error> {
        match &self.runtime_loop_handoff {
            Some(handoff) => crate::vm::with_host_state(state, || handoff(state, target, field)),
            None => Ok(()),
        }
    }

    /// Tell the VM whether a filtered loop's `"start"` handoff runs a host
    /// constructor for the loop object. The predicate's context names are then
    /// resolved after that handoff (the constructor may change them) instead
    /// of before the source is iterated.
    pub fn set_runtime_loop_constructed<F>(&mut self, constructed: F)
    where F: Fn(&State) -> Result<bool, Error> + Send + Sync + 'static {
        self.runtime_loop_constructed = Some(Arc::new(constructed));
    }

    pub(crate) fn runtime_loop_constructed(&self, state: &State) -> Result<bool, Error> {
        match &self.runtime_loop_constructed {
            Some(constructed) => crate::vm::with_host_state(state, || constructed(state)),
            None => Ok(false),
        }
    }

    pub(crate) fn runtime_loop_post_lookup(&self, state: &State, dispatcher: &Value, target: &Value,
        attribute: Option<&str>, key: Option<&Value>, value: Value) -> Result<Value, Error> {
        match &self.runtime_loop_post_lookup {
            Some(check) => check(state, dispatcher, target, attribute, key, value),
            None => Ok(value),
        }
    }

    /// Normalize host string keys for native loop item lookup.
    pub fn set_runtime_loop_item_key<F>(&mut self, normalize: F)
    where F: Fn(&Value, &Value) -> Result<Option<Value>, Error> + Send + Sync + 'static {
        self.runtime_loop_item_key = Some(Arc::new(normalize));
    }

    pub(crate) fn runtime_loop_item_key(&self, dispatcher: &Value, key: &Value) -> Result<Option<Value>, Error> {
        match &self.runtime_loop_item_key {
            Some(normalize) => normalize(dispatcher, key),
            None => Ok(None),
        }
    }

    /// Optionally preserve the identity of compile-time tuple subscripts.
    pub fn set_runtime_subscript_tuple<F>(&mut self, handler: F)
    where F: Fn(&State, Value) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_subscript_tuple = Some(Arc::new(handler));
    }

    /// Preserve the identity of a constant that is distinct by source site.
    pub fn set_runtime_unique_constant<F>(&mut self, handler: F)
    where F: Fn(&State, Value, u32) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_unique_constant = Some(Arc::new(handler));
    }

    pub(crate) fn runtime_unique_constant(&self, state: &State, value: Value, site: u32) -> Result<Value, Error> {
        match &self.runtime_unique_constant {
            Some(handler) => handler(state, value, site),
            None => Ok(value),
        }
    }

    /// Resolve non-finite numeric constants through a foreign runtime.
    pub fn set_runtime_nonfinite_constant<F>(&mut self, handler: F)
    where F: Fn(f64) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_nonfinite_constant = Some(Arc::new(handler));
    }

    pub(crate) fn runtime_nonfinite_constant(&self, value: f64) -> Option<Result<Value, Error>> {
        self.runtime_nonfinite_constant.as_ref().map(|handler| handler(value))
    }

    pub(crate) fn runtime_subscript_tuple(&self, state: &State, value: Value) -> Result<Value, Error> {
        match &self.runtime_subscript_tuple {
            Some(handler) => handler(state, value),
            None => Ok(value),
        }
    }

    /// Select intercepted arithmetic during compilation. The returned factory
    /// resolves a runtime callee before operands. Its result is called directly,
    /// without implicit awaiting, after the operands have been evaluated.
    pub fn set_operator_call_factory<F>(&mut self, factory: F)
    where F: Fn(&str, &str, bool) -> Result<Option<Value>, Error> + Send + Sync + 'static {
        self.templates.template_config.operator_call_factory = Some(Arc::new(factory));
        self.templates.template_config.native_optimizations = false;
    }

    /// Delegate a complete concatenation sequence to a foreign runtime.
    pub fn set_runtime_concat<F>(&mut self, concat: F)
    where F: Fn(&State, &[Value]) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_concat = Some(Arc::new(concat));
    }

    pub(crate) fn runtime_concat(&self, state: &State, values: &[Value]) -> Option<Result<Value, Error>> {
        self.runtime_concat.as_ref().map(|concat| concat(state, values))
    }

    /// Install a joiner for foreign fragments in nested macro captures.
    pub fn set_fragment_joiner<F>(&mut self, joiner: F)
    where F: Fn(Vec<Value>, AutoEscape) -> Result<Value, Error> + Send + Sync + 'static {
        self.fragment_joiner = Some(Arc::new(joiner));
        self.templates.template_config.fragment_boundaries = true;
    }

    /// Return the installed joiner, if foreign fragments are enabled.
    pub fn fragment_joiner(&self) -> Option<&crate::FragmentJoiner> {
        self.fragment_joiner.as_deref()
    }

    /// Resolve a foreign awaitable at language-defined suspension points.
    /// The host owns scheduling; the same VM retains all frames and output.
    pub fn set_runtime_await<F>(&mut self, await_value: F)
    where F: Fn(&State, Value) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_await = Some(Arc::new(await_value));
    }

    pub(crate) fn runtime_await(&self, state: &State, value: Value) -> Result<Value, Error> {
        match &self.runtime_await { Some(resolve) => resolve(state, value), None => Ok(value) }
    }

    /// Allow a host to defer a parent block until its awaitable is driven.
    pub fn set_runtime_deferred_super<F>(&mut self, resolve: F)
    where F: for<'t, 'e> Fn(&mut State<'t, 'e>, &mut dyn FnMut(&mut State<'t, 'e>) -> Result<Value, Error>)
        -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_deferred_super = Some(Arc::new(resolve));
    }

    pub(crate) fn runtime_deferred_super(&self) -> Option<&RuntimeDeferredSuper> {
        self.runtime_deferred_super.as_deref()
    }

    /// Let a host execute an explicitly replaced block at a given stack depth.
    /// Returning false leaves the native block in control of its own output.
    pub fn set_runtime_block_dispatch<F>(&mut self, dispatch: F)
    where F: Fn(&State, &str, usize, &mut Output) -> Result<bool, Error> + Send + Sync + 'static {
        self.runtime_block_dispatch = Some(Arc::new(dispatch));
    }

    pub(crate) fn runtime_block_dispatch(&self, state: &State, name: &str, depth: usize, out: &mut Output) -> Result<bool, Error> {
        match &self.runtime_block_dispatch { Some(dispatch) => dispatch(state, name, depth, out), None => Ok(false) }
    }

    /// Check the effective stack length at a required block's declaration.
    pub fn set_runtime_required_block_check<F>(&mut self, check: F)
    where F: Fn(&State, &str, usize) -> Result<bool, Error> + Send + Sync + 'static {
        self.runtime_required_block_check = Some(Arc::new(check));
    }

    pub(crate) fn runtime_required_block_check(&self, state: &State, name: &str, native_length: usize) -> Result<bool, Error> {
        match &self.runtime_required_block_check {
            Some(check) => check(state, name, native_length),
            None => Ok(native_length > 1),
        }
    }

    /// Guard a host-owned self-block call before its native body runs.
    pub fn set_runtime_self_block_guard<F>(&mut self, guard: F)
    where F: Fn(&State, bool) -> Result<(), Error> + Send + Sync + 'static {
        self.runtime_self_block_guard = Some(Arc::new(guard));
    }

    pub(crate) fn runtime_self_block_guard(&self, state: &State, sandboxed: bool) -> Result<(), Error> {
        match &self.runtime_self_block_guard {
            Some(guard) => guard(state, sandboxed),
            None => Ok(()),
        }
    }

    /// Validate evaluated self-block arguments at the host binding boundary.
    pub fn set_runtime_self_block_arguments<F>(&mut self, validate: F)
    where F: Fn(&State, &[Value], bool) -> Result<(), Error> + Send + Sync + 'static {
        self.runtime_self_block_arguments = Some(Arc::new(validate));
    }

    /// Resolve an overridden self-block attribute before its arguments run.
    /// Returning None retains the VM's native block execution.
    pub fn set_runtime_self_block_prepare<F>(&mut self, prepare: F)
    where F: Fn(&State, &str) -> Result<Option<Value>, Error> + Send + Sync + 'static {
        self.runtime_self_block_prepare = Some(Arc::new(prepare));
    }

    pub(crate) fn runtime_self_block_prepare(&self, state: &State, name: &str) -> Result<Value, Error> {
        self.runtime_self_block_prepare.as_ref().map_or(Ok(Value::UNDEFINED), |prepare|
            prepare(state, name).map(|value| value.unwrap_or(Value::UNDEFINED)))
    }

    pub(crate) fn runtime_self_block_arguments(&self, state: &State, args: &[Value], sandboxed: bool) -> Result<(), Error> {
        match &self.runtime_self_block_arguments {
            Some(validate) => validate(state, args, sandboxed),
            None if args.is_empty() => Ok(()),
            None if args.len() == 1 && args[0].is_kwargs()
                && args[0].try_iter()?.next().is_none() => Ok(()),
            None => Err(Error::new(crate::ErrorKind::TooManyArguments, "block takes no arguments")),
        }
    }

    /// Allow a host to defer a recursive loop body until its awaitable runs.
    pub fn set_runtime_deferred_loop<F>(&mut self, resolve: F)
    where F: for<'t, 'e> Fn(&mut State<'t, 'e>, &mut dyn FnMut(&mut State<'t, 'e>) -> Result<Value, Error>)
        -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_deferred_loop = Some(Arc::new(resolve));
    }

    pub(crate) fn runtime_deferred_loop(&self) -> Option<&RuntimeDeferredSuper> {
        self.runtime_deferred_loop.as_deref()
    }

    /// Prepare foreign await dispatch before evaluating the awaited operand.
    pub fn set_runtime_prepare_await<F>(&mut self, prepare: F)
    where F: Fn(&State) -> Result<(), Error> + Send + Sync + 'static {
        self.runtime_prepare_await = Some(Arc::new(prepare));
        self.templates.template_config.prepare_await = true;
    }

    pub(crate) fn runtime_prepare_await(&self, state: &State) -> Result<(), Error> {
        match &self.runtime_prepare_await { Some(prepare) => prepare(state), None => Ok(()) }
    }

    /// Adapt unpacking length errors to a foreign runtime. `None` means there
    /// were too many values; `Some(actual)` means there were too few.
    pub fn set_unpack_mismatch_handler<F>(&mut self, handler: F)
    where F: Fn(usize, Option<usize>) -> Error + Send + Sync + 'static {
        self.unpack_mismatch_handler = Some(Arc::new(handler));
    }

    pub(crate) fn unpack_mismatch(&self, expected: usize, actual: Option<usize>) -> Error {
        if let Some(handler) = &self.unpack_mismatch_handler { return handler(expected, actual); }
        let detail = match actual {
            Some(actual) => format!("sequence of wrong length (expected {expected}, got {actual})"),
            None => format!("sequence of wrong length (expected {expected}, got more)"),
        };
        Error::new(ErrorKind::CannotUnpack, detail)
    }

    /// Adapt an iterator-start error while preserving the original value.
    pub fn set_unpack_iterator_error_handler<F>(&mut self, handler: F)
    where F: Fn(&Value, Error) -> Error + Send + Sync + 'static {
        self.unpack_iterator_error_handler = Some(Arc::new(handler));
    }

    pub(crate) fn unpack_iterator_error(&self, value: &Value, error: Error) -> Error {
        match &self.unpack_iterator_error_handler {
            Some(handler) => handler(value, error),
            None => error,
        }
    }

    /// Delegate unpacking where a foreign runtime must retain raw objects
    /// until the exact arity has been checked. Returning `None` uses the VM.
    pub fn set_runtime_unpack_handler<F>(&mut self, handler: F)
    where F: Fn(&Value, usize) -> Option<Result<RuntimeUnpack, Error>> + Send + Sync + 'static {
        self.runtime_unpack_handler = Some(Arc::new(handler));
    }

    pub(crate) fn runtime_unpack(&self, value: &Value, count: usize) -> Option<Result<RuntimeUnpack, Error>> {
        self.runtime_unpack_handler.as_ref().and_then(|handler| handler(value, count))
    }

    /// Configure original-runtime tuple/map construction without adding names
    /// to the template's public globals.
    pub fn set_runtime_container_factory<F>(&mut self, factory: F)
    where F: Fn(RuntimeContainer) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_container_factory = Some(Arc::new(factory));
        self.templates.template_config.native_optimizations = false;
    }

    /// Observe context ownership without adding a second evaluator.
    pub fn set_runtime_context_handler<F>(&mut self, handler: F)
    where F: Fn(&State, RuntimeContextEvent) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_context_handler = Some(Arc::new(handler));
        self.templates.template_config.publish_locals = true;
    }

    /// Bind foreign macro calls while the existing VM executes their bodies.
    pub fn set_runtime_macro_call<F>(&mut self, call: F)
    where F: Fn(&State, &Value, &[Value]) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_macro_call = Some(Arc::new(call));
    }

    /// Construct a stable foreign macro object at its definition site.
    pub fn set_runtime_macro_factory<F>(&mut self, factory: F)
    where F: Fn(&State, &Value) -> Result<Value, Error> + Send + Sync + 'static {
        self.runtime_macro_factory = Some(Arc::new(factory));
    }

    pub(crate) fn runtime_macro_value(&self, state: &State, value: &Value) -> Option<Result<Value, Error>> {
        self.runtime_macro_factory.as_ref().map(|factory| factory(state, value))
    }

    pub(crate) fn runtime_macro_call(&self, state: &State, value: &Value, args: &[Value]) -> Option<Result<Value, Error>> {
        self.runtime_macro_call.as_ref().map(|call| call(state, value, args))
    }

    pub(crate) fn runtime_context_event(&self, state: &State, event: RuntimeContextEvent) -> Result<Value, Error> {
        match &self.runtime_context_handler {
            Some(handler) => handler(state, event),
            None => Ok(Value::UNDEFINED),
        }
    }

    pub(crate) fn runtime_list(&self, values: Vec<Value>) -> Result<Value, Error> {
        match &self.runtime_container_factory {
            Some(factory) => factory(RuntimeContainer::List(values)),
            None => Ok(Value::from(values)),
        }
    }

    pub(crate) fn runtime_tuple(&self, values: Vec<Value>) -> Result<Value, Error> {
        match &self.runtime_container_factory {
            Some(factory) => factory(RuntimeContainer::Tuple(values)),
            None => Ok(Value::from(values)),
        }
    }

    pub(crate) fn runtime_map(&self, values: Vec<(Value, Value)>) -> Result<Value, Error> {
        match &self.runtime_container_factory {
            Some(factory) => factory(RuntimeContainer::Map(values)),
            None => Ok(values.into_iter().collect()),
        }
    }

    pub(crate) fn runtime_keyword_sources(&self, values: Vec<Value>) -> Option<Result<Value, Error>> {
        self.runtime_container_factory.as_ref().map(|factory| factory(RuntimeContainer::KeywordSources(values)))
    }

    /// Creates a new environment with sensible defaults.
    ///
    /// This environment does not yet contain any templates but it will have all
    /// the default filters, tests and globals loaded.  If you do not want any
    /// default configuration you can use the alternative
    /// [`empty`](Environment::empty) method.
    #[cfg_attr(
        not(feature = "serde"),
        deprecated(
            since = "2.0.4",
            note = "Attempted to instantiate an environment with serde.  Future \
        versions of MiniJinja will require enabling the 'serde' feature to use \
        serde types.  To silence this warning add 'serde' to the list of features of minijinja."
        )
    )]
    pub fn new() -> Environment<'source> {
        Environment {
            templates: TemplateStore::new(TemplateConfig::new(default_auto_escape_callback())),
            filters: defaults::get_builtin_filters(),
            tests: defaults::get_builtin_tests(),
            globals: defaults::get_globals(),
            path_join_callback: None,
            unknown_method_callback: None,
            undefined_behavior: UndefinedBehavior::default(),
            formatter: default_formatter(),
            formatter_is_default: true,
            runtime_container_factory: None,
            runtime_context_handler: None,
            runtime_prepared_lookup: None,
            runtime_prepared_use: None,
            runtime_root_missing: None,
            runtime_import_missing: None,
            runtime_error_observer: None,
            runtime_error_frame_observer: None,
            runtime_macro_call: None,
            runtime_macro_factory: None,
            runtime_operator: None,
            string_format_operator: None,
            runtime_loop_changed: None,
            runtime_loop_changed_with_state: None,
            runtime_loop_changed_scope: None,
            runtime_loop_arguments: None,
            runtime_loop_recursion: None,
            runtime_loop_advance: None,
            runtime_loop_iterator: None,
            runtime_loop_next: None,
            runtime_filtered_loop_length: None,
            runtime_filtered_loop_length_with_input: None,
            runtime_constant_output_guard: None,
            runtime_capture_wrapper: None,
            runtime_loop_post_lookup: None,
            runtime_loop_handoff: None,
            runtime_loop_constructed: None,
            runtime_module_created: None,
            runtime_template_not_found: None,
            runtime_host_error: None,
            runtime_include_foreign: None,
            runtime_blocks_loaded: None,
            runtime_loop_item_key: None,
            runtime_nonfinite_constant: None,
            runtime_subscript_tuple: None,
            runtime_unique_constant: None,
            runtime_prepare_call: None,
            runtime_prepare_attribute: None,
            runtime_attribute: None,
            runtime_release_attribute: None,
            runtime_prepare_item: None,
            runtime_item: None,
            runtime_release_item: None,
            runtime_concat: None,
            fragment_joiner: None,
            runtime_await: None,
            runtime_deferred_super: None,
            runtime_block_dispatch: None,
            runtime_required_block_check: None,
            runtime_self_block_guard: None,
            runtime_self_block_arguments: None,
            runtime_self_block_prepare: None,
            runtime_deferred_loop: None,
            runtime_prepare_await: None,
            unpack_mismatch_handler: None,
            unpack_iterator_error_handler: None,
            runtime_unpack_handler: None,
            preserve_test_results: false,
            loop_else_on_completed_iteration: false,
            respect_include_context: false,
            output_preparer: None,
            prepared_formatter: None,
            #[cfg(feature = "debug")]
            debug: cfg!(debug_assertions),
            #[cfg(feature = "fuel")]
            fuel: None,
            recursion_limit: MAX_RECURSION,
        }
    }

    /// Creates a completely empty environment.
    ///
    /// This environment has no filters, no templates, no globals and no default
    /// logic for auto escaping configured.
    pub fn empty() -> Environment<'source> {
        Environment {
            templates: TemplateStore::new(TemplateConfig::new(no_auto_escape_callback())),
            filters: Default::default(),
            tests: Default::default(),
            globals: Default::default(),
            path_join_callback: None,
            unknown_method_callback: None,
            undefined_behavior: UndefinedBehavior::default(),
            formatter: default_formatter(),
            formatter_is_default: true,
            runtime_container_factory: None,
            runtime_context_handler: None,
            runtime_prepared_lookup: None,
            runtime_prepared_use: None,
            runtime_root_missing: None,
            runtime_import_missing: None,
            runtime_error_observer: None,
            runtime_error_frame_observer: None,
            runtime_macro_call: None,
            runtime_macro_factory: None,
            runtime_operator: None,
            string_format_operator: None,
            runtime_loop_changed: None,
            runtime_loop_changed_with_state: None,
            runtime_loop_changed_scope: None,
            runtime_loop_arguments: None,
            runtime_loop_recursion: None,
            runtime_loop_advance: None,
            runtime_loop_iterator: None,
            runtime_loop_next: None,
            runtime_filtered_loop_length: None,
            runtime_filtered_loop_length_with_input: None,
            runtime_constant_output_guard: None,
            runtime_capture_wrapper: None,
            runtime_loop_post_lookup: None,
            runtime_loop_handoff: None,
            runtime_loop_constructed: None,
            runtime_module_created: None,
            runtime_template_not_found: None,
            runtime_host_error: None,
            runtime_include_foreign: None,
            runtime_blocks_loaded: None,
            runtime_loop_item_key: None,
            runtime_nonfinite_constant: None,
            runtime_subscript_tuple: None,
            runtime_unique_constant: None,
            runtime_prepare_call: None,
            runtime_prepare_attribute: None,
            runtime_attribute: None,
            runtime_release_attribute: None,
            runtime_prepare_item: None,
            runtime_item: None,
            runtime_release_item: None,
            runtime_concat: None,
            fragment_joiner: None,
            runtime_await: None,
            runtime_deferred_super: None,
            runtime_block_dispatch: None,
            runtime_required_block_check: None,
            runtime_self_block_guard: None,
            runtime_self_block_arguments: None,
            runtime_self_block_prepare: None,
            runtime_deferred_loop: None,
            runtime_prepare_await: None,
            unpack_mismatch_handler: None,
            unpack_iterator_error_handler: None,
            runtime_unpack_handler: None,
            preserve_test_results: false,
            loop_else_on_completed_iteration: false,
            respect_include_context: false,
            output_preparer: None,
            prepared_formatter: None,
            #[cfg(feature = "debug")]
            debug: cfg!(debug_assertions),
            #[cfg(feature = "fuel")]
            fuel: None,
            recursion_limit: MAX_RECURSION,
        }
    }

    /// Loads a template from a string into the environment.
    ///
    /// The `name` parameter defines the name of the template which identifies
    /// it.  To look up a loaded template use the [`get_template`](Self::get_template)
    /// method.
    ///
    /// ```
    /// # use minijinja::Environment;
    /// let mut env = Environment::new();
    /// env.add_template("index.html", "Hello {{ name }}!").unwrap();
    /// ```
    /// This method fails if the template has a syntax error.
    ///
    /// Note that there are situations where the interface of this method is
    /// too restrictive as you need to hold on to the strings for the lifetime
    /// of the environment. To avoid this restriction use
    /// [`add_template_owned`](Self::add_template_owned).
    pub fn add_template(&mut self, name: &'source str, source: &'source str) -> Result<(), Error> {
        self.templates.insert(name, source)
    }

    /// Adds a template without borrowing.
    ///
    /// This lets you place an owned [`String`] in the environment rather than the
    /// borrowed `&str` without having to worry about lifetimes.
    ///
    /// ```
    /// # use minijinja::Environment;
    /// let mut env = Environment::new();
    /// env.add_template_owned("index.html".to_string(), "Hello {{ name }}!".to_string()).unwrap();
    /// ```
    ///
    /// **Note**: the name is a bit of a misnomer as this API also allows borrowing,
    /// as the parameters are actually [`Cow`].  This method fails if the template
    /// has a syntax error.
    pub fn add_template_owned<N, S>(&mut self, name: N, source: S) -> Result<(), Error>
    where
        N: Into<Cow<'source, str>>,
        S: Into<Cow<'source, str>>,
    {
        self.templates.insert_cow(name.into(), source.into())
    }

    /// Register a template loader as source of templates.
    ///
    /// When a template loader is registered, the environment gains the ability
    /// to dynamically load templates.  The loader is invoked with the name of
    /// the template.  If this template exists `Ok(Some(template_source))` has
    /// to be returned, otherwise `Ok(None)`.  Once a template has been loaded
    /// it's stored on the environment.  This means the loader is only invoked
    /// once per template name.
    ///
    /// For loading templates from the file system, you can use the
    /// [`path_loader`](crate::path_loader) function.
    ///
    /// # Example
    ///
    /// ```rust
    /// # use minijinja::Environment;
    /// fn create_env() -> Environment<'static> {
    ///     let mut env = Environment::new();
    ///     env.set_loader(|name| {
    ///         if name == "layout.html" {
    ///             Ok(Some("...".into()))
    ///         } else {
    ///             Ok(None)
    ///         }
    ///     });
    ///     env
    /// }
    /// ```
    pub fn set_loader<F>(&mut self, f: F)
    where
        F: Fn(&str) -> Result<Option<String>, Error> + Send + Sync + 'static,
    {
        self.templates.set_loader(f);
    }

    /// Preserve the trailing newline when rendering templates.
    ///
    /// The default is `false`, which causes a single newline, if present, to be
    /// stripped from the end of the template.
    ///
    /// This setting is used whenever a template is loaded into the environment.
    /// Changing it at a later point only affects future templates loaded.
    pub fn set_keep_trailing_newline(&mut self, yes: bool) {
        self.templates
            .template_config
            .ws_config
            .keep_trailing_newline = yes;
    }

    /// Returns the value of the trailing newline preservation flag.
    pub fn keep_trailing_newline(&self) -> bool {
        self.templates
            .template_config
            .ws_config
            .keep_trailing_newline
    }

    /// Remove the first newline after a block.
    ///
    /// If this is set to `true` then the first newline after a block is removed
    /// (block, not variable tag!). Defaults to `false`.
    pub fn set_trim_blocks(&mut self, yes: bool) {
        self.templates.template_config.ws_config.trim_blocks = yes;
    }

    /// Returns the value of the trim blocks flag.
    pub fn trim_blocks(&self) -> bool {
        self.templates.template_config.ws_config.trim_blocks
    }

    /// Remove leading spaces and tabs from the start of a line to a block.
    ///
    /// If this is set to `true` then leading spaces and tabs from the start of a line
    /// to the block tag are removed.
    pub fn set_lstrip_blocks(&mut self, yes: bool) {
        self.templates.template_config.ws_config.lstrip_blocks = yes;
    }

    /// Returns the value of the lstrip blocks flag.
    pub fn lstrip_blocks(&self) -> bool {
        self.templates.template_config.ws_config.lstrip_blocks
    }

    /// Removes a template by name.
    pub fn remove_template(&mut self, name: &str) {
        self.templates.remove(name);
    }

    /// Sets a callback to join template paths.
    ///
    /// By default this returns the template path unchanged, but it can be used
    /// to implement relative path resolution between templates.  The first
    /// argument to the callback is the name of the template to be loaded, the
    /// second argument is the parent path.
    ///
    /// The following example demonstrates how a basic path joining algorithm
    /// can be implemented.
    ///
    /// ```
    /// # let mut env = minijinja::Environment::new();
    /// env.set_path_join_callback(|name, parent| {
    ///     let mut rv = parent.split('/').collect::<Vec<_>>();
    ///     rv.pop();
    ///     name.split('/').for_each(|segment| match segment {
    ///         "." => {}
    ///         ".." => { rv.pop(); }
    ///         _ => { rv.push(segment); }
    ///     });
    ///     rv.join("/").into()
    /// });
    /// ```
    pub fn set_path_join_callback<F>(&mut self, f: F)
    where
        F: for<'s> Fn(&'s str, &'s str) -> Cow<'s, str> + Send + Sync + 'static,
    {
        self.path_join_callback = Some(Arc::new(f));
    }

    /// Sets a callback invoked for unknown methods on objects.
    ///
    /// This registers a function with the environment that is invoked when invoking a method
    /// on a value results in a [`UnknownMethod`](crate::ErrorKind::UnknownMethod) error.
    /// In that case the callback is invoked with [`State`], the [`Value`], the name of
    /// the method as `&str` as well as all arguments in a slice.
    ///
    /// This for instance implements a `.items()` method that invokes the `|items` filter:
    ///
    /// ```rust
    /// use minijinja::value::{ValueKind, from_args};
    /// use minijinja::{Error, ErrorKind};
    /// # let mut env = minijinja::Environment::new();
    ///
    /// env.set_unknown_method_callback(|state, value, method, args| {
    ///     if value.kind() == ValueKind::Map && method == "items" {
    ///         let _: () = from_args(args)?;
    ///         state.apply_filter("items", &[value.clone()])
    ///     } else {
    ///         Err(Error::from(ErrorKind::UnknownMethod))
    ///     }
    /// });
    /// ```
    ///
    /// This can be used to increase the compatibility with Jinja2 templates that might
    /// call Python methods on objects which are not available in minijinja.  A range of
    /// common Python methods is implemented in `minijinja-contrib`.  For more information
    /// see [minijinja_contrib::pycompat](https://docs.rs/minijinja-contrib/latest/minijinja_contrib/pycompat/).
    pub fn set_unknown_method_callback<F>(&mut self, f: F)
    where
        F: Fn(&State, &Value, &str, &[Value]) -> Result<Value, Error> + Sync + Send + 'static,
    {
        self.unknown_method_callback = Some(Arc::new(f));
    }

    /// Removes all stored templates.
    ///
    /// This method is mainly useful when combined with a loader as it causes
    /// the loader to "reload" templates.  By calling this method one can trigger
    /// a reload.
    pub fn clear_templates(&mut self) {
        self.templates.clear();
    }

    /// Returns an iterator over the already loaded templates and their names.
    ///
    /// Only templates that are already loaded will be returned.
    ///
    /// ```
    /// # use minijinja::{Environment, context};
    /// let mut env = Environment::new();
    /// env.add_template("hello.txt", "Hello {{ name }}!").unwrap();
    /// env.add_template("goodbye.txt", "Goodbye {{ name }}!").unwrap();
    ///
    /// # assert_eq!(env.templates().count(), 2);
    /// for (name, tmpl) in env.templates() {
    ///     println!("{}", tmpl.render(context!{ name => "World" }).unwrap());
    /// }
    /// ```
    pub fn templates(&self) -> impl Iterator<Item = (&str, Template<'_, '_>)> {
        self.templates.iter().map(|(name, template)| {
            let template = Template::new(self, template);
            (name, template)
        })
    }

    /// Fetches a template by name.
    ///
    /// This requires that the template has been loaded with
    /// [`add_template`](Environment::add_template) beforehand.  If the template was
    /// not loaded an error of kind `TemplateNotFound` is returned.  If a loader was
    /// added to the engine this can also dynamically load templates.
    ///
    /// ```
    /// # use minijinja::{Environment, context};
    /// let mut env = Environment::new();
    /// env.add_template("hello.txt", "Hello {{ name }}!").unwrap();
    /// let tmpl = env.get_template("hello.txt").unwrap();
    /// println!("{}", tmpl.render(context!{ name => "World" }).unwrap());
    /// ```
    pub fn get_template(&self, name: &str) -> Result<Template<'_, '_>, Error> {
        let compiled = self.templates.get_handle(name)?;
        Ok(Template::new(self, compiled))
    }

    /// Compile a named source without inserting it into the loader cache.
    pub fn compile_owned_template(&self, name: String, source: String) -> Result<OwnedTemplate, Error> {
        crate::loader::LoadedTemplate::compile(name.into(), source, &self.templates.template_config, false)
            .map(OwnedTemplate)
    }

    /// Choose whether every filtered loop that uses `loop` evaluates its
    /// predicate lazily (the default) or only the simple cases do.
    pub fn set_lazy_filtered_predicates(&mut self, enabled: bool) {
        self.templates.template_config.lazy_filtered_predicates = enabled;
    }

    /// Share dynamically loaded templates across clones of this environment.
    pub fn enable_shared_loaded_cache(&mut self) {
        self.templates.enable_shared_loaded_cache();
    }

    /// Decide, on each reuse, whether a template the loader cache holds is
    /// still valid; `false` loads it again.
    pub fn set_loaded_template_validator<F>(&mut self, validator: F)
    where F: Fn(&str) -> Result<bool, Error> + Send + Sync + 'static {
        self.templates.set_loaded_validator(Arc::new(validator));
    }

    pub(crate) fn cached_module(&self, template: &Arc<crate::loader::LoadedTemplate>) -> Option<(Value, isize)> {
        self.templates.cached_module(template)
    }

    pub(crate) fn store_module(&self, template: &Arc<crate::loader::LoadedTemplate>, module: Value, state: isize) {
        self.templates.store_module(template, module, state)
    }

    pub(crate) fn hands_out_owned_templates(&self) -> bool {
        self.templates.hands_out_owned_templates()
    }

    /// Bind an owned compilation to its originating environment for rendering.
    pub fn template_from_owned(&self, template: &OwnedTemplate) -> Template<'_, '_> {
        Template::new(self, CompiledTemplateRef::Adapted(template.0.clone()))
    }

    /// Loads a template from a string.
    ///
    /// In some cases you only need to work with (e.g., render) a template once.
    ///
    /// ```
    /// # use minijinja::{Environment, context};
    /// let env = Environment::new();
    /// let tmpl = env.template_from_named_str("template_name", "Hello {{ name }}").unwrap();
    /// let rv = tmpl.render(context! { name => "World" });
    /// println!("{}", rv.unwrap());
    /// ```
    pub fn template_from_named_str(
        &self,
        name: &'source str,
        source: &'source str,
    ) -> Result<Template<'_, 'source>, Error> {
        if self.templates.template_config.ast_adapter.is_some()
            || self.templates.template_config.rewrites_source(source) {
            return Ok(Template::new(self, CompiledTemplateRef::Adapted(
                crate::loader::LoadedTemplate::compile(name.into(), source.to_owned(), &self.templates.template_config, false)?)));
        }
        Ok(Template::new(
            self,
            CompiledTemplateRef::Owned(Arc::new(ok!(CompiledTemplate::new(
                name,
                source,
                &self.templates.template_config,
            )))),
        ))
    }

    /// Loads a template from a string, with name `<string>`.
    ///
    /// This is a shortcut to [`template_from_named_str`](Self::template_from_named_str)
    /// with name set to `<string>`.
    pub fn template_from_str(&self, source: &'source str) -> Result<Template<'_, 'source>, Error> {
        self.template_from_named_str("<string>", source)
    }

    /// Parses and renders a template from a string in one go with name.
    ///
    /// Like [`render_str`](Self::render_str), but provide a name for the
    /// template to be used instead of the default `<string>`.  This is an
    /// alias for [`template_from_named_str`](Self::template_from_named_str) paired with
    /// [`render`](Template::render).
    ///
    /// ```
    /// # use minijinja::{Environment, context};
    /// let env = Environment::new();
    /// let rv = env.render_named_str(
    ///     "template_name",
    ///     "Hello {{ name }}",
    ///     context!{ name => "World" }
    /// );
    /// println!("{}", rv.unwrap());
    /// ```
    ///
    /// **Note on values:** The [`Value`] type implements `Serialize` and can be
    /// efficiently passed to render.  It does not undergo actual serialization.
    pub fn render_named_str<S: Serialize>(
        &self,
        name: &str,
        source: &str,
        ctx: S,
    ) -> Result<String, Error> {
        ok!(self.template_from_named_str(name, source)).render(ctx)
    }

    /// Parses and renders a template from a string in one go.
    ///
    /// In some cases you really only need a template to be rendered once from
    /// a string and returned.  The internal name of the template is `<string>`.
    ///
    /// This is an alias for [`template_from_str`](Self::template_from_str) paired with
    /// [`render`](Template::render).
    ///
    /// **Note on values:** The [`Value`] type implements `Serialize` and can be
    /// efficiently passed to render.  It does not undergo actual serialization.
    pub fn render_str<S: Serialize>(&self, source: &str, ctx: S) -> Result<String, Error> {
        // reduce total amount of code falling under mono morphization into
        // this function, and share the rest in _eval.
        ok!(self.template_from_str(source)).render(ctx)
    }

    /// Sets a new function to select the default auto escaping.
    ///
    /// This function is invoked when templates are loaded into the environment
    /// to determine the default auto escaping behavior.  The function is
    /// invoked with the name of the template and can make an initial auto
    /// escaping decision based on that.  The default implementation
    /// ([`default_auto_escape_callback`](defaults::default_auto_escape_callback))
    /// turns on escaping depending on the file extension.
    ///
    /// ```
    /// # use minijinja::{Environment, AutoEscape};
    /// # let mut env = Environment::new();
    /// env.set_auto_escape_callback(|name| {
    ///     if matches!(name.rsplit('.').next().unwrap_or(""), "html" | "htm" | "aspx") {
    ///         AutoEscape::Html
    ///     } else {
    ///         AutoEscape::None
    ///     }
    /// });
    /// ```
    pub fn set_auto_escape_callback<F>(&mut self, f: F)
    where
        F: Fn(&str) -> AutoEscape + 'static + Sync + Send,
    {
        self.templates.template_config.default_auto_escape = Arc::new(f);
    }

    /// Changes the undefined behavior.
    ///
    /// This changes the runtime behavior of [`undefined`](Value::UNDEFINED) values in
    /// the template engine.  For more information see [`UndefinedBehavior`].  The
    /// default is [`UndefinedBehavior::Lenient`].
    pub fn set_undefined_behavior(&mut self, behavior: UndefinedBehavior) {
        self.undefined_behavior = behavior;
    }

    /// Returns the current undefined behavior.
    ///
    /// This is particularly useful if a filter function or similar wants to change its
    /// behavior with regards to undefined values.
    #[inline(always)]
    pub fn undefined_behavior(&self) -> UndefinedBehavior {
        self.undefined_behavior
    }

    /// Sets a different formatter function.
    ///
    /// The formatter is invoked to format the given value into the provided
    /// [`Output`].  The default implementation is
    /// [`escape_formatter`](defaults::escape_formatter).
    ///
    /// When implementing a custom formatter it depends on if auto escaping
    /// should be supported or not.  If auto escaping should be supported then
    /// it's easiest to just wrap the default formatter.  The
    /// following example swaps out `None` values before rendering for
    /// `Undefined` which renders as an empty string instead.
    ///
    /// The current value of the auto escape flag can be retrieved directly
    /// from the [`State`].
    ///
    /// ```
    /// # use minijinja::Environment;
    /// # let mut env = Environment::new();
    /// use minijinja::escape_formatter;
    /// use minijinja::value::Value;
    ///
    /// env.set_formatter(|out, state, value| {
    ///     escape_formatter(
    ///         out,
    ///         state,
    ///         if value.is_none() {
    ///             &Value::UNDEFINED
    ///         } else {
    ///             value
    ///         },
    ///     )
    ///});
    /// # assert_eq!(env.render_str("{{ none }}", ()).unwrap(), "");
    /// ```
    pub fn set_formatter<F>(&mut self, f: F)
    where
        F: Fn(&mut Output, &State, &Value) -> Result<(), Error> + 'static + Sync + Send,
    {
        self.formatter = Arc::new(f);
        self.formatter_is_default = false;
    }

    /// Enable or disable the debug mode.
    ///
    /// When the debug mode is enabled the engine will dump out some of the
    /// execution state together with the source information of the executing
    /// template when an error is created.  The cost of this is relatively
    /// high as the data including the template source is cloned.
    ///
    /// When this is enabled templates will print debug information with source
    /// context when the error is printed.
    ///
    /// This requires the `debug` feature.  This is enabled by default if
    /// debug assertions are enabled and false otherwise.
    #[cfg(feature = "debug")]
    #[cfg_attr(docsrs, doc(cfg(feature = "debug")))]
    pub fn set_debug(&mut self, enabled: bool) {
        self.debug = enabled;
    }

    /// Returns the current value of the debug flag.
    #[cfg(feature = "debug")]
    pub fn debug(&self) -> bool {
        self.debug
    }

    /// Sets the optional fuel of the engine.
    ///
    /// When MiniJinja is compiled with the `fuel` feature then every
    /// instruction consumes a certain amount of fuel.  Usually `1`, some will
    /// consume no fuel.  By default the engine has the fuel feature disabled
    /// (`None`).  To turn on fuel set something like `Some(50000)` which will
    /// allow 50.000 instructions to execute before running out of fuel.
    ///
    /// To find out how much fuel is consumed, you can access the fuel levels
    /// from the [`State`](crate::State).
    ///
    /// Fuel consumed per-render.
    #[cfg(feature = "fuel")]
    #[cfg_attr(docsrs, doc(cfg(feature = "fuel")))]
    pub fn set_fuel(&mut self, fuel: Option<u64>) {
        self.fuel = fuel;
    }

    /// Returns the configured fuel.
    #[cfg(feature = "fuel")]
    #[cfg_attr(docsrs, doc(cfg(feature = "fuel")))]
    pub fn fuel(&self) -> Option<u64> {
        self.fuel
    }

    /// Sets the syntax for the environment.
    ///
    /// This setting is used whenever a template is loaded into the environment.
    /// Changing it at a later point only affects future templates loaded.
    ///
    /// See [`SyntaxConfig`](crate::syntax::SyntaxConfig) for more information.
    #[cfg(feature = "custom_syntax")]
    #[cfg_attr(docsrs, doc(cfg(feature = "custom_syntax")))]
    pub fn set_syntax(&mut self, syntax: crate::syntax::SyntaxConfig) {
        self.templates.template_config.syntax_config = syntax;
    }

    /// Returns the current syntax config.
    #[cfg(feature = "custom_syntax")]
    #[cfg_attr(docsrs, doc(cfg(feature = "custom_syntax")))]
    pub fn syntax(&self) -> &crate::syntax::SyntaxConfig {
        &self.templates.template_config.syntax_config
    }

    /// Reconfigures the runtime recursion limit.
    ///
    /// This defaults to `500`.  Raising it above that level requires the `stacker`
    /// feature to be enabled.  Otherwise the limit is silently capped at that safe
    /// maximum.  Note that the maximum is not necessarily safe if the thread uses
    /// a lot of stack space already, it's just a value that was validated once to
    /// provide basic protection.
    ///
    /// Every operation that requires recursion in MiniJinja increments an internal
    /// recursion counter.  The actual cost attributed to that recursion depends on
    /// the cost of the operation.  If statements and for loops for instance only
    /// increase the counter by 1, whereas template includes and macros might increase
    /// it to 10 or more.
    ///
    /// **Note on stack growth:** even if the stacker feature is enabled it does not
    /// mean that in all cases stack can grow to the limits desired.  For instance in
    /// WASM the maximum limits are additionally enforced by the runtime.
    pub fn set_recursion_limit(&mut self, level: usize) {
        #[cfg(not(feature = "stacker"))]
        {
            self.recursion_limit = level.min(MAX_RECURSION);
        }
        #[cfg(feature = "stacker")]
        {
            self.recursion_limit = level;
        }
    }

    /// Returns the current max recursion limit.
    pub fn recursion_limit(&self) -> usize {
        self.recursion_limit
    }

    /// Compiles an expression.
    ///
    /// This lets you compile an expression in the template language and evaluate it.
    /// This makes it possible to use the language's expressions as a minimal
    /// scripting language.  For more information and an
    /// example see [`Expression`].
    pub fn compile_expression(&self, expr: &'source str) -> Result<Expression<'_, 'source>, Error> {
        self._compile_expression(expr)
            .map(|instr| Expression::new(self, instr))
    }

    /// Compiles an expression without capturing the lifetime.
    ///
    /// This works exactly like [`compile_expression`](Self::compile_expression) but
    /// lets you pass an owned string without capturing the lifetime.
    pub fn compile_expression_owned<E>(&self, expr: E) -> Result<Expression<'_, 'source>, Error>
    where
        E: Into<Cow<'source, str>>,
    {
        crate::loader::OwnedInstructions::try_new(Box::from(expr.into()), |expr| {
            self._compile_expression(expr)
        })
        .map(|instr| Expression::new_owned(self, instr))
    }

    fn _compile_expression<'expr>(&self, expr: &'expr str) -> Result<Instructions<'expr>, Error> {
        attach_basic_debug_info(
            parse_expr(expr).and_then(|ast| {
                let mut g = CodeGenerator::new("<expression>", expr);
                g.set_native_optimizations(self.templates.template_config.native_optimizations);
                g.set_prepare_attribute(self.templates.template_config.prepare_attribute);
                g.set_prepare_item(self.templates.template_config.prepare_item);
                g.set_prepare_await(self.templates.template_config.prepare_await);
                g.set_publish_locals(self.templates.template_config.publish_locals);
                g.set_operator_call_factory(self.templates.template_config.operator_call_factory.clone());
                g.compile_expr(&ast);
                if let Some(error) = g.take_error() { return Err(error); }
                Ok(g.finish().0)
            }),
            expr,
        )
    }

    /// Adds a new filter function.
    ///
    /// Filter functions are functions that can be applied to values in
    /// templates.  For details about filters have a look at
    /// [`filters`](crate::filters).
    pub fn add_filter<N, F, Rv, Args>(&mut self, name: N, f: F)
    where
        N: Into<Cow<'source, str>>,
        F: functions::Function<Rv, Args>,
        Rv: FunctionResult,
        Args: for<'a> FunctionArgs<'a>,
    {
        Arc::make_mut(&mut self.filters).insert(name.into(), Value::from_function(f));
    }

    /// Removes a filter by name.
    pub fn remove_filter(&mut self, name: &str) {
        Arc::make_mut(&mut self.filters).remove(name);
    }

    /// Adds a new test function.
    ///
    /// Test functions are similar to filters but perform a check on a value
    /// where the return value is always true or false.  For details about tests
    /// have a look at [`tests`](crate::tests).
    pub fn add_test<N, F, Rv, Args>(&mut self, name: N, f: F)
    where
        N: Into<Cow<'source, str>>,
        F: functions::Function<Rv, Args>,
        Rv: FunctionResult,
        Args: for<'a> FunctionArgs<'a>,
    {
        Arc::make_mut(&mut self.tests).insert(name.into(), Value::from_function(f));
    }

    /// Removes a test by name.
    pub fn remove_test(&mut self, name: &str) {
        Arc::make_mut(&mut self.tests).remove(name);
    }

    /// Adds a new global function.
    ///
    /// For details about functions have a look at [`functions`].  Note that
    /// functions and other global variables share the same namespace.
    /// For more details about functions have a look at
    /// [`Function`](crate::functions::Function).
    ///
    /// This is a shortcut for calling [`add_global`](Self::add_global) with the
    /// function wrapped with [`Value::from_function`].
    pub fn add_function<N, F, Rv, Args>(&mut self, name: N, f: F)
    where
        N: Into<Cow<'source, str>>,
        F: functions::Function<Rv, Args>,
        Rv: FunctionResult,
        Args: for<'a> FunctionArgs<'a>,
    {
        self.add_global(name.into(), Value::from_function(f))
    }

    /// Adds a global variable.
    pub fn add_global<N, V>(&mut self, name: N, value: V)
    where
        N: Into<Cow<'source, str>>,
        V: Into<Value>,
    {
        Arc::make_mut(&mut self.globals).insert(name.into(), value.into());
    }

    /// Removes a global function or variable by name.
    pub fn remove_global(&mut self, name: &str) {
        Arc::make_mut(&mut self.globals).remove(name);
    }

    /// Returns an iterator of all global variables.
    pub fn globals(&self) -> impl Iterator<Item = (&str, Value)> {
        self.globals
            .iter()
            .map(|(key, value)| (key as &str, value.clone()))
    }

    /// Returns an empty [`State`] for testing purposes and similar.
    pub fn empty_state(&self) -> State<'_, '_> {
        State::new_for_env(self)
    }

    /// Looks up a global.
    pub(crate) fn get_global(&self, name: &str) -> Option<Value> {
        self.globals.get(name).cloned()
    }

    /// Looks up a filter.
    pub(crate) fn get_filter(&self, name: &str) -> Option<&Value> {
        self.filters.get(name)
    }

    /// Looks up a test function.
    pub(crate) fn get_test(&self, name: &str) -> Option<&Value> {
        self.tests.get(name)
    }

    pub(crate) fn initial_auto_escape(&self, name: &str) -> AutoEscape {
        (self.templates.template_config.default_auto_escape)(name)
    }

    #[inline(always)]
    pub(crate) fn is_default_formatter(&self) -> bool {
        self.formatter_is_default
    }

    /// Formats a value into the final format.
    ///
    /// This step is called finalization in Jinja2 but since we are writing into
    /// the output stream rather than converting values, it's renamed to format
    /// here.
    pub(crate) fn format(
        &self,
        value: &Value,
        state: &State,
        out: &mut Output,
    ) -> Result<(), Error> {
        match (self.undefined_behavior, &value.0) {
            // this intentionally does not check for SilentUndefined.  SilentUndefined is
            // exclusively used in the missing else condition of an if expression to match
            // Jinja2 behavior.  Those go straight to the formatter.
            (
                UndefinedBehavior::Strict | UndefinedBehavior::SemiStrict,
                &ValueRepr::Undefined(UndefinedType::Default),
            ) => Err(Error::from(ErrorKind::UndefinedError)),
            _ => {
                if self.formatter_is_default {
                    write_escaped(out, state.auto_escape(), value)
                } else {
                    (self.formatter)(out, state, value)
                }
            }
        }
    }

    /// Performs a template path join.
    pub(crate) fn join_template_path<'s>(&self, name: &'s str, parent: &'s str) -> Cow<'s, str> {
        match self.path_join_callback {
            Some(ref cb) => cb(name, parent),
            None => Cow::Borrowed(name),
        }
    }
}

use crate::loader::LoaderStore as TemplateStore;

#[cfg(test)]
mod native_operator_intercept_tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn compiled_constant_guard_runs_only_when_emitted() {
        let calls = Arc::new(AtomicUsize::new(0));
        let observed = calls.clone();
        let mut env = Environment::new();
        env.set_constant_output(|_, _, _, _, _| Ok(Some("\u{1}marker\u{1}".to_owned())));
        env.set_runtime_constant_output_guard(move |_| {
            observed.fetch_add(1, Ordering::Relaxed);
            Err(Error::new(ErrorKind::InvalidOperation, "reserved output"))
        });
        let template = env.template_from_str("{% if enabled %}{{ 'x' }}{% endif %}ready").unwrap();
        assert_eq!(template.render(crate::context!(enabled => false)).unwrap(), "ready");
        assert_eq!(calls.load(Ordering::Relaxed), 0);
        assert!(template.render(crate::context!(enabled => true)).is_err());
        assert_eq!(calls.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn raw_output_guard_runs_only_when_emitted() {
        let calls = Arc::new(AtomicUsize::new(0));
        let observed = calls.clone();
        let mut env = Environment::new();
        env.set_runtime_constant_output_guard(move |_| {
            observed.fetch_add(1, Ordering::Relaxed);
            Err(Error::new(ErrorKind::InvalidOperation, "reserved output"))
        });
        let template = env.template_from_str("{% if enabled %}\u{1}marker\u{1}{% endif %}ready").unwrap();
        assert_eq!(template.render(crate::context!(enabled => false)).unwrap(), "ready");
        assert_eq!(calls.load(Ordering::Relaxed), 0);
        assert!(template.render(crate::context!(enabled => true)).is_err());
        assert_eq!(calls.load(Ordering::Relaxed), 1);
    }

    #[cfg(feature = "loader")]
    #[test]
    fn shared_rust_environment_concurrent_loader_misses() {
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let loader_barrier = barrier.clone();
        let (send, receive) = std::sync::mpsc::channel::<()>();
        let receive = Arc::new(Mutex::new(receive));
        let mut env = Environment::new();
        env.set_loader(move |_| {
            let current = std::thread::current();
            let label = current.name().unwrap().to_owned();
            loader_barrier.wait();
            if label == "A" { receive.lock().unwrap().recv().unwrap(); }
            Ok(Some(label))
        });
        env.enable_shared_loaded_cache();
        let shared = Arc::new(env);
        let first_env = shared.clone();
        let first = std::thread::Builder::new().name("A".into()).spawn(move || {
            first_env.get_template("part").unwrap().render(()).unwrap()
        }).unwrap();
        let second_env = shared.clone();
        let second = std::thread::Builder::new().name("B".into()).spawn(move || {
            let output = second_env.get_template("part").unwrap().render(()).unwrap();
            send.send(()).unwrap();
            output
        }).unwrap();
        assert_eq!(first.join().unwrap(), "A");
        assert_eq!(second.join().unwrap(), "B");
    }

    #[cfg(feature = "loader")]
    #[test]
    fn captured_import_keeps_replaced_template_alive() {
        #[derive(Debug)]
        struct Tracker(Arc<AtomicUsize>);
        impl crate::value::Object for Tracker {}
        impl Drop for Tracker {
            fn drop(&mut self) { self.0.fetch_add(1, Ordering::SeqCst); }
        }

        let dropped = Arc::new(AtomicUsize::new(0));
        let tracker = dropped.clone();
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let loader_barrier = barrier.clone();
        let (b_ready, wait_b) = std::sync::mpsc::channel::<()>();
        let wait_b = Arc::new(Mutex::new(wait_b));
        let (a_ready, wait_a) = std::sync::mpsc::channel::<()>();
        let mut env = Environment::new();
        env.set_template_metadata(move |name| {
            Ok(if name == "part" { Value::from_object(Tracker(tracker.clone())) } else { Value::UNDEFINED })
        });
        env.set_loader(move |_| {
            let current = std::thread::current();
            let label = current.name().unwrap().to_owned();
            loader_barrier.wait();
            if label == "A" { wait_b.lock().unwrap().recv().unwrap(); }
            Ok(Some(format!("{{% macro output() %}}{label}{{% endmacro %}}")))
        });
        env.enable_shared_loaded_cache();
        let env = Arc::new(env);
        let first_env = env.clone();
        let first = std::thread::Builder::new().name("A".into()).spawn(move || {
            let template = first_env.template_from_str("{% from 'part' import output %}").unwrap();
            let captured = template.render_captured(()).unwrap();
            assert_eq!(captured.state().call_macro("output", &[]).unwrap(), "A");
            a_ready.send(()).unwrap();
        }).unwrap();
        let second_env = env.clone();
        let second_dropped = dropped.clone();
        let second = std::thread::Builder::new().name("B".into()).spawn(move || {
            let template = second_env.template_from_str("{% from 'part' import output %}").unwrap();
            let captured = template.render_captured(()).unwrap();
            b_ready.send(()).unwrap();
            wait_a.recv().unwrap();
            assert_eq!(second_dropped.load(Ordering::SeqCst), 0);
            assert_eq!(captured.state().call_macro("output", &[]).unwrap(), "B");
            drop(captured);
            assert_eq!(second_dropped.load(Ordering::SeqCst), 1);
        }).unwrap();
        first.join().unwrap();
        second.join().unwrap();
        drop(env);
        assert_eq!(dropped.load(Ordering::SeqCst), 2);
    }

    #[cfg(feature = "loader")]
    #[test]
    fn retained_parent_blocks_survive_cache_replacement() {
        for capture in [false, true] {
            let barrier = Arc::new(std::sync::Barrier::new(2));
            let loader_barrier = barrier.clone();
            let (b_ready, wait_b) = std::sync::mpsc::channel::<()>();
            let wait_b = Arc::new(Mutex::new(wait_b));
            let (a_ready, wait_a) = std::sync::mpsc::channel::<()>();
            let mut env = Environment::new();
            env.set_loader(move |_| {
                let current = std::thread::current();
                let label = current.name().unwrap().to_owned();
                loader_barrier.wait();
                if label == "A" { wait_b.lock().unwrap().recv().unwrap(); }
                Ok(Some(format!("{{% block item %}}{label}{{% endblock %}}")))
            });
            env.enable_shared_loaded_cache();
            let template = env.template_from_str("{% extends 'part' %}").unwrap();
            std::thread::scope(|scope| {
                let template_ref = &template;
                let first = std::thread::Builder::new().name("A".into()).spawn_scoped(scope, move || {
                    #[allow(deprecated)]
                    let (output, mut state) = template_ref.render_and_return_state(()).unwrap();
                    assert_eq!(output, "A");
                    assert_eq!(state.render_block("item").unwrap(), "A");
                    a_ready.send(()).unwrap();
                }).unwrap();
                let template_ref = &template;
                let second = std::thread::Builder::new().name("B".into()).spawn_scoped(scope, move || {
                    if capture {
                        let mut state = template_ref.render_captured(()).unwrap();
                        assert_eq!(state.output(), "B");
                        b_ready.send(()).unwrap();
                        wait_a.recv().unwrap();
                        assert_eq!(state.with_state_mut(|state| state.render_block("item")).unwrap(), "B");
                    } else {
                        #[allow(deprecated)]
                        let (output, mut state) = template_ref.render_and_return_state(()).unwrap();
                        assert_eq!(output, "B");
                        b_ready.send(()).unwrap();
                        wait_a.recv().unwrap();
                        assert_eq!(state.render_block("item").unwrap(), "B");
                    }
                }).unwrap();
                first.join().unwrap();
                second.join().unwrap();
            });
        }
    }

    #[cfg(feature = "loader")]
    #[test]
    fn repeated_cached_includes_retain_one_compilation() {
        let mut env = Environment::new();
        env.set_loader(|_| Ok(Some("x".to_owned())));
        env.enable_shared_loaded_cache();
        let template = env.template_from_str("{% for x in range(1000) %}{% include 'part' %}{% endfor %}").unwrap();
        let captured = template.render_captured(()).unwrap();
        assert_eq!(captured.output().len(), 1000);
        assert_eq!(captured.state().retained_template_count(), 1);
    }

    #[cfg(feature = "loader")]
    #[test]
    fn repeated_include_observes_completed_cache_replacement() {
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let loader_barrier = barrier.clone();
        let (b_ready, wait_b) = std::sync::mpsc::channel::<()>();
        let wait_b = Arc::new(Mutex::new(wait_b));
        let (a_ready, wait_a) = std::sync::mpsc::channel::<()>();
        let wait_a = Arc::new(Mutex::new(wait_a));
        let mut env = Environment::new();
        env.set_loader(move |_| {
            let current = std::thread::current();
            let label = current.name().unwrap().to_owned();
            loader_barrier.wait();
            if label == "A" { wait_b.lock().unwrap().recv().unwrap(); }
            Ok(Some(label))
        });
        env.add_function("gate", move || {
            if std::thread::current().name() == Some("B") {
                b_ready.send(()).unwrap();
                wait_a.lock().unwrap().recv().unwrap();
            }
            ""
        });
        env.enable_shared_loaded_cache();
        let template = env.template_from_str("{% include 'part' %}{{ gate() }}{% include 'part' %}").unwrap();
        std::thread::scope(|scope| {
            let template_ref = &template;
            let first = std::thread::Builder::new().name("A".into()).spawn_scoped(scope, move || {
                let output = template_ref.render(()).unwrap();
                a_ready.send(()).unwrap();
                output
            }).unwrap();
            let template_ref = &template;
            let second = std::thread::Builder::new().name("B".into()).spawn_scoped(scope, move || {
                template_ref.render(()).unwrap()
            }).unwrap();
            assert_eq!(first.join().unwrap(), "AA");
            assert_eq!(second.join().unwrap(), "BA");
        });
    }

    #[cfg(feature = "loader")]
    #[test]
    fn direct_loaded_handle_survives_replacement_and_releases_its_metadata() {
        #[derive(Debug)]
        struct Tracker(Arc<AtomicUsize>);
        impl crate::value::Object for Tracker {}
        impl Drop for Tracker {
            fn drop(&mut self) { self.0.fetch_add(1, Ordering::SeqCst); }
        }

        let dropped = Arc::new(AtomicUsize::new(0));
        let tracker = dropped.clone();
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let loader_barrier = barrier.clone();
        let (b_ready, wait_b) = std::sync::mpsc::channel::<()>();
        let wait_b = Arc::new(Mutex::new(wait_b));
        let (a_ready, wait_a) = std::sync::mpsc::channel::<()>();
        let mut env = Environment::new();
        env.set_template_metadata(move |_| Ok(Value::from_object(Tracker(tracker.clone()))));
        env.set_loader(move |_| {
            let current = std::thread::current();
            let label = current.name().unwrap().to_owned();
            loader_barrier.wait();
            if label == "A" { wait_b.lock().unwrap().recv().unwrap(); }
            Ok(Some(label))
        });
        env.enable_shared_loaded_cache();
        let env = Arc::new(env);
        let first_env = env.clone();
        let first = std::thread::Builder::new().name("A".into()).spawn(move || {
            let template = first_env.get_template("part").unwrap();
            assert_eq!(template.render(()).unwrap(), "A");
            a_ready.send(()).unwrap();
        }).unwrap();
        let second_env = env.clone();
        let second_dropped = dropped.clone();
        let second = std::thread::Builder::new().name("B".into()).spawn(move || {
            let template = second_env.get_template("part").unwrap();
            b_ready.send(()).unwrap();
            wait_a.recv().unwrap();
            assert_eq!(second_dropped.load(Ordering::SeqCst), 0);
            assert_eq!(template.render(()).unwrap(), "B");
            drop(template);
            assert_eq!(second_dropped.load(Ordering::SeqCst), 1);
        }).unwrap();
        first.join().unwrap();
        second.join().unwrap();
        assert_eq!(env.get_template("part").unwrap().render(()).unwrap(), "A");
        drop(env);
        assert_eq!(dropped.load(Ordering::SeqCst), 2);
    }

    #[cfg(feature = "loader")]
    #[test]
    fn shared_rust_environment_concurrent_includes() {
        for source in ["{% include 'part' %}",
            "{% block body %}{% include 'part' %}{% endblock %}",
            "{% macro part() %}{% include 'part' %}{% endmacro %}{{ part() }}"] {
            let barrier = Arc::new(std::sync::Barrier::new(2));
            let loader_barrier = barrier.clone();
            let (send, receive) = std::sync::mpsc::channel::<()>();
            let receive = Arc::new(Mutex::new(receive));
            let mut env = Environment::new();
            env.set_loader(move |_| {
                let current = std::thread::current();
                let label = current.name().unwrap().to_owned();
                loader_barrier.wait();
                if label == "A" { receive.lock().unwrap().recv().unwrap(); }
                Ok(Some(label))
            });
            env.enable_shared_loaded_cache();
            let shared = Arc::new(env);
            let first_env = shared.clone();
            let first = std::thread::Builder::new().name("A".into()).spawn(move || {
                first_env.template_from_str(source).unwrap().render(()).unwrap()
            }).unwrap();
            let second_env = shared.clone();
            let second = std::thread::Builder::new().name("B".into()).spawn(move || {
                let output = second_env.template_from_str(source).unwrap().render(()).unwrap();
                send.send(()).unwrap();
                output
            }).unwrap();
            assert_eq!(first.join().unwrap(), "A");
            assert_eq!(second.join().unwrap(), "B");
        }
    }

    #[cfg(feature = "loader")]
    #[test]
    fn shared_rust_template_concurrent_includes() {
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let loader_barrier = barrier.clone();
        let (send, receive) = std::sync::mpsc::channel::<()>();
        let receive = Arc::new(Mutex::new(receive));
        let mut env = Environment::new();
        env.set_loader(move |_| {
            let current = std::thread::current();
            let label = current.name().unwrap().to_owned();
            loader_barrier.wait();
            if label == "A" { receive.lock().unwrap().recv().unwrap(); }
            Ok(Some(label))
        });
        env.enable_shared_loaded_cache();
        let template = env.template_from_str("{% include 'part' %}").unwrap();
        std::thread::scope(|scope| {
            let first = std::thread::Builder::new().name("A".into()).spawn_scoped(scope, || {
                template.render(()).unwrap()
            }).unwrap();
            let second = std::thread::Builder::new().name("B".into()).spawn_scoped(scope, || {
                let output = template.render(()).unwrap();
                send.send(()).unwrap();
                output
            }).unwrap();
            assert_eq!(first.join().unwrap(), "A");
            assert_eq!(second.join().unwrap(), "B");
        });
    }

    #[cfg(feature = "loader")]
    #[test]
    fn shared_rust_environment_state_keeps_environment_identity() {
        let mut env = Environment::new();
        env.enable_shared_loaded_cache();
        let captured = env.template_from_str("ok").unwrap().render_captured(()).unwrap();
        assert!(std::ptr::eq(captured.state().env(), &env));
    }

    #[cfg(feature = "loader")]
    #[test]
    fn concurrent_loader_results_remain_distinct_and_old_result_is_released() {
        #[derive(Debug)]
        struct Tracker(Arc<AtomicUsize>);
        impl crate::value::Object for Tracker {}
        impl Drop for Tracker {
            fn drop(&mut self) { self.0.fetch_add(1, Ordering::Relaxed); }
        }

        let dropped = Arc::new(AtomicUsize::new(0));
        let tracker = dropped.clone();
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let loader_barrier = barrier.clone();
        let (send, receive) = std::sync::mpsc::channel::<()>();
        let receive = Arc::new(Mutex::new(receive));
        let mut env = Environment::new();
        env.set_template_metadata(move |_| Ok(Value::from_object(Tracker(tracker.clone()))));
        env.set_loader(move |_| {
            let current = std::thread::current();
            let label = current.name().unwrap().to_owned();
            loader_barrier.wait();
            if label == "A" { receive.lock().unwrap().recv().unwrap(); }
            Ok(Some(label))
        });
        env.enable_shared_loaded_cache();
        let first_env = env.clone();
        let first = std::thread::Builder::new().name("A".into()).spawn(move || {
            first_env.get_template("part").unwrap().render(()).unwrap()
        }).unwrap();
        let second_env = env.clone();
        let second = std::thread::Builder::new().name("B".into()).spawn(move || {
            let output = second_env.get_template("part").unwrap().render(()).unwrap();
            send.send(()).unwrap();
            output
        }).unwrap();
        assert_eq!(first.join().unwrap(), "A");
        assert_eq!(second.join().unwrap(), "B");
        assert_eq!(dropped.load(Ordering::Relaxed), 1);
        let templates = env.templates().map(|(name, template)| (name.to_owned(), template.render(()).unwrap())).collect::<Vec<_>>();
        assert_eq!(templates, [("part".to_owned(), "A".to_owned())]);
        drop(env);
        assert_eq!(dropped.load(Ordering::Relaxed), 2);
    }

    #[cfg(feature = "loader")]
    #[test]
    fn compilation_origin_distinguishes_loader_and_restores_nested_calls() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let recorded = seen.clone();
        let mut env = Environment::new();
        env.set_source_validator(move |name, _| {
            recorded.lock().unwrap().push((name.to_owned(), crate::compiling_loaded_template()));
            Ok(())
        });
        env.set_loader(|_| Ok(Some("loaded".to_owned())));
        env.compile_owned_template("direct".into(), "direct".into()).unwrap();
        env.get_template("loaded").unwrap();
        assert_eq!(*seen.lock().unwrap(), [("direct".into(), false), ("loaded".into(), true)]);
        crate::template::with_compilation_origin(true, || {
            assert!(crate::compiling_loaded_template());
            crate::template::with_compilation_origin(false, || assert!(!crate::compiling_loaded_template()));
            assert!(crate::compiling_loaded_template());
        });
        assert!(!crate::compiling_loaded_template());
    }

    #[cfg(feature = "loader")]
    #[test]
    fn newline_normalization_follows_validation_on_every_compile_path() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let recorded = seen.clone();
        let mut env = Environment::new();
        env.set_normalize_newlines(true);
        env.set_source_validator(move |_, source| { recorded.lock().unwrap().push(source.to_owned()); Ok(()) });
        env.set_loader(|_| Ok(Some("l\r\n{{ 'x\ry' }}\r".to_owned())));
        let source = "a\r\nb\rc{{ 'd\r\ne' }}";
        let owned = env.compile_owned_template("owned".into(), source.into()).unwrap();
        assert_eq!(env.template_from_owned(&owned).render(()).unwrap(), "a\nb\ncd\ne");
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "a\nb\ncd\ne");
        assert_eq!(env.get_template("loaded").unwrap().render(()).unwrap(), "l\nx\ny");
        env.add_template("named", source).unwrap();
        assert_eq!(env.get_template("named").unwrap().render(()).unwrap(), "a\nb\ncd\ne");
        // Validators observe the original source, like Jinja preprocessing.
        assert!(seen.lock().unwrap().iter().all(|text| text.contains('\r')));
        let mut plain = Environment::new();
        plain.add_template("raw", "a\r\nb").unwrap();
        assert_eq!(plain.get_template("raw").unwrap().render(()).unwrap(), "a\r\nb");
    }

    #[cfg(feature = "loader")]
    #[test]
    fn block_names_keep_declaration_order_for_owned_and_named_templates() {
        let source = "{% block zebra %}{% block nested %}N{% endblock %}{% endblock %}{% if false %}{% block alpha %}A{% endblock %}{% endif %}";
        let mut env = Environment::new();
        let owned = env.compile_owned_template("owned".into(), source.into()).unwrap();
        env.add_template("named", source).unwrap();
        for template in [env.template_from_owned(&owned), env.get_template("named").unwrap()] {
            assert_eq!(template.block_names().collect::<Vec<_>>(), ["zebra", "nested", "alpha"]);
        }
    }

    #[cfg(feature = "loader")]
    #[test]
    fn private_compilation_key_preserves_public_template_name() {
        let observed = Arc::new(Mutex::new(Vec::<String>::new()));
        let mut env = Environment::new();
        env.set_compilation_key(|name| format!("{name}:private"));
        let calls = observed.clone();
        env.set_source_validator(move |key, _| { calls.lock().unwrap().push(format!("validate:{key}")); Ok(()) });
        let calls = observed.clone();
        env.set_template_metadata(move |key| { calls.lock().unwrap().push(format!("metadata:{key}")); Ok(Value::UNDEFINED) });
        let calls = observed.clone();
        env.set_constant_output(move |key, _, _, _, _| { calls.lock().unwrap().push(format!("output:{key}")); Ok(None) });
        let calls = observed.clone();
        env.set_compilation_cleanup(move |key| { calls.lock().unwrap().push(format!("cleanup:{key}")); Ok(()) });
        env.add_function("name", |state: &State| state.name().to_owned());
        let template = env.compile_owned_template("visible".into(), "{{ name() }}".into()).unwrap();
        let template = env.template_from_owned(&template);
        assert_eq!(template.name(), "visible");
        assert_eq!(template.render(()).unwrap(), "visible");
        let events = observed.lock().unwrap();
        assert!(events.iter().any(|item| item == "validate:visible:private"));
        assert!(events.iter().any(|item| item == "metadata:visible:private"));
        assert!(events.iter().any(|item| item == "output:visible:private"));
        assert!(events.iter().any(|item| item == "cleanup:visible:private"));
    }

    #[cfg(feature = "loader")]
    #[test]
    fn template_finalizer_runs_after_successful_codegen_only() {
        let events = Arc::new(Mutex::new(Vec::<String>::new()));
        let mut env = Environment::new();
        env.set_template_metadata(|_| Ok(Value::from(42)));
        let seen = events.clone();
        env.set_template_prelude(move |metadata| {
            assert_eq!(metadata.and_then(Value::as_i64), Some(42));
            seen.lock().unwrap().push("prelude".into());
            Ok(())
        });
        let seen = events.clone();
        env.set_constant_output(move |_, _, _, _, _| { seen.lock().unwrap().push("compile".into()); Ok(None) });
        let seen = events.clone();
        env.set_template_finalizer(move |metadata| {
            assert_eq!(metadata.and_then(Value::as_i64), Some(42));
            seen.lock().unwrap().push("finalize".into());
            Ok(())
        });
        env.template_from_str("{{ 'x' }}").unwrap();
        assert_eq!(*events.lock().unwrap(), ["prelude", "compile", "finalize"]);
        events.lock().unwrap().clear();
        env.compile_owned_template("named".into(), "{{ 'x' }}".into()).unwrap();
        assert_eq!(*events.lock().unwrap(), ["prelude", "compile", "finalize"]);
        events.lock().unwrap().clear();
        assert!(env.template_from_str("{% if %}").is_err());
        assert!(events.lock().unwrap().is_empty());
    }

    #[test]
    fn await_preparation_precedes_nested_operands() {
        let events = Arc::new(Mutex::new(Vec::<String>::new()));
        let mut env = Environment::new();
        let seen = events.clone();
        env.set_runtime_prepare_await(move |_| { seen.lock().unwrap().push("prepare".into()); Ok(()) });
        let seen = events.clone();
        env.add_function("inner", move || { seen.lock().unwrap().push("inner".into()); "x" });
        let seen = events.clone();
        env.add_function("outer", move |value: String| { seen.lock().unwrap().push("outer".into()); value });
        assert_eq!(env.template_from_str("{{ outer(inner()) }}").unwrap().render(()).unwrap(), "x");
        assert_eq!(*events.lock().unwrap(), ["prepare", "prepare", "inner", "outer"]);
    }

    #[test]
    fn await_preparation_failure_prevents_operand_calls() {
        let calls = Arc::new(AtomicUsize::new(0));
        let mut env = Environment::new();
        env.set_runtime_prepare_await(|_| Err(Error::new(ErrorKind::InvalidOperation, "missing await")));
        let seen = calls.clone();
        env.add_function("produce", move || { seen.fetch_add(1, Ordering::Relaxed); "x" });
        let error = env.template_from_str("{{ produce() }}").unwrap().render(()).unwrap_err();
        assert!(error.to_string().contains("missing await"));
        assert_eq!(calls.load(Ordering::Relaxed), 0);
    }

    #[cfg(feature = "loader")]
    #[test]
    fn template_finalizer_failure_still_cleans_compilation_state() {
        let cleanups = Arc::new(AtomicUsize::new(0));
        let seen = cleanups.clone();
        let mut env = Environment::new();
        env.set_compilation_cleanup(move |_| { seen.fetch_add(1, Ordering::Relaxed); Ok(()) });
        env.set_template_finalizer(|_| Err(Error::new(ErrorKind::InvalidOperation, "finalizer failed")));
        let error = env.compile_owned_template("named".into(), "hello".into()).err().unwrap();
        assert!(error.to_string().contains("finalizer failed"));
        assert_eq!(cleanups.load(Ordering::Relaxed), 1);
    }

    #[cfg(feature = "loader")]
    #[test]
    fn template_metadata_tracks_compilation_through_include_and_block() {
        let serial = Arc::new(AtomicUsize::new(0));
        let counter = serial.clone();
        let mut env = Environment::new();
        env.set_template_metadata(move |_| Ok(Value::from(counter.fetch_add(1, Ordering::Relaxed) + 1)));
        env.set_loader(|_| Ok(Some("{{ read() }}".to_owned())));
        env.enable_shared_loaded_cache();
        env.add_function("read", |state: &State| state.template_metadata().cloned().unwrap());
        let source = "{% block body %}{{ read() }}{% include 'part' %}{{ read() }}{% endblock %}";
        let first = env.compile_owned_template("part".into(), source.into()).unwrap();
        assert_eq!(env.template_from_owned(&first).render(()).unwrap(), "121");
        let second = env.compile_owned_template("part".into(), source.into()).unwrap();
        assert_eq!(env.template_from_owned(&second).render(()).unwrap(), "323");
        assert_eq!(env.template_from_owned(&first).render(()).unwrap(), "121");
    }

    #[cfg(feature = "loader")]
    #[test]
    fn owned_source_and_shared_loader_cache_keep_distinct_names() {
        let loads = Arc::new(AtomicUsize::new(0));
        let seen = loads.clone();
        let mut env = Environment::new();
        env.set_loader(move |name| {
            seen.fetch_add(1, Ordering::Relaxed);
            Ok((name == "part").then(|| "P".to_owned()))
        });
        env.enable_shared_loaded_cache();
        let root = env.compile_owned_template("part".into(), "A{% include 'part' %}B".into()).unwrap();
        let clone = env.clone();
        assert_eq!(clone.template_from_owned(&root).render(()).unwrap(), "APB");
        assert_eq!(env.template_from_owned(&root).render(()).unwrap(), "APB");
        assert_eq!(loads.load(Ordering::Relaxed), 1);
        assert!(env.templates().any(|(name, _)| name == "part"));

        env.add_template_owned("part", "local").unwrap();
        assert_eq!(env.get_template("part").unwrap().render(()).unwrap(), "local");
        env.remove_template("part");
        assert_eq!(env.get_template("part").unwrap().render(()).unwrap(), "P");
        assert_eq!(loads.load(Ordering::Relaxed), 2);
        assert_eq!(clone.get_template("part").unwrap().render(()).unwrap(), "P");
        env.clear_templates();
        assert_eq!(env.get_template("part").unwrap().render(()).unwrap(), "P");
        assert_eq!(loads.load(Ordering::Relaxed), 3);
    }

    #[cfg(feature = "loader")]
    #[test]
    fn shared_loader_can_load_another_template_reentrantly() {
        let reference = Arc::new(Mutex::new(std::sync::Weak::<Environment<'static>>::new()));
        let captured = reference.clone();
        let mut env = Environment::new();
        env.set_loader(move |name| {
            if name == "outer" {
                let environment = captured.lock().unwrap().upgrade().unwrap();
                assert_eq!(environment.get_template("inner")?.render(())?, "I");
                Ok(Some("O".to_owned()))
            } else { Ok(Some("I".to_owned())) }
        });
        env.enable_shared_loaded_cache();
        let env = Arc::new(env);
        *reference.lock().unwrap() = Arc::downgrade(&env);
        assert_eq!(env.get_template("outer").unwrap().render(()).unwrap(), "O");
    }

    #[test]
    fn constant_output_receives_complete_expression_byte_ranges() {
        let spans = Arc::new(Mutex::new(Vec::new()));
        let captured = spans.clone();
        let mut env = Environment::new();
        env.set_constant_output(move |_, source, start, end, _| {
            captured.lock().unwrap().push(source[start..end].to_owned());
            Ok(None)
        });
        let source = "é {{ 1 in [1] }} {{ 1 not in [1] }} {{ 1 < 2 < 3 }} {{ 'a' if true else 'b' }} {{ 'x' if false else 'y' if true else 'z' }}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "é True False True a y");
        assert_eq!(*spans.lock().unwrap(), ["1 in [1]", "1 not in [1]", "1 < 2 < 3", "'a' if true else 'b'", "'x' if false else 'y' if true else 'z'"]);
    }

    #[test]
    fn callee_precedes_operands_and_only_ordinary_calls_are_awaited() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let awaits = Arc::new(AtomicUsize::new(0));
        let mut env = Environment::new();
        let calls = trace.clone();
        env.set_operator_call_factory(move |_, token, unary| {
            assert_eq!((token, unary), ("+", false));
            let calls = calls.clone();
            Ok(Some(Value::from_function(move || {
                calls.lock().unwrap().push("prepare");
                let calls = calls.clone();
                Value::from_function(move |left: i64, right: i64| {
                    calls.lock().unwrap().push("apply");
                    left + right
                })
            })))
        });
        for (name, value) in [("left", 2), ("right", 3)] {
            let calls = trace.clone();
            env.add_function(name, move || { calls.lock().unwrap().push(name); value });
        }
        let count = awaits.clone();
        env.set_runtime_await(move |_state, value| { count.fetch_add(1, Ordering::Relaxed); Ok(value) });
        let result = env.template_from_str("{{ left() + right() }}").unwrap().render(()).unwrap();
        assert_eq!(result, "5");
        assert_eq!(*trace.lock().unwrap(), ["prepare", "left", "right", "apply"]);
        assert_eq!(awaits.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn constant_expression_obeys_interception_and_reports_policy_errors() {
        let mut env = Environment::new();
        env.set_operator_call_factory(|_, token, unary| {
            if token == "+" && !unary {
                Ok(Some(Value::from_function(|| Value::from_function(|a: i64, b: i64| a + b + 100))))
            } else {
                Err(Error::new(crate::ErrorKind::InvalidOperation, "operator policy failed"))
            }
        });
        assert_eq!(env.compile_expression("2 + 3").unwrap().eval(()).unwrap().as_i64(), Some(105));
        assert_eq!(env.compile_expression("2 - 3").err().unwrap().kind(), crate::ErrorKind::InvalidOperation);
        assert_eq!(env.template_from_str("{{ 2 - 3 }}").err().unwrap().kind(), crate::ErrorKind::InvalidOperation);
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn block_subgenerators_retain_operator_policy() {
        let mut env = Environment::new();
        env.set_operator_call_factory(|_, token, unary| {
            assert_eq!((token, unary), ("+", false));
            Ok(Some(Value::from_function(|| Value::from_function(|_: i64, _: i64| 42))))
        });
        env.add_template("case", "{% block body %}{{ 2 + 3 }}{% endblock %}").unwrap();
        assert_eq!(env.get_template("case").unwrap().render(()).unwrap(), "42");
    }

    #[test]
    fn unary_sign_wraps_postfix_and_precedes_filters() {
        let mut env = Environment::new();
        env.add_function("value", || 7);
        for (source, expected) in [("+value()", 7), ("-value()", -7), ("--value()", 7), ("-value()|abs", 7)] {
            assert_eq!(env.compile_expression(source).unwrap().eval(()).unwrap().as_i64(), Some(expected));
        }
        let source = format!("{}1", "-".repeat(2000));
        assert!(env.compile_expression(&source).is_err());
    }
}
