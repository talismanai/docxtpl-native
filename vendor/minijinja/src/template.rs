use std::cell::RefCell;
use std::collections::{BTreeMap, HashSet};
use std::sync::Arc;
use std::{fmt, io};

use crate::vendor::self_cell::self_cell;
use serde::Serialize;

use crate::compiler::codegen::CodeGenerator;
use crate::compiler::instructions::Instructions;
use crate::compiler::lexer::WhitespaceConfig;
use crate::compiler::meta::find_undeclared;
use crate::compiler::parser::parse;
use crate::environment::Environment;
use crate::error::{attach_basic_debug_info, Error};
use crate::output::{CaptureMode, Output, WriteWrapper};
use crate::syntax::SyntaxConfig;
use crate::utils::AutoEscape;
use crate::value::Value;
use crate::vm::{prepare_blocks, Context, State, Vm};

thread_local! { static LOADER_COMPILATION: std::cell::Cell<bool> = const { std::cell::Cell::new(false) }; }

// Accessed only through functions that are never inlined: a render that a
// host suspends may resume on another thread, and an activation live across
// that suspension must not reuse a thread-local address computed before it.
#[inline(never)]
fn replace_compilation_origin(loaded: bool) -> bool {
    LOADER_COMPILATION.with(|origin| origin.replace(loaded))
}

struct CompilationOriginGuard(bool);
impl Drop for CompilationOriginGuard {
    fn drop(&mut self) { replace_compilation_origin(self.0); }
}

pub(crate) fn with_compilation_origin<R>(loaded: bool, f: impl FnOnce() -> R) -> R {
    let _guard = CompilationOriginGuard(replace_compilation_origin(loaded));
    f()
}

/// Whether the current compilation was initiated by the template loader.
#[inline(never)]
pub fn compiling_loaded_template() -> bool {
    LOADER_COMPILATION.with(|origin| origin.get())
}

/// Callback for auto escape determination
pub type AutoEscapeFunc = dyn Fn(&str) -> AutoEscape + Sync + Send;

/// Finalized compile-time output, either UTF-8 text or a foreign fragment.
pub enum ConstantOutput {
    /// Ordinary UTF-8 text.
    Text(String),
    /// A value preserved for a fragment-aware output.
    Fragment(Value),
}

/// Compile-time conversion of an output expression to finalized output.
pub type ConstantOutputFunc = dyn Fn(&str, &str, usize, usize, Option<bool>) -> Result<Option<ConstantOutput>, Error> + Send + Sync;

/// An owned syntax tree lowered into the same AST as the native parser.
/// Strings borrowed by the generated instructions live with this tree.
pub trait TemplateAst: Send + Sync {
    /// Borrow this tree's storage while constructing the native compiler AST.
    fn lower(&self) -> Result<crate::compiler::ast::Stmt<'_>, Error>;
}

/// Adapter invoked after source validation, before native compilation.
pub type AstAdapter = dyn Fn(&str, &str) -> Result<Option<Box<dyn TemplateAst>>, Error> + Send + Sync;

/// A folded value, with mutable list literals rebuilt at each evaluation.
pub enum ConstantArgument {
    /// Immutable literal value.
    Value(Value),
    /// List literal whose elements are rebuilt for each evaluation.
    List(Vec<ConstantArgument>),
    /// Tuple literal whose elements are rebuilt for each evaluation.
    Tuple(Vec<ConstantArgument>),
    /// Mapping literal whose entries are rebuilt for each evaluation.
    Map(Vec<(ConstantArgument, ConstantArgument)>),
    /// Native callable that constructs a fresh foreign-runtime literal.
    Factory(Value),
}

/// Optional foreign folding for a literal input to a no-argument filter.
pub type ConstantArgumentFunc = dyn Fn(&str, &str, &Value, Option<bool>) -> Result<Option<ConstantArgument>, Error> + Send + Sync;

/// Evaluate supported literal arithmetic with the foreign numeric runtime.
pub type ConstantArithmeticFunc = dyn Fn(&str, &str, &Value, Option<&Value>, Option<bool>, bool, bool, bool) -> Result<Option<(Value, bool)>, Error> + Send + Sync;

/// Whether foreign arithmetic is folded after source-language code generation.
pub type ArithmeticCodegenMode = dyn Fn(&str) -> bool + Send + Sync;

/// Query whether the foreign optimizer is active and an operator is intercepted.
pub type JinjaArithmeticPolicy = dyn Fn(&str, Option<&str>, bool, Option<bool>) -> Result<Option<bool>, Error> + Send + Sync;

