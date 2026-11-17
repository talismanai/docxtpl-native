use std::borrow::Cow;
use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicUsize, Ordering};

use crate::compiler::instructions::Instructions;
use crate::environment::Environment;
use crate::error::{Error, ErrorKind};
use crate::output::Output;
use crate::template::Template;
use crate::utils::{AutoEscape, UndefinedBehavior};
use crate::value::{ArgType, Object, Value};
use crate::vm::context::Context;

#[cfg(feature = "fuel")]
use crate::vm::fuel::FuelTracker;

/// When macros are used, the state carries an `id` counter.  Whenever a state is
/// created, the counter is incremented.  This exists because macros can keep a reference
/// to instructions from another state by index.  Without this counter it would
/// be possible for a macro to be called with a different state (different id)
/// which mean we likely panic.
#[cfg(feature = "macros")]
static STATE_ID: std::sync::atomic::AtomicIsize = std::sync::atomic::AtomicIsize::new(0);

thread_local! {
    static HOST_STATE: Cell<*const ()> = const { Cell::new(std::ptr::null()) };
}

// A host may resume a suspended render on another thread. An activation
// that is live across the suspension must not reuse a thread-local address
// computed before it, so HOST_STATE is only accessed through functions that
// are never inlined and that read or write the slot themselves.
#[inline(never)]
fn host_state_get() -> *const () {
    HOST_STATE.with(Cell::get)
}

#[inline(never)]
fn host_state_replace(value: *const ()) -> *const () {
    HOST_STATE.with(|slot| slot.replace(value))
}

struct RestoreHostState(*const ());
impl Drop for RestoreHostState {
    fn drop(&mut self) { host_state_replace(self.0); }
}

/// Run `f` with `state` as the state host code may use to evaluate template
/// code it reaches without one, such as the predicates of a filtered loop
/// that a host object advances. Restores the previous state afterwards.
pub fn with_host_state<R>(state: &State<'_, '_>, f: impl FnOnce() -> R) -> R {
    let _restore = RestoreHostState(host_state_replace(state as *const State<'_, '_> as *const ()));
    f()
}

/// Render the definition at `depth` (0 is the innermost override) of block
/// `name` in a fork of the state installed with [`with_host_state`].
#[cfg(feature = "multi_template")]
pub fn render_block_in_host_state(name: &str, depth: usize) -> Result<Value, Error> {
    with_current_host_state(|state| match state {
        Some(state) => crate::vm::Vm::new(state.env()).render_block_for_host(state, name, depth),
        None => Err(Error::new(ErrorKind::InvalidOperation, "a block can only be rendered during its render")),
    })
}

/// Run `f` with no state installed, restoring the installed one afterwards:
/// a host suspends a render this way so code that runs meanwhile on the same
/// thread does not see that render's state.
pub fn without_host_state<R>(f: impl FnOnce() -> R) -> R {
    let _restore = RestoreHostState(host_state_replace(std::ptr::null()));
    f()
}

/// Whether a [`with_host_state`] scope is active on this thread.
pub fn has_host_state() -> bool {
    !host_state_get().is_null()
}

/// An owned copy of the state installed by the innermost [`with_host_state`],
/// for host code that pulls template work (such as a filtered loop's
/// predicates) on its own coroutine; see [`State::detach_for_host`]. `None`
/// without an installed state or when it cannot be copied.
///
/// # Safety
///
/// The environment of the installed state must be an `Environment<'static>`.
pub unsafe fn detach_current_host_state() -> Option<crate::vm::DetachedHostState> {
    with_current_host_state(|state| state.and_then(|state| unsafe { state.detach_for_host() }))
}

/// The state installed by the innermost [`with_host_state`], if any.
pub(crate) fn with_current_host_state<R>(f: impl FnOnce(Option<&State<'_, '_>>) -> R) -> R {
    let pointer = host_state_get();
    // SAFETY: the pointer was installed from a live shared borrow by
    // with_host_state, whose scope encloses this call; the reference does
    // not escape `f`.
    let state = (!pointer.is_null()).then(|| unsafe { &*(pointer as *const State<'_, '_>) });
    f(state)
}

/// An owned copy of a state and of the environment it borrows.
pub(crate) struct DetachedState {
    // Declared first: the state borrows the environment below.
    pub(crate) state: State<'static, 'static>,
    _env: Box<Environment<'static>>,
}

#[derive(Default)]
struct RetainedTemplates {
    entries: Mutex<BTreeMap<usize, Arc<crate::loader::LoadedTemplate>>>,
    // Set when the root template's own allocation is in `entries`.
    root_retained: std::sync::atomic::AtomicBool,
}

/// Kind of an active VM render frame. These do not describe all Python frames.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RuntimeRenderFrameKind {
    /// A root render activation.
    Root,
    /// A template entered by an include or import.
    Include,
    /// A parent template entered by inheritance.
    Inheritance,
    /// A block activation.
    Block,
    /// A parent block entered through `super`.
    Super,
}

