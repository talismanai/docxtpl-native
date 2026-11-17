use std::fmt;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

use crate::error::{Error, ErrorKind};
use crate::value::{Enumerator, Object, Value, ValueIter, ValueKind};
use crate::vm::state::State;

#[derive(Clone)]
pub(crate) struct LoopState {
    pub(crate) with_loop_var: bool,
    pub(crate) completed_iteration: bool,
    pub(crate) accepted_iteration: bool,
    // Return target and capture state for recursive loop calls.
    pub(crate) current_recursion_jump: Option<(u32, bool, usize)>,
    pub(crate) object: Arc<Loop>,
}

impl LoopState {
    pub fn new(iter: ValueIter, iterable: Value, depth: usize, with_loop_var: bool,
        recurse_jump_target: Option<u32>, current_recursion_jump: Option<(u32, bool, usize)>, filter_pc: Option<u32>,
        filter_capture: Option<Value>) -> LoopState {
        let (iter, filtered) = if filter_pc.is_some() {
            (ValueIter::from_iterator(Box::new(std::iter::empty())),
             Some(Mutex::new(FilteredInput { iter, exhausted: false })))
        } else {
            (iter, None)
        };
        LoopState { with_loop_var, current_recursion_jump, completed_iteration: false, accepted_iteration: false,
            object: Arc::new(Loop { idx: AtomicUsize::new(!0usize), len: Mutex::new(None), supplied_length: Mutex::new(None), iterable,
                depth, recurse_jump_target, last_changed_value: Mutex::default(),
                iter: Mutex::new(AdjacentLoopItemIterWrapper::new(iter)), filtered, filter_pc, filter_capture: Mutex::new(filter_capture),
                delegate: Mutex::new(None), predicate_state: Mutex::new(None),
                predicate_bindings: Mutex::new(Vec::new()) }) }
    }
    pub fn did_not_iterate(&self, completed_iteration_policy: bool) -> bool {
        if completed_iteration_policy { !self.completed_iteration }
        else { self.object.idx.load(Ordering::Relaxed) == !0usize }
    }
    pub fn did_not_iterate_filtered(&self, completed_iteration_policy: bool) -> bool {
        if completed_iteration_policy { !self.completed_iteration }
        else { !self.accepted_iteration }
    }
    pub fn next(&mut self) -> Option<Value> {
        self.object.advance().unwrap_or_else(|error| Some(Value::from(error)))
    }
}

pub(crate) struct AdjacentLoopItemIterWrapper {
    prev_item: Option<Value>, current_item: Option<Value>, next_item: Option<Value>,
    iter: ValueIter,
    buffered: VecDeque<Value>,
}
impl AdjacentLoopItemIterWrapper {
    pub fn new(iter: ValueIter) -> Self {
        Self { prev_item: None, current_item: None, next_item: None, iter, buffered: VecDeque::new() }
    }
    fn take_next(&mut self) -> Option<Value> {
        let value = if self.iter.manages_lookahead() && self.buffered.is_empty() {
            self.iter.next()
        } else { self.next_item.take().or_else(|| self.buffered.pop_front()).or_else(|| self.iter.next()) }?;
        Some(match value.validate() { Ok(value) => value, Err(error) => Value::from(error) })
    }
    fn peek(&mut self) -> Result<Option<Value>, Error> {
        if self.iter.manages_lookahead() && self.buffered.is_empty() {
            return self.iter.next_for_lookahead().map(Value::validate).transpose();
        }
        if self.next_item.is_none() { self.next_item = self.buffered.pop_front().or_else(|| self.iter.next_for_lookahead()); }
        self.next_item.clone().map(Value::validate).transpose()
    }
    fn remaining_len(&mut self) -> Result<(Value, usize, ValueIter), Error> {
        // Keep a peeked item separate, including when materialization fails.
        let (rest, declared_length) = self.iter.materialize_remaining()?;
        let buffered = self.buffered.len();
        let previous = std::mem::replace(&mut self.iter, rest);
        Ok((declared_length, buffered, previous))
    }
    fn lookahead_occupied(&self) -> Result<bool, Error> {
        Ok(self.iter.lookahead_occupied()?.unwrap_or_else(|| self.next_item.is_some()))
    }
}

struct FilteredInput { iter: ValueIter, exhausted: bool }

/// A host object that owns a loop's iteration state after the loop escaped
/// to host code, so host and template observe one live loop.
pub trait LoopDelegate: Send + Sync + fmt::Debug {
    /// Advance the loop; `None` when it is exhausted.
    fn advance(&self) -> Result<Option<Value>, Error>;
    /// Read a loop attribute.
    fn get(&self, key: &str) -> Result<Option<Value>, Error>;
    /// Call a loop method (`cycle`, `changed`).
    fn call_method(&self, state: &State, name: &str, args: &[Value]) -> Result<Value, Error>;
    /// The host object, as a value.
    fn value(&self) -> Value;
    /// The VM popped the loop's frame; `others` references to the loop remain
    /// besides the frame's (closures, values and host objects that hold it).
    fn released(&self, others: usize) {
        let _ = others;
    }
}

/// The result of a loop call completed by the host's recursion hook.
#[derive(Debug)]
pub struct LoopCallOutcome(pub Value);
impl Object for LoopCallOutcome {}

/// The native loop state handed to a host when it takes over a loop.
#[derive(Debug)]
pub struct LoopHandoff {
    /// Zero-based index of the current iteration, `None` before the first.
    pub index0: Option<usize>,
    /// Zero-based depth of a recursive loop.
    pub depth0: usize,
    /// Whether the loop is marked recursive.
    pub recursive: bool,
    /// The iterable the loop was created with.
    pub iterable: Value,
    /// The cached length, if it was computed.
    pub length: Option<Value>,
    /// An item already taken from the iterator for lookahead.
    pub after: Option<Value>,
    /// The current item.
    pub current: Option<Value>,
    /// The previous item.
    pub previous: Option<Value>,
    /// The arguments of the last `changed` call.
    pub last_changed: Option<Vec<Value>>,
}

/// Native loop state, exposed opaquely to runtime adapters.
pub struct Loop {
    pub(crate) len: Mutex<Option<Value>>,
    iterable: Value,
    pub(crate) idx: AtomicUsize,
    pub(crate) depth: usize,
    supplied_length: Mutex<Option<Value>>,
    pub(crate) last_changed_value: Mutex<Option<Vec<Value>>>,
    pub(crate) recurse_jump_target: Option<u32>,
    iter: Mutex<AdjacentLoopItemIterWrapper>,
    filtered: Option<Mutex<FilteredInput>>,
    pub(crate) filter_pc: Option<u32>,
    pub(crate) filter_capture: Mutex<Option<Value>>,
    delegate: Mutex<Option<Arc<dyn LoopDelegate>>>,
    // A copy of the render's state for predicates pulled after the render.
    predicate_state: Mutex<Option<crate::vm::state::DetachedState>>,
    // Context names of the predicate, resolved when the loop started.
    pub(crate) predicate_bindings: Mutex<Vec<(String, Value)>>,
}
impl Loop {
    /// Keep a copy of the state installed with [`crate::with_host_state`] so
    /// a host can still pull accepted items of this filtered loop after the
    /// render: the predicates then run in that copy, as a retained Jinja
    /// generator runs in its closure. Does nothing for other loops, without
    /// an installed state, or when the state cannot be copied.
    ///
    /// # Safety
    ///
    /// The environment of the installed state must be an `Environment<'static>`.
    pub unsafe fn retain_predicate_state(&self) {
        if self.filter_pc.is_none() || self.predicate_state.lock().unwrap().is_some() { return; }
        // The copy drops loop controllers: it must not keep this loop alive.
        let detached = crate::vm::state::with_current_host_state(|state|
            state.and_then(|state| unsafe { state.detach_with(true) }));
        if let Some(detached) = detached {
            let previous = self.predicate_state.lock().unwrap().replace(detached);
            drop(previous);
        }
    }

    /// Tell the delegate, if any, that the VM popped this loop's frame.
    pub(crate) fn release_from_frame(self: &Arc<Self>) {
        let others = Arc::strong_count(self) - 1;
        if let Some(delegate) = self.delegate() { delegate.released(others); }
    }

    /// The host object that took over this loop, if any.
    pub fn delegate(&self) -> Option<Arc<dyn LoopDelegate>> {
        self.delegate.lock().unwrap().clone()
    }

    /// Hand this loop's state to a host object built by `make`, which then
    /// drives iteration, attributes and methods; returns the existing delegate
    /// when there is one. Items not yet consumed stay in the native iterator;
    /// the host reads them with [`Loop::next_remaining_item`].
    pub fn delegate_with(&self, make: impl FnOnce(LoopHandoff) -> Result<Arc<dyn LoopDelegate>, Error>)
        -> Result<Arc<dyn LoopDelegate>, Error> {
        if let Some(delegate) = self.delegate() { return Ok(delegate); }
        let handoff = {
            let mut iter = self.iterator()?;
            let index = self.idx.load(Ordering::Relaxed);
            LoopHandoff {
                index0: (index != !0usize).then_some(index),
                depth0: self.depth,
                recursive: self.recurse_jump_target.is_some(),
                iterable: self.iterable.clone(),
                length: self.len.lock().unwrap().clone().filter(|value| !value.is_none()),
                after: iter.next_item.take(),
                current: iter.current_item.clone(),
                previous: iter.prev_item.clone(),
                last_changed: self.last_changed_value.lock().unwrap().clone(),
            }
        };
        let delegate = make(handoff)?;
        let mut slot = self.delegate.lock().unwrap();
        // A reentrant handoff may have installed a delegate first; keep it.
        Ok(slot.get_or_insert(delegate).clone())
    }

    /// Render the loop body recursively for `iterable`, as `loop(iterable)`
    /// does, in a fork of the state installed with [`crate::with_host_state`].
    pub fn recurse_from_host(&self, iterable: Value) -> Result<Value, Error> {
        let Some(target) = self.recurse_jump_target else {
            return Err(Error::new(ErrorKind::InvalidOperation, "cannot recurse outside of recursive loop"));
        };
        crate::vm::state::with_current_host_state(|state| match state {
            Some(state) => {
                let mut fork = state.fork();
                crate::vm::Vm::new(state.env()).recurse_loop(&mut fork, iterable, target, self.depth + 1)
            }
            None => Err(Error::new(ErrorKind::InvalidOperation,
                "a recursive loop can only be called during its render")),
        })
    }

    /// Whether this loop has a predicate (`for x in y if ...`).
    pub fn is_filtered(&self) -> bool { self.filter_pc.is_some() }