/// Observe a foreign constant-attribute lookup attempt.
pub type JinjaConstAttributeLookup = dyn Fn() -> Result<Option<Value>, Error> + Send + Sync;

/// Release a captured foreign attribute lookup at its source-language boundary.
pub type JinjaConstAttributeRelease = dyn Fn(Value) + Send + Sync;

/// Select an operator call during compilation. The returned callable prepares
/// a runtime callee before operands are evaluated; neither call is auto-awaited.
pub type OperatorCallFactory = dyn Fn(&str, &str, bool) -> Result<Option<Value>, Error> + Send + Sync;

/// Select foreign call dispatch during compilation.
pub type CallPolicyFactory = dyn Fn(&str) -> Result<bool, Error> + Send + Sync;

/// Internal struct that holds template loading level config values.
#[derive(Clone)]
pub struct TemplateConfig {
    /// Emit a native root-frame symbol initialization prologue.
    pub root_symbol_prologue: bool,
    /// Publish completed assignments to a foreign runtime context.
    pub publish_locals: bool,
    /// Capture output dependencies before evaluating expressions.
    pub prepare_output: bool,
    /// Capture foreign attribute dispatch before receiver evaluation.
    pub prepare_attribute: bool,
    /// Capture foreign item dispatch before operand evaluation.
    pub prepare_item: bool,
    /// Prepare foreign await dispatch before evaluating expressions.
    pub prepare_await: bool,
    /// Run the predicate of any filtered loop that uses `loop` lazily, as a
    /// VM subroutine, instead of only the simple cases.
    pub lazy_filtered_predicates: bool,
    /// Preserve boundaries between output groups in fragment mode.
    pub fragment_boundaries: bool,
    /// Optional foreign compile-time output conversion.
    pub constant_output: Option<Arc<ConstantOutputFunc>>,
    /// Optional folding of a filtered constant function argument.
    pub constant_argument: Option<Arc<ConstantArgumentFunc>>,
    /// Optional arithmetic constant folding for compatibility runtimes.
    pub constant_arithmetic: Option<Arc<ConstantArithmeticFunc>>,
    /// Selects foreign arithmetic folded after source-language code generation.
    pub arithmetic_codegen_mode: Option<Arc<ArithmeticCodegenMode>>,
    /// Optional source-language constant-expression interception policy.
    pub jinja_arithmetic_policy: Option<Arc<JinjaArithmeticPolicy>>,
    /// Optional foreign constant-attribute lookup preparation.
    pub jinja_const_attribute_lookup: Option<Arc<JinjaConstAttributeLookup>>,
    /// Optional foreign constant-item lookup preparation.
    pub jinja_const_item_lookup: Option<Arc<JinjaConstAttributeLookup>>,
    /// Optional foreign attribute lookup release operation.
    pub jinja_const_attribute_release: Option<Arc<JinjaConstAttributeRelease>>,
    /// Optional compilation policy for intercepted arithmetic operators.
    pub operator_call_factory: Option<Arc<OperatorCallFactory>>,
    /// Optional source-language call-dispatch policy.
    pub call_policy_factory: Option<Arc<CallPolicyFactory>>,
    /// Enable native constant folding and native literal representation.
    pub native_optimizations: bool,
    /// Optional source-language validator; execution still uses the native VM.
    pub source_validator: Option<Arc<dyn Fn(&str, &str) -> Result<(), Error> + Send + Sync>>,
    /// Capture host metadata after validation for the lifetime of this compilation.
    pub template_metadata: Option<Arc<dyn Fn(&str) -> Result<Value, Error> + Send + Sync>>,
    /// Complete host metadata only after successful native compilation.
    pub template_finalizer: Option<Arc<dyn Fn(Option<&Value>) -> Result<(), Error> + Send + Sync>>,
    /// Prepare host metadata after initial autoescape, before code generation.
    pub template_prelude: Option<Arc<dyn Fn(Option<&Value>) -> Result<(), Error> + Send + Sync>>,
    /// Optional private key for callbacks belonging to one compilation.
    pub compilation_key: Option<Arc<dyn Fn(&str) -> String + Send + Sync>>,
    /// Release private compilation state after the compiler finishes.
    pub compilation_cleanup: Option<Arc<dyn Fn(&str) -> Result<(), Error> + Send + Sync>>,
    /// Optional syntax adapter for environment-owned templates and loaders.
    pub ast_adapter: Option<Arc<AstAdapter>>,
    /// The syntax used for the template.
    pub syntax_config: SyntaxConfig,
    /// Controls whitespace behavior.
    pub ws_config: WhitespaceConfig,
    /// The callback that determines the initial auto escaping for templates.
    pub default_auto_escape: Arc<AutoEscapeFunc>,
    /// Convert CRLF and bare CR to LF after source validation, as Jinja's lexer does.
    pub normalize_newlines: bool,
}

