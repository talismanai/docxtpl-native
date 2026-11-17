use std::collections::BTreeSet;
use std::fmt;
use std::sync::{Arc, OnceLock};

use crate::error::{Error, ErrorKind};
use crate::output::{CaptureMode, Output};
use crate::utils::AutoEscape;
use crate::value::{Enumerator, Kwargs, Object, Value};
use crate::vm::state::State;
use crate::vm::Vm;

/// Opaque macro implementation for foreign argument binders.
pub struct Macro {
    pub(crate) name: Value,
    pub(crate) arg_spec: Vec<Value>,
    pub(crate) argument_names: Value,
    pub(crate) capture_varargs: bool,
    pub(crate) capture_kwargs: bool,
    // because values need to be 'static, we can't hold a reference to the
    // instructions that declared the macro.  Instead of that we place the
    // reference to the macro instruction (and the jump offset) in the
    // state under `state.macros`.
    pub(crate) macro_ref_id: usize,
    pub(crate) reference: Arc<()>,
    pub(crate) state_id: isize,
    pub(crate) closure: Value,
    pub(crate) runtime_context: Value,
    pub(crate) caller_reference: bool,
    pub(crate) foreign: OnceLock<Value>,
}

impl fmt::Debug for Macro {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "<macro {}>", self.name)
    }
}

impl Macro {
    /// The stable foreign object constructed when this macro was defined.
    pub fn foreign_value(&self) -> Option<&Value> { self.foreign.get() }

    /// Whether this state still owns the exact defining instruction entry.
    pub fn is_available_in(&self, state: &State) -> bool {
        state.id == self.state_id && state.macros.get(self.macro_ref_id)
            .is_some_and(|(_, _, reference, _, _, _)| Arc::ptr_eq(reference, &self.reference))
    }

    /// Execute the body with already bound arguments, preserving the defining context.
    pub fn call_bound(&self, state: &State, values: Vec<Value>) -> Result<Value, Error> {
        if !self.is_available_in(state) {
            return Err(Error::new(ErrorKind::InvalidOperation, "macro body outside its defining VM activation"));
        }
        let implicit_caller = self.caller_reference && !self.arg_spec.iter().any(|arg| arg.as_str() == Some("caller"));
        let expected = self.arg_spec.len() + usize::from(implicit_caller)
            + usize::from(self.capture_kwargs) + usize::from(self.capture_varargs);
        if values.len() != expected {
            return Err(Error::new(ErrorKind::InvalidOperation, "foreign macro binder supplied an invalid body argument count"));
        }
        let mut values = values.into_iter();
        let args: Vec<_> = values.by_ref().take(self.arg_spec.len()).collect();
        let caller = if implicit_caller {
            Some(values.next().unwrap_or(Value::UNDEFINED))
        } else { None };
        let mut captures = Vec::new();
        if self.capture_kwargs { captures.push(("kwargs", values.next().unwrap_or(Value::UNDEFINED))); }
        if self.capture_varargs { captures.push(("varargs", values.next().unwrap_or(Value::UNDEFINED))); }
        let mut output = String::new();
        if let Some(joiner) = state.env().fragment_joiner() {
            let mut stream = Output::with_fragments(&mut output, joiner);
            stream.begin_capture(CaptureMode::Capture);
            Vm::new(state.env()).eval_macro(state, self.macro_ref_id, &mut stream,
                self.closure.clone(), self.runtime_context.clone(), caller, captures, args)?;
            stream.end_capture(AutoEscape::None)
        } else {
            Vm::new(state.env()).eval_macro(state, self.macro_ref_id, &mut Output::new(&mut output),
                self.closure.clone(), self.runtime_context.clone(), caller, captures, args)?;
            Ok(Value::from(output))
        }
    }
}

impl Object for Macro {
    fn enumerate(self: &Arc<Self>) -> Enumerator {
        Enumerator::Str(&["name", "arguments", "caller", "catch_varargs", "catch_kwargs"])
    }

    fn get_value(self: &Arc<Self>, key: &Value) -> Option<Value> {
        Some(match some!(key.as_str()) {
            "name" => self.name.clone(),
            "arguments" => self.argument_names.clone(),
            "caller" => Value::from(self.caller_reference),
            "catch_varargs" => Value::from(self.capture_varargs),
            "catch_kwargs" => Value::from(self.capture_kwargs),
            _ => return None,
        })
    }