    /// Drop the items of a delegated loop that no host object can reach any
    /// more: the host's item source over this loop was released, as Jinja's
    /// LoopContext releases its iterator when it replaces or drops it. The
    /// iterators are dropped after their locks are released, since a host
    /// finalizer may advance the loop. Does nothing for a loop that is not
    /// delegated, or whose iterator is in use.
    pub fn release_remaining(&self) {
        if self.delegate().is_none() { return; }
        let Ok(mut iter) = self.iter.try_lock() else { return; };
        let empty = || ValueIter::from_iterator(Box::new(std::iter::empty()));
        let previous = std::mem::replace(&mut iter.iter, empty());
        let buffered = std::mem::take(&mut iter.buffered);
        let next = iter.next_item.take();
        drop(iter);
        let input = self.filtered.as_ref().and_then(|input| input.try_lock().ok()).map(|mut input| {
            input.exhausted = true;
            std::mem::replace(&mut input.iter, empty())
        });
        drop((previous, buffered, next, input));
    }

    /// The next item the loop has not consumed, without changing the loop's
    /// index or lookahead. Used by a host delegate as its item source. For a
    /// filtered loop, predicates run when an accepted item is needed, in a
    /// fork of the state installed with [`crate::with_host_state`]; outside
    /// such a scope a filtered loop with no accepted item fails.
    pub fn next_remaining_item(&self) -> Result<Option<Value>, Error> {
        if self.filter_pc.is_some() {
            if let Some(value) = self.iterator()?.buffered.pop_front() { return value.validate().map(Some); }
            crate::vm::state::with_current_host_state(|state| match state {
                Some(state) => {
                    let mut fork = state.fork();
                    crate::vm::Vm::new(state.env()).fill_filtered_from_host(&mut fork, self)
                }
                None => {
                    let mut retained = self.predicate_state.try_lock().map_err(|_| Error::new(
                        ErrorKind::InvalidOperation, "filtered loop predicates are already running"))?;
                    match retained.as_mut() {
                        Some(detached) => {
                            let mut fork = detached.state.fork();
                            crate::vm::Vm::new(detached.state.env()).fill_filtered_from_host(&mut fork, self)
                        }
                        None => Err(Error::new(ErrorKind::InvalidOperation,
                            "a filtered loop can only be advanced during its render")),
                    }
                }
            })?;
            let value = self.iterator()?.buffered.pop_front();
            return value.map(Value::validate).transpose();
        }
        let mut iter = self.iterator()?;
        let value = iter.buffered.pop_front().or_else(|| iter.iter.next());
        drop(iter);
        value.map(Value::validate).transpose()
    }

    /// Consume a previously accepted filtered input without advancing loop
    /// metadata or taking the loop's separate lookahead item. Evaluating a new
    /// predicate still requires the VM; this method never consumes raw input.
    pub fn take_filtered_input_from_host(&self) -> Result<Option<Value>, Error> {
        let input = self.filtered.as_ref().ok_or_else(|| Error::new(
            ErrorKind::InvalidOperation, "loop has no filtered input"))?;
        let input = input.try_lock().map_err(|_| Error::new(ErrorKind::InvalidOperation,
            "filtered iterator is already active"))?;
        let value = self.iterator()?.buffered.pop_front();
        let exhausted = input.exhausted;
        drop(input);
        match value {
            Some(value) => value.validate().map(Some),
            None if exhausted => Ok(None),
            None => Err(Error::new(ErrorKind::InvalidOperation,
                "host advancement of a filtered loop requires the VM")),
        }
    }

    pub(crate) fn advance(&self) -> Result<Option<Value>, Error> {
        if let Some(delegate) = self.delegate() {
            let item = delegate.advance()?;
            if item.is_some() { self.idx.fetch_add(1, Ordering::Relaxed); }
            return Ok(item);
        }
        let (item, previous) = {
            let mut iter = self.iterator()?;
            let item = iter.take_next();
            // The terminal next() and failed next() leave retained metadata
            // pointing at the last successfully yielded item.
            let previous = if item.as_ref().is_some_and(|value| value.kind() != ValueKind::Invalid) {
                self.idx.fetch_add(1, Ordering::Relaxed);
                let current = iter.current_item.clone();
                std::mem::replace(&mut iter.prev_item, current)
            } else { None };
            (item, previous)
        };
        // Replacing a previous value can invoke user finalizers. Match the
        // loop protocol's updated index and do not hold an iterator lock.
        drop(previous);
        if let Some(value) = item.as_ref().filter(|value| value.kind() != ValueKind::Invalid) {
            let old = self.iterator()?.current_item.replace(value.clone());
            drop(old);
        }
        Ok(item)
    }

    /// Advance an item already available in the same iterator used by the VM.
    ///
    /// A host is responsible for entering the appropriate runtime context.
    /// Invalid iterator values are returned as errors. A deferred predicate
    /// requires VM execution when there is no previously accepted item.
    pub fn advance_from_host(&self) -> Result<Option<Value>, Error> {
        if let Some(input) = &self.filtered {
            let input = input.try_lock().map_err(|_| Error::new(ErrorKind::InvalidOperation,
                "filtered iterator is already active"))?;
            if self.filtered_buffered()? == 0 {
                if input.exhausted { return Ok(None); }
                return Err(Error::new(ErrorKind::InvalidOperation,
                    "host advancement of a filtered loop requires the VM"));
            }
            drop(input);
            return self.advance()?.map(Value::validate).transpose();
        }
        self.advance()?.map(Value::validate).transpose()
    }

    fn iterator(&self) -> Result<MutexGuard<'_, AdjacentLoopItemIterWrapper>, Error> {
        self.iter.try_lock().map_err(|_| Error::new(ErrorKind::InvalidOperation, "loop iterator is already active"))
    }
    fn length(&self) -> Result<Value, Error> {
        if let Some(length) = self.supplied_length.lock().unwrap().take() {
            return Ok(length);
        }
        if let Some(length) = self.len.lock().unwrap().as_ref().filter(|value| !value.is_none()).cloned() {
            return Ok(length);
        }
        // Do not hold the cache lock across a foreign __len__ callback.
        let length = match self.iterable.loop_length_value()? {
            Some(length) => length,
            None => {
                let (remaining, buffered, previous) = self.iterator()?.remaining_len()?;
                // Dropping a host iterator can invoke finalizers. The new
                // iterator must be installed and unlocked before that happens.
                drop(previous);
                let index = self.idx.load(Ordering::Relaxed).wrapping_add(1) + buffered;
                let partial = self.iterable.loop_length_add(&remaining, &Value::from(index))?;
                let peeked = self.iterator()?.lookahead_occupied()?;
                self.iterable.loop_length_add(&partial, &Value::from(peeked))?
            }
        };
        let previous = self.len.lock().unwrap().replace(length.clone());
        // A reentrant length callback may have populated the cache. Releasing
        // that value can invoke a host finalizer that reads this loop again.
        drop(previous);
        Ok(length)
    }
    pub(crate) fn filtered_next_raw(&self) -> Result<Option<Value>, Error> {
        let Some(input) = &self.filtered else { return Ok(None); };
        let mut input = input.try_lock().map_err(|_| Error::new(ErrorKind::InvalidOperation, "filtered iterator is already active"))?;
        if input.exhausted { return Ok(None); }
        match input.iter.next() {
            Some(value) => value.validate().map(Some),
            None => { input.exhausted = true; Ok(None) }
        }
    }
    pub(crate) fn filtered_close_after_error(&self) -> Result<(), Error> {
        let Some(input) = &self.filtered else { return Ok(()); };
        let previous = {
            let mut input = input.try_lock().map_err(|_| Error::new(ErrorKind::InvalidOperation,
                "filtered iterator is already active"))?;
            input.exhausted = true;
            std::mem::replace(&mut input.iter, ValueIter::from_iterator(Box::new(std::iter::empty())))
        };
        let buffered = std::mem::take(&mut self.iterator()?.buffered);
        // Foreign iterators and buffered values can execute finalizers.
        drop(previous);
        drop(buffered);
        Ok(())
    }
    pub(crate) fn filtered_push(&self, value: Value) -> Result<(), Error> {
        self.iterator()?.buffered.push_back(value); Ok(())
    }
    pub(crate) fn filtered_buffered(&self) -> Result<usize, Error> {
        let iter = self.iterator()?;
        Ok(iter.buffered.len() + usize::from(iter.next_item.is_some()))
    }
    pub(crate) fn filtered_input_buffered(&self) -> Result<usize, Error> {
        Ok(self.iterator()?.buffered.len())
    }
    pub(crate) fn filtered_set_length(&self) -> Result<(), Error> {
        let length = self.idx.load(Ordering::Relaxed).wrapping_add(1) + self.filtered_buffered()?;
        let previous = self.len.lock().unwrap().replace(Value::from(length));
        drop(previous);
        Ok(())
    }
    pub(crate) fn filtered_has_length(&self) -> bool {
        self.len.lock().unwrap().as_ref().is_some_and(|value| !value.is_none())
    }
    pub(crate) fn filtered_supply_length(&self, length: Value) {
        let previous = self.len.lock().unwrap().replace(length.clone());
        drop(previous);
        let previous = self.supplied_length.lock().unwrap().replace(length);
        drop(previous);
    }
}
impl fmt::Debug for Loop {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let length = self.len.lock().unwrap().clone();
        f.debug_struct("Loop").field("len", &length).field("idx", &self.idx)
            .field("depth", &self.depth).finish()
    }
}