impl TemplateConfig {
    pub(crate) fn new(default_auto_escape: Arc<AutoEscapeFunc>) -> TemplateConfig {
        TemplateConfig {
            root_symbol_prologue: false,
            publish_locals: false,
            prepare_output: false,
            prepare_attribute: false,
            prepare_item: false,
            prepare_await: false,
            lazy_filtered_predicates: true,
            fragment_boundaries: false,
            constant_output: None,
            constant_argument: None,
            constant_arithmetic: None,
            arithmetic_codegen_mode: None,
            jinja_arithmetic_policy: None,
            jinja_const_attribute_lookup: None,
            jinja_const_item_lookup: None,
            jinja_const_attribute_release: None,
            operator_call_factory: None,
            call_policy_factory: None,
            native_optimizations: true,
            source_validator: None,
            template_metadata: None,
            template_finalizer: None,
            template_prelude: None,
            compilation_key: None,
            compilation_cleanup: None,
            ast_adapter: None,
            syntax_config: SyntaxConfig::default(),
            ws_config: WhitespaceConfig::default(),
            default_auto_escape,
            normalize_newlines: false,
        }
    }

    /// Whether compiling `source` needs an owned, newline-normalized copy.
    pub(crate) fn rewrites_source(&self, source: &str) -> bool {
        self.normalize_newlines && source.contains('\r')
    }
}

/// Jinja's lexer turns CRLF and bare CR into LF before tokenizing.
pub(crate) fn normalize_newlines(source: String) -> String {
    if !source.contains('\r') { return source; }
    let mut output = String::with_capacity(source.len());
    let mut chars = source.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\r' {
            if chars.peek() == Some(&'\n') { chars.next(); }
            output.push('\n');
        } else {
            output.push(ch);
        }
    }
    output
}

/// Represents a handle to a template.
///
/// Templates are stored in the [`Environment`] as bytecode instructions.  With the
/// [`Environment::get_template`] method that is looked up and returned in form of
/// this handle.  Such a template can be cheaply copied as it only holds references.
///
/// To render the [`render`](Template::render) method can be used.
#[derive(Clone)]
pub struct Template<'env: 'source, 'source> {
    env: &'env Environment<'env>,
    pub(crate) compiled: CompiledTemplateRef<'env, 'source>,
}

struct CapturedData<'state> {
    output: String,
    state: State<'state, 'state>,
}

self_cell! {
    struct CapturedCell<'source> {
        owner: Template<'source, 'source>,

        #[covariant]
        dependent: CapturedData,
    }
}

/// Represents a rendered template output together with a captured [`State`].
///
/// This type keeps the originating [`Template`] alive together with its
/// [`State`].  This is useful in situations where a temporary template handle
/// is used and a state needs to be inspected later (for instance to call
/// exported macros).
///
/// When created from [`render_captured`](Template::render_captured)
/// the [`output`](Self::output) contains the rendered string.  When created from
/// [`render_captured_to`](Template::render_captured_to) the output is
/// an empty string as the output was written to the provided writer.
pub struct Captured<'source> {
    cell: CapturedCell<'source>,
}

impl fmt::Debug for Captured<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Captured")
            .field("output", &self.output())
            .field("state", &self.state())
            .finish()
    }
}

impl<'source> Captured<'source> {
    /// Returns the rendered output.
    ///
    /// When created from [`render_captured_to`](Template::render_captured_to)
    /// this returns an empty string.
    pub fn output(&self) -> &str {
        self.cell.borrow_dependent().output.as_str()
    }

    /// Returns a reference to the captured state.
    pub fn state(&self) -> &State<'_, '_> {
        &self.cell.borrow_dependent().state
    }

    /// Invokes a closure with mutable access to the captured state.
    pub fn with_state_mut<R>(
        &mut self,
        f: impl for<'state> FnOnce(&mut State<'state, 'state>) -> R,
    ) -> R {
        self.cell
            .with_dependent_mut(|_, dependent| f(&mut dependent.state))
    }

    /// Consumes the capture and returns the rendered output string.
    ///
    /// When created from [`render_captured_to`](Template::render_captured_to)
    /// this returns an empty string.
    pub fn into_output(mut self) -> String {
        self.cell
            .with_dependent_mut(|_, dependent| std::mem::take(&mut dependent.output))
    }
}

