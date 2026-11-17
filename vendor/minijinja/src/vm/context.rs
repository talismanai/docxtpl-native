use std::borrow::Cow;
use std::collections::{BTreeMap, HashSet};
use std::fmt;

#[cfg(feature = "macros")]
use std::sync::Arc;

use crate::environment::Environment;
use crate::error::{Error, ErrorKind};
use crate::value::Value;
use crate::vm::loop_object::LoopState;

#[cfg(feature = "macros")]
use crate::vm::closure_object::Closure;

type Locals<'env> = BTreeMap<&'env str, Value>;

#[derive(Default, Clone)]
pub(crate) struct RootBindings<'env>(Vec<(&'env str, Value)>);

impl Drop for RootBindings<'_> {
    fn drop(&mut self) {
        for (_, value) in self.0.drain(..) { drop(value); }
    }
}

#[derive(Clone)]
pub(crate) struct Frame<'env> {
    pub(crate) locals: Locals<'env>,
    pub(crate) published: Locals<'env>,
    pub(crate) imported: HashSet<&'env str>,
    pub(crate) module_exports: Option<Value>,
    pub(crate) root_bindings: RootBindings<'env>,
    pub(crate) scope: crate::RuntimeScope,
    pub(crate) ctx: Value,
    pub(crate) current_loop: Option<LoopState>,

    // normally a frame does not carry a closure, but it can when a macro is
    // declared.  Once that happens, all writes to the frames locals are also
    // duplicated into the closure.  Macros declared on that level, then share
    // the closure object to enclose the parent values.  This emulates the
    // behavior of closures in Jinja2.
    #[cfg(feature = "macros")]
    pub(crate) closure: Option<Arc<Closure>>,
}

impl<'env> Default for Frame<'env> {
    fn default() -> Frame<'env> {
        Frame::new(Value::UNDEFINED)
    }
}

impl<'env> Frame<'env> {
    /// Creates a new frame with the given context and no validation
    pub fn new(ctx: Value) -> Frame<'env> {
        Frame {
            locals: Locals::new(),
            published: Locals::new(),
            imported: HashSet::new(),
            module_exports: None,
            root_bindings: RootBindings::default(),
            scope: crate::RuntimeScope::Local,
            ctx,
            current_loop: None,
            #[cfg(feature = "macros")]
            closure: None,
        }
    }

    /// Creates a new frame with the given context and validates the value is not invalid
    pub fn new_checked(root: Value) -> Result<Frame<'env>, Error> {
        Ok(Frame::new(ok!(root.validate())))
    }
}

#[cfg(feature = "internal_debug")]
impl fmt::Debug for Frame<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut m = f.debug_map();
        m.entry(&"locals", &self.locals);
        if let Some(LoopState {
            object: ref controller,
            ..
        }) = self.current_loop
        {
            m.entry(&"loop", controller);
        }
        if !self.ctx.is_undefined() {
            m.entry(&"ctx", &self.ctx);
        }
        m.finish()
    }
}

#[cfg_attr(feature = "internal_debug", derive(Debug))]
#[derive(Default)]
struct PreparedBindings(Vec<(String, Value)>);

impl Drop for PreparedBindings {
    fn drop(&mut self) {
        // Preserve this scope's capture order when releasing its values.
        for (_, value) in self.0.drain(..) { drop(value); }
    }
}

#[cfg_attr(feature = "internal_debug", derive(Debug))]
pub(crate) struct Stack {
    values: Vec<Value>,
    retained_arguments: Vec<usize>,
    prepared_bindings: Vec<PreparedBindings>,
    retained_binding_scopes: Vec<PreparedBindings>,
    prepared_root_calls: Vec<(String, Value)>,
}

#[cfg(test)]
#[test]
fn retained_arguments_keep_their_position_across_stack_growth_and_nesting() {
    let mut stack = Stack::default();
    stack.push(Value::from("outer function"));
    stack.push(Value::from("outer function"));
    stack.push(Value::from("outer binding"));
    stack.retain_argument_below_prepared_function();
    stack.push(Value::from("intermediate"));
    assert_eq!(stack.retained_argument().as_str(), Some("outer binding"));
    stack.push(Value::from("inner function"));
    stack.push(Value::from("inner function"));
    stack.push(Value::from("inner binding"));
    stack.retain_argument_below_prepared_function();
    stack.push(Value::from("inner intermediate"));
    assert_eq!(stack.retained_argument().as_str(), Some("inner binding"));
    stack.release_retained_argument();
    assert_eq!(stack.retained_argument().as_str(), Some("outer binding"));
}