/// Snapshot of a VM frame; its process-wide activation token can wrap.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuntimeRenderFrame {
    /// Process-wide activation token, subject to integer wraparound.
    pub token: usize,
    /// The kind of VM render frame.
    pub kind: RuntimeRenderFrameKind,
    /// Diagnostic name of the executing template.
    pub template: String,
}

static RENDER_FRAME_TOKEN: AtomicUsize = AtomicUsize::new(0);

/// A VM render frame stays active through nested execution and unwinding.
pub(crate) struct RenderFrameGuard {
    frames: Arc<Mutex<Vec<RuntimeRenderFrame>>>,
    token: usize,
}

impl Drop for RenderFrameGuard {
    fn drop(&mut self) {
        let mut frames = self.frames.lock().unwrap_or_else(|error| error.into_inner());
        if let Some(position) = frames.iter().position(|frame| frame.token == self.token) {
            frames.remove(position);
        }
    }
}

/// Scope an active native filtered-input evaluation, including suspension.
pub(crate) struct FilteredLoopGuard {
    depth: Arc<AtomicUsize>,
}

impl Drop for FilteredLoopGuard {
    fn drop(&mut self) { self.depth.fetch_sub(1, Ordering::Relaxed); }
}

/// Provides access to the current execution state of the engine.
///
/// A read only reference is passed to filter functions and similar objects to
/// allow limited interfacing with the engine.  The state is useful to look up
/// information about the engine in filter, test or global functions.  It not
/// only provides access to the template environment but also the context
/// variables of the engine, the current auto escaping behavior as well as the
/// auto escape flag.
///
/// In some testing scenarios or more advanced use cases you might need to get
/// a [`State`].  The state is managed as part of the template execution but the
/// initial state can be retrieved via [`Template::new_state`](crate::Template::new_state).
/// The most common way to get hold of the state however is via functions of filters.
///
/// **Notes on lifetimes:** the state object exposes some of the internal
/// lifetimes through the type.  You should always elide these lifetimes
/// as there might be lifetimes added or removed between releases.
pub struct State<'template, 'env> {
    pub(crate) ctx: Context<'env>,
    pub(crate) runtime_context: Value,
    pub(crate) callback_context_limit: Option<usize>,
    render_frames: Arc<Mutex<Vec<RuntimeRenderFrame>>>,
    filtered_loop_depth: Arc<AtomicUsize>,
    pub(crate) current_block: Option<&'env str>,
    pub(crate) auto_escape: Cell<AutoEscape>,
    /// Jinja's runtime `eval_ctx.autoescape`: set by autoescape blocks like
    /// `auto_escape`, but a macro body keeps its caller's (its output escaping
    /// is lexical). A macro returns safe markup when it is on at the call.
    pub(crate) eval_auto_escape: Cell<AutoEscape>,
    pub(crate) call_policy: Cell<Option<bool>>,
    pub(crate) pending_calls: RefCell<Vec<Value>>,
    pub(crate) prepared_call: RefCell<Option<Value>>,
    // The deferred operation whose hook is running; see `detach_deferred`.
    pub(crate) pending_deferred: RefCell<Option<crate::vm::DeferredOperation>>,
    pub(crate) instructions: &'template Instructions<'env>,
    pub(crate) temps: Arc<Mutex<BTreeMap<Box<str>, Value>>>,
    pub(crate) blocks: BTreeMap<&'env str, BlockStack<'template, 'env>>,
    #[allow(unused)]
    pub(crate) loaded_templates: BTreeSet<&'env str>,
    #[cfg(feature = "macros")]
    pub(crate) id: isize,
    #[cfg(feature = "macros")]
    // instructions, offset, reference, defining blocks, blocks and the
    // lexical autoescape where the macro is defined
    pub(crate) macros: std::sync::Arc<Vec<(&'template Instructions<'env>, u32, std::sync::Arc<()>, std::sync::Arc<()>, BTreeMap<&'env str, BlockStack<'template, 'env>>, AutoEscape)>>,
    #[cfg(feature = "macros")]
    pub(crate) defining_blocks: Arc<()>,
    #[cfg(feature = "macros")]
    pub(crate) closure_tracker: std::sync::Arc<crate::vm::closure_object::ClosureTracker>,
    #[cfg(feature = "fuel")]
    pub(crate) fuel_tracker: Option<std::sync::Arc<FuelTracker>>,
    // This must remain last: other fields can borrow the retained templates.
    // Nested macro states share the arena so their instructions remain live.
    owned_loaded_templates: Arc<RetainedTemplates>,
}

impl fmt::Debug for State<'_, '_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut ds = f.debug_struct("State");
        ds.field("name", &self.instructions.name());
        ds.field("current_block", &self.current_block);
        ds.field("auto_escape", &self.auto_escape.get());
        ds.field("ctx", &self.ctx);
        ds.field("env", &self.env());
        ds.finish()
    }
}