impl fmt::Debug for Template<'_, '_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut ds = f.debug_struct("Template");
        ds.field("name", &self.name());
        #[cfg(feature = "internal_debug")]
        {
            ds.field("instructions", &self.compiled.get().instructions);
            ds.field("blocks", &self.compiled.get().blocks);
        }
        ds.field("initial_auto_escape", &self.compiled.get().initial_auto_escape);
        ds.finish()
    }
}

impl<'env, 'source> Template<'env, 'source> {
    pub(crate) fn new(
        env: &'env Environment<'env>,
        compiled: CompiledTemplateRef<'env, 'source>,
    ) -> Template<'env, 'source> {
        Template { env, compiled }
    }

    /// Returns the name of the template.
    pub fn name(&self) -> &str {
        self.compiled.get().instructions.name()
    }

    /// Returns the source code of the template.
    pub fn source(&self) -> &str {
        self.compiled.get().instructions.source()
    }

    /// Returns the names of blocks in compilation traversal order.
    pub fn block_names(&self) -> impl Iterator<Item = &str> + '_ {
        self.compiled.get().instructions.block_order().iter().copied()
    }

    /// Renders the template into a string.
    ///
    /// The provided value is used as the initial context for the template.  It
    /// can be any object that implements [`Serialize`](serde::Serialize).  You
    /// can either create your own struct and derive `Serialize` for it or the
    /// [`context!`](crate::context) macro can be used to create an ad-hoc context.
    ///
    /// For very large contexts and to avoid the overhead of serialization of
    /// potentially unused values, you might consider using a dynamic
    /// [`Object`](crate::value::Object) as value.  For more
    /// information see [Map as Context](crate::value::Object#map-as-context).
    ///
    /// ```
    /// # use minijinja::{Environment, context};
    /// # let mut env = Environment::new();
    /// # env.add_template("hello", "Hello {{ name }}!").unwrap();
    /// let tmpl = env.get_template("hello").unwrap();
    /// println!("{}", tmpl.render(context!(name => "John")).unwrap());
    /// ```
    ///
    /// To render a single block use [`render_captured`](Self::render_captured)
    /// in combination with [`State::render_block`].
    ///
    /// **Note on values:** The [`Value`] type implements `Serialize` and can be
    /// efficiently passed to render.  It does not undergo actual serialization.
    pub fn render<S: Serialize>(&self, ctx: S) -> Result<String, Error> {
        // reduce total amount of code falling under mono morphization into
        // this function, and share the rest in _render.
        self._render(Value::from_serialize(&ctx)).map(|x| x.0)
    }

    /// Render using value fragments, combining them at each capture boundary.
    ///
    /// A formatter may use [`Output::write_fragment`] to preserve a value that
    /// cannot be represented by a Rust string. Ordinary text continues to use
    /// the normal output path. The joiner is called only for mixed captures.
    pub fn render_fragments<S>(&self, ctx: S, joiner: &crate::FragmentJoiner) -> Result<Value, Error>
    where
        S: Serialize,
    {
        let mut sink = String::new();
        let mut output = Output::with_fragments(&mut sink, joiner);
        output.begin_capture(CaptureMode::Capture);
        self._eval(Value::from_serialize(&ctx), &mut output)?;
        output.end_capture(AutoEscape::None)
    }

    /// Render the outer stream normally while preserving values in nested captures.
    pub fn render_with_capture_fragments<S>(&self, ctx: S, joiner: &crate::FragmentJoiner) -> Result<String, Error>
    where S: Serialize {
        let mut output = String::with_capacity(self.compiled.get().buffer_size_hint);
        self._eval(Value::from_serialize(&ctx), &mut Output::with_fragments(&mut output, joiner))?;
        Ok(output)
    }

    /// Render with the outer output left as ordered values and text fragments.
    /// Nested captures are still joined using `joiner`; callers own the final
    /// treatment of each outer fragment and must not flatten trusted and
    /// untrusted fragments before applying their respective semantics.
    pub fn render_unjoined_fragments<S>(&self, ctx: S, joiner: &crate::FragmentJoiner) -> Result<Vec<Value>, Error>
    where
        S: Serialize,
    {
        let mut sink = String::new();
        let mut output = Output::with_fragments(&mut sink, joiner);
        output.begin_root_fragments();
        self._eval(Value::from_serialize(&ctx), &mut output)?;
        output.end_root_fragments()
    }

    /// Like [`render`](Self::render) but also return the evaluated [`State`].
    ///
    /// This can be used to inspect the [`State`] of the template post evaluation
    /// for instance to get fuel consumption numbers or to access globally set
    /// variables.
    ///
    /// ```
    /// # use minijinja::{Environment, context, value::Value};
    /// # let mut env = Environment::new();
    /// let tmpl = env.template_from_str("{% set x = 42 %}Hello {{ what }}!").unwrap();
    /// let (rv, state) = tmpl.render_and_return_state(context!{ what => "World" }).unwrap();
    /// assert_eq!(rv, "Hello World!");
    /// assert_eq!(state.lookup("x"), Some(Value::from(42)));
    /// ```
    ///
    /// **Note on values:** The [`Value`] type implements `Serialize` and can be
    /// efficiently passed to render.  It does not undergo actual serialization.
    #[deprecated(since = "2.18.0", note = "use render_captured instead")]
    pub fn render_and_return_state<S: Serialize>(
        &self,
        ctx: S,
    ) -> Result<(String, State<'_, '_>), Error> {
        // reduce total amount of code falling under mono morphization into
        // this function, and share the rest in _render.
        self._render(Value::from_serialize(&ctx))
    }

    /// Like [`render`](Self::render) but also returns the evaluated [`State`]
    /// while keeping the template alive with the returned state.
    ///
    /// This is primarily useful when working with temporary template handles,
    /// as the resulting [`State`] can continue to be used through the returned
    /// wrapper.
    ///
    /// ```
    /// # use minijinja::{Environment, value::Value};
    /// let env = Environment::new();
    /// let rendered = env
    ///     .template_from_str("{% set x = 42 %}")
    ///     .unwrap()
    ///     .render_captured(())
    ///     .unwrap();
    /// assert_eq!(rendered.output(), "");
    /// assert_eq!(rendered.state().lookup("x"), Some(Value::from(42)));
    /// ```
    pub fn render_captured<S: Serialize>(&self, ctx: S) -> Result<Captured<'source>, Error> {
        self.clone()
            ._capture_state(Value::from_serialize(&ctx), true)
    }

    /// Like [`render`](Self::render) but writes to an [`io::Write`] and keeps
    /// the template alive with the returned state.
    ///
    /// This is useful when working with temporary template handles and
    /// the state needs to be inspected afterwards.  The [`output`](Captured::output)
    /// of the returned [`Captured`] will be an empty string since the
    /// output was written to the provided writer.
    ///
    /// ```
    /// # use minijinja::{Environment, context};
    /// let env = Environment::new();
    /// let mut buf = Vec::new();
    /// let captured = env
    ///     .template_from_str("{% set x = 42 %}Hello!")
    ///     .unwrap()
    ///     .render_captured_to((), &mut buf)
    ///     .unwrap();
    /// assert_eq!(std::str::from_utf8(&buf).unwrap(), "Hello!");
    /// assert_eq!(captured.output(), "");
    /// ```
    pub fn render_captured_to<S: Serialize, W: io::Write>(
        &self,
        ctx: S,
        w: W,
    ) -> Result<Captured<'source>, Error> {
        let root = Value::from_serialize(&ctx);
        let w = std::cell::RefCell::new(WriteWrapper { w, err: None });
        self.clone()
            ._capture_state_with_output(root, &w)
            .map_err(|err| w.into_inner().take_err(err))
    }

    fn _render(&self, root: Value) -> Result<(String, State<'_, '_>), Error> {
        let mut rv = String::with_capacity(self.compiled.get().buffer_size_hint);
        self._eval(root, &mut Output::new(&mut rv))
            .map(|(_, state)| (rv, state))
    }

    fn _capture_state(self, root: Value, capture_output: bool) -> Result<Captured<'source>, Error> {
        let this: Template<'source, 'source> = self;
        let cell = ok!(CapturedCell::try_new(
            this,
            move |template| -> Result<CapturedData<'_>, Error> {
                let mut output = if capture_output {
                    String::with_capacity(template.compiled.get().buffer_size_hint)
                } else {
                    String::new()
                };
                let (_, state) = if capture_output {
                    ok!(template._eval(root, &mut Output::new(&mut output)))
                } else {
                    ok!(template._eval(root, &mut Output::null()))
                };
                Ok(CapturedData { output, state })
            }
        ));
        Ok(Captured { cell })
    }

    fn _capture_state_with_output<W: io::Write>(
        self,
        root: Value,
        w: &RefCell<WriteWrapper<W>>,
    ) -> Result<Captured<'source>, Error> {
        let this: Template<'source, 'source> = self;
        let cell = ok!(CapturedCell::try_new(
            this,
            move |template| -> Result<CapturedData<'_>, Error> {
                let (_, state) = ok!(template._eval(root, &mut Output::new(&mut *w.borrow_mut())));
                Ok(CapturedData {
                    output: String::new(),
                    state,
                })
            }
        ));
        Ok(Captured { cell })
    }

    /// Renders the template into an [`io::Write`].
    ///
    /// This works exactly like [`render`](Self::render), but writes the template
    /// into an [`io::Write`] as it is evaluated.
    ///
    /// ```
    /// # use minijinja::{Environment, context};
    /// # let mut env = Environment::new();
    /// # env.add_template("hello", "Hello {{ name }}!").unwrap();
    /// use std::io::stdout;
    ///
    /// let tmpl = env.get_template("hello").unwrap();
    /// tmpl.render_to_write(context!(name => "John"), &mut stdout()).unwrap();
    /// ```
    ///
    /// **Note on values:** The [`Value`] type implements `Serialize` and can be
    /// efficiently passed to render.  It does not undergo actual serialization.
    #[deprecated(since = "2.18.0", note = "use render_captured_to instead")]
    pub fn render_to_write<S: Serialize, W: io::Write>(
        &self,
        ctx: S,
        w: W,
    ) -> Result<State<'_, '_>, Error> {
        let mut wrapper = WriteWrapper { w, err: None };
        self._eval(Value::from_serialize(&ctx), &mut Output::new(&mut wrapper))
            .map(|(_, state)| state)
            .map_err(|err| wrapper.take_err(err))
    }

    /// Evaluates the template into a [`State`].
    ///
    /// This evaluates the template, discards the output and returns the final
    /// `State` for introspection.  From there global variables or blocks
    /// can be accessed.  What this does is quite similar to how the engine
    /// internally works with templates that are extended or imported from.
    ///
    /// ```
    /// # use minijinja::{Environment, context};
    /// # fn test() -> Result<(), minijinja::Error> {
    /// # let mut env = Environment::new();
    /// # env.add_template("hello", "")?;
    /// let tmpl = env.get_template("hello")?;
    /// let state = tmpl.eval_to_state(context!(name => "John"))?;
    /// println!("{:?}", state.exports());
    /// # Ok(()) }
    /// ```
    ///
    /// If you also want to render, use [`render_captured`](Self::render_captured).
    ///
    /// For more information see [`State`].
    #[deprecated(since = "2.18.0", note = "use render_captured instead")]
    pub fn eval_to_state<S: Serialize>(&self, ctx: S) -> Result<State<'_, '_>, Error> {
        let root = Value::from_serialize(&ctx);
        let mut out = Output::null();
        let vm = Vm::new(self.env);
        let state = ok!(vm.eval(
            &self.compiled.get().instructions,
            root,
            &self.compiled.get().blocks,
            &mut out,
            self.compiled.get().initial_auto_escape,
        ))
        .1;
        Ok(state)
    }

    fn _eval(
        &self,
        root: Value,
        out: &mut Output,
    ) -> Result<(Option<Value>, State<'_, '_>), Error> {
        let retained = match &self.compiled {
            CompiledTemplateRef::Adapted(loaded) => Some(loaded),
            _ => None,
        };
        Vm::new(self.env).eval_retaining(
            &self.compiled.get().instructions,
            root,
            &self.compiled.get().blocks,
            out,
            self.compiled.get().initial_auto_escape,
            retained,
        )
    }

    /// Returns a set of all undeclared variables in the template.
    ///
    /// This returns a set of all variables that might be looked up
    /// at runtime by the template.  Since this runs a static
    /// analysis, the actual control flow is not considered.  This
    /// also cannot take into account what happens due to includes,
    /// imports or extending.  If `nested` is set to `true`, then also
    /// nested trivial attribute lookups are considered and returned.
    ///
    /// ```rust
    /// # use minijinja::Environment;
    /// let mut env = Environment::new();
    /// env.add_template("x", "{% set x = foo %}{{ x }}{{ bar.baz }}").unwrap();
    /// let tmpl = env.get_template("x").unwrap();
    /// let undeclared = tmpl.undeclared_variables(false);
    /// // returns ["foo", "bar"]
    /// let undeclared = tmpl.undeclared_variables(true);
    /// // returns ["foo", "bar.baz"]
    /// ```
    ///
    /// Note that this does not special case global variables.  This means
    /// that for instance a template that uses `namespace()` will return
    /// `namespace` in the return value.
    pub fn undeclared_variables(&self, nested: bool) -> HashSet<String> {
        if let Some((flat, dotted)) = &self.compiled.get().adapted_variables {
            return if nested { dotted } else { flat }.clone();
        }
        match parse(
            self.compiled.get().instructions.source(),
            self.name(),
            self.compiled.get().syntax_config.clone(),
            // TODO: this is not entirely great, but good enough for this use case.
            Default::default(),
        ) {
            Ok(ast) => find_undeclared(&ast, nested),
            Err(_) => HashSet::new(),
        }
    }

    /// Render the definition at `depth` of block `name` outside a render of
    /// this template, with `root` as the template context, as a host calls a
    /// block function it received before or after the render.
    #[cfg(feature = "multi_template")]
    pub fn render_block_with_root(&self, name: &str, depth: usize, root: Value) -> Result<Value, Error> {
        let mut state = State::new(
            Context::new_with_frame(self.env, crate::vm::context::Frame::new_checked(root)?),
            self.compiled.get().initial_auto_escape,
            &self.compiled.get().instructions,
            prepare_blocks(&self.compiled.get().blocks),
        );
        if let CompiledTemplateRef::Adapted(loaded) = &self.compiled { state.retain_root_template(loaded); }
        state.runtime_context = self.env.runtime_context_event(&state, crate::RuntimeContextEvent::Root)?;
        Vm::new(self.env).render_block_for_host(&state, name, depth)
    }

    /// Creates an empty [`State`] for this template.
    ///
    /// It's very rare that you need to actually do this but it can be useful when
    /// testing values or working with macros or other callable objects from outside
    /// the template environment.
    pub fn new_state(&self) -> State<'_, '_> {
        State::new(
            Context::new(self.env),
            self.compiled.get().initial_auto_escape,
            &self.compiled.get().instructions,
            prepare_blocks(&self.compiled.get().blocks),
        )
    }

    /// Returns the instructions and blocks if the template is loaded from the
    /// environment.
    ///
    /// For templates loaded as string on the environment this API contract
    /// cannot be upheld because the template might not live long enough.  Under
    /// normal circumstances however such a template object would never make it
    /// to the callers of this API as this API is used for including or extending,
    /// both of which should only ever get access to a template from the environment
    /// which holds a borrowed ref.
    #[cfg(feature = "multi_template")]
    pub(crate) fn instructions_and_blocks(
        &self,
    ) -> Result<
        (
            &'env Instructions<'env>,
            &'env BTreeMap<&'env str, Instructions<'env>>,
        ),
        Error,
    > {
        match self.compiled {
            CompiledTemplateRef::Borrowed(x) => Ok((&x.instructions, &x.blocks)),
            _ => Err(Error::new(
                crate::ErrorKind::InvalidOperation,
                "cannot extend or include template not borrowed from environment",
            )),
        }
    }

    /// Returns the initial auto escape setting.
    #[cfg(feature = "multi_template")]
    pub(crate) fn initial_auto_escape(&self) -> AutoEscape {
        self.compiled.get().initial_auto_escape
    }
}

