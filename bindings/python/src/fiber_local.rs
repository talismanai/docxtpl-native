//! Thread-local slots that code on a render fiber reads or restores.
//!
//! A suspended render fiber can resume on another thread (see
//! `python_async`). A function activation that is live across a suspension
//! must not reuse the address of a thread-local it computed before the
//! suspension: the compiler may keep that address, which then names the old
//! thread's slot. Every access to these slots therefore happens in a function
//! that is never inlined and that reads or writes the slot itself, so the
//! address is computed on the thread that runs the access.
//!
//! The render step clears the slots before it resumes a fiber and restores
//! them afterwards (`isolate`), so a fiber always starts from empty slots on
//! whichever thread resumes it; values a fiber installs are moved to the
//! resuming thread by the scope that saved them around the suspension.
use std::cell::Cell;
use std::thread::LocalKey;

/// A pointer-sized thread-local slot; null when unset.
pub(crate) struct Slot {
    key: &'static LocalKey<Cell<*const ()>>,
}

impl Slot {
    pub(crate) const fn new(key: &'static LocalKey<Cell<*const ()>>) -> Self {
        Self { key }
    }

    #[inline(never)]
    pub(crate) fn get(&self) -> *const () {
        self.key.with(Cell::get)
    }

    #[inline(never)]
    pub(crate) fn replace(&self, value: *const ()) -> *const () {
        self.key.with(|slot| slot.replace(value))
    }

    pub(crate) fn is_set(&self) -> bool {
        !self.get().is_null()
    }

    /// Run `f` with the slot holding `value`, then restore the previous
    /// value on the thread that finishes `f`.
    pub(crate) fn set<R>(&'static self, value: *const (), f: impl FnOnce() -> R) -> R {
        struct Restore(&'static Slot, *const ());
        impl Drop for Restore {
            fn drop(&mut self) {
                self.0.replace(self.1);
            }
        }
        let previous = self.replace(value);
        let _restore = Restore(self, previous);
        f()
    }
}

/// Declare a fiber-local slot.
macro_rules! fiber_slot {
    ($vis:vis static $name:ident) => {
        $vis static $name: $crate::fiber_local::Slot = {
            ::std::thread_local! {
                static KEY: ::std::cell::Cell<*const ()> = const { ::std::cell::Cell::new(::std::ptr::null()) };
            }
            $crate::fiber_local::Slot::new(&KEY)
        };
    };
}
pub(crate) use fiber_slot;

/// Run `f` with the binding's fiber slots and the VM's host state empty,
/// restoring them afterwards: a resumed fiber sees only what it installs.
pub(crate) fn isolate<R>(f: impl FnOnce() -> R) -> R {
    crate::python_macro::suspend(|| crate::python_deferred::suspend(||
        crate::python_async::without_reentrancy_guard(|| minijinja::without_host_state(f))))
}

/// `Python::attach` for code that runs after a fiber may have resumed on
/// another thread: pyo3 keeps its attach count in a thread-local, so the
/// attach (and the drops in `f`) must not be inlined into an activation that
/// computed that thread-local's address before the suspension.
#[inline(never)]
pub(crate) fn attach<R>(f: impl for<'py> FnOnce(pyo3::Python<'py>) -> R) -> R {
    pyo3::Python::attach(f)
}