impl<'template, 'env> State<'template, 'env> {
    /// Creates a new state.
    pub(crate) fn new(
        ctx: Context<'env>,
        auto_escape: AutoEscape,
        instructions: &'template Instructions<'env>,
        blocks: BTreeMap<&'env str, BlockStack<'template, 'env>>,
    ) -> State<'template, 'env> {
        State {
            runtime_context: Value::UNDEFINED,
            callback_context_limit: None,
            render_frames: Arc::new(Mutex::new(Vec::new())),
            filtered_loop_depth: Arc::new(AtomicUsize::new(0)),
            #[cfg(feature = "macros")]
            id: STATE_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
            current_block: None,
            auto_escape: Cell::new(auto_escape),
            eval_auto_escape: Cell::new(auto_escape),
            call_policy: Cell::new(None),
            pending_calls: RefCell::new(Vec::new()),
            prepared_call: RefCell::new(None),
            pending_deferred: RefCell::new(None),
            instructions,
            #[cfg(feature = "macros")]
            defining_blocks: Arc::new(()),
            blocks,
            temps: Default::default(),
            loaded_templates: BTreeSet::new(),
            #[cfg(feature = "macros")]
            macros: Default::default(),
            #[cfg(feature = "macros")]
            closure_tracker: Default::default(),
            #[cfg(feature = "fuel")]
            fuel_tracker: ctx.env().fuel().map(FuelTracker::new),
            ctx,
            owned_loaded_templates: Default::default(),
        }
    }

    /// Creates an empty state for an environment.
    pub(crate) fn new_for_env(env: &'env Environment) -> State<'env, 'env> {
        State::new(
            Context::new(env),
            AutoEscape::None,
            &crate::compiler::instructions::EMPTY_INSTRUCTIONS,
            BTreeMap::new(),
        )
    }

    #[cfg(feature = "macros")]
    pub(crate) fn new_for_macro(
        ctx: Context<'env>,
        state: &State<'template, 'env>,
        instructions: &'template Instructions<'env>,
        runtime_context: Value,
        defining_blocks: Arc<()>,
        blocks: BTreeMap<&'env str, BlockStack<'template, 'env>>,
        auto_escape: AutoEscape,
    ) -> State<'template, 'env> {
        State {
            ctx,
            runtime_context,
            callback_context_limit: None,
            render_frames: state.render_frames.clone(),
            filtered_loop_depth: state.filtered_loop_depth.clone(),
            current_block: None,
            auto_escape: Cell::new(auto_escape),
            eval_auto_escape: Cell::new(state.eval_auto_escape.get()),
            call_policy: Cell::new(None),
            pending_calls: RefCell::new(Vec::new()),
            prepared_call: RefCell::new(None),
            pending_deferred: RefCell::new(None),
            instructions,
            blocks,
            defining_blocks,
            temps: state.temps.clone(),
            loaded_templates: Default::default(),
            id: state.id,
            macros: state.macros.clone(),
            closure_tracker: state.closure_tracker.clone(),
            #[cfg(feature = "fuel")]
            fuel_tracker: state.fuel_tracker.clone(),
            owned_loaded_templates: state.owned_loaded_templates.clone(),
        }
    }

    /// Returns a reference to the current environment.
    #[inline(always)]
    pub fn env(&self) -> &'env Environment<'env> {
        self.ctx.env()
    }

    /// Foreign context belonging to the current template activation.
    pub fn runtime_context(&self) -> &Value { &self.runtime_context }
    /// Current VM render-frame depth. Macro bodies share their caller's depth.
    /// This is not, by itself, a count of host-language coroutine frames.
    pub fn render_frame_depth(&self) -> usize { self.render_frames.lock().unwrap().len() }

    /// Snapshot the active VM render frames from outermost to innermost.
    pub fn render_frames(&self) -> Vec<RuntimeRenderFrame> { self.render_frames.lock().unwrap().clone() }

    /// Active native filtered-input evaluations, not a count of host generators.
    pub fn filtered_loop_depth(&self) -> usize { self.filtered_loop_depth.load(Ordering::Relaxed) }

    #[cfg(test)]
    pub(crate) fn filtered_loop_depth_handle(&self) -> Arc<AtomicUsize> { self.filtered_loop_depth.clone() }

    pub(crate) fn enter_filtered_loop(&self) -> FilteredLoopGuard {
        self.filtered_loop_depth.fetch_add(1, Ordering::Relaxed);
        FilteredLoopGuard { depth: self.filtered_loop_depth.clone() }
    }

    pub(crate) fn enter_render_frame(&self, kind: RuntimeRenderFrameKind, template: &str) -> RenderFrameGuard {
        let token = RENDER_FRAME_TOKEN.fetch_add(1, Ordering::Relaxed);
        self.render_frames.lock().unwrap().push(RuntimeRenderFrame { token, kind, template: template.to_owned() });
        RenderFrameGuard { frames: self.render_frames.clone(), token }
    }
    /// Assignment visibility at the current callback's lexical frame.
    pub fn runtime_scope(&self) -> crate::RuntimeScope { self.ctx.scope(self.callback_context_limit) }
    /// Explicit loop/block assignments, distinct from iteration targets.
    pub fn runtime_locals(&self) -> Vec<(String, Value)> { self.ctx.published(self.callback_context_limit) }