    fn call(self: &Arc<Self>, state: &State<'_, '_>, args: &[Value]) -> Result<Value, Error> {
        // we can only call macros that point to loaded template state.
        if !self.is_available_in(state) {
            return Err(Error::new(
                ErrorKind::InvalidOperation,
                "cannot call this macro. template state went away.",
            ));
        }

        let value = Value::from_dyn_object(self.clone());
        if let Some(result) = state.env().runtime_macro_call(state, &value, args) {
            return result;
        }

        let (args, kwargs) = match args.last() {
            Some(last) => match Kwargs::try_extract(last)? {
                Some(kwargs) => (&args[..args.len() - 1], Some(kwargs)),
                None => (args, None),
            },
            _ => (args, None),
        };

        if args.len() > self.arg_spec.len() && !self.capture_varargs {
            return Err(Error::from(ErrorKind::TooManyArguments));
        }

        let mut kwargs_used = BTreeSet::new();
        let mut arg_values = Vec::with_capacity(self.arg_spec.len());
        for (idx, name) in self.arg_spec.iter().enumerate() {
            let name = match name.as_str() {
                Some(name) => name,
                None => {
                    arg_values.push(Value::UNDEFINED);
                    continue;
                }
            };
            let kwarg: Option<&Value> = match kwargs {
                Some(ref kwargs) => kwargs.get(name).ok(),
                _ => None,
            };
            arg_values.push(match (args.get(idx), kwarg) {
                (Some(arg), Some(_)) => arg.clone(),
                (Some(arg), None) => arg.clone(),
                (None, Some(kwarg)) => {
                    kwargs_used.insert(name as &str);
                    kwarg.clone()
                }
                (None, None) => Value::UNDEFINED,
            });
        }

        let caller = if self.caller_reference {
            kwargs_used.insert("caller");
            Some(
                kwargs
                    .as_ref()
                    .and_then(|x| x.get("caller").ok())
                    .unwrap_or(Value::UNDEFINED),
            )
        } else {
            None
        };

        let mut extra_kwargs = Vec::new();
        if let Some(kwargs) = kwargs {
            for key in kwargs.values.keys().filter_map(|x| x.as_str()) {
                if !kwargs_used.contains(key) {
                    if self.capture_kwargs {
                        extra_kwargs.push((Value::from(key), kwargs.values.get(&Value::from(key)).unwrap().clone()));
                        continue;
                    }
                    return Err(Error::new(
                        ErrorKind::TooManyArguments,
                        format!("unknown keyword argument `{key}`"),
                    ));
                }
            }
        }

        let vm = Vm::new(state.env());
        let mut rv = String::new();
        let mut captures = Vec::new();
        if self.capture_varargs {
            captures.push(("varargs", state.env().runtime_tuple(args.get(self.arg_spec.len()..).unwrap_or(&[]).to_vec())?));
        }
        if self.capture_kwargs { captures.push(("kwargs", state.env().runtime_map(extra_kwargs)?)); }

        // This requires some explanation here.  Because we get the state as
        // &State and not &mut State we are required to create a new state in
        // eval_macro.  This is unfortunate but makes the calling interface more
        // convenient for the rest of the system.  Because macros cannot return
        // anything other than strings (most importantly they) can't return
        // other macros this is however not an issue, as modifications in the
        // macro cannot leak out.
        let joiner = state.env().fragment_joiner();
        let mut stream = match joiner {
            Some(joiner) => {
                let mut stream = Output::with_fragments(&mut rv, joiner);
                stream.begin_capture(CaptureMode::Capture);
                stream
            }
            None => Output::new(&mut rv),
        };
        ok!(vm.eval_macro(
            state,
            self.macro_ref_id,
            &mut stream,
            self.closure.clone(),
            self.runtime_context.clone(),
            caller,
            captures,
            arg_values
        ));

        // As Jinja's Macro: the result is markup when the caller's evaluation
        // context autoescapes, whatever the body's (lexical) escaping was.
        if joiner.is_some() {
            return stream.end_capture(state.eval_auto_escape());
        }
        drop(stream);

        Ok(if !matches!(state.eval_auto_escape(), AutoEscape::None) {
            Value::from_safe_string(rv)
        } else {
            Value::from(rv)
        })
    }

    fn render(self: &Arc<Self>, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "<macro {}>", self.name)
    }
}