impl Default for Stack {
    fn default() -> Stack {
        Stack {
            values: Vec::with_capacity(24),
            retained_arguments: Vec::new(),
            prepared_bindings: Vec::new(),
            retained_binding_scopes: Vec::new(),
            prepared_root_calls: Vec::new(),
        }
    }
}

impl Stack {
    pub fn push(&mut self, arg: Value) {
        self.values.push(arg);
    }

    #[track_caller]
    pub fn pop(&mut self) -> Value {
        self.values.pop().unwrap()
    }

    pub fn reverse_top(&mut self, n: usize) {
        let start = self.values.len() - n;
        self.values[start..].reverse();
    }

    pub fn get_call_args(&mut self, n: Option<u16>) -> &[Value] {
        let n = match n {
            Some(n) => n as usize,
            None => self.pop().as_usize().unwrap(),
        };
        &self.values[self.values.len() - n..]
    }

    pub fn get_prepared_function_args(&mut self, n: Option<u16>) -> &[Value] {
        let n = match n {
            Some(n) => n as usize,
            None => self.pop().as_usize().unwrap(),
        };
        &self.values[self.values.len() - n - 1..]
    }

    pub fn retain_argument_below_prepared_function(&mut self) {
        let value = self.values.pop().expect("prepared argument");
        let position = self.values.len().checked_sub(2).expect("prepared function");
        self.values.insert(position, value);
        self.retained_arguments.push(position);
    }

    pub fn retained_argument(&self) -> &Value {
        &self.values[*self.retained_arguments.last().expect("retained argument")]
    }

    pub fn release_retained_argument(&mut self) {
        self.retained_arguments.pop().expect("retained argument");
    }

    pub fn begin_prepared_bindings(&mut self) {
        self.prepared_bindings.push(PreparedBindings::default());
    }

    pub fn insert_prepared_binding(&mut self, name: &str, value: Value) {
        self.prepared_bindings.last_mut().expect("prepared bindings").0.push((name.to_owned(), value));
    }

    pub fn prepared_binding(&self, name: &str) -> &Value {
        &self.prepared_bindings.last().expect("prepared bindings").0.iter()
            .find(|(key, _)| key == name).expect("prepared binding").1
    }

    pub fn end_prepared_bindings(&mut self) {
        let bindings = self.prepared_bindings.pop().expect("prepared bindings");
        self.retained_binding_scopes.push(bindings);
    }

    pub fn prepare_root_call(&mut self, name: &str, value: Value) {
        self.prepared_root_calls.push((name.to_owned(), value));
    }

    pub fn take_prepared_root_call(&mut self, name: &str) -> Option<Value> {
        if self.prepared_root_calls.last().is_some_and(|(key, _)| key == name) {
            self.prepared_root_calls.pop().map(|(_, value)| value)
        } else { None }
    }

    pub fn drop_top(&mut self, n: usize) {
        self.values.truncate(self.values.len() - n);
    }

    pub fn try_pop(&mut self) -> Option<Value> {
        self.values.pop()
    }

    #[track_caller]
    pub fn peek(&self) -> &Value {
        self.values.last().unwrap()
    }

    #[cfg(feature = "multi_template")]
    pub fn peek_depth(&self, depth: usize) -> &Value {
        &self.values[self.values.len() - depth - 1]
    }
}

impl From<Vec<Value>> for Stack {
    fn from(values: Vec<Value>) -> Stack {
        Stack { values, retained_arguments: Vec::new(), prepared_bindings: Vec::new(), retained_binding_scopes: Vec::new(), prepared_root_calls: Vec::new() }
    }
}

pub(crate) struct Context<'env> {
    env: &'env Environment<'env>,
    stack: Vec<Frame<'env>>,
    outer_stack_depth: usize,
    recursion_limit: usize,
}

impl fmt::Debug for Context<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut vars = Vec::from_iter(self.known_variables(false));
        vars.sort();
        f.debug_map()
            .entries(vars.into_iter().map(|key| {
                let value = self.load(&key).unwrap_or_default();
                (key, value)
            }))
            .finish()
    }
}