    /// Returns the name of the current template.
    pub fn name(&self) -> &str {
        self.instructions.name()
    }

    /// Host metadata retained by the currently executing compilation.
    pub fn template_metadata(&self) -> Option<&Value> { self.instructions.metadata() }

    /// Consume the source-language dispatch decision for the current call.
    pub fn take_call_policy(&self) -> Option<bool> { self.call_policy.take() }
    /// Consume the dispatcher captured before this call's arguments.
    pub fn take_prepared_call(&self) -> Option<Value> { self.prepared_call.borrow_mut().take() }

    /// Inspect the captured host call dispatcher without consuming its lifetime.
    pub fn prepared_call(&self) -> Option<Value> { self.prepared_call.borrow().clone() }

    /// Inspect the captured host call policy without consuming it.
    pub fn call_policy(&self) -> Option<bool> { self.call_policy.get() }

    /// Returns the current value of the auto escape flag.
    #[inline(always)]
    pub fn auto_escape(&self) -> AutoEscape {
        self.auto_escape.get()
    }

    /// The runtime autoescape of the evaluation context (Jinja's
    /// `eval_ctx.autoescape`), which a macro body inherits from its caller.
    pub fn eval_auto_escape(&self) -> AutoEscape {
        self.eval_auto_escape.get()
    }

    pub(crate) fn with_auto_escape<R>(
        &self,
        auto_escape: AutoEscape,
        f: impl FnOnce(&State<'template, 'env>) -> R,
    ) -> R {
        if self.auto_escape.get() == auto_escape {
            return f(self);
        }

        let old = self.auto_escape.replace(auto_escape);
        let rv = f(self);
        self.auto_escape.set(old);
        rv
    }

    /// Returns the current undefined behavior.
    #[inline(always)]
    pub fn undefined_behavior(&self) -> UndefinedBehavior {
        self.env().undefined_behavior()
    }

    /// Returns the name of the innermost block.
    #[inline(always)]
    pub fn current_block(&self) -> Option<&str> {
        self.current_block
    }

    /// Looks up a variable by name in the context.
    ///
    /// # Note on Closures
    ///
    /// Macros and call blocks analyze which variables are referenced and
    /// create closures for them.  This means that unless a variable is defined
    /// as a [global](Environment::add_global) in the environment, was passed in the
    /// initial render context, or was referenced by a macro, this method won't be
    /// able to find it.
    #[inline(always)]
    pub fn lookup(&self, name: &str) -> Option<Value> {
        self.ctx.load(name)
    }

    /// Looks up a global macro and calls it.
    ///
    /// This looks up a value as [`lookup`](Self::lookup) does and calls it
    /// with the passed args.
    #[cfg(feature = "macros")]
    #[cfg_attr(docsrs, doc(cfg(feature = "macros")))]
    pub fn call_macro(&self, name: &str, args: &[Value]) -> Result<String, Error> {
        let f = ok!(self.lookup(name).ok_or_else(|| Error::new(
            crate::error::ErrorKind::UnknownFunction,
            "macro not found"
        )));
        f.call(self, args).map(Into::into)
    }

    /// Renders a block with the given name into a string.
    ///
    /// This method works like [`Template::render`](crate::Template::render) but
    /// it only renders a specific block in the template.  The first argument is
    /// the name of the block.
    ///
    /// This renders only the block `hi` in the template:
    ///
    /// ```
    /// # use minijinja::{Environment, context};
    /// # fn test() -> Result<(), minijinja::Error> {
    /// # let mut env = Environment::new();
    /// # env.add_template("hello", "{% block hi %}Hello {{ name }}!{% endblock %}")?;
    /// let tmpl = env.get_template("hello")?;
    /// let mut rendered = tmpl
    ///     .render_captured(context!(name => "John"))?;
    /// let rv = rendered.with_state_mut(|state| state.render_block("hi"))?;
    /// println!("{}", rv);
    /// # Ok(()) }
    /// ```
    ///
    /// Note that rendering a block is a stateful operation.  If an error
    /// is returned the module has to be re-created as the internal state
    /// can end up corrupted.  This also means you can only render blocks
    /// if you have a mutable reference to the state which is not possible
    /// from within filters or similar.
    #[cfg(feature = "multi_template")]
    #[cfg_attr(docsrs, doc(cfg(feature = "multi_template")))]
    pub fn render_block(&mut self, block: &str) -> Result<String, Error> {
        let mut buf = String::new();
        crate::vm::Vm::new(self.env())
            .call_block(block, self, &mut Output::new(&mut buf))
            .map(|_| buf)
    }