impl Object for Loop {
    fn call(self: &Arc<Self>, _state: &State, _args: &[Value]) -> Result<Value, Error> {
        // Recursive calls in templates use the VM's special recursion opcode.
        Err(Error::new(ErrorKind::InvalidOperation, "loop recursion cannot be called this way"))
    }
    fn call_method(self: &Arc<Self>, state: &State, name: &str, args: &[Value]) -> Result<Value, Error> {
        if self.delegate().is_none() && matches!(name, "changed" | "cycle") {
            state.env().runtime_loop_handoff(state, &Value::from_dyn_object(self.clone()), name)?;
        }
        if let Some(delegate) = self.delegate() { return delegate.call_method(state, name, args); }
        let normalized = if matches!(name, "changed" | "cycle") {
            state.env().runtime_loop_arguments(state, name, args).transpose()?
        } else { None };
        let args = normalized.as_deref().unwrap_or(args);
        if name == "changed" {
            let mut execute = || {
                // Comparison can call foreign code; never hold the mutex across it.
                let previous = self.last_changed_value.lock().unwrap().clone();
                let changed = match previous.as_deref() {
                    Some(previous) => state.env().runtime_loop_changed(state, previous, args)?,
                    None => true,
                };
                drop(previous);
                if changed {
                    let replaced = self.last_changed_value.lock().unwrap().replace(args.to_owned());
                    // Comparison may have reentered and replaced the slot; its
                    // values can run finalizers while being released.
                    drop(replaced);
                }
                Ok(Value::from(changed))
            };
            state.env().runtime_loop_changed_scope(state, &mut execute)
        } else if name == "cycle" {
            if args.is_empty() { return Err(Error::new(ErrorKind::InvalidOperation, "loop.cycle requires an argument")); }
            Ok(args[self.idx.load(Ordering::Relaxed) % args.len()].clone())
        } else { Err(Error::from(ErrorKind::UnknownMethod)) }
    }
    fn enumerate(self: &Arc<Self>) -> Enumerator {
        Enumerator::Str(&["index0", "index", "length", "revindex", "revindex0", "first", "last", "depth", "depth0",
            #[cfg(feature="adjacent_loop_items")] "previtem",
            #[cfg(feature="adjacent_loop_items")] "nextitem"])
    }
    fn get_value(self: &Arc<Self>, key: &Value) -> Option<Value> { self.get_value_by_str(some!(key.as_str())) }
    fn get_value_by_str(self: &Arc<Self>, key: &str) -> Option<Value> {
        if let Some(delegate) = self.delegate() {
            return delegate.get(key).unwrap_or_else(|error| Some(Value::from(error)));
        }
        let idx = self.idx.load(Ordering::Relaxed);
        if idx == !0usize { return Some(Value::UNDEFINED); }
        let result: Result<Option<Value>, Error> = (|| Ok(match key {
            "index" => Some(Value::from(idx + 1)), "index0" => Some(Value::from(idx)),
            "first" => Some(Value::from(idx == 0)),
            "length" => Some(self.length()?),
            "revindex" | "revindex0" => {
                // Length may call user code that advances this same loop.
                // Jinja reads the index only after that callback returns.
                let length = self.length()?;
                let current = self.idx.load(Ordering::Relaxed);
                let index = if key == "revindex0" { current + 1 } else { current };
                Some(self.iterable.loop_length_subtract(&length, &Value::from(index))?)
            },
            "last" => Some(Value::from(self.iterator()?.peek()?.is_none())),
            "depth" => Some(Value::from(self.depth + 1)), "depth0" => Some(Value::from(self.depth)),
            #[cfg(feature="adjacent_loop_items")]
            "previtem" => Some(self.iterator()?.prev_item.clone().unwrap_or(Value::UNDEFINED)),
            #[cfg(feature="adjacent_loop_items")]
            "nextitem" => Some(self.iterator()?.peek()?.unwrap_or(Value::UNDEFINED)),
            _ => None,
        }))();
        result.unwrap_or_else(|error| Some(Value::from(error)))
    }
    fn render(self: &Arc<Self>, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let index = self.idx.load(Ordering::Relaxed);
        let length = self.len.lock().unwrap().clone();
        write!(f, "<loop {}/{}>", index,
            length.as_ref().map(|n| n.to_string()).unwrap_or_else(|| "?".to_owned()))
    }
}

#[cfg(test)]
mod compatibility_tests {
    use super::*;
    use crate::value::ObjectRepr;

    #[test]
    fn closing_filtered_input_releases_foreign_values_after_unlocking() {
        struct IteratorProbe {
            owner: Arc<Mutex<std::sync::Weak<Loop>>>,
            events: Arc<Mutex<Vec<&'static str>>>,
        }
        impl Iterator for IteratorProbe {
            type Item = Value;
            fn next(&mut self) -> Option<Value> { None }
        }
        impl Drop for IteratorProbe {
            fn drop(&mut self) {
                let owner = self.owner.lock().unwrap().upgrade().unwrap();
                let input_unlocked = owner.filtered.as_ref().unwrap().try_lock().is_ok();
                let iterator_unlocked = owner.iter.try_lock().is_ok();
                self.events.lock().unwrap().push(if input_unlocked && iterator_unlocked { "iterator" } else { "locked" });
            }
        }
        #[derive(Debug)]
        struct BufferedProbe {
            owner: std::sync::Weak<Loop>,
            events: Arc<Mutex<Vec<&'static str>>>,
        }
        impl Object for BufferedProbe {}
        impl Drop for BufferedProbe {
            fn drop(&mut self) {
                let owner = self.owner.upgrade().unwrap();
                let input_unlocked = owner.filtered.as_ref().unwrap().try_lock().is_ok();
                let iterator_unlocked = owner.iter.try_lock().is_ok();
                self.events.lock().unwrap().push(if input_unlocked && iterator_unlocked { "buffer" } else { "locked" });
            }
        }
        let link = Arc::new(Mutex::new(std::sync::Weak::new()));
        let events = Arc::new(Mutex::new(Vec::new()));
        let iterator = ValueIter::from_iterator(Box::new(IteratorProbe { owner: link.clone(), events: events.clone() }));
        let owner = LoopState::new(iterator, Value::from(()), 0, true, None, None, Some(1), None).object;
        *link.lock().unwrap() = Arc::downgrade(&owner);
        owner.filtered_push(Value::from_object(BufferedProbe { owner: Arc::downgrade(&owner), events: events.clone() })).unwrap();
        owner.filtered_close_after_error().unwrap();
        assert_eq!(*events.lock().unwrap(), ["iterator", "buffer"]);
        assert!(owner.filtered_next_raw().unwrap().is_none());
    }

    #[test]
    fn materialized_iterator_is_released_after_replacement_and_unlock() {
        struct IteratorProbe {
            owner: Arc<Mutex<std::sync::Weak<Loop>>>,
            events: Arc<Mutex<Vec<String>>>,
            next: i32,
        }
        impl Iterator for IteratorProbe {
            type Item = Value;
            fn next(&mut self) -> Option<Value> {
                if self.next > 2 { return None; }
                let value = self.next;
                self.next += 1;
                Some(Value::from(value))
            }
        }
        impl Drop for IteratorProbe {
            fn drop(&mut self) {
                let owner = self.owner.lock().unwrap().upgrade().unwrap();
                let unlocked = owner.iter.try_lock().is_ok();
                let event = if unlocked {
                    owner.advance_from_host().unwrap().unwrap().to_string()
                } else { "locked".to_owned() };
                self.events.lock().unwrap().push(event);
            }
        }
        let link = Arc::new(Mutex::new(std::sync::Weak::new()));
        let events = Arc::new(Mutex::new(Vec::new()));
        let iter = ValueIter::from_iterator(Box::new(IteratorProbe {
            owner: link.clone(), events: events.clone(), next: 1,
        }));
        let owner = LoopState::new(iter, Value::from(()), 0, true, None, None, None, None).object;
        *link.lock().unwrap() = Arc::downgrade(&owner);
        assert_eq!(owner.advance_from_host().unwrap().unwrap().to_string(), "1");
        assert_eq!(owner.length().unwrap().to_string(), "3");
        assert_eq!(*events.lock().unwrap(), ["2"]);
        assert_eq!(owner.get_value_by_str("index0").unwrap().to_string(), "1");
    }

    #[test]
    fn released_delegated_loop_drops_its_iterator_unlocked() {
        use crate::vm::loop_object::LoopDelegate;
        struct DropProbe { owner: Arc<Mutex<std::sync::Weak<Loop>>>, events: Arc<Mutex<Vec<String>>> }
        impl Iterator for DropProbe {
            type Item = Value;
            fn next(&mut self) -> Option<Value> { Some(Value::from(1)) }
        }
        impl Drop for DropProbe {
            fn drop(&mut self) {
                let owner = self.owner.lock().unwrap().upgrade().unwrap();
                let unlocked = owner.iter.try_lock().is_ok();
                self.events.lock().unwrap().push(if unlocked { "dropped" } else { "locked" }.to_owned());
            }
        }
        #[derive(Debug)]
        struct Host;
        impl LoopDelegate for Host {
            fn advance(&self) -> Result<Option<Value>, Error> { Ok(None) }
            fn get(&self, _: &str) -> Result<Option<Value>, Error> { Ok(None) }
            fn call_method(&self, _: &State, _: &str, _: &[Value]) -> Result<Value, Error> { Ok(Value::UNDEFINED) }
            fn value(&self) -> Value { Value::UNDEFINED }
        }
        let link = Arc::new(Mutex::new(std::sync::Weak::new()));
        let events = Arc::new(Mutex::new(Vec::new()));
        let iter = ValueIter::from_iterator(Box::new(DropProbe { owner: link.clone(), events: events.clone() }));
        let owner = LoopState::new(iter, Value::from(()), 0, true, None, None, None, None).object;
        *link.lock().unwrap() = Arc::downgrade(&owner);
        // A loop the VM still iterates keeps its iterator.
        owner.release_remaining();
        assert!(events.lock().unwrap().is_empty());
        owner.delegate_with(|_| Ok(Arc::new(Host))).unwrap();
        owner.release_remaining();
        assert_eq!(*events.lock().unwrap(), ["dropped"]);
        assert!(owner.next_remaining_item().unwrap().is_none());
    }

    #[test]
    fn loop_formatting_does_not_hold_length_lock_across_host_render() {
        #[derive(Debug)]
        struct RenderProbe {
            owner: std::sync::Weak<Loop>,
            unlocked_calls: Arc<AtomicUsize>,
        }
        impl Object for RenderProbe {
            fn render(self: &Arc<Self>, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                let owner = self.owner.upgrade().unwrap();
                if owner.len.try_lock().is_ok() {
                    drop(owner.length().unwrap());
                    self.unlocked_calls.fetch_add(1, Ordering::Relaxed);
                }
                f.write_str("host")
            }
        }
        let iter = ValueIter::from_iterator(Box::new([Value::from(1)].into_iter()));
        let owner = LoopState::new(iter, Value::from(()), 0, true, None, None, None, None).object;
        owner.advance_from_host().unwrap().unwrap();
        let calls = Arc::new(AtomicUsize::new(0));
        *owner.len.lock().unwrap() = Some(Value::from_object(RenderProbe {
            owner: Arc::downgrade(&owner), unlocked_calls: calls.clone(),
        }));
        assert_eq!(Value::from_dyn_object(owner.clone()).to_string(), "<loop 0/host>");
        assert!(format!("{owner:?}").contains("host"));
        assert_eq!(calls.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn filtered_length_replacements_release_values_outside_locks_in_order() {
        use std::sync::atomic::AtomicBool;

        #[derive(Debug)]
        struct DropProbe {
            owner: std::sync::Weak<Loop>,
            observed: Arc<AtomicBool>,
            expected_length: &'static str,
            expected_supplied: Option<&'static str>,
        }
        impl Object for DropProbe {}
        impl Drop for DropProbe {
            fn drop(&mut self) {
                let owner = self.owner.upgrade().unwrap();
                let Ok(length) = owner.len.try_lock() else { return; };
                let Ok(supplied) = owner.supplied_length.try_lock() else { return; };
                let length = length.as_ref().map(ToString::to_string);
                let supplied = supplied.as_ref().map(ToString::to_string);
                self.observed.store(length.as_deref() == Some(self.expected_length)
                    && supplied.as_deref() == self.expected_supplied, Ordering::Relaxed);
            }
        }
        for case in 0..3 {
            let iter = ValueIter::from_iterator(Box::new([Value::from(1)].into_iter()));
            let owner = LoopState::new(iter, Value::from(()), 0, true, None, None, None, None).object;
            owner.advance_from_host().unwrap().unwrap();
            let observed = Arc::new(AtomicBool::new(false));
            let probe = Value::from_object(DropProbe {
                owner: Arc::downgrade(&owner), observed: observed.clone(),
                expected_length: if case == 0 { "1" } else { "7" },
                expected_supplied: if case == 2 { Some("7") } else { None },
            });
            if case == 2 { *owner.supplied_length.lock().unwrap() = Some(probe); }
            else { *owner.len.lock().unwrap() = Some(probe); }
            if case == 0 { owner.filtered_set_length().unwrap(); }
            else { owner.filtered_supply_length(Value::from(7)); }
            assert!(observed.load(Ordering::Relaxed), "case {case}");
        }
    }

    #[test]
    fn reentrant_length_releases_replaced_cache_outside_lock() {
        use std::sync::atomic::AtomicBool;

        #[derive(Debug)]
        struct DropProbe {
            owner: std::sync::Weak<Loop>,
            released_without_lock: Arc<AtomicBool>,
        }
        impl Object for DropProbe {}
        impl Drop for DropProbe {
            fn drop(&mut self) {
                let owner = self.owner.upgrade().unwrap();
                let unlocked = owner.len.try_lock().is_ok();
                if unlocked {
                    let visible = owner.length().unwrap();
                    self.released_without_lock.store(visible.to_string() == "5", Ordering::Relaxed);
                }
            }
        }
        #[derive(Debug)]
        struct ReentrantLength {
            owner: Arc<Mutex<std::sync::Weak<Loop>>>,
            entered: AtomicBool,
            released_without_lock: Arc<AtomicBool>,
        }
        impl Object for ReentrantLength {
            fn loop_length_value(self: &Arc<Self>) -> Result<Option<Value>, Error> {
                let owner = self.owner.lock().unwrap().upgrade().unwrap();
                if !self.entered.swap(true, Ordering::Relaxed) {
                    drop(owner.length()?);
                    Ok(Some(Value::from(5)))
                } else {
                    Ok(Some(Value::from_object(DropProbe {
                        owner: Arc::downgrade(&owner),
                        released_without_lock: self.released_without_lock.clone(),
                    })))
                }
            }
        }
        let link = Arc::new(Mutex::new(std::sync::Weak::new()));
        let released = Arc::new(AtomicBool::new(false));
        let iterable = Value::from_object(ReentrantLength {
            owner: link.clone(), entered: AtomicBool::new(false), released_without_lock: released.clone(),
        });
        let iter = ValueIter::from_iterator(Box::new([Value::from(1)].into_iter()));
        let owner = LoopState::new(iter, iterable, 0, true, None, None, None, None).object;
        *link.lock().unwrap() = Arc::downgrade(&owner);
        owner.advance_from_host().unwrap().unwrap();
        assert_eq!(owner.length().unwrap().to_string(), "5");
        assert!(released.load(Ordering::Relaxed));
    }

    #[test]
    fn reverse_indices_read_position_after_reentrant_length() {
        #[derive(Debug)]
        struct ReentrantLength(Arc<Mutex<std::sync::Weak<Loop>>>);
        impl Object for ReentrantLength {
            fn loop_length_value(self: &Arc<Self>) -> Result<Option<Value>, Error> {
                let owner = self.0.lock().unwrap().upgrade().unwrap();
                owner.advance_from_host()?.expect("the next item must remain available");
                Ok(Some(Value::from(5)))
            }
        }
        for (attribute, expected) in [("revindex", "4"), ("revindex0", "3")] {
            let link = Arc::new(Mutex::new(std::sync::Weak::new()));
            let iter = ValueIter::from_iterator(Box::new([Value::from(1), Value::from(2)].into_iter()));
            let state = LoopState::new(iter, Value::from_object(ReentrantLength(link.clone())), 0, true, None, None, None, None);
            let owner = state.object;
            *link.lock().unwrap() = Arc::downgrade(&owner);
            owner.advance_from_host().unwrap().unwrap();
            assert_eq!(owner.get_value_by_str(attribute).unwrap().to_string(), expected);
            assert_eq!(owner.get_value_by_str("index0").unwrap().to_string(), "1");
        }
    }

    #[test]
    fn reverse_index_arithmetic_preserves_foreign_index_value() {
        #[derive(Debug)]
        struct ForeignIndex;
        impl Object for ForeignIndex {}

        #[derive(Debug)]
        struct ForeignIterable;
        impl Object for ForeignIterable {
            fn loop_length_subtract(self: &Arc<Self>, _length: &Value, index: &Value) -> Result<Value, Error> {
                Ok(index.clone())
            }
        }

        let iterable = Value::from_object(ForeignIterable);
        let index = Value::from_object(ForeignIndex);
        let original = index.downcast_object::<ForeignIndex>().unwrap();
        let result = iterable.loop_length_subtract(&Value::from(5), &index).unwrap();
        let returned = result.downcast_object::<ForeignIndex>().unwrap();
        assert!(Arc::ptr_eq(&original, &returned));
    }

    #[test]
    fn changed_reentrant_replacement_releases_value_outside_lock() {
        use std::sync::Weak;

        #[derive(Debug)]
        struct Probe {
            owner: Weak<Loop>,
            seen: Arc<Mutex<Vec<bool>>>,
        }
        impl Object for Probe {}
        impl Drop for Probe {
            fn drop(&mut self) {
                if let Some(owner) = self.owner.upgrade() {
                    match owner.last_changed_value.try_lock() {
                        Ok(mut slot) => {
                            self.seen.lock().unwrap().push(true);
                            *slot = Some(vec![Value::from(99)]);
                        }
                        Err(_) => self.seen.lock().unwrap().push(false),
                    }
                }
            }
        }

        let captured: Arc<Mutex<Option<Arc<Loop>>>> = Arc::new(Mutex::new(None));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let advance_capture = captured.clone();
        let compare_capture = captured.clone();
        let compare_seen = seen.clone();
        let mut env = crate::Environment::new();
        env.set_runtime_loop_advance(move |_, value| {
            let object = value.downcast_object::<Loop>().unwrap();
            *advance_capture.lock().unwrap() = Some(object);
            Ok(())
        });
        env.set_runtime_loop_changed(move |_, _| {
            let object = compare_capture.lock().unwrap().as_ref().unwrap().clone();
            let probe = Value::from_object(Probe { owner: Arc::downgrade(&object), seen: compare_seen.clone() });
            *object.last_changed_value.lock().unwrap() = Some(vec![probe]);
            Ok(true)
        });
        let source = "{% for x in [1,2] %}{{ loop.changed(x) }};{% endfor %}";
        let output = env.template_from_str(source).unwrap().render(()).unwrap();
        assert_eq!(output, "True;True;");
        assert_eq!(*seen.lock().unwrap(), [true]);
        let object = captured.lock().unwrap().as_ref().unwrap().clone();
        assert_eq!(object.last_changed_value.lock().unwrap().as_ref().unwrap()[0].to_string(), "99");
    }

    #[test]
    fn changed_state_hook_receives_state_and_last_setter_wins() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let observed = seen.clone();
        let mut env = crate::Environment::new();
        env.set_runtime_loop_changed(|_, _| Ok(true));
        env.set_runtime_loop_changed_with_state(move |state, previous, current| {
            observed.lock().unwrap().push((state.name().to_owned(), previous[0].to_string(), current[0].to_string()));
            Ok(false)
        });
        let source = "{% for x in [1,2] %}{{ loop.changed(x) }};{% endfor %}";
        assert_eq!(env.template_from_named_str("loop-test", source).unwrap().render(()).unwrap(), "True;False;");
        assert_eq!(*seen.lock().unwrap(), [("loop-test".into(), "1".into(), "2".into())]);
        env.set_runtime_loop_changed(|_, _| Ok(true));
        assert_eq!(env.template_from_named_str("loop-test", source).unwrap().render(()).unwrap(), "True;True;");
        assert_eq!(seen.lock().unwrap().len(), 1);
    }

    fn recursion_uses_called_loop_depth_not_innermost_loop() {
        let env = crate::Environment::new();
        let source = "{% for n in [1] recursive %}{{loop.depth}}{% if n == 1 %}{% set recur=loop %}{% for y in [1] %}{{loop.depth}}{{recur([2])}}{% endfor %}{% endif %}{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "112");
    }

    #[derive(Debug)]
    struct Stream { events: Arc<Mutex<Vec<String>>>, failing: bool }
    impl Object for Stream {
        fn repr(self: &Arc<Self>) -> ObjectRepr { ObjectRepr::Iterable }
        fn enumerator_len(self: &Arc<Self>) -> Option<usize> { None }
        fn enumerate(self: &Arc<Self>) -> Enumerator {
            let events = self.events.clone(); let failing = self.failing;
            Enumerator::Iter(Box::new((0..3).map(move |n| {
                events.lock().unwrap().push(format!("yield:{n}"));
                if failing && n == 1 { Value::from(Error::new(ErrorKind::InvalidOperation, "iterator failed")) }
                else { Value::from(n) }
            })))
        }
    }
    fn render(source: &str, failing: bool) -> (Result<String, Error>, Vec<String>) {
        let events = Arc::new(Mutex::new(Vec::new())); let ticks = events.clone();
        let mut env = crate::Environment::new();
        env.add_function("tick", move || { ticks.lock().unwrap().push("tick".into()); "" });
        let context: Value = [("values", Value::from_object(Stream { events: events.clone(), failing }))].into_iter().collect();
        let result = env.template_from_str(source).unwrap().render(context);
        let events = events.lock().unwrap().clone(); (result, events)
    }
    #[test]
    fn general_filtered_predicates_run_when_the_loop_needs_an_item() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let mut env = crate::Environment::new();
        let log = events.clone();
        env.add_function("log", move |value: i64| { log.lock().unwrap().push(format!("pred{value}")); true });
        let log = events.clone();
        env.add_function("body", move |value: i64| { log.lock().unwrap().push(format!("body{value}")); String::new() });
        let source = "{% for x in [1, 2, 3] if log(x) and x != 2 %}{{ body(x) }}{{ loop.index }};{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "1;2;");
        assert_eq!(*events.lock().unwrap(), ["pred1", "body1", "pred2", "pred3", "body3"]);
    }

    #[test]
    fn host_delegate_pulls_filtered_items_through_the_vm() {
        use crate::vm::loop_object::{Loop, LoopDelegate, LoopHandoff};
        #[derive(Debug)]
        struct Host { source: std::sync::Weak<Loop> }
        impl LoopDelegate for Host {
            fn advance(&self) -> Result<Option<Value>, Error> { self.source.upgrade().unwrap().next_remaining_item() }
            fn get(&self, _: &str) -> Result<Option<Value>, Error> { Ok(Some(Value::from("host"))) }
            fn call_method(&self, _: &State, _: &str, _: &[Value]) -> Result<Value, Error> { Ok(Value::UNDEFINED) }
            fn value(&self) -> Value { Value::from("host loop") }
        }
        let events = Arc::new(Mutex::new(Vec::new()));
        let mut env = crate::Environment::new();
        let log = events.clone();
        env.add_test("kept", move |value: i64| { log.lock().unwrap().push(value); value % 2 == 1 });
        env.add_function("take", |value: Value| -> Result<String, Error> {
            let source = value.downcast_object::<Loop>().unwrap();
            let weak = Arc::downgrade(&source);
            source.delegate_with(|handoff: LoopHandoff| {
                assert!(handoff.after.is_none());
                Ok(Arc::new(Host { source: weak }))
            })?;
            Ok(String::new())
        });
        let source = "{% for x in [1, 2, 3, 4, 5] if x is kept %}{{ take(loop) }}{{ x }}{{ loop.index }};{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "1host;3host;5host;");
        assert_eq!(*events.lock().unwrap(), [1, 2, 3, 4, 5]);
    }

    #[test]
    fn handoff_hook_and_retained_predicate_state_outlive_the_render() {
        use crate::vm::loop_object::{Loop, LoopDelegate};
        #[derive(Debug)]
        struct Host;
        impl LoopDelegate for Host {
            fn advance(&self) -> Result<Option<Value>, Error> { Ok(None) }
            fn get(&self, key: &str) -> Result<Option<Value>, Error> { Ok(Some(Value::from(format!("host {key}")))) }
            fn call_method(&self, _: &State, _: &str, _: &[Value]) -> Result<Value, Error> { Ok(Value::UNDEFINED) }
            fn value(&self) -> Value { Value::UNDEFINED }
        }
        let retained = Arc::new(Mutex::new(None));
        let events = Arc::new(Mutex::new(Vec::new()));
        let mut env = crate::Environment::new();
        let log = events.clone();
        env.add_test("kept", move |value: i64| { log.lock().unwrap().push(value); value != 2 });
        let keep = retained.clone();
        env.set_runtime_loop_handoff(move |_, target, field| {
            if field == "start" { return Ok(()); }
            assert_eq!(field, "length");
            let source = target.downcast_object::<Loop>().unwrap();
            source.delegate_with(|_| Ok(Arc::new(Host)))?;
            // SAFETY: the environment is an Environment<'static>.
            unsafe { source.retain_predicate_state() };
            *keep.lock().unwrap() = Some(source);
            Ok(())
        });
        env.enable_shared_loaded_cache();
        let compiled = env.compile_owned_template("case".into(),
            "{% for x in [1, 2, 3] if x is kept %}{{ loop.length }};{% endfor %}".into()).unwrap();
        assert_eq!(env.template_from_owned(&compiled).render(()).unwrap(), "host length;");
        drop(compiled);
        drop(env);
        assert_eq!(*events.lock().unwrap(), [1]);
        let source = retained.lock().unwrap().take().unwrap();
        // The predicates of the remaining items run in the retained copy.
        assert_eq!(source.next_remaining_item().unwrap(), Some(Value::from(3)));
        assert_eq!(source.next_remaining_item().unwrap(), None);
        assert_eq!(*events.lock().unwrap(), [1, 2, 3]);
    }

    #[test]
    fn recursion_hook_can_complete_the_call_or_recurse_from_host() {
        use crate::vm::loop_object::{Loop, LoopCallOutcome};
        let mut env = crate::Environment::new();
        env.set_runtime_loop_recursion(|state, called, args, _| {
            if args[0].as_str() == Some("skip") { return Ok(Value::from_object(LoopCallOutcome(Value::from("done")))); }
            let source = called.downcast_object::<Loop>().unwrap();
            let rendered = crate::with_host_state(state, || source.recurse_from_host(args[0].clone()))?;
            Ok(Value::from_object(LoopCallOutcome(Value::from(format!("<{rendered}>")))))
        });
        // Expression calls go through the hook; a bare `{{ loop(x) }}` uses FastRecurse.
        let source = "{% for x in [1] recursive %}{% if x == 1 %}{{ loop([2]) ~ '' }}|{{ loop('skip') ~ '' }}{% else %}{{ x }}:{{ loop.depth }}{% endif %}{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "<2:2>|done");
    }

    #[test]
    fn filtered_loop_resolves_predicate_context_names_once_at_start() {
        #[derive(Debug)]
        struct Root { lookups: Arc<Mutex<Vec<String>>> }
        impl Object for Root {
            fn get_value(self: &Arc<Self>, key: &Value) -> Option<Value> {
                let key = key.as_str()?;
                let mut lookups = self.lookups.lock().unwrap();
                lookups.push(key.to_owned());
                // Every lookup of `limit` sees a larger value; the loop keeps the first.
                match key {
                    "xs" => Some(Value::from(vec![1, 2, 3, 4])),
                    "limit" => Some(Value::from(lookups.iter().filter(|name| *name == "limit").count())),
                    _ => None,
                }
            }
        }
        let lookups = Arc::new(Mutex::new(Vec::new()));
        let starts = Arc::new(Mutex::new(0));
        let mut env = crate::Environment::new();
        let counted = starts.clone();
        env.set_runtime_loop_handoff(move |_, _, field| {
            if field == "start" { *counted.lock().unwrap() += 1; }
            Ok(())
        });
        let root = Value::from_object(Root { lookups: lookups.clone() });
        let source = "{% for x in xs if x > limit %}{{ x }}{{ loop.index }};{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(root).unwrap(), "21;32;43;");
        assert_eq!(*lookups.lock().unwrap(), ["xs", "limit"]);
        assert_eq!(*starts.lock().unwrap(), 1);
    }

    #[test]
    fn constructed_filtered_loop_resolves_predicate_names_after_start() {
        #[derive(Debug)]
        struct Root { events: Arc<Mutex<Vec<String>>> }
        impl Object for Root {
            fn get_value(self: &Arc<Self>, key: &Value) -> Option<Value> {
                let key = key.as_str()?;
                self.events.lock().unwrap().push(format!("lookup:{key}"));
                match key { "xs" => Some(Value::from(vec![1, 2, 3])), "limit" => Some(Value::from(1)), _ => None }
            }
        }
        let events = Arc::new(Mutex::new(Vec::new()));
        let mut env = crate::Environment::new();
        let asked = events.clone();
        env.set_runtime_loop_constructed(move |_| { asked.lock().unwrap().push("constructed?".to_owned()); Ok(true) });
        let started = events.clone();
        env.set_runtime_loop_handoff(move |_, _, field| {
            if field == "start" { started.lock().unwrap().push("start".to_owned()); }
            Ok(())
        });
        let root = Value::from_object(Root { events: events.clone() });
        let source = "{% for x in xs if x > limit %}{{ x }}{{ loop.index }};{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(root.clone()).unwrap(), "21;32;");
        // The captured name resolves after the start handoff.
        assert_eq!(*events.lock().unwrap(), ["lookup:xs", "constructed?", "constructed?", "start", "lookup:limit"]);
        // Without a loop variable no loop object is built, so the hook is not asked.
        events.lock().unwrap().clear();
        let source = "{% for x in xs if x > limit %}{{ x }};{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(root.clone()).unwrap(), "2;3;");
        assert!(!events.lock().unwrap().iter().any(|event| event == "constructed?" || event == "start"));
        // A general predicate's names resolve from the enclosing base after it.
        events.lock().unwrap().clear();
        env.set_lazy_filtered_predicates(true);
        let source = "{% for x in xs if x > limit + 0 %}{{ x }}{{ loop.index }};{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(root).unwrap(), "21;32;");
        assert_eq!(*events.lock().unwrap(), ["lookup:xs", "constructed?", "start", "lookup:limit"]);
    }

    #[test]
    fn host_delegate_takes_over_iteration_attributes_and_methods() {
        use crate::vm::loop_object::{Loop, LoopDelegate, LoopHandoff};
        #[derive(Debug)]
        struct Host { source: std::sync::Weak<Loop>, index: Mutex<usize>, after: Mutex<Option<Value>> }
        impl LoopDelegate for Host {
            fn advance(&self) -> Result<Option<Value>, Error> {
                let after = self.after.lock().unwrap().take();
                let item = match after { Some(item) => Some(item), None => self.source.upgrade().unwrap().next_remaining_item()? };
                if item.is_some() { *self.index.lock().unwrap() += 10; }
                Ok(item)
            }
            fn get(&self, key: &str) -> Result<Option<Value>, Error> {
                Ok(match key { "index" => Some(Value::from(*self.index.lock().unwrap())), _ => None })
            }
            fn call_method(&self, _: &State, name: &str, _: &[Value]) -> Result<Value, Error> {
                Ok(Value::from(format!("host {name}")))
            }
            fn value(&self) -> Value { Value::from("host loop") }
        }
        let mut env = crate::Environment::new();
        env.add_function("take", |value: Value| -> Result<String, Error> {
            let source = value.downcast_object::<Loop>().unwrap();
            let weak = Arc::downgrade(&source);
            source.delegate_with(|handoff: LoopHandoff| {
                assert_eq!((handoff.index0, handoff.depth0, handoff.recursive), (Some(0), 0, false));
                assert_eq!(handoff.current, Some(Value::from(1)));
                Ok(Arc::new(Host { source: weak, index: Mutex::new(100), after: Mutex::new(handoff.after) }))
            })?;
            Ok(String::new())
        });
        let source = "{% for x in [1, 2, 3] %}{% if loop.first and loop.nextitem %}{{ take(loop) }}{% endif %}{{ x }}:{{ loop.index }}:{{ loop.cycle(1) }};{% endfor %}";
        let output = env.template_from_str(source).unwrap().render(()).unwrap();
        // The peeked item (2) is handed over and the host yields it first.
        assert_eq!(output, "1:100:host cycle;2:110:host cycle;3:120:host cycle;");
    }

    #[test]
    fn start_handoff_runs_for_loops_with_a_loop_variable() {
        let starts = Arc::new(Mutex::new(Vec::new()));
        let mut env = crate::Environment::new();
        let counted = starts.clone();
        env.set_runtime_loop_handoff(move |_, _, field| {
            if field == "start" { counted.lock().unwrap().push(()); }
            Ok(())
        });
        for (source, expected) in [("{% for x in [1, 2] %}{{ x }}{% endfor %}", 0),
                                   ("{% for x in [1, 2] %}{{ loop.index }}{% endfor %}", 1),
                                   ("{% for x in [1, 2] if x %}{{ loop.index }}{% endfor %}", 1)] {
            starts.lock().unwrap().clear();
            env.template_from_str(source).unwrap().render(()).unwrap();
            assert_eq!(starts.lock().unwrap().len(), expected, "{source}");
        }
    }

    #[test]
    fn host_delegate_is_told_when_the_vm_pops_the_loop_frame() {
        use crate::vm::loop_object::{Loop, LoopDelegate, LoopHandoff};
        #[derive(Debug)]
        struct Host { releases: Arc<Mutex<Vec<usize>>> }
        impl LoopDelegate for Host {
            fn advance(&self) -> Result<Option<Value>, Error> { Ok(None) }
            fn get(&self, _: &str) -> Result<Option<Value>, Error> { Ok(None) }
            fn call_method(&self, _: &State, _: &str, _: &[Value]) -> Result<Value, Error> { Ok(Value::UNDEFINED) }
            fn value(&self) -> Value { Value::from("host loop") }
            fn released(&self, others: usize) { self.releases.lock().unwrap().push(others); }
        }
        for (source, expected) in [
            // Only the frame holds the loop when it is popped.
            ("{% for x in [1, 2] %}{{ take(loop) }}{% endfor %}", vec![0]),
            // A macro defined in the loop keeps a reference to it.
            ("{% for x in [1, 2] %}{{ take(loop) }}{% macro m() %}{{ loop.index }}{% endmacro %}{{ keep(m) }}{% endfor %}", vec![1]),
        ] {
            let releases = Arc::new(Mutex::new(Vec::new()));
            let kept = Arc::new(Mutex::new(Vec::new()));
            let mut env = crate::Environment::new();
            let observed = releases.clone();
            env.add_function("take", move |value: Value| -> Result<String, Error> {
                let source = value.downcast_object::<Loop>().unwrap();
                let releases = observed.clone();
                source.delegate_with(|_: LoopHandoff| Ok(Arc::new(Host { releases })))?;
                Ok(String::new())
            });
            let keep = kept.clone();
            env.add_function("keep", move |value: Value| { keep.lock().unwrap().push(value); String::new() });
            env.template_from_str(source).unwrap().render(()).unwrap();
            assert_eq!(*releases.lock().unwrap(), expected, "{source}");
        }
    }

    #[test]
    fn lazy_last_peeks_at_the_access_point() {
        let (result, events) = render("{% for x in values %}{{x}}{% if not loop.last %},{% endif %}{{tick()}}{% endfor %}", false);
        assert_eq!(result.unwrap(), "0,1,2");
        assert_eq!(events, ["yield:0", "yield:1", "tick", "yield:2", "tick", "tick"]);
    }
    #[test]
    fn lazy_length_buffers_remaining_items_once() {
        let (result, events) = render("{% for x in values %}{{x}}:{{loop.length}}:{{loop.revindex}};{{tick()}}{% endfor %}", false);
        assert_eq!(result.unwrap(), "0:3:3;1:3:2;2:3:1;");
        assert_eq!(events, ["yield:0", "yield:1", "yield:2", "tick", "tick", "tick"]);
    }
    #[test]
    fn materialization_stops_at_the_first_error() {
        let (result, events) = render("{% for x in values %}{{loop.length}}{{tick()}}{% endfor %}", true);
        assert!(result.unwrap_err().to_string().contains("iterator failed"));
        assert_eq!(events, ["yield:0", "yield:1"]);
    }

    #[test]
    fn legacy_loop_iterator_materialization_protocol_still_works() {
        struct LegacyIterator(std::vec::IntoIter<Value>);
        impl Iterator for LegacyIterator {
            type Item = Value;
            fn next(&mut self) -> Option<Value> { self.0.next() }
        }
        impl crate::value::LoopIterator for LegacyIterator {
            fn materialization_iterator(&mut self) -> Option<Result<(Box<dyn Iterator<Item = Value> + Send + Sync>, Option<usize>), Error>> {
                let rest = std::mem::take(&mut self.0);
                Some(Ok((Box::new(rest), Some(5))))
            }
        }
        #[derive(Debug)]
        struct Source;
        impl Object for Source {
            fn enumerator_len(self: &Arc<Self>) -> Option<usize> { None }
            fn loop_iterator(self: &Arc<Self>, _: bool) -> Option<Result<Box<dyn crate::value::LoopIterator>, Error>> {
                Some(Ok(Box::new(LegacyIterator(vec![Value::from(1), Value::from(2)].into_iter()))))
            }
        }
        let env = crate::Environment::new();
        let context: Value = [("values", Value::from_object(Source))].into_iter().collect();
        let result = env.template_from_str("{% for x in values %}{{loop.length}}:{{x}};{% endfor %}").unwrap().render(context).unwrap();
        assert_eq!(result, "6:1;6:2;");
    }

    #[test]
    fn post_lookup_exposes_original_loop_and_key_and_can_replace_result() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let observed = events.clone();
        let mut env = crate::Environment::new();
        env.set_runtime_attribute_handlers(|| Ok(Value::from("attribute dispatcher")),
            |_, _, _, _| Ok(None), |_| {});
        env.set_runtime_item_handlers(|| Ok(Value::from("item dispatcher")),
            |_, _, _, _| Ok(None), |_| {});
        env.set_runtime_loop_post_lookup(move |_, dispatcher, target, attribute, key, value| {
            let loop_object = target.downcast_object::<Loop>().expect("native loop target");
            let identity = Arc::as_ptr(&loop_object) as usize;
            let name = attribute.or_else(|| key.and_then(Value::as_str)).unwrap();
            observed.lock().unwrap().push((identity, dispatcher.to_string(), name.to_owned(), value.to_string()));
            Ok(Value::from("replacement"))
        });
        let source = "{% for x in [1, 2] %}{{ loop.index }}:{{ loop['index'] }};{% endfor %}";
        let output = env.template_from_str(source).unwrap().render(()).unwrap();
        assert_eq!(output, "replacement:replacement;replacement:replacement;");
        let events = events.lock().unwrap();
        assert_eq!(events.len(), 4);
        assert!(events.iter().all(|event| event.0 == events[0].0));
        assert_eq!((&events[0].1, &events[0].2, &events[0].3), (&"attribute dispatcher".into(), &"index".into(), &"1".into()));
        assert_eq!((&events[1].1, &events[1].2, &events[1].3), (&"item dispatcher".into(), &"index".into(), &"1".into()));
        assert_eq!(events[2].3, "2");
        assert_eq!(events[3].3, "2");
    }

    #[test]
    fn advance_hook_retains_loop_identity_without_changing_iteration() {
        let observed = Arc::new(Mutex::new(Vec::<(Arc<Loop>, usize)>::new()));
        let captured = observed.clone();
        let mut env = crate::Environment::new();
        env.set_runtime_loop_advance(move |_, value| {
            let object = value.downcast_object::<Loop>().expect("native loop");
            let index = object.idx.load(Ordering::Relaxed);
            captured.lock().unwrap().push((object, index));
            Ok(())
        });
        let source = "{% for x in [1,2] %}{% set keep = loop %}{{x}}{% for y in [3,4] %}{% set keep = loop %}{{y}}{% endfor %}{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "134234");
        let events = observed.lock().unwrap();
        assert_eq!(events.iter().map(|(_, index)| *index).collect::<Vec<_>>(), [0, 0, 1, 1, 0, 1]);
        assert!(Arc::ptr_eq(&events[0].0, &events[3].0));
        assert!(Arc::ptr_eq(&events[1].0, &events[2].0));
        assert!(Arc::ptr_eq(&events[4].0, &events[5].0));
        assert!(!Arc::ptr_eq(&events[1].0, &events[4].0));
        assert_eq!(events[0].0.idx.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn advance_hook_observes_only_valid_filtered_items() {
        let observed = Arc::new(Mutex::new(Vec::new()));
        let captured = observed.clone();
        let mut env = crate::Environment::new();
        env.set_runtime_loop_advance(move |_, value| {
            let loop_value = value.downcast_object::<Loop>().expect("native loop");
            captured.lock().unwrap().push(loop_value.idx.load(Ordering::Relaxed));
            Ok(())
        });
        let source = "{% for x in [1,2,3,4] if x > 2 %}{{loop.index}}:{{x}};{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "1:3;2:4;");
        assert_eq!(*observed.lock().unwrap(), [0, 1]);
    }

    #[test]
    fn host_advance_shares_live_iterator_with_vm() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let stream = Value::from_object(Stream { events: events.clone(), failing: false });
        let callback_events = events.clone();
        let calls = Arc::new(AtomicUsize::new(0));
        let invoked = calls.clone();
        let mut env = crate::Environment::new();
        env.add_function("advance", move |value: Value| -> Result<String, Error> {
            let object = value.downcast_object::<Loop>().unwrap();
            callback_events.lock().unwrap().push("host".into());
            if invoked.fetch_add(1, Ordering::Relaxed) == 0 {
                let item = object.advance_from_host()?.unwrap();
                return Ok(format!("{}:{}", item, object.get_value_by_str("index").unwrap()));
            }
            Ok("-".into())
        });
        let context: Value = [("values", stream)].into_iter().collect();
        let source = "{% for x in values %}{{x}}:{{loop.index}}:{{advance(loop)}};{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(context).unwrap(), "0:1:1:2;2:3:-;");
        assert_eq!(*events.lock().unwrap(), ["yield:0", "host", "yield:1", "yield:2", "host"]);
        assert_eq!(calls.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn previous_item_finalizer_can_reenter_after_index_advances() {
        use std::sync::Weak;

        #[derive(Debug)]
        struct Probe {
            owner: Arc<Mutex<Weak<Loop>>>,
            seen: Arc<Mutex<Vec<(bool, String)>>>,
        }
        impl Object for Probe {}
        impl Drop for Probe {
            fn drop(&mut self) {
                if let Some(owner) = self.owner.lock().unwrap().upgrade() {
                    let unlocked = owner.iterator().is_ok();
                    let index = owner.get_value_by_str("index").unwrap().to_string();
                    self.seen.lock().unwrap().push((unlocked, index));
                }
            }
        }

        let owner = Arc::new(Mutex::new(Weak::new()));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let first = Value::from_object(Probe { owner: owner.clone(), seen: seen.clone() });
        let iter = ValueIter::from_iterator(Box::new(vec![first, Value::from(2), Value::from(3)].into_iter()));
        let mut state = LoopState::new(iter, Value::from(()), 0, true, None, None, None, None);
        *owner.lock().unwrap() = Arc::downgrade(&state.object);
        drop(state.next());
        drop(state.next());
        drop(state.next());
        assert_eq!(*seen.lock().unwrap(), [(true, "3".to_owned())]);
    }

    #[test]
    fn previous_item_finalizer_can_advance_same_loop() {
        use std::sync::Weak;

        #[derive(Debug)]
        struct Probe {
            owner: Arc<Mutex<Weak<Loop>>>,
            seen: Arc<Mutex<Vec<(String, String)>>>,
        }
        impl Object for Probe {}
        impl Drop for Probe {
            fn drop(&mut self) {
                if let Some(owner) = self.owner.lock().unwrap().upgrade() {
                    let next = owner.advance_from_host().unwrap().unwrap().to_string();
                    let index = owner.get_value_by_str("index").unwrap().to_string();
                    self.seen.lock().unwrap().push((next, index));
                }
            }
        }

        for filtered in [false, true] {
            let owner = Arc::new(Mutex::new(Weak::new()));
            let seen = Arc::new(Mutex::new(Vec::new()));
            let first = Value::from_object(Probe { owner: owner.clone(), seen: seen.clone() });
            let iter = ValueIter::from_iterator(Box::new(vec![first, Value::from(2), Value::from(3), Value::from(4)].into_iter()));
            let mut state = LoopState::new(iter, Value::from(()), 0, true, None, None, filtered.then_some(1), None);
            *owner.lock().unwrap() = Arc::downgrade(&state.object);
            if filtered {
                while let Some(value) = state.object.filtered_next_raw().unwrap() {
                    state.object.filtered_push(value).unwrap();
                }
            }
            drop(state.next());
            drop(state.next());
            assert_eq!(state.object.advance_from_host().unwrap().unwrap().to_string(), "3");
            assert_eq!(*seen.lock().unwrap(), [("4".to_owned(), "4".to_owned())]);
            assert_eq!(state.object.get_value_by_str("index").unwrap().to_string(), "4");
            assert_eq!(state.object.get_value_by_str("previtem").unwrap().to_string(), "2");
            assert!(state.next().is_none());
        }
    }

    #[test]
    fn host_filtered_input_does_not_advance_loop_or_consume_lookahead() {
        let iter = ValueIter::from_iterator(Box::new([1, 2, 3].into_iter().map(Value::from)));
        let mut state = LoopState::new(iter, Value::from(()), 0, true, None, None, Some(1), None);
        let object = state.object.clone();
        for _ in 0..3 {
            let value = object.filtered_next_raw().unwrap().unwrap();
            object.filtered_push(value).unwrap();
        }
        assert_eq!(state.next().unwrap().to_string(), "1");
        assert_eq!(object.get_value_by_str("nextitem").unwrap().to_string(), "2");
        assert_eq!(object.take_filtered_input_from_host().unwrap().unwrap().to_string(), "3");
        assert_eq!(object.get_value_by_str("index").unwrap().to_string(), "1");
        assert_eq!(object.get_value_by_str("nextitem").unwrap().to_string(), "2");
        assert!(object.take_filtered_input_from_host().is_err());
        assert!(object.filtered_next_raw().unwrap().is_none());
        assert!(object.take_filtered_input_from_host().unwrap().is_none());
        assert_eq!(state.next().unwrap().to_string(), "2");
        assert_eq!(object.get_value_by_str("index").unwrap().to_string(), "2");
    }

    #[test]
    fn host_filtered_input_rejects_reentry_without_consuming() {
        let iter = ValueIter::from_iterator(Box::new([Value::from(1)].into_iter()));
        let state = LoopState::new(iter, Value::from(()), 0, true, None, None, Some(1), None);
        let object = state.object;
        let value = object.filtered_next_raw().unwrap().unwrap();
        object.filtered_push(value).unwrap();
        let active = object.filtered.as_ref().unwrap().lock().unwrap();
        assert!(object.take_filtered_input_from_host().unwrap_err().to_string().contains("already active"));
        drop(active);
        assert_eq!(object.take_filtered_input_from_host().unwrap().unwrap().to_string(), "1");
        assert_eq!(object.idx.load(Ordering::Relaxed), !0usize);
    }

    #[test]
    fn host_filtered_input_rejects_unfiltered_loop() {
        let iter = ValueIter::from_iterator(Box::new([Value::from(1)].into_iter()));
        let mut state = LoopState::new(iter, Value::from(()), 0, true, None, None, None, None);
        assert!(state.object.take_filtered_input_from_host().unwrap_err().to_string().contains("no filtered input"));
        assert_eq!(state.next().unwrap().to_string(), "1");
    }

    #[test]
    fn host_advance_rejects_filtered_loop_without_consuming_it() {
        let count = Arc::new(AtomicUsize::new(0));
        let observed = count.clone();
        let iter = ValueIter::from_iterator(Box::new((1..=2).map(move |n| {
            observed.fetch_add(1, Ordering::Relaxed);
            Value::from(n)
        })));
        let mut state = LoopState::new(iter, Value::from(()), 0, true, None, None, Some(1), None);
        assert!(state.object.advance_from_host().unwrap_err().to_string().contains("filtered loop"));
        assert_eq!(count.load(Ordering::Relaxed), 0);
        let accepted = state.object.filtered_next_raw().unwrap().unwrap();
        state.object.filtered_push(accepted).unwrap();
        assert_eq!(state.next().unwrap().to_string(), "1");
        assert_eq!(count.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn host_advance_consumes_only_previously_accepted_filtered_items() {
        let count = Arc::new(AtomicUsize::new(0));
        let observed = count.clone();
        let iter = ValueIter::from_iterator(Box::new((1..=2).map(move |n| {
            observed.fetch_add(1, Ordering::Relaxed);
            Value::from(n)
        })));
        let state = LoopState::new(iter, Value::from(()), 0, true, None, None, Some(1), None);
        let first = state.object.filtered_next_raw().unwrap().unwrap();
        state.object.filtered_push(first).unwrap();
        {
            let active = state.object.filtered.as_ref().unwrap().lock().unwrap();
            assert!(state.object.advance_from_host().unwrap_err().to_string().contains("already active"));
            drop(active);
        }
        assert_eq!(state.object.advance_from_host().unwrap().unwrap().to_string(), "1");
        assert_eq!(state.object.get_value_by_str("index").unwrap().to_string(), "1");
        assert_eq!(count.load(Ordering::Relaxed), 1);
        assert!(state.object.advance_from_host().is_err());
        assert_eq!(count.load(Ordering::Relaxed), 1);
        let second = state.object.filtered_next_raw().unwrap().unwrap();
        state.object.filtered_push(second).unwrap();
        assert!(!state.object.get_value_by_str("last").unwrap().is_true());
        assert!(state.object.filtered_next_raw().unwrap().is_none());
        assert_eq!(state.object.advance_from_host().unwrap().unwrap().to_string(), "2");
        assert!(state.object.advance_from_host().unwrap().is_none());
        assert_eq!(state.object.get_value_by_str("index").unwrap().to_string(), "2");
        assert_eq!(count.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn host_advance_preserves_index_after_error_and_exhaustion() {
        let iter = ValueIter::from_iterator(Box::new(vec![
            Value::from(1), Value::from(Error::new(ErrorKind::InvalidOperation, "failed")), Value::from(2),
        ].into_iter()));
        let mut state = LoopState::new(iter, Value::from(()), 0, true, None, None, None, None);
        assert_eq!(state.next().unwrap().to_string(), "1");
        let error = state.object.advance_from_host().unwrap_err();
        assert!(error.to_string().contains("failed"));
        assert_eq!(state.object.get_value_by_str("index").unwrap().to_string(), "1");
        assert_eq!(state.object.advance_from_host().unwrap().unwrap().to_string(), "2");
        assert_eq!(state.object.get_value_by_str("index").unwrap().to_string(), "2");
        assert!(state.object.advance_from_host().unwrap().is_none());
        assert_eq!(state.object.get_value_by_str("index").unwrap().to_string(), "2");
        assert!(state.next().is_none());
    }

    #[test]
    fn host_advance_rejects_reentrant_iterator_without_consuming() {
        let iter = ValueIter::from_iterator(Box::new(vec![Value::from(1)].into_iter()));
        let mut state = LoopState::new(iter, Value::from(()), 0, true, None, None, None, None);
        let active = state.object.iterator().unwrap();
        assert!(state.object.advance_from_host().unwrap_err().to_string().contains("already active"));
        drop(active);
        assert_eq!(state.next().unwrap().to_string(), "1");
    }

    #[test]
    fn filtered_input_depth_is_scoped_to_filter_evaluation() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let selected = events.clone();
        let body = events.clone();
        let mut env = crate::Environment::new();
        env.add_test("selected", move |state: &State, value: i64| {
            selected.lock().unwrap().push(("predicate", state.filtered_loop_depth()));
            value > 1
        });
        env.add_function("record", move |state: &State| {
            body.lock().unwrap().push(("body", state.filtered_loop_depth()));
            ""
        });
        let source = "{% for x in [1,2,3] if x is selected %}{{record()}}{{loop.index}}{% endfor %}{{record()}}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "12");
        assert_eq!(*events.lock().unwrap(), [
            ("predicate", 1), ("predicate", 1), ("body", 0),
            ("predicate", 1), ("body", 0), ("body", 0),
        ]);
    }

    #[test]
    fn filtered_input_depth_covers_streamed_and_collected_paths() {
        for (source, expected) in [
            ("{% for x in [1,2] if x is selected %}{{record()}}{{x}}{% endfor %}",
             vec![("predicate", 1), ("body", 0), ("predicate", 1), ("body", 0)]),
            ("{% for x in [1,2] if x is selected %}{{record()}}{{loop.index}}{% else %}empty{% endfor %}",
             vec![("predicate", 1), ("predicate", 1), ("body", 0), ("body", 0)]),
        ] {
            let events = Arc::new(Mutex::new(Vec::new()));
            let selected = events.clone();
            let body = events.clone();
            let mut env = crate::Environment::new();
            env.add_test("selected", move |state: &State, _value: i64| {
                selected.lock().unwrap().push(("predicate", state.filtered_loop_depth()));
                true
            });
            env.add_function("record", move |state: &State| {
                body.lock().unwrap().push(("body", state.filtered_loop_depth()));
                ""
            });
            assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "12");
            assert_eq!(*events.lock().unwrap(), expected);
        }
    }

    #[test]
    fn filtered_input_scope_includes_truth_testing_and_unwinds_on_error() {
        #[derive(Debug)]
        struct TruthProbe {
            depth: Arc<AtomicUsize>,
            observed: Arc<Mutex<Vec<usize>>>,
            fail: bool,
        }
        impl Object for TruthProbe {
            fn is_true_fallible(self: &Arc<Self>) -> Result<bool, Error> {
                self.observed.lock().unwrap().push(self.depth.load(Ordering::Relaxed));
                if self.fail { Err(Error::new(ErrorKind::InvalidOperation, "truth failed")) } else { Ok(false) }
            }
        }
        for source in [
            "{% for x in [1] if x is selected %}{{x}}{% endfor %}",
            "{% for x in [1] if x is selected %}{{loop.index}}{% else %}empty{% endfor %}",
            "{% for x in [1] if x is selected %}{{loop.index}}{% endfor %}",
        ] {
            for fail in [false, true] {
                let observed = Arc::new(Mutex::new(Vec::new()));
                let handles = Arc::new(Mutex::new(Vec::<Arc<AtomicUsize>>::new()));
                let seen = observed.clone();
                let saved = handles.clone();
                let mut env = crate::Environment::new();
                env.set_preserve_test_results(true);
                env.add_test("selected", move |state: &State, _value: i64| {
                    let depth = state.filtered_loop_depth_handle();
                    saved.lock().unwrap().push(depth.clone());
                    Value::from_object(TruthProbe { depth, observed: seen.clone(), fail })
                });
                let result = env.template_from_str(source).unwrap().render(());
                assert_eq!(result.is_err(), fail);
                assert_eq!(*observed.lock().unwrap(), [1]);
                assert_eq!(handles.lock().unwrap()[0].load(Ordering::Relaxed), 0);
            }
        }
    }

    #[test]
    fn loop_next_hook_receives_filtered_scope_during_input_advance() {
        for source in [
            "{% for x in [1,2] if x is selected %}{{x}}{% endfor %}",
            "{% for x in [1,2] if x is selected %}{{loop.index}}{% endfor %}",
            "{% for x in [1,2] if x is selected %}{{loop.index}}{% else %}empty{% endfor %}",
        ] {
            let observed = Arc::new(Mutex::new(Vec::new()));
            let captured = observed.clone();
            let mut env = crate::Environment::new();
            env.add_test("selected", |_value: i64| true);
            env.set_runtime_loop_next(move |frames, depth, next| {
                if depth != 0 { captured.lock().unwrap().push((frames.len(), depth)); }
                next()
            });
            assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "12");
            assert_eq!(*observed.lock().unwrap(), [(1, 1), (1, 1), (1, 1)]);
        }
    }

    #[test]
    fn loop_next_hook_error_restores_filtered_scope() {
        let handles = Arc::new(Mutex::new(Vec::<Arc<AtomicUsize>>::new()));
        let saved = handles.clone();
        let mut env = crate::Environment::new();
        env.add_function("save", move |state: &State| {
            saved.lock().unwrap().push(state.filtered_loop_depth_handle());
            ""
        });
        env.add_test("selected", |_value: i64| true);
        env.set_runtime_loop_next(|_, depth, next| {
            if depth != 0 { Err(Error::new(ErrorKind::InvalidOperation, "next rejected")) } else { next() }
        });
        let source = "{{save()}}{% for x in [1] if x is selected %}{{x}}{% endfor %}";
        assert!(env.template_from_str(source).unwrap().render(()).is_err());
        assert_eq!(handles.lock().unwrap()[0].load(Ordering::Relaxed), 0);
    }

    #[test]
    fn advance_hook_skips_empty_and_failed_items() {
        let observed = Arc::new(Mutex::new(Vec::new()));
        let captured = observed.clone();
        let mut env = crate::Environment::new();
        env.set_runtime_loop_advance(move |_, value| {
            let loop_value = value.downcast_object::<Loop>().expect("native loop");
            captured.lock().unwrap().push(loop_value.idx.load(Ordering::Relaxed));
            Ok(())
        });
        let template = env.template_from_str("{% for x in items %}{% set keep = loop %}{{x}}{% endfor %}").unwrap();
        assert_eq!(template.render(crate::context!(items => Vec::<Value>::new())).unwrap(), "");
        let items = vec![Value::from(1), Value::from(Error::new(ErrorKind::InvalidOperation, "failed item"))];
        assert!(template.render(crate::context!(items => items)).is_err());
        assert_eq!(*observed.lock().unwrap(), [0]);
    }

    #[test]
    fn advance_hook_does_not_change_source_consumption() {
        for filtered in [false, true] {
            let source = if filtered {
                "{% for x in values if x > 0 %}{{loop.index}}{{tick()}}{% endfor %}"
            } else {
                "{% for x in values %}{{loop.index}}{{tick()}}{% endfor %}"
            };
            let observe = |hooked: bool| {
                let events = Arc::new(Mutex::new(Vec::new()));
                let mut env = crate::Environment::new();
                if hooked {
                    let captured = events.clone();
                    env.set_runtime_loop_advance(move |_, value| {
                        let object = value.downcast_object::<Loop>().expect("native loop");
                        captured.lock().unwrap().push(format!("advance:{}", object.idx.load(Ordering::Relaxed)));
                        Ok(())
                    });
                }
                let ticks = events.clone();
                env.add_function("tick", move || { ticks.lock().unwrap().push("tick".into()); "" });
                let context: Value = [("values", Value::from_object(Stream { events: events.clone(), failing: false }))].into_iter().collect();
                let output = env.template_from_str(source).unwrap().render(context).unwrap();
                let events = events.lock().unwrap().clone();
                (output, events)
            };
            let (expected, baseline) = observe(false);
            let (actual, hooked) = observe(true);
            assert_eq!(actual, expected);
            assert_eq!(hooked.iter().filter(|event| !event.starts_with("advance:")).cloned().collect::<Vec<_>>(), baseline);
            let advances = hooked.iter().filter(|event| event.starts_with("advance:")).cloned().collect::<Vec<_>>();
            if filtered {
                assert_eq!(advances, ["advance:0", "advance:1"]);
            } else {
                assert_eq!(advances, ["advance:0", "advance:1", "advance:2"]);
            }
        }
    }

    #[test]
    fn advance_hook_error_stops_before_the_loop_body() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let mut env = crate::Environment::new();
        env.set_runtime_loop_advance(|_, _| Err(Error::new(ErrorKind::InvalidOperation, "advance rejected")));
        let ticks = events.clone();
        env.add_function("tick", move || { ticks.lock().unwrap().push("tick"); "" });
        let result = env.template_from_str("{% for x in [1] %}{{loop.index}}{{tick()}}{% endfor %}").unwrap().render(());
        assert!(result.unwrap_err().to_string().contains("advance rejected"));
        assert!(events.lock().unwrap().is_empty());
    }

    #[test]
    fn advance_hook_tracks_lazy_filtered_loop_without_advancing_rejected_items() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let captured = events.clone();
        let mut env = crate::Environment::new();
        env.set_runtime_loop_advance(move |_, value| {
            let object = value.downcast_object::<Loop>().expect("native loop");
            captured.lock().unwrap().push(format!("advance:{}", object.idx.load(Ordering::Relaxed)));
            Ok(())
        });
        let ticks = events.clone();
        env.add_function("tick", move || { ticks.lock().unwrap().push("tick".into()); "" });
        let context: Value = [("values", Value::from_object(Stream { events: events.clone(), failing: false }))].into_iter().collect();
        let source = "{% for x in values if x is odd %}{{loop.index}}:{{x}}{{tick()}}{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(context).unwrap(), "1:1");
        assert_eq!(*events.lock().unwrap(), ["yield:0", "yield:1", "advance:0", "tick", "yield:2"]);
    }

    #[test]
    fn comparison_filter_yields_accepted_items_before_next_source_item() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let ticks = events.clone();
        let mut env = crate::Environment::new();
        env.add_function("tick", move || { ticks.lock().unwrap().push("tick".into()); "" });
        let context: Value = [("values", Value::from_object(Stream { events: events.clone(), failing: false }))].into_iter().collect();
        let source = "{% for x in values if x > 0 %}{{loop.index}}:{{x}}{{tick()}}{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(context).unwrap(), "1:12:2");
        assert_eq!(*events.lock().unwrap(), ["yield:0", "yield:1", "tick", "yield:2", "tick"]);
    }

    #[test]
    fn comparison_filter_retains_outer_binding_across_loop_local_assignment() {
        let env = crate::Environment::new();
        let source = "{% for x in values if x > threshold %}{{loop.index}}:{{x}};{% set threshold = 9 %}{% endfor %}";
        let context = crate::context!(values => vec![1, 2, 3], threshold => 0);
        assert_eq!(env.template_from_str(source).unwrap().render(context).unwrap(), "1:1;2:2;3:3;");
    }

    #[test]
    fn comparison_filter_reuses_direct_factory_argument() {
        let mut env = crate::Environment::new();
        env.add_function("values", |start: i64| vec![start, start + 1, start + 2]);
        let source = "{% for x in values(threshold) if x > threshold %}{{loop.index}}:{{x}};{% set threshold = 9 %}{% endfor %}";
        let context = crate::context!(threshold => 0);
        assert_eq!(env.template_from_str(source).unwrap().render(context).unwrap(), "1:1;2:2;");
    }

    #[test]
    fn comparison_filter_reuses_binding_inside_composite_argument() {
        let mut env = crate::Environment::new();
        env.add_function("values", |start: i64| vec![start, start + 1, start + 2]);
        let source = "{% for x in values(threshold + threshold) if x > threshold %}{{loop.index}}:{{x}};{% set threshold = 9 %}{% endfor %}";
        let context = crate::context!(threshold => 1);
        assert_eq!(env.template_from_str(source).unwrap().render(context).unwrap(), "1:2;2:3;3:4;");
    }

    #[test]
    fn prepared_use_hook_runs_for_callable_and_each_argument_use() {
        let uses = Arc::new(Mutex::new(0));
        let observed = uses.clone();
        let mut env = crate::Environment::new();
        env.add_function("values", |start: i64| vec![start]);
        env.set_runtime_prepared_use(move |value| { *observed.lock().unwrap() += 1; Ok(value) });
        let source = "{% for x in values(threshold + threshold) if x > threshold %}{{loop.index}}{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(crate::context!(threshold => 1)).unwrap(), "1");
        assert_eq!(*uses.lock().unwrap(), 3);
    }

    #[test]
    fn composite_argument_bindings_resolve_once_in_source_order() {
        let names = Arc::new(Mutex::new(Vec::new()));
        let observed = names.clone();
        let mut env = crate::Environment::new();
        env.add_function("values", |start: i64| vec![start]);
        env.set_runtime_prepared_lookup(move |_, name| {
            if name == "threshold" || name == "offset" {
                observed.lock().unwrap().push(name.to_owned());
                Some(Some(Value::from(1)))
            } else { None }
        });
        let source = "{% for x in values(offset + threshold + offset) if x > threshold %}{{loop.index}}{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "1");
        assert_eq!(*names.lock().unwrap(), ["offset", "threshold"]);
    }

    #[test]
    fn prepared_factory_preserves_missing_and_explicit_undefined_errors() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let observed = events.clone();
        let mut env = crate::Environment::new();
        env.add_function("argument", move || { observed.lock().unwrap().push("argument"); 1 });
        let direct = env.template_from_str("{{values(argument())}}").unwrap();
        let filtered = env.template_from_str("{% for x in values(argument()) if x > threshold %}{{loop.index}}{% endfor %}").unwrap();
        for context in [crate::context!(threshold => 0), crate::context!(threshold => 0, values => Value::UNDEFINED)] {
            events.lock().unwrap().clear();
            let expected = direct.render(context.clone()).unwrap_err();
            let expected_events = events.lock().unwrap().clone();
            events.lock().unwrap().clear();
            let actual = filtered.render(context).unwrap_err();
            assert_eq!(actual.kind(), expected.kind());
            assert_eq!(actual.detail(), expected.detail());
            assert_eq!(*events.lock().unwrap(), expected_events);
            assert_eq!(expected_events, ["argument"]);
        }
    }

    #[test]
    fn prepared_lookup_intercepts_only_captured_bindings() {
        let mut env = crate::Environment::new();
        env.add_function("values", |start: i64| vec![start, start + 1]);
        env.set_runtime_prepared_lookup(|_, name| {
            if name == "threshold" { Some(Some(Value::from(1))) } else { None }
        });
        let source = "{{threshold}}:{% for x in values(1) if x > threshold %}{{loop.index}}:{{x}};{% endfor %}";
        let context = crate::context!(threshold => 0);
        assert_eq!(env.template_from_str(source).unwrap().render(context).unwrap(), "0:1:2;");
    }

    #[test]
    fn prepared_lookup_can_continue_to_global() {
        let mut env = crate::Environment::new();
        env.add_function("values", |start: i64| vec![start, start + 1]);
        env.add_global("threshold", Value::from(1));
        env.set_runtime_prepared_lookup(|_, name| if name == "threshold" { Some(None) } else { None });
        let source = "{% for x in values(1) if x > threshold %}{{loop.index}}:{{x}};{% endfor %}";
        let context = crate::context!(threshold => 0);
        assert_eq!(env.template_from_str(source).unwrap().render(context).unwrap(), "1:2;");
    }
}