impl<'env> Context<'env> {
    /// Creates an empty context.
    pub fn new(env: &'env Environment<'env>) -> Context<'env> {
        Context {
            env,
            stack: Vec::with_capacity(40),
            outer_stack_depth: 0,
            recursion_limit: env.recursion_limit(),
        }
    }

    /// Creates a context
    pub fn new_with_frame(env: &'env Environment<'env>, mut frame: Frame<'env>) -> Context<'env> {
        let mut rv = Context::new(env);
        frame.scope = crate::RuntimeScope::Root;
        rv.stack.push(frame);
        rv
    }

    /// The env
    #[inline(always)]
    pub fn env(&self) -> &'env Environment<'env> {
        self.env
    }

    /// A copy of this context's frames bound to another environment. Loops
    /// keep sharing their controller, as a retained Jinja closure does.
    pub(crate) fn clone_for_env(&self, env: &'env Environment<'env>) -> Context<'env> {
        Context {
            env,
            stack: self.stack.clone(),
            outer_stack_depth: self.outer_stack_depth,
            recursion_limit: self.recursion_limit,
        }
    }

    /// Stores a variable in the context.
    pub fn store(&mut self, key: &'env str, value: Value) {
        let top = self.stack.last_mut().unwrap();
        #[cfg(feature = "macros")]
        {
            if let Some(ref closure) = top.closure {
                closure.store(key, value.clone());
            }
        }
        top.locals.insert(key, value);
    }

    pub(crate) fn scope(&self, limit: Option<usize>) -> crate::RuntimeScope {
        self.stack.get(limit.unwrap_or(self.stack.len()).saturating_sub(1))
            .map_or(crate::RuntimeScope::Local, |frame| frame.scope)
    }

    pub(crate) fn set_scope(&mut self, scope: crate::RuntimeScope) -> crate::RuntimeScope {
        std::mem::replace(&mut self.stack.last_mut().unwrap().scope, scope)
    }

    pub(crate) fn replace_current_base(&mut self, value: Value) -> (Value, RootBindings<'env>) {
        let frame = self.stack.last_mut().unwrap();
        (std::mem::replace(&mut frame.ctx, value), std::mem::take(&mut frame.root_bindings))
    }

    pub(crate) fn restore_current_base(&mut self, value: Value, bindings: RootBindings<'env>) {
        let frame = self.stack.last_mut().unwrap();
        frame.ctx = value;
        frame.root_bindings = bindings;
    }

    pub(crate) fn current_base(&self) -> &Value {
        &self.stack.last().unwrap().ctx
    }

    pub(crate) fn store_root_binding(&mut self, name: &'env str, value: Value) {
        self.stack.last_mut().unwrap().root_bindings.0.push((name, value));
    }

    /// The template variables visible here (assignments and loop variables,
    /// innermost first wins), not names resolved from the context mapping.
    pub(crate) fn template_locals(&self) -> Vec<(String, Value)> {
        let mut seen = std::collections::BTreeMap::new();
        for frame in self.stack.iter().rev() {
            if let Some(ref l) = frame.current_loop {
                if l.with_loop_var {
                    seen.entry("loop".to_owned()).or_insert_with(|| Value::from_dyn_object(l.object.clone()));
                }
            }
            for (name, value) in frame.locals.iter() {
                seen.entry((*name).to_owned()).or_insert_with(|| value.clone());
            }
        }
        seen.into_iter().collect()
    }

    /// Whether a template frame (an assignment, a root binding or a loop
    /// variable) provides `name`, as opposed to the context mapping.
    pub(crate) fn binds_locally(&self, name: &str) -> bool {
        self.stack.iter().any(|frame| frame.locals.contains_key(name)
            || frame.root_bindings.0.iter().any(|(bound, _)| *bound == name)
            || (name == "loop" && frame.current_loop.as_ref().is_some_and(|l| l.with_loop_var)))
    }

    /// A frame whose root bindings are `bindings`.
    pub(crate) fn frame_with_root_bindings(bindings: Vec<(&'env str, Value)>) -> Frame<'env> {
        Frame { root_bindings: RootBindings(bindings), ..Frame::default() }
    }

    pub(crate) fn resolve_current_base_prepared(&self, name: &str) -> Option<Value> {
        self.resolve_base_prepared(self.current_base(), name)
    }

    /// The base of the frame `skip` frames below the topmost.
    pub(crate) fn base_below(&self, skip: usize) -> &Value {
        &self.stack[self.stack.len() - 1 - skip].ctx
    }

    pub(crate) fn resolve_base_prepared(&self, base: &Value, name: &str) -> Option<Value> {
        match self.env.runtime_prepared_lookup(base, name) {
            Some(value) => value,
            None => base.get_attr_fast(name),
        }.or_else(|| self.env.get_global(name))
    }

    pub(crate) fn publish(&mut self, names: &[&'env str]) -> Vec<(String, Value)> {
        let frame = self.stack.last_mut().unwrap();
        let values: Vec<_> = names.iter().filter_map(|name| frame.locals.get(name).map(|value| (*name, value.clone()))).collect();
        values.into_iter().map(|(name, value)| {
            frame.imported.remove(name);
            frame.published.insert(name, value.clone());
            (name.to_owned(), value)
        }).collect()
    }

    pub(crate) fn publish_imports(&mut self, names: &[&'env str]) -> Vec<(String, Value)> {
        let frame = self.stack.last_mut().unwrap();
        names.iter().filter_map(|name| {
            frame.imported.insert(name);
            frame.locals.get(name).map(|value| ((*name).to_owned(), value.clone()))
        }).collect()
    }

    #[cfg(feature = "multi_template")]
    pub(crate) fn current_imports_mut(&mut self) -> &mut HashSet<&'env str> {
        &mut self.stack.last_mut().unwrap().imported
    }

    #[cfg(feature = "multi_template")]
    pub(crate) fn current_module_exports_mut(&mut self) -> &mut Option<Value> {
        &mut self.stack.last_mut().unwrap().module_exports
    }

    pub(crate) fn published(&self, limit: Option<usize>) -> Vec<(String, Value)> {
        self.stack.get(limit.unwrap_or(self.stack.len()).saturating_sub(1)).into_iter()
            .flat_map(|frame| frame.published.iter().map(|(name, value)| ((*name).to_owned(), value.clone()))).collect()
    }

    /// Adds a value to a closure if missing.
    ///
    /// All macros declare on a certain level reuse the same closure.  This is done
    /// to emulate the behavior of how scopes work in Jinja2 in Python.  The
    /// unfortunate downside is that this has to be done with a `Mutex`.
    #[cfg(feature = "macros")]
    pub fn enclose(&mut self, key: &str) {
        self.stack
            .last_mut()
            .unwrap()
            .closure
            .as_mut()
            .unwrap()
            .clone()
            .store_if_missing(key, || self.load(key).unwrap_or(Value::UNDEFINED));
    }

    /// Loads the closure and returns it.
    #[cfg(feature = "macros")]
    pub fn closure(&mut self) -> Option<&Arc<Closure>> {
        self.stack.last_mut().unwrap().closure.as_ref()
    }

    /// Temporarily takes the closure.
    ///
    /// This is done because includes are in the same scope as the module that
    /// triggers the import, but we do not want to allow closures to be modified
    /// from another file as this would be very confusing.
    ///
    /// This means that if you override a variable referenced by a macro after
    /// including in the parent template, it will not override the value seen by
    /// the macro.
    #[cfg(all(feature = "multi_template", feature = "macros"))]
    pub fn take_closure(&mut self) -> Option<Arc<Closure>> {
        self.stack.last_mut().unwrap().closure.take()
    }

    /// Puts the closure back.
    #[cfg(feature = "macros")]
    pub fn reset_closure(&mut self, closure: Option<Arc<Closure>>) {
        self.stack.last_mut().unwrap().closure = closure;
    }

    pub fn clone_active_base(&self) -> Value {
        self.stack.iter().rev().find(|frame| !frame.ctx.is_undefined())
            .map(|frame| frame.ctx.clone()).unwrap_or_default()
    }

    /// Return the base context value
    #[cfg(feature = "macros")]
    pub fn clone_base(&self) -> Value {
        self.stack
            .first()
            .map(|x| x.ctx.clone())
            .unwrap_or_default()
    }

    /// Looks up a variable in the context.
    pub fn load(&self, key: &str) -> Option<Value> {
        self.load_at(key, self.stack.len())
    }

    pub(crate) fn load_prepared(&self, key: &str) -> Option<Value> {
        for frame in self.stack.iter().rev() {
            if let Some(value) = frame.locals.get(key) { return Some(value.clone()); }
            if let Some(ref loop_state) = frame.current_loop {
                if loop_state.with_loop_var && key == "loop" {
                    return Some(Value::from_dyn_object(loop_state.object.clone()));
                }
            }
            if let Some((_, value)) = frame.root_bindings.0.iter().find(|(name, _)| *name == key) {
                return Some(value.clone());
            }
            let value = match self.env.runtime_prepared_lookup(&frame.ctx, key) {
                Some(value) => value,
                None => frame.ctx.get_attr_fast(key),
            };
            if value.is_some() { return value; }
        }
        self.env.get_global(key)
    }

    pub(crate) fn load_at(&self, key: &str, limit: usize) -> Option<Value> {
        self.load_at_with_root_bindings(key, limit, true).0
    }

    pub(crate) fn load_callback_at(&self, key: &str, limit: usize) -> Option<Value> {
        self.load_at_with_root_bindings(key, limit, false).0
    }

    pub(crate) fn load_callback(&self, key: &str) -> Option<Value> {
        self.load_callback_at(key, self.stack.len())
    }

    pub(crate) fn error_frame_bindings(&self) -> Vec<(String, Value)> {
        let mut result = Vec::new();
        for frame in &self.stack {
            result.extend(frame.root_bindings.0.iter().map(|(name, value)| ((*name).to_owned(), value.clone())));
            result.extend(frame.locals.iter().map(|(name, value)| ((*name).to_owned(), value.clone())));
            if let Some(ref loop_state) = frame.current_loop {
                if loop_state.with_loop_var {
                    result.push(("loop".to_owned(), Value::from_dyn_object(loop_state.object.clone())));
                }
            }
        }
        result
    }

    pub(crate) fn load_instruction(&self, key: &str) -> (Option<Value>, bool) {
        self.load_at_with_root_bindings(key, self.stack.len(), true)
    }

    fn load_at_with_root_bindings(&self, key: &str, limit: usize, use_root: bool) -> (Option<Value>, bool) {
        for frame in self.stack.iter().take(limit).rev() {
            // look at locals first
            if let Some(value) = frame.locals.get(key) {
                return (Some(value.clone()), false);
            }

            // if we are a loop, check if we are looking up the special loop var.
            if let Some(ref l) = frame.current_loop {
                if l.with_loop_var && key == "loop" {
                    return (Some(Value::from_dyn_object(l.object.clone())), false);
                }
            }

            if use_root {
                if let Some((_, value)) = frame.root_bindings.0.iter().find(|(name, _)| *name == key) {
                    return (Some(value.clone()), true);
                }
            }

            // perform a fast lookup.  This one will not produce errors if the
            // context is undefined or of the wrong type.
            if let Some(rv) = frame.ctx.get_attr_fast(key) {
                return (Some(rv), false);
            }
        }

        (self.env.get_global(key), false)
    }

    /// Returns an iterable of all declared variables.
    pub fn known_variables(&self, with_globals: bool) -> HashSet<Cow<'_, str>> {
        self.known_variables_at(with_globals, self.stack.len())
    }

    pub(crate) fn known_variables_at(&self, with_globals: bool, limit: usize) -> HashSet<Cow<'_, str>> {
        let mut seen = HashSet::<Cow<'_, str>>::new();
        for frame in self.stack.iter().take(limit).rev() {
            for key in frame.locals.keys() {
                if !seen.contains(&Cow::Borrowed(*key)) {
                    seen.insert(Cow::Borrowed(key));
                }
            }

            if let Some(ref l) = frame.current_loop {
                if l.with_loop_var && !seen.contains("loop") {
                    seen.insert(Cow::Borrowed("loop"));
                }
            }

            if let Ok(iter) = frame.ctx.try_iter() {
                for key in iter {
                    if let Some(str_key) = key.as_str() {
                        if !seen.contains(&Cow::Borrowed(str_key))
                            && frame.ctx.get_item(&key).is_ok()
                        {
                            seen.insert(Cow::Owned(str_key.to_owned()));
                        }
                    }
                }
            }
        }
        if with_globals {
            seen.extend(self.env.globals().map(|x| Cow::Borrowed(x.0)));
        }
        seen
    }

    /// Pushes a new layer.
    pub fn push_frame(&mut self, layer: Frame<'env>) -> Result<(), Error> {
        ok!(self.check_depth());
        self.stack.push(layer);
        Ok(())
    }

    /// Pops the topmost layer.
    #[track_caller]
    pub fn pop_frame(&mut self) -> Frame<'env> {
        self.stack.pop().unwrap()
    }

    /// Keep the active template's root context while hiding nested locals.
    /// Suspended frames stay alive and count toward the recursion limit.
    #[cfg(feature = "multi_template")]
    pub(crate) fn suspend_nested_template_frames(&mut self, for_self: bool) -> Vec<Frame<'env>> {
        let root = self.stack.iter().rposition(|frame| frame.scope == crate::RuntimeScope::Root
            || (for_self && frame.scope == crate::RuntimeScope::Block)).unwrap_or(0);
        let suspended = self.stack.split_off(root + 1);
        self.outer_stack_depth += suspended.len();
        suspended
    }

    #[cfg(feature = "multi_template")]
    pub(crate) fn restore_nested_template_frames(&mut self, suspended: Vec<Frame<'env>>) {
        self.outer_stack_depth -= suspended.len();
        self.stack.extend(suspended);
    }

    /// Returns the root locals (exports)
    #[track_caller]
    pub fn exports(&self) -> &Locals<'env> {
        &self.stack.first().unwrap().locals
    }

    /// Returns the current locals mutably.
    #[track_caller]
    #[cfg(feature = "multi_template")]
    pub fn current_locals_mut(&mut self) -> &mut Locals<'env> {
        &mut self.stack.last_mut().unwrap().locals
    }

    /// Returns the current innermost loop state.
    pub fn current_loop(&mut self) -> Option<&mut LoopState> {
        self.stack
            .iter_mut()
            .rev()
            .filter_map(|x| x.current_loop.as_mut())
            .next()
    }

    /// Drop every frame's loop controller (a copy that must not keep loops alive).
    pub(crate) fn clear_loop_controllers(&mut self) {
        for frame in &mut self.stack { frame.current_loop = None; }
    }

    /// Reset the iteration locals of the innermost loop frame, as a new item does.
    pub fn clear_loop_locals(&mut self) {
        if let Some(frame) = self.stack.iter_mut().rev().find(|x| x.current_loop.is_some()) {
            frame.locals.clear();
            frame.published.clear();
        }
    }

    pub fn next_loop_item(&mut self) -> Option<Value> {
        let frame = self
            .stack
            .iter_mut()
            .rev()
            .find(|x| x.current_loop.is_some())?;
        let item = frame.current_loop.as_mut()?.next();
        if item.is_some() {
            frame.locals.clear();
            frame.published.clear();
        }
        item
    }

    /// The real depth of the context.
    pub fn depth(&self) -> usize {
        self.outer_stack_depth + self.stack.len()
    }

    pub(crate) fn frame_checkpoint(&self) -> (usize, usize) {
        (self.stack.len(), self.outer_stack_depth)
    }

    pub(crate) fn restore_frame_checkpoint(&mut self, checkpoint: (usize, usize)) {
        self.stack.truncate(checkpoint.0);
        self.outer_stack_depth = checkpoint.1;
    }

    /// Increase the stack depth.
    #[allow(unused)]
    pub fn incr_depth(&mut self, delta: usize) -> Result<(), Error> {
        let previous = self.outer_stack_depth;
        self.outer_stack_depth = previous.checked_add(delta).ok_or_else(|| {
            Error::new(ErrorKind::InvalidOperation, "recursion limit exceeded")
        })?;
        if let Err(error) = self.check_depth() {
            self.outer_stack_depth = previous;
            return Err(error);
        }
        Ok(())
    }

    /// Decrease the stack depth.
    #[allow(unused)]
    pub fn decr_depth(&mut self, delta: usize) {
        self.outer_stack_depth -= delta;
    }

    fn check_depth(&self) -> Result<(), Error> {
        if self.outer_stack_depth.checked_add(self.stack.len())
            .is_none_or(|depth| depth > self.recursion_limit) {
            return Err(Error::new(
                ErrorKind::InvalidOperation,
                "recursion limit exceeded",
            ));
        }
        Ok(())
    }
}