    /// Renders a block with the given name into an [`io::Write`](std::io::Write).
    ///
    /// For details see [`render_block`](Self::render_block).
    #[cfg(feature = "multi_template")]
    #[cfg_attr(docsrs, doc(cfg(feature = "multi_template")))]
    pub fn render_block_to_write<W>(&mut self, block: &str, w: W) -> Result<(), Error>
    where
        W: std::io::Write,
    {
        let mut wrapper = crate::output::WriteWrapper { w, err: None };
        crate::vm::Vm::new(self.env())
            .call_block(block, self, &mut Output::new(&mut wrapper))
            .map(|_| ())
            .map_err(|err| wrapper.take_err(err))
    }

    /// Returns a list of the names of all exports (top-level variables).
    pub fn exports(&self) -> Vec<&str> {
        self.ctx.exports().keys().copied().collect()
    }

    /// Return the root context value, excluding local frames and globals.
    #[cfg(feature = "macros")]
    pub fn context_base(&self) -> Value { self.ctx.clone_base() }

    /// Return the closest active non-empty template context.
    pub fn active_template_base(&self) -> Value { self.ctx.clone_active_base() }


    /// Returns a list of all known variables.
    ///
    /// This list contains all variables that are currently known to the state.
    /// To retrieve the values you can use [`lookup`](Self::lookup).  This will
    /// include all the globals of the environment.  Note that if the context
    /// has been initialized with an object that lies about variables (eg: it
    /// does not correctly implement enumeration), the returned list might not
    /// be complete.
    pub fn known_variables(&self) -> Vec<Cow<'_, str>> {
        Vec::from_iter(self.ctx.known_variables(true))
    }

    /// Variables visible to callbacks in a filtered iterator's predicate.
    pub fn callback_known_variables(&self) -> Vec<Cow<'_, str>> {
        match self.callback_context_limit {
            Some(limit) => Vec::from_iter(self.ctx.known_variables_at(true, limit)),
            None => self.known_variables(),
        }
    }

    /// Look up a callback variable using the predicate's outer context.
    pub fn callback_lookup(&self, name: &str) -> Option<Value> {
        match self.callback_context_limit {
            Some(limit) => self.ctx.load_callback_at(name, limit),
            None => self.ctx.load_callback(name),
        }
    }

    /// Snapshot lexical bindings while handling a failed root activation.
    /// Entries are ordered outer to inner and can include repeated names.
    pub fn error_frame_bindings(&self) -> Vec<(String, Value)> {
        self.ctx.error_frame_bindings()
    }

    /// Fetches a template by name with path joining.
    ///
    /// This works like [`Environment::get_template`] with the difference that the lookup
    /// undergoes path joining.  If the environment has a configured path joining callback,
    /// it will be invoked with the name of the current template as parent template.
    ///
    /// For more information see [`Environment::set_path_join_callback`].
    pub fn get_template(&self, name: &str) -> Result<Template<'env, 'env>, Error> {
        self.env()
            .get_template(&self.env().join_template_path(name, self.name()))
    }

    pub(crate) fn get_template_for_execution(&self, name: &str) -> Result<Template<'env, 'env>, Error> {
        self.env()
            .get_template(&self.env().join_template_path(name, self.name()))
    }

    #[cfg(feature = "multi_template")]
    /// The caller must keep every returned reference within this state or a
    /// nested macro state sharing its arena, and never let it escape them.
    pub(crate) unsafe fn retain_template_instructions(
        &mut self,
        template: &Template<'env, 'env>,
    ) -> Result<(&'env Instructions<'env>, &'env BTreeMap<&'env str, Instructions<'env>>), Error> {
        match &template.compiled {
            crate::template::CompiledTemplateRef::Borrowed(_) => template.instructions_and_blocks(),
            crate::template::CompiledTemplateRef::Adapted(loaded) => {
                self.owned_loaded_templates.entries.lock().unwrap()
                    .entry(Arc::as_ptr(loaded) as usize).or_insert_with(|| loaded.clone());
                // SAFETY: the Arc inserted above keeps this self-cell and its
                // dependent instructions at a stable address until the last
                // state sharing the arena is dropped. The arena is append-only
                // and dropped after all fields that can reference it. These
                // references are used only by VM states sharing that arena;
                // public state accessors tie returned borrows to &self.
                let compiled = loaded.borrow_dependent();
                let compiled = unsafe { &*(compiled as *const crate::template::CompiledTemplate<'_> as *const crate::template::CompiledTemplate<'env>) };
                Ok((&compiled.instructions, &compiled.blocks))
            }
            _ => Err(Error::new(ErrorKind::InvalidOperation, "unsupported owned execution template")),
        }
    }

    #[cfg(test)]
    pub(crate) fn retained_template_count(&self) -> usize {
        self.owned_loaded_templates.entries.lock().unwrap().len()
    }

    /// The blocks of the running template and how many definitions each has
    /// (the template's own and those of the templates it extends).
    #[cfg(feature = "multi_template")]
    pub fn block_stacks(&self) -> Vec<(&str, usize)> {
        self.blocks.iter().map(|(name, stack)| (*name, stack.len())).collect()
    }

    /// A state sharing this one's environment, templates, macros, temporaries
    /// and render frames, with its own copy of the frames: template code run
    /// in it from host code leaves this state unchanged.
    pub(crate) fn fork(&self) -> State<'template, 'env> {
        State {
            ctx: self.ctx.clone_for_env(self.env()),
            runtime_context: self.runtime_context.clone(),
            callback_context_limit: self.callback_context_limit,
            render_frames: self.render_frames.clone(),
            filtered_loop_depth: self.filtered_loop_depth.clone(),
            current_block: self.current_block,
            auto_escape: Cell::new(self.auto_escape.get()),
            eval_auto_escape: Cell::new(self.eval_auto_escape.get()),
            call_policy: Cell::new(None),
            pending_calls: RefCell::new(Vec::new()),
            prepared_call: RefCell::new(None),
            pending_deferred: RefCell::new(None),
            instructions: self.instructions,
            temps: self.temps.clone(),
            blocks: self.blocks.clone(),
            loaded_templates: self.loaded_templates.clone(),
            #[cfg(feature = "macros")]
            id: self.id,
            #[cfg(feature = "macros")]
            macros: self.macros.clone(),
            #[cfg(feature = "macros")]
            defining_blocks: self.defining_blocks.clone(),
            #[cfg(feature = "macros")]
            closure_tracker: self.closure_tracker.clone(),
            #[cfg(feature = "fuel")]
            fuel_tracker: self.fuel_tracker.clone(),
            owned_loaded_templates: self.owned_loaded_templates.clone(),
        }
    }

    /// Keep the root template alive for as long as any state sharing the arena.
    pub(crate) fn retain_root_template(&self, template: &Arc<crate::loader::LoadedTemplate>) {
        self.owned_loaded_templates.entries.lock().unwrap()
            .entry(Arc::as_ptr(template) as usize).or_insert_with(|| template.clone());
        self.owned_loaded_templates.root_retained.store(true, Ordering::Relaxed);
    }

    /// Snapshot this activation for the deferred operation (`super()`, a self
    /// block or a recursive loop call) whose runtime hook is running, so the
    /// operation can run after this state is gone. The snapshot copies the
    /// frames and block stacks and shares macros, temporaries and the retained
    /// template arena; it owns a clone of the environment.
    ///
    /// Returns `None` outside such a hook, or when some instructions could be
    /// borrowed from outside the arena: a root template not rendered from an
    /// owned compilation, or an environment without the shared loaded cache or
    /// with templates added from borrowed sources.
    ///
    /// # Safety
    ///
    /// The environment must be an `Environment<'static>`: the snapshot's clone
    /// outlives this state's borrows, so nothing reachable from it (names,
    /// sources, callbacks) may borrow data with a shorter lifetime.
    pub unsafe fn detach_deferred(&self) -> Option<crate::vm::DetachedActivation> {
        let operation = self.pending_deferred.borrow().clone()?;
        let detached = unsafe { self.detach() }?;
        Some(crate::vm::DetachedActivation::new(detached, operation))
    }

    /// An owned copy of this state for host code that runs template code
    /// (a macro body) independently of this state, for example suspended on
    /// its own coroutine while this one cannot suspend. What is copied, shared
    /// and refused is described in [`State::detach_deferred`].
    ///
    /// # Safety
    ///
    /// The environment must be an `Environment<'static>`.
    pub unsafe fn detach_for_host(&self) -> Option<crate::vm::DetachedHostState> {
        unsafe { self.detach() }.map(crate::vm::DetachedHostState::new)
    }

    /// An owned copy of this state for running template code after it is
    /// gone; see [`State::detach_deferred`] for what is copied, shared and
    /// refused. With `without_loops`, frames drop their loop controllers.
    ///
    /// # Safety
    ///
    /// The environment must be an `Environment<'static>`.
    pub(crate) unsafe fn detach(&self) -> Option<DetachedState> {
        unsafe { self.detach_with(false) }
    }

    pub(crate) unsafe fn detach_with(&self, without_loops: bool) -> Option<DetachedState> {
        if !self.env().hands_out_owned_templates()
            || !self.owned_loaded_templates.root_retained.load(Ordering::Relaxed) {
            return None;
        }
        let env: Box<Environment<'static>> = Box::new(unsafe {
            std::mem::transmute::<Environment<'env>, Environment<'static>>(self.env().clone())
        });
        // SAFETY: the box gives the environment a stable address for the life
        // of the copy, which drops its state before the box.
        let env_ref: &'env Environment<'env> = unsafe { &*(&*env as *const Environment<'static>).cast() };
        let mut ctx = self.ctx.clone_for_env(env_ref);
        if without_loops { ctx.clear_loop_controllers(); }
        let state = State {
            ctx,
            runtime_context: self.runtime_context.clone(),
            callback_context_limit: self.callback_context_limit,
            render_frames: Arc::new(Mutex::new(Vec::new())),
            filtered_loop_depth: Arc::new(AtomicUsize::new(0)),
            current_block: self.current_block,
            auto_escape: Cell::new(self.auto_escape.get()),
            eval_auto_escape: Cell::new(self.eval_auto_escape.get()),
            call_policy: Cell::new(None),
            pending_calls: RefCell::new(Vec::new()),
            prepared_call: RefCell::new(None),
            pending_deferred: RefCell::new(None),
            instructions: self.instructions,
            temps: self.temps.clone(),
            blocks: self.blocks.clone(),
            loaded_templates: self.loaded_templates.clone(),
            #[cfg(feature = "macros")]
            id: self.id,
            #[cfg(feature = "macros")]
            macros: self.macros.clone(),
            #[cfg(feature = "macros")]
            defining_blocks: self.defining_blocks.clone(),
            #[cfg(feature = "macros")]
            closure_tracker: self.closure_tracker.clone(),
            #[cfg(feature = "fuel")]
            fuel_tracker: self.fuel_tracker.clone(),
            owned_loaded_templates: self.owned_loaded_templates.clone(),
        };
        // SAFETY: every borrow in the state points into the boxed environment,
        // the retained arena it shares (root and loaded templates, checked
        // above) or static instructions; both owners live in the copy.
        let state = unsafe { std::mem::transmute::<State<'template, 'env>, State<'static, 'static>>(state) };
        Some(DetachedState { state, _env: env })
    }

    /// Invokes a filter with some arguments.
    ///
    /// ```
    /// # use minijinja::Environment;
    /// # let mut env = Environment::new();
    /// # env.add_filter("upper", |x: &str| x.to_uppercase());
    /// # let tmpl = env.template_from_str("").unwrap();
    /// # let state = tmpl.new_state();
    /// let rv = state.apply_filter("upper", &["hello world".into()]).unwrap();
    /// assert_eq!(rv.as_str(), Some("HELLO WORLD"));
    /// ```
    pub fn apply_filter(&self, filter: &str, args: &[Value]) -> Result<Value, Error> {
        match self.env().get_filter(filter) {
            Some(filter) => filter.call(self, args),
            None => Err(Error::from(ErrorKind::UnknownFilter)),
        }
    }

    /// Invokes a test function on a value.
    ///
    /// ```
    /// # use minijinja::Environment;
    /// # let mut env = Environment::new();
    /// # env.add_test("even", |x: i32| x % 2 == 0);
    /// # let tmpl = env.template_from_str("").unwrap();
    /// # let state = tmpl.new_state();
    /// let rv = state.perform_test("even", &[42i32.into()]).unwrap();
    /// assert!(rv);
    /// ```
    pub fn perform_test(&self, test: &str, args: &[Value]) -> Result<bool, Error> {
        match self.env().get_test(test) {
            Some(test) => test.call(self, args).map(|x| x.is_true()),
            None => Err(Error::from(ErrorKind::UnknownTest)),
        }
    }

    /// Formats a value to a string using the formatter on the environment.
    ///
    /// ```
    /// # use minijinja::{value::Value, Environment};
    /// # let mut env = Environment::new();
    /// # let tmpl = env.template_from_str("").unwrap();
    /// # let state = tmpl.new_state();
    /// let rv = state.format(Value::from(42)).unwrap();
    /// assert_eq!(rv, "42");
    /// ```
    pub fn format(&self, value: Value) -> Result<String, Error> {
        let mut rv = String::new();
        let mut out = Output::new(&mut rv);
        self.env().format(&value, self, &mut out).map(|_| rv)
    }

    /// Returns the fuel levels.
    ///
    /// When the fuel feature is enabled, during evaluation the template will keep
    /// track of how much fuel it has consumed.  If the fuel tracker is turned on
    /// the returned value will be `Some((consumed, remaining))`.  If fuel tracking
    /// is not enabled, `None` is returned instead.
    #[cfg(feature = "fuel")]
    #[cfg_attr(docsrs, doc(cfg(feature = "fuel")))]
    pub fn fuel_levels(&self) -> Option<(u64, u64)> {
        self.fuel_tracker
            .as_ref()
            .map(|x| (x.consumed(), x.remaining()))
    }

    /// Looks up a temp and returns it.
    ///
    /// Temps are similar to context values but the engine never looks them up
    /// on their own and they are not scoped.  The lifetime of temps is limited
    /// to the rendering process of a template.  Temps are useful so that
    /// filters and other things can temporarily stash away state without having
    /// to resort to thread locals which are hard to manage.  Unlike context
    /// variables, temps can also be modified during evaluation by filters and
    /// functions.
    ///
    /// Temps are values but if you want to hold complex state you can store a
    /// custom object there.
    ///
    /// # Example
    ///
    /// ```
    /// use minijinja::{Value, State};
    ///
    /// fn inc(state: &State) -> Value {
    ///     let old = state
    ///         .get_temp("my_counter")
    ///         .unwrap_or_else(|| Value::from(0i64));
    ///     let new = Value::from(i64::try_from(old).unwrap() + 1);
    ///     state.set_temp("my_counter", new.clone());
    ///     new
    /// }
    /// ```
    pub fn get_temp(&self, name: &str) -> Option<Value> {
        self.temps.lock().unwrap().get(name).cloned()
    }

    /// Inserts a temp and returns the old temp.
    ///
    /// For more information see [`get_temp`](Self::get_temp).
    pub fn set_temp(&self, name: &str, value: Value) -> Option<Value> {
        self.temps
            .lock()
            .unwrap()
            .insert(name.to_owned().into(), value)
    }

    /// Shortcut for registering an object as a temp.
    ///
    /// If the value is already there, it's returned as an object. If it's
    /// not there yet, the function is invoked to create it.
    ///
    /// # Example
    ///
    /// ```
    /// use std::sync::atomic::{AtomicUsize, Ordering};
    /// use minijinja::{Value, State};
    /// use minijinja::value::Object;
    ///
    /// #[derive(Debug, Default)]
    /// struct MyObject(AtomicUsize);
    ///
    /// impl Object for MyObject {}
    ///
    /// fn inc(state: &State) -> Value {
    ///     let obj = state.get_or_set_temp_object("my_counter", MyObject::default);
    ///     let old = obj.0.fetch_add(1, Ordering::AcqRel);
    ///     Value::from(old + 1)
    /// }
    /// ```
    ///
    /// # Panics
    ///
    /// This will panic if the value registered under that name is not
    /// the object expected.
    pub fn get_or_set_temp_object<O, F>(&self, name: &str, f: F) -> Arc<O>
    where
        O: Object + 'static,
        F: FnOnce() -> O,
    {
        self.get_temp(name)
            .unwrap_or_else(|| {
                let rv = Value::from_object(f());
                self.set_temp(name, rv.clone());
                rv
            })
            .downcast_object()
            .expect("downcast unexpectedly failed. Name conflict?")
    }

    #[cfg(feature = "debug")]
    pub(crate) fn make_debug_info(
        &self,
        pc: u32,
        instructions: &Instructions<'_>,
    ) -> crate::debug::DebugInfo {
        crate::debug::DebugInfo {
            template_source: Some(instructions.source().to_string()),
            referenced_locals: instructions
                .get_referenced_names(pc)
                .into_iter()
                .filter_map(|n| Some((n.to_string(), some!(self.lookup(n)))))
                .collect(),
        }
    }
}