#[derive(Clone)]
pub(crate) enum CompiledTemplateRef<'env: 'source, 'source> {
    Owned(Arc<CompiledTemplate<'source>>),
    Adapted(Arc<crate::loader::LoadedTemplate>),
    Borrowed(&'env CompiledTemplate<'source>),
}

impl CompiledTemplateRef<'_, '_> {
    pub(crate) fn get(&self) -> &CompiledTemplate<'_> {
        match *self {
            CompiledTemplateRef::Owned(ref x) => x,
            CompiledTemplateRef::Adapted(ref x) => x.borrow_dependent(),
            CompiledTemplateRef::Borrowed(x) => x,
        }
    }
}

/// Represents a compiled template in memory.
pub struct CompiledTemplate<'source> {
    /// Variable metadata from adapted syntax, which need not parse as source.
    pub adapted_variables: Option<(HashSet<String>, HashSet<String>)>,
    /// The root instructions.
    pub instructions: Instructions<'source>,
    /// Block local instructions.
    pub blocks: BTreeMap<&'source str, Instructions<'source>>,
    /// Optional size hint for string rendering.
    pub buffer_size_hint: usize,
    /// The syntax config that created it.
    pub syntax_config: SyntaxConfig,
    /// The initial setting of auto escaping.
    pub initial_auto_escape: AutoEscape,
}