impl<'a> ArgType<'a> for &State<'_, '_> {
    type Output = &'a State<'a, 'a>;

    fn from_value(_value: Option<&'a Value>) -> Result<Self::Output, Error> {
        Err(Error::new(
            ErrorKind::InvalidOperation,
            "cannot use state type in this position",
        ))
    }

    fn from_state_and_value(
        state: Option<&'a State>,
        _value: Option<&'a Value>,
    ) -> Result<(Self::Output, usize), Error> {
        match state {
            None => Err(Error::new(ErrorKind::InvalidOperation, "state unavailable")),
            Some(state) => Ok((state, 0)),
        }
    }
}

/// Tracks a block and its parents for super.
#[derive(Clone, Default)]
pub(crate) struct BlockStack<'template, 'env> {
    instructions: Vec<&'template Instructions<'env>>,
    depth: usize,
}

impl<'template, 'env> BlockStack<'template, 'env> {
    #[cfg(feature = "macros")]
    pub fn from_start(&self) -> Self {
        Self { instructions: self.instructions.clone(), depth: 0 }
    }

    pub fn new(instructions: &'template Instructions<'env>) -> BlockStack<'template, 'env> {
        BlockStack {
            instructions: vec![instructions],
            depth: 0,
        }
    }

    pub fn instructions(&self) -> &'template Instructions<'env> {
        self.instructions.get(self.depth).copied().unwrap()
    }

    pub(crate) fn depth(&self) -> usize { self.depth }

    pub(crate) fn set_depth(&mut self, depth: usize) -> bool {
        if depth < self.instructions.len() { self.depth = depth; true } else { false }
    }

    #[cfg(feature = "multi_template")]
    pub fn len(&self) -> usize {
        self.instructions.len()
    }

    pub fn push(&mut self) -> bool {
        if self.depth + 1 < self.instructions.len() {
            self.depth += 1;
            true
        } else {
            false
        }
    }

    #[track_caller]
    pub fn pop(&mut self) {
        self.depth = self.depth.checked_sub(1).unwrap()
    }

    #[cfg(feature = "multi_template")]
    pub fn append_instructions(&mut self, instructions: &'template Instructions<'env>) {
        self.instructions.push(instructions);
    }
}