impl fmt::Debug for CompiledTemplate<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut ds = f.debug_struct("CompiledTemplate");
        #[cfg(feature = "internal_debug")]
        {
            ds.field("instructions", &self.instructions);
            ds.field("blocks", &self.blocks);
        }
        ds.finish()
    }
}

impl<'source> CompiledTemplate<'source> {
    /// Creates a compiled template from name and source using the given settings.
    pub fn new(
        name: &'source str,
        source: &'source str,
        config: &TemplateConfig,
    ) -> Result<CompiledTemplate<'source>, Error> {
        with_compilation_origin(false, || attach_basic_debug_info(Self::_new_impl(name, source, config), source))
    }

    fn _new_impl(
        name: &'source str,
        source: &'source str,
        config: &TemplateConfig,
    ) -> Result<CompiledTemplate<'source>, Error> {
        let key = config.compilation_key.as_ref().map(|make| make(name));
        let callback_name = key.as_deref().unwrap_or(name);
        let result = (|| {
            if let Some(validate) = &config.source_validator { validate(callback_name, source)?; }
            if config.rewrites_source(source) {
                return Err(Error::new(crate::ErrorKind::InvalidOperation,
                    "newline-normalized sources require an environment-owned template"));
            }
            let metadata = config.template_metadata.as_ref().map(|capture| capture(callback_name)).transpose()?;
            if let Some(adapt) = &config.ast_adapter {
                if adapt(callback_name, source)?.is_some() {
                    return Err(Error::new(crate::ErrorKind::InvalidOperation,
                        "adapted syntax trees require an environment-owned template"));
                }
            }
            Self::from_prepared(name, source, config, None, metadata, key.clone())
        })();
        let cleanup = config.compilation_cleanup.as_ref().map(|finish| finish(callback_name)).transpose();
        if result.is_ok() { cleanup?; }
        result
    }

    pub(crate) fn from_prepared(
        name: &'source str,
        source: &'source str,
        config: &TemplateConfig,
        adapted: Option<&'source dyn TemplateAst>,
        metadata: Option<Value>,
        key: Option<String>,
    ) -> Result<CompiledTemplate<'source>, Error> {
        let ast = match adapted {
            Some(tree) => tree.lower()?,
            None => parse(source, name, config.syntax_config.clone(), config.ws_config)?,
        };
        let adapted_variables = adapted.map(|_| (find_undeclared(&ast, false), find_undeclared(&ast, true)));
        let mut g = CodeGenerator::new(name, source);
        g.set_template_metadata(metadata);
        g.set_compilation_key(key);
        g.set_root_symbol_prologue(config.root_symbol_prologue);
        g.set_native_optimizations(config.native_optimizations);
        g.set_publish_locals(config.publish_locals);
        g.set_prepare_output(config.prepare_output);
        g.set_prepare_attribute(config.prepare_attribute);
        g.set_prepare_item(config.prepare_item);
        g.set_prepare_await(config.prepare_await);
        g.set_lazy_filtered_predicates(config.lazy_filtered_predicates);
        g.set_fragment_boundaries(config.fragment_boundaries);
        let initial_auto_escape = (config.default_auto_escape)(name);
        if let Some(prelude) = &config.template_prelude { prelude(g.instructions_metadata())?; }
        g.set_constant_output(config.constant_output.clone(), Some(initial_auto_escape != AutoEscape::None));
        g.set_constant_argument(config.constant_argument.clone());
        g.set_constant_arithmetic(config.constant_arithmetic.clone());
        g.set_arithmetic_codegen_mode(config.arithmetic_codegen_mode.clone());
        g.set_jinja_arithmetic_policy(config.jinja_arithmetic_policy.clone());
        g.set_jinja_const_attribute_lookup(config.jinja_const_attribute_lookup.clone());
        g.set_jinja_const_item_lookup(config.jinja_const_item_lookup.clone());
        g.set_jinja_const_attribute_release(config.jinja_const_attribute_release.clone());
        g.set_operator_call_factory(config.operator_call_factory.clone());
        g.set_call_policy_factory(config.call_policy_factory.clone());
        g.compile_stmt(&ast);
        if let Some(error) = g.take_error() { return Err(error); }
        let buffer_size_hint = g.buffer_size_hint();
        let block_order = g.block_order().to_vec();
        let (mut instructions, blocks) = g.finish();
        instructions.set_block_order(block_order);
        if let Some(finalizer) = &config.template_finalizer { finalizer(instructions.metadata())?; }
        Ok(CompiledTemplate {
            adapted_variables,
            instructions,
            blocks,
            buffer_size_hint,
            syntax_config: config.syntax_config.clone(),
            initial_auto_escape,
        })
    }
}
