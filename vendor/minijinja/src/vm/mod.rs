use std::borrow::Cow;
use std::collections::BTreeMap;
use std::mem;

use std::sync::Arc;

use crate::compiler::instructions::{
    CompareOp, Instruction, Instructions, LOOP_FLAG_FILTERED_INPUT, LOOP_FLAG_RECURSIVE, LOOP_FLAG_WITH_LOOP_VAR, MAX_LOCALS,
};
use crate::environment::Environment;
use crate::error::{Error, ErrorKind};
use crate::output::{CaptureMode, Output};
use crate::utils::{untrusted_size_hint, write_escaped, AutoEscape, UndefinedBehavior};
use crate::value::namespace_object::Namespace;
use crate::value::{
    ops, value_map_with_capacity, Kwargs, ObjectRepr, UndefinedType, Value, ValueKind, ValueMap, ValueRepr,
};
use crate::vm::context::{Frame, Stack};
use crate::vm::loop_object::{Loop, LoopCallOutcome, LoopState};
use crate::vm::state::BlockStack;

#[cfg(feature = "macros")]
use crate::vm::closure_object::Closure;

pub(crate) use crate::vm::context::Context;
pub use crate::vm::state::{detach_current_host_state, has_host_state, with_host_state, without_host_state, RuntimeRenderFrame, RuntimeRenderFrameKind, State};
#[cfg(feature = "multi_template")]
pub use crate::vm::state::render_block_in_host_state;

#[cfg(feature = "macros")]
mod closure_object;
pub(crate) mod context;
#[cfg(feature = "fuel")]
mod fuel;
pub(crate) mod loop_object;
#[cfg(feature = "macros")]
pub(crate) mod macro_object;
#[cfg(feature = "multi_template")]
pub(crate) mod module_object;
mod state;

#[derive(Debug)]
struct MissingPreparedFunction;
impl crate::value::Object for MissingPreparedFunction {}

#[cfg(test)]
mod unpack_protocol_tests {
    use std::sync::{Arc, Mutex};
    use crate::{Environment, Error, ErrorKind, Value};
    use crate::value::{Enumerator, Object, ObjectRepr};

    #[test]
    fn loop_iterator_hook_distinguishes_streamed_filter_input() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let observed = seen.clone();
        let mut environment = Environment::new();
        environment.set_runtime_loop_iterator(move |_, _, metadata, filtered| {
            observed.lock().unwrap().push((metadata, filtered));
            None
        });
        let context = crate::context! { values => vec![1, 2] };
        assert_eq!(environment.render_str("{% for x in values %}{{ x }}{% endfor %}", &context).unwrap(), "12");
        assert_eq!(environment.render_str("{% for x in values if x > 1 %}{{ x }}{% endfor %}", &context).unwrap(), "2");
        assert_eq!(*seen.lock().unwrap(), vec![(false, false), (false, true)]);
    }

    #[test]
    fn membership_propagates_iterator_errors_in_simple_and_chained_comparisons() {
        let input = Value::make_iterable(|| [
            Value::from(1),
            Value::from(Error::new(ErrorKind::InvalidOperation, "iterator failed")),
        ].into_iter());
        let environment = Environment::new();
        for expression in ["2 in xs", "2 not in xs", "0 < 2 in xs", "0 < 2 not in xs"] {
            let source = format!("{{{{ {expression} }}}}");
            let error = environment.render_str(&source, crate::context!(xs => input.clone())).unwrap_err();
            assert!(error.to_string().contains("iterator failed"), "{expression}: {error}");
        }
    }

    #[test]
    fn filtered_length_hook_can_consume_input_lazily_without_advancing_loop() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let stream = Value::from_object(Stream { trace: trace.clone(), error_at: None });
        let observed = trace.clone();
        let mut environment = Environment::new();
        environment.set_runtime_filtered_loop_length_with_input(move |_, next| {
            assert_eq!(*observed.lock().unwrap(), [0, 1]);
            assert_eq!(next()?.unwrap().to_string(), "2");
            assert_eq!(*observed.lock().unwrap(), [0, 1, 2]);
            Ok(Some(Value::from(7)))
        });
        let source = "{% for x in xs if x > 0 %}{{loop.index}}/{{loop.length}}:{{x}};{% endfor %}";
        let output = environment.template_from_str(source).unwrap()
            .render(crate::context!(xs => stream)).unwrap();
        assert_eq!(output, "1/7:1;2/7:3;3/7:4;");
        assert_eq!(*trace.lock().unwrap(), [0, 1, 2, 3, 4]);
    }

    #[test]
    fn filtered_length_input_keeps_loop_lookahead_separate() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let stream = Value::from_object(Stream { trace: trace.clone(), error_at: None });
        let mut environment = Environment::new();
        environment.set_runtime_filtered_loop_length_with_input(|_, next| {
            assert_eq!(next()?.unwrap().to_string(), "3");
            Ok(Some(Value::from(7)))
        });
        let source = "{% for x in xs if x > 0 %}{{loop.nextitem}}:{{loop.length}}:{{x}};{% endfor %}";
        let output = environment.template_from_str(source).unwrap()
            .render(crate::context!(xs => stream)).unwrap();
        assert_eq!(output, "2:7:1;4:7:2;:7:4;");
        assert_eq!(*trace.lock().unwrap(), [0, 1, 2, 3, 4]);
    }

    #[test]
    fn filtered_length_input_repeated_exhaustion_does_not_advance_source() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let stream = Value::from_object(Stream { trace: trace.clone(), error_at: None });
        let mut environment = Environment::new();
        environment.set_runtime_filtered_loop_length_with_input(|_, next| {
            assert!(next()?.is_none());
            assert!(next()?.is_none());
            Ok(Some(Value::from(7)))
        });
        let source = "{% for x in xs if x > 3 %}{{loop.length}}:{{x}};{% endfor %}";
        assert_eq!(environment.template_from_str(source).unwrap()
            .render(crate::context!(xs => stream)).unwrap(), "7:4;");
        assert_eq!(*trace.lock().unwrap(), [0, 1, 2, 3, 4]);
    }

    #[test]
    fn filtered_length_input_propagates_iterator_error() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let stream = Value::from_object(Stream { trace: trace.clone(), error_at: Some(2) });
        let mut environment = Environment::new();
        environment.set_runtime_filtered_loop_length_with_input(|_, next| {
            next()?;
            Ok(Some(Value::from(7)))
        });
        let source = "{% for x in xs if x > 0 %}{{loop.length}}:{{x}};{% endfor %}";
        let error = environment.template_from_str(source).unwrap()
            .render(crate::context!(xs => stream)).unwrap_err();
        assert!(error.to_string().contains("iteration failed"));
        assert_eq!(*trace.lock().unwrap(), [0, 1, 2]);
    }

    #[test]
    fn caught_filtered_predicate_error_exhausts_generator() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let observed = events.clone();
        let mut environment = Environment::new();
        environment.add_test("selected", move |value: i64| -> Result<bool, Error> {
            observed.lock().unwrap().push(value);
            if value == 2 { return Err(Error::new(ErrorKind::InvalidOperation, "predicate failed")); }
            Ok(true)
        });
        environment.set_runtime_filtered_loop_length_with_input(|_, next| {
            assert!(next().unwrap_err().to_string().contains("predicate failed"));
            assert!(next()?.is_none());
            Ok(Some(Value::from(7)))
        });
        let source = "{% for x in xs if x is selected %}{{loop.length}}:{{x}};{% endfor %}";
        assert_eq!(environment.template_from_str(source).unwrap()
            .render(crate::context!(xs => vec![1, 2, 3])).unwrap(), "7:1;");
        assert_eq!(*events.lock().unwrap(), [1, 2]);
    }

    #[test]
    fn caught_filtered_predicate_truth_error_exhausts_generator() {
        #[derive(Debug)]
        struct FailingTruth;
        impl Object for FailingTruth {
            fn is_true_fallible(self: &Arc<Self>) -> Result<bool, Error> {
                Err(Error::new(ErrorKind::InvalidOperation, "truth failed"))
            }
        }

        for preserve in [false, true] {
            let events = Arc::new(Mutex::new(Vec::new()));
            let observed = events.clone();
            let mut environment = Environment::new();
            environment.set_preserve_test_results(preserve);
            environment.add_test("selected", move |value: i64| -> Value {
                observed.lock().unwrap().push(value);
                if value == 2 { Value::from_object(FailingTruth) } else { Value::from(true) }
            });
            environment.set_runtime_filtered_loop_length_with_input(|_, next| {
                assert!(next().unwrap_err().to_string().contains("truth failed"));
                assert!(next()?.is_none());
                Ok(Some(Value::from(7)))
            });
            let source = "{% for x in xs if x is selected %}{{loop.length}}:{{x}};{% endfor %}";
            assert_eq!(environment.template_from_str(source).unwrap()
                .render(crate::context!(xs => vec![1, 2, 3])).unwrap(), "7:1;");
            assert_eq!(*events.lock().unwrap(), [1, 2]);
        }
    }

    #[test]
    fn caught_filtered_source_error_exhausts_generator() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let stream = Value::from_object(Stream { trace: trace.clone(), error_at: Some(2) });
        let mut environment = Environment::new();
        environment.set_runtime_filtered_loop_length_with_input(|_, next| {
            assert!(next().unwrap_err().to_string().contains("iteration failed"));
            assert!(next()?.is_none());
            Ok(Some(Value::from(7)))
        });
        let source = "{% for x in xs if x > 0 %}{{loop.length}}:{{x}};{% endfor %}";
        assert_eq!(environment.template_from_str(source).unwrap()
            .render(crate::context!(xs => stream)).unwrap(), "7:1;");
        assert_eq!(*trace.lock().unwrap(), [0, 1, 2]);
    }

    #[test]
    fn filtered_length_input_can_consume_then_decline() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let stream = Value::from_object(Stream { trace: trace.clone(), error_at: None });
        let mut environment = Environment::new();
        environment.set_runtime_filtered_loop_length_with_input(|_, next| {
            assert_eq!(next()?.unwrap().to_string(), "2");
            Ok(None)
        });
        let source = "{% for x in xs if x > 0 %}{{loop.length}}:{{x}};{% endfor %}";
        assert_eq!(environment.template_from_str(source).unwrap()
            .render(crate::context!(xs => stream)).unwrap(), "3:1;3:3;3:4;");
        assert_eq!(*trace.lock().unwrap(), [0, 1, 2, 3, 4]);
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn include_depth_failure_does_not_change_caller_depth() {
        use crate::output::Output;
        use crate::vm::context::Frame;
        let mut env = Environment::new();
        env.set_recursion_limit(2);
        env.add_template("part", "body").unwrap();
        let template = env.template_from_str("root").unwrap();
        let mut state = template.new_state();
        state.ctx.push_frame(Frame::default()).unwrap();
        let checkpoint = state.ctx.frame_checkpoint();
        let mut text = String::new();
        let mut output = Output::new(&mut text);
        let vm = super::Vm::new(&env);
        assert!(vm.perform_include(Value::from("part"), &mut state, &mut output, false).is_err());
        assert_eq!(state.ctx.frame_checkpoint(), checkpoint);
        assert!(state.ctx.incr_depth(usize::MAX).is_err());
        assert_eq!(state.ctx.frame_checkpoint(), checkpoint);
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn include_failure_restores_caller_frames_context_and_capture() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use crate::output::{CaptureMode, Output};
        use crate::vm::context::Frame;
        let calls = Arc::new(AtomicUsize::new(0));
        let observed = calls.clone();
        let mut env = Environment::new();
        env.add_function("fail", move || -> Result<&'static str, Error> {
            if observed.fetch_add(1, Ordering::Relaxed) == 0 {
                Err(Error::new(ErrorKind::InvalidOperation, "callback failed"))
            } else { Ok("ok") }
        });
        env.set_runtime_context_handler(|_, event| Ok(match event {
            crate::RuntimeContextEvent::Include => Value::from("included-context"),
            crate::RuntimeContextEvent::Namespace => Value::from("included-base"),
            _ => Value::UNDEFINED,
        }));
        env.add_template("part", "{% with x='inner' %}{% set value %}{{fail()}}{% endset %}{{value}}{% endwith %}").unwrap();
        let template = env.template_from_str("root").unwrap();
        let mut state = template.new_state();
        state.ctx.push_frame(Frame::new(Value::from("outer-base"))).unwrap();
        #[cfg(feature = "macros")]
        let closure = {
            let closure = Arc::new(crate::vm::closure_object::Closure::default());
            state.ctx.reset_closure(Some(closure.clone()));
            closure
        };
        state.runtime_context = Value::from("outer-context");
        let checkpoint = state.ctx.frame_checkpoint();
        let mut text = String::new();
        let mut output = Output::new(&mut text);
        output.begin_capture(CaptureMode::Capture);
        let vm = super::Vm::new(&env);
        assert!(vm.perform_include(Value::from("part"), &mut state, &mut output, false).is_err());
        assert_eq!(state.ctx.frame_checkpoint(), checkpoint);
        assert_eq!(state.ctx.current_base().to_string(), "outer-base");
        assert_eq!(state.runtime_context.to_string(), "outer-context");
        #[cfg(feature = "macros")]
        assert!(Arc::ptr_eq(state.ctx.closure().unwrap(), &closure));
        assert_eq!(output.capture_depth(), 1);
        vm.perform_include(Value::from("part"), &mut state, &mut output, false).unwrap();
        assert_eq!(state.ctx.frame_checkpoint(), checkpoint);
        assert_eq!(state.ctx.current_base().to_string(), "outer-base");
        assert_eq!(state.runtime_context.to_string(), "outer-context");
        assert_eq!(output.end_capture(crate::utils::AutoEscape::None).unwrap().to_string(), "ok");
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn super_frame_limit_restores_state_and_capture_before_retry() {
        use crate::output::Output;
        use crate::vm::context::Frame;
        let mut env = Environment::new();
        env.set_recursion_limit(1);
        let template = env.template_from_str("{% block body %}BODY{% endblock %}").unwrap();
        let mut state = template.new_state();
        let root_instructions = state.instructions;
        let block_instructions = state.blocks.get("body").unwrap().instructions();
        state.blocks.get_mut("body").unwrap().append_instructions(block_instructions);
        state.current_block = Some("body");
        state.ctx.push_frame(Frame::default()).unwrap();
        state.ctx.push_frame(Frame::default()).unwrap();
        let mut text = String::new();
        let mut output = Output::new(&mut text);
        let vm = super::Vm::new(&env);
        assert!(vm.perform_super(&mut state, &mut output, true).is_err());
        assert!(std::ptr::eq(state.instructions, root_instructions));
        assert!(std::ptr::eq(state.blocks.get("body").unwrap().instructions(), block_instructions));
        assert_eq!(output.capture_depth(), 0);
        state.ctx.pop_frame();
        assert_eq!(vm.perform_super(&mut state, &mut output, true).unwrap().to_string(), "BODY");
        assert_eq!(output.capture_depth(), 0);
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn super_body_error_discards_only_its_own_capture_before_retry() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use crate::output::{CaptureMode, Output};
        use crate::vm::context::Frame;
        let calls = Arc::new(AtomicUsize::new(0));
        let observed = calls.clone();
        let mut env = Environment::new();
        env.add_function("fallible", move || -> Result<&'static str, Error> {
            if observed.fetch_add(1, Ordering::Relaxed) == 0 {
                Err(Error::new(ErrorKind::InvalidOperation, "callback failed"))
            } else { Ok("ok") }
        });
        let template = env.template_from_str("{% block body %}{{fallible()}}{% endblock %}").unwrap();
        let mut state = template.new_state();
        let root_instructions = state.instructions;
        let block_instructions = state.blocks.get("body").unwrap().instructions();
        state.blocks.get_mut("body").unwrap().append_instructions(block_instructions);
        state.current_block = Some("body");
        state.ctx.push_frame(Frame::default()).unwrap();
        let mut text = String::new();
        let mut output = Output::new(&mut text);
        output.begin_capture(CaptureMode::Capture);
        let vm = super::Vm::new(&env);
        assert!(vm.perform_super(&mut state, &mut output, true).is_err());
        assert!(std::ptr::eq(state.instructions, root_instructions));
        assert_eq!(output.capture_depth(), 1);
        assert_eq!(vm.perform_super(&mut state, &mut output, true).unwrap().to_string(), "ok");
        assert_eq!(output.capture_depth(), 1);
        output.end_capture(crate::utils::AutoEscape::None).unwrap();
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn super_uncaptured_error_unwinds_nested_capture() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use crate::output::Output;
        use crate::vm::context::Frame;
        let calls = Arc::new(AtomicUsize::new(0));
        let observed = calls.clone();
        let mut env = Environment::new();
        env.add_function("fallible", move || -> Result<&'static str, Error> {
            if observed.fetch_add(1, Ordering::Relaxed) == 0 {
                Err(Error::new(ErrorKind::InvalidOperation, "callback failed"))
            } else { Ok("ok") }
        });
        let template = env.template_from_str("{% block body %}{% set value %}{{fallible()}}{% endset %}{{value}}{% endblock %}").unwrap();
        let mut state = template.new_state();
        let block_instructions = state.blocks.get("body").unwrap().instructions();
        state.blocks.get_mut("body").unwrap().append_instructions(block_instructions);
        state.current_block = Some("body");
        state.ctx.push_frame(Frame::default()).unwrap();
        let mut text = String::new();
        {
            let mut output = Output::new(&mut text);
            let vm = super::Vm::new(&env);
            assert!(vm.perform_super(&mut state, &mut output, false).is_err());
            assert_eq!(output.capture_depth(), 0);
            vm.perform_super(&mut state, &mut output, false).unwrap();
            assert_eq!(output.capture_depth(), 0);
        }
        assert_eq!(text, "ok");
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn super_body_error_restores_nested_frames_and_lexical_autoescape() {
        use crate::output::Output;
        use crate::utils::AutoEscape;
        use crate::vm::context::Frame;
        let mut env = Environment::new();
        env.add_function("fail", || -> Result<&'static str, Error> {
            Err(Error::new(ErrorKind::InvalidOperation, "callback failed"))
        });
        let template = env.template_from_str("{% block body %}{% with x='inner' %}{% autoescape true %}{{fail()}}{% endautoescape %}{% endwith %}{% endblock %}").unwrap();
        let mut state = template.new_state();
        let block_instructions = state.blocks.get("body").unwrap().instructions();
        state.blocks.get_mut("body").unwrap().append_instructions(block_instructions);
        state.current_block = Some("body");
        state.ctx.push_frame(Frame::default()).unwrap();
        let checkpoint = state.ctx.frame_checkpoint();
        let mut text = String::new();
        let mut output = Output::new(&mut text);
        let vm = super::Vm::new(&env);
        assert!(vm.perform_super(&mut state, &mut output, true).is_err());
        assert_eq!(state.ctx.frame_checkpoint(), checkpoint);
        assert_eq!(state.auto_escape(), AutoEscape::None);
        assert_eq!(output.capture_depth(), 0);
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn block_body_error_restores_nested_frames_capture_and_lexical_autoescape() {
        use crate::output::{CaptureMode, Output};
        use crate::utils::AutoEscape;
        use crate::vm::context::Frame;
        let mut env = Environment::new();
        env.add_function("fail", || -> Result<&'static str, Error> {
            Err(Error::new(ErrorKind::InvalidOperation, "callback failed"))
        });
        let template = env.template_from_str("{% block body %}{% with x='inner' %}{% autoescape true %}{% set value %}{{fail()}}{% endset %}{% endautoescape %}{% endwith %}{% endblock %}").unwrap();
        let mut state = template.new_state();
        let root_instructions = state.instructions;
        state.current_block = Some("outer");
        state.ctx.push_frame(Frame::default()).unwrap();
        let checkpoint = state.ctx.frame_checkpoint();
        let mut text = String::new();
        let mut output = Output::new(&mut text);
        output.begin_capture(CaptureMode::Capture);
        let vm = super::Vm::new(&env);
        assert!(vm.call_block("body", &mut state, &mut output).is_err());
        assert!(std::ptr::eq(state.instructions, root_instructions));
        assert_eq!(state.current_block, Some("outer"));
        assert_eq!(state.ctx.frame_checkpoint(), checkpoint);
        assert_eq!(state.auto_escape(), AutoEscape::None);
        assert_eq!(output.capture_depth(), 1);
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn failed_block_arguments_restore_outer_prepared_dispatch() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use crate::output::Output;
        let attempts = Arc::new(AtomicUsize::new(0));
        let observed = attempts.clone();
        let mut env = Environment::new();
        env.set_call_policy_factory(|_| Ok(true));
        env.set_runtime_prepare_call(|_, _| Ok(Value::from("inner-dispatch")));
        env.add_function("fallible", move || -> Result<i32, Error> {
            if observed.fetch_add(1, Ordering::Relaxed) == 0 {
                Err(Error::new(ErrorKind::InvalidOperation, "argument failed"))
            } else { Ok(7) }
        });
        env.add_function("consume", |value: i32| value);
        let template = env.template_from_str("{% block body %}{{consume(fallible())}}{% endblock %}").unwrap();
        let mut state = template.new_state();
        state.pending_calls.borrow_mut().push(Value::from("outer-pending"));
        *state.prepared_call.borrow_mut() = Some(Value::from("outer-prepared"));
        state.call_policy.set(Some(false));
        let mut text = String::new();
        let mut out = Output::new(&mut text);
        let vm = super::Vm::new(&env);
        assert!(vm.call_block("body", &mut state, &mut out).is_err());
        assert_eq!(state.pending_calls.borrow().as_slice(), &[Value::from("outer-pending")]);
        assert_eq!(state.prepared_call(), Some(Value::from("outer-prepared")));
        assert_eq!(state.call_policy(), Some(false));
        vm.call_block("body", &mut state, &mut out).unwrap();
        assert_eq!(state.pending_calls.borrow().as_slice(), &[Value::from("outer-pending")]);
        assert_eq!(state.prepared_call(), Some(Value::from("outer-prepared")));
        assert_eq!(state.call_policy(), Some(false));
        assert_eq!(text, "7");
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn host_dispatch_can_replace_parent_block_without_replacing_child() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let observed = events.clone();
        let mut env = Environment::new();
        env.set_runtime_block_dispatch(move |_, name, depth, out| {
            observed.lock().unwrap().push((name.to_owned(), depth));
            if name == "body" && depth == 1 {
                out.write_str("HOST").map_err(Error::from)?;
                Ok(true)
            } else { Ok(false) }
        });
        env.add_template("base", "{% block body %}BASE{% endblock %}").unwrap();
        let result = env.template_from_str("{% extends 'base' %}{% block body %}CHILD:{{super()}}{% endblock %}")
            .unwrap().render(()).unwrap();
        assert_eq!(result, "CHILD:HOST");
        assert_eq!(*events.lock().unwrap(), [("body".to_owned(), 0), ("body".to_owned(), 1)]);
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn required_check_applies_at_declaration_not_self_call() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let observed = events.clone();
        let mut env = Environment::new();
        env.set_runtime_block_dispatch(move |_, name, depth, out| {
            observed.lock().unwrap().push((name.to_owned(), depth));
            out.write_str("HOST").map_err(Error::from)?;
            Ok(true)
        });
        let required = env.template_from_str("{% block body required %}{% endblock %}").unwrap();
        assert!(required.render(()).unwrap_err().to_string().contains("Required block 'body' not found"));
        assert!(events.lock().unwrap().is_empty());
        let self_call = env.template_from_str("{% if false %}{% block body required %}{% endblock %}{% endif %}{{self.body()}}").unwrap();
        assert_eq!(self_call.render(()).unwrap(), "HOST");
        assert_eq!(*events.lock().unwrap(), [("body".to_owned(), 0)]);
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn host_block_dispatch_error_preserves_state_for_retry() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use crate::output::Output;
        let attempts = Arc::new(AtomicUsize::new(0));
        let observed = attempts.clone();
        let mut env = Environment::new();
        env.set_runtime_block_dispatch(move |_, _, _, out| {
            if observed.fetch_add(1, Ordering::Relaxed) == 0 {
                Err(Error::new(ErrorKind::InvalidOperation, "host failed"))
            } else {
                out.write_str("HOST").map_err(Error::from)?;
                Ok(true)
            }
        });
        let template = env.template_from_str("{% block body %}NATIVE{% endblock %}").unwrap();
        let mut state = template.new_state();
        let mut text = String::new();
        let mut out = Output::new(&mut text);
        let vm = super::Vm::new(&env);
        assert!(vm.call_block("body", &mut state, &mut out).is_err());
        vm.call_block("body", &mut state, &mut out).unwrap();
        assert_eq!(text, "HOST");
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn host_parent_dispatch_error_restores_stack_for_retry() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use crate::output::Output;
        let attempts = Arc::new(AtomicUsize::new(0));
        let observed = attempts.clone();
        let mut env = Environment::new();
        env.set_runtime_block_dispatch(move |_, _, depth, out| {
            assert_eq!(depth, 1);
            if observed.fetch_add(1, Ordering::Relaxed) == 0 {
                Err(Error::new(ErrorKind::InvalidOperation, "host failed"))
            } else {
                out.write_str("HOST").map_err(Error::from)?;
                Ok(true)
            }
        });
        let template = env.template_from_str("{% block body %}NATIVE{% endblock %}").unwrap();
        let mut state = template.new_state();
        let root_instructions = state.instructions;
        let instructions = state.blocks.get("body").unwrap().instructions();
        state.blocks.get_mut("body").unwrap().append_instructions(instructions);
        state.current_block = Some("body");
        let mut text = String::new();
        let mut out = Output::new(&mut text);
        let vm = super::Vm::new(&env);
        assert!(vm.perform_super(&mut state, &mut out, true).is_err());
        assert!(std::ptr::eq(state.instructions, root_instructions));
        assert_eq!(state.blocks.get("body").unwrap().depth(), 0);
        assert_eq!(out.capture_depth(), 0);
        assert_eq!(vm.perform_super(&mut state, &mut out, true).unwrap().to_string(), "HOST");
        assert_eq!(state.blocks.get("body").unwrap().depth(), 0);
        assert_eq!(out.capture_depth(), 0);
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn self_block_value_uses_deferred_hook_before_body() {
        for execute in [false, true] {
            let events = Arc::new(Mutex::new(Vec::new()));
            let observed = events.clone();
            let mut env = Environment::new();
            env.add_function("body", move || {
                observed.lock().unwrap().push("body");
                "body"
            });
            let observed = events.clone();
            env.set_runtime_deferred_super(move |state, body| {
                observed.lock().unwrap().push("hook");
                if execute { body(state) } else { Ok(Value::from("skipped")) }
            });
            let source = "{% if false %}{% block b %}{{body()}}{% endblock %}{% endif %}{{ self.b() ~ '!' }}";
            let output = env.template_from_str(source).unwrap().render(()).unwrap();
            assert_eq!(output, if execute { "body!" } else { "skipped!" });
            assert_eq!(*events.lock().unwrap(), if execute { vec!["hook", "body"] } else { vec!["hook"] });
        }
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn direct_self_block_output_uses_deferred_hook_after_compilation() {
        for execute in [false, true] {
            let events = Arc::new(Mutex::new(Vec::new()));
            let observed = events.clone();
            let mut env = Environment::new();
            env.add_function("body", move || {
                observed.lock().unwrap().push("body");
                "body"
            });
            let source = "{% if false %}{% block b %}{{body()}}{% endblock %}{% endif %}{{self.b()}}";
            env.add_template("test", source).unwrap();
            let observed = events.clone();
            env.set_runtime_deferred_super(move |state, body| {
                observed.lock().unwrap().push("hook");
                if execute { body(state) } else { Ok(Value::from("skipped")) }
            });
            let output = env.get_template("test").unwrap().render(()).unwrap();
            assert_eq!(output, if execute { "body" } else { "skipped" });
            assert_eq!(*events.lock().unwrap(), if execute { vec!["hook", "body"] } else { vec!["hook"] });
        }
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn direct_deferred_self_block_formats_like_expression() {
        use crate::AutoEscape;
        for custom_formatter in [false, true] {
            for execute in [false, true] {
                let mut env = Environment::new();
                env.set_auto_escape_callback(|_| AutoEscape::Html);
                if custom_formatter {
                    env.set_formatter(|out, _state, value| {
                        use std::fmt::Write;
                        write!(out, "[{value}]").map_err(|_| Error::from(ErrorKind::WriteFailure))
                    });
                }
                env.set_runtime_deferred_super(move |state, body| {
                    if execute { body(state) } else { Ok(Value::from("<unsafe>")) }
                });
                let direct = "{% if false %}{% block b %}<b>body</b>{% endblock %}{% endif %}{{self.b()}}";
                let expression = "{% if false %}{% block b %}<b>body</b>{% endblock %}{% endif %}{% set x = self.b() %}{{x}}";
                assert_eq!(env.template_from_str(direct).unwrap().render(()).unwrap(),
                           env.template_from_str(expression).unwrap().render(()).unwrap());
            }
        }
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn direct_deferred_self_block_propagates_hook_error() {
        let mut env = Environment::new();
        env.set_runtime_deferred_super(|_, _| Err(Error::new(ErrorKind::InvalidOperation, "hook failed")));
        let source = "{% if false %}{% block b %}body{% endblock %}{% endif %}{{self.b()}}";
        assert!(env.template_from_str(source).unwrap().render(()).unwrap_err().to_string().contains("hook failed"));
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn self_block_value_guard_runs_before_the_block() {
        for sandboxed in [false, true] {
            let events = Arc::new(Mutex::new(Vec::new()));
            let observed = events.clone();
            let mut env = Environment::new();
            env.add_function("body", move || {
                observed.lock().unwrap().push("body");
                "body"
            });
            env.set_call_policy_factory(move |_| Ok(sandboxed));
            let observed = events.clone();
            env.set_runtime_self_block_guard(move |_, policy| {
                observed.lock().unwrap().push("guard");
                if policy { Err(Error::new(ErrorKind::InvalidOperation, "blocked")) } else { Ok(()) }
            });
            let source = "{% if false %}{% block b %}{{body()}}{% endblock %}{% endif %}{{ self.b() ~ '!' }}";
            let result = env.template_from_str(source).unwrap().render(());
            if sandboxed {
                assert!(result.unwrap_err().to_string().contains("blocked"));
                assert_eq!(*events.lock().unwrap(), ["guard"]);
            } else {
                assert_eq!(result.unwrap(), "body!");
                assert_eq!(*events.lock().unwrap(), ["guard", "body"]);
            }
        }
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn direct_self_block_output_obeys_host_guard() {
        for sandboxed in [true, false] {
            let events = Arc::new(Mutex::new(Vec::new()));
            let observed = events.clone();
            let mut env = Environment::new();
            env.add_function("body", move || {
                observed.lock().unwrap().push("body");
                "body"
            });
            env.set_call_policy_factory(move |_| Ok(sandboxed));
            let observed = events.clone();
            env.set_runtime_self_block_guard(move |_, policy| {
                observed.lock().unwrap().push("guard");
                if policy { Err(Error::new(ErrorKind::InvalidOperation, "blocked")) } else { Ok(()) }
            });
            let source = "{% if false %}{% block b %}{{body()}}{% endblock %}{% endif %}{{self.b()}}";
            let result = env.template_from_str(source).unwrap().render(());
            if sandboxed {
                assert!(result.unwrap_err().to_string().contains("blocked"));
                assert_eq!(*events.lock().unwrap(), ["guard"]);
            } else {
                assert_eq!(result.unwrap(), "body");
                assert_eq!(*events.lock().unwrap(), ["guard", "body"]);
            }
        }
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn guarded_direct_self_block_preserves_autoescaping() {
        use crate::AutoEscape;
        for autoescape in [AutoEscape::None, AutoEscape::Html] {
            let mut env = Environment::new();
            env.set_auto_escape_callback(move |_| autoescape);
            env.set_call_policy_factory(|_| Ok(false));
            let source = "{% if false %}{% block b %}<b>{{value}}</b>{% endblock %}{% endif %}{{self.b()}}";
            let output = env.template_from_str(source).unwrap().render(context!(value => "<&")).unwrap();
            assert_eq!(output, if autoescape == AutoEscape::Html { "<b>&lt;&amp;</b>" } else { "<b><&</b>" });
        }
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn self_block_accepts_empty_argument_expansions_without_host_hooks() {
        let env = Environment::new();
        let source = "{% if false %}{% block b %}BODY{% endblock %}{% endif %}{{self.b(*[])}}/{{self.b(**{})}}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "BODY/BODY");
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn self_block_evaluates_arguments_before_host_guard() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let observed = events.clone();
        let mut env = Environment::new();
        env.add_function("argument", move || {
            observed.lock().unwrap().push("argument");
            7
        });
        env.set_call_policy_factory(|_| Ok(true));
        env.set_runtime_self_block_arguments(|_, _, _| Ok(()));
        let observed = events.clone();
        env.set_runtime_self_block_guard(move |_, _| {
            observed.lock().unwrap().push("guard");
            Err(Error::new(ErrorKind::InvalidOperation, "blocked"))
        });
        let source = "{% if false %}{% block b %}BODY{% endblock %}{% endif %}{{self.b(argument())}}";
        assert!(env.template_from_str(source).unwrap().render(()).is_err());
        assert_eq!(*events.lock().unwrap(), ["argument", "guard"]);
    }

    #[cfg(all(feature = "multi_template", feature = "macros"))]
    #[test]
    fn macro_block_registry_belongs_to_defining_template_and_includes_parents() {
        let mut env = Environment::new();
        env.add_template("base", "{% block outer %}{% endblock %}|{% block body %}BASE{% endblock %}").unwrap();
        env.add_template("library", "{% macro m(x) %}{% block body scoped %}lib{{super()}}{{x|default('M')}}{% endblock %}{% endmacro %}{% extends 'base' %}{% block outer %}{{m(3)}}{% endblock %}").unwrap();
        env.add_template("other", "{% block body %}OTHER{% endblock %}").unwrap();
        let source = "{% from 'library' import m %}{% include 'other' %}{{m(4)}}{% block body %}CALLER{% endblock %}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "OTHERlibBASE4CALLER");
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn unscoped_blocks_hide_loop_locals_and_restore_them_after_return() {
        let environment = crate::Environment::new();
        let source = "{% for x in [1,2] %}{% block body %}{{x|default('missing')}}{% endblock %}:{{x}};{% endfor %}";
        assert_eq!(environment.template_from_str(source).unwrap().render(()).unwrap(), "missing:1;missing:2;");
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn self_calls_use_block_context_while_unscoped_nested_blocks_do_not() {
        let environment = crate::Environment::new();
        let source = "{% for x in [1] %}{% block outer scoped %}{{self.inner()}}{% endblock %}{% endfor %}{% block inner %}{{x|default('missing')}}{% endblock %}";
        assert_eq!(environment.template_from_str(source).unwrap().render(()).unwrap(), "1missing");
        let nested = "{% block outer %}{% set x=1 %}{% block inner %}{{x|default('missing')}}{% endblock %}{% endblock %}";
        assert_eq!(environment.template_from_str(nested).unwrap().render(()).unwrap(), "missing");
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn deferred_super_hook_controls_parent_execution() {
        for execute in [false, true] {
            let events = Arc::new(Mutex::new(Vec::new()));
            let observed = events.clone();
            let mut environment = Environment::new();
            environment.add_function("parent", move || {
                observed.lock().unwrap().push("parent");
                "base"
            });
            environment.set_runtime_deferred_super(move |state, body| {
                if execute { body(state) } else { Ok(Value::from("skipped")) }
            });
            environment.add_template("base", "{% block x %}{{parent()}}{% endblock %}").unwrap();
            let template = environment.template_from_str("{% extends 'base' %}{% block x %}{{super()}}{% endblock %}").unwrap();
            let output = template.render(crate::context!()).unwrap();
            assert_eq!(output, if execute { "base" } else { "skipped" });
            assert_eq!(*events.lock().unwrap(), if execute { vec!["parent"] } else { vec![] });
        }
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn detached_deferred_operations_run_after_the_render_and_environment_are_gone() {
        let cases = [
            ("{% extends 'base' %}{% block x %}{{ super() }}{% endblock %}", "parent:7"),
            ("{% for x in [0] recursive %}{% if x == 0 %}{{ loop([value]) }}{% else %}{{ parent() }}:{{ x }}/{{ loop.depth }}{% endif %}{% endfor %}", "parent:7/2"),
            ("{% if false %}{% block y %}{{ parent() }}:{{ value }}{% endblock %}{% endif %}[{{ self.y() }}]", "parent:7"),
        ];
        for (source, expected) in cases {
            let events = Arc::new(Mutex::new(Vec::new()));
            let retained = Arc::new(Mutex::new(Vec::new()));
            let mut environment = Environment::new();
            let observed = events.clone();
            environment.add_function("parent", move || {
                observed.lock().unwrap().push("parent");
                "parent"
            });
            environment.set_loader(|name| Ok((name == "base").then(|| "{% block x %}{{ parent() }}:{{ value }}{% endblock %}".to_owned())));
            environment.enable_shared_loaded_cache();
            fn retain(keep: &Mutex<Vec<crate::DetachedActivation>>, state: &crate::State) -> Result<Value, Error> {
                // SAFETY: the environment is an Environment<'static>.
                keep.lock().unwrap().push(unsafe { state.detach_deferred() }.expect("detachable"));
                Ok(Value::from("deferred"))
            }
            let keep = retained.clone();
            environment.set_runtime_deferred_super(move |state, _| retain(&keep, state));
            let keep = retained.clone();
            environment.set_runtime_deferred_loop(move |state, _| retain(&keep, state));
            let compiled = environment.compile_owned_template("case".into(), source.into()).unwrap();
            let output = environment.template_from_owned(&compiled).render(crate::context!(value => 7)).unwrap();
            assert_eq!(output, if source.contains("self.y") { "[deferred]" } else { "deferred" });
            drop(compiled);
            drop(environment);
            assert!(events.lock().unwrap().is_empty());
            let activation = retained.lock().unwrap().pop().unwrap();
            let value = activation.run(|state, execute| execute(state)).unwrap();
            assert_eq!(value.to_string(), expected, "{source}");
            assert_eq!(*events.lock().unwrap(), vec!["parent"]);
        }
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn deferred_operations_with_borrowed_templates_do_not_detach() {
        let detached = Arc::new(Mutex::new(Vec::new()));
        let mut environment = Environment::new();
        let seen = detached.clone();
        environment.set_runtime_deferred_super(move |state, body| {
            // SAFETY: the environment is an Environment<'static>.
            seen.lock().unwrap().push(unsafe { state.detach_deferred() }.is_some());
            body(state)
        });
        environment.add_template("base", "{% block x %}base{% endblock %}").unwrap();
        let source = "{% extends 'base' %}{% block x %}{{ super() }}{% endblock %}";
        assert_eq!(environment.template_from_str(source).unwrap().render(()).unwrap(), "base");
        environment.enable_shared_loaded_cache();
        let compiled = environment.compile_owned_template("case".into(), source.into()).unwrap();
        assert_eq!(environment.template_from_owned(&compiled).render(()).unwrap(), "base");
        assert_eq!(*detached.lock().unwrap(), vec![false, false]);
    }

    #[cfg(feature = "macros")]
    #[test]
    fn detached_host_state_runs_a_macro_after_the_render_and_environment_are_gone() {
        let retained = Arc::new(Mutex::new(Vec::new()));
        let mut environment = Environment::new();
        let keep = retained.clone();
        environment.add_function("hold", move |state: &crate::State, value: Value| -> Value {
            // SAFETY: the environment is an Environment<'static>.
            keep.lock().unwrap().push((unsafe { state.detach_for_host() }.expect("detachable"), value));
            Value::from("")
        });
        environment.enable_shared_loaded_cache();
        let source = "{% macro m(x) %}{{ x }}:{{ value }}{% endmacro %}{{ hold(m) }}";
        let compiled = environment.compile_owned_template("case".into(), source.into()).unwrap();
        environment.template_from_owned(&compiled).render(crate::context!(value => 7)).unwrap();
        drop(compiled);
        drop(environment);
        let (detached, function) = retained.lock().unwrap().pop().unwrap();
        let output = detached.run(|state| function.call(state, &[Value::from(1)])).unwrap();
        assert_eq!(output.to_string(), "1:7");
    }

    #[test]
    fn installed_host_state_detaches_only_inside_its_scope() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let mut environment = Environment::new();
        let record = seen.clone();
        environment.add_function("probe", move |state: &crate::State| -> Value {
            // SAFETY: the environment is an Environment<'static>.
            let outside = unsafe { crate::detach_current_host_state() }.is_some();
            let inside = crate::with_host_state(state, || unsafe { crate::detach_current_host_state() })
                .map(|detached| detached.run(|state| state.name().to_owned()));
            record.lock().unwrap().push((outside, inside));
            Value::from("")
        });
        // A state is copied only from an owned template in the shared cache.
        environment.enable_shared_loaded_cache();
        let compiled = environment.compile_owned_template("case".into(), "{{ probe() }}".into()).unwrap();
        environment.template_from_owned(&compiled).render(()).unwrap();
        assert_eq!(*seen.lock().unwrap(), vec![(false, Some("case".to_owned()))]);
    }

    #[test]
    fn state_outside_a_deferral_hook_does_not_detach() {
        let mut environment = Environment::new();
        environment.add_function("probe", |state: &crate::State| -> bool {
            // SAFETY: the environment is an Environment<'static>.
            unsafe { state.detach_deferred() }.is_none()
        });
        environment.enable_shared_loaded_cache();
        let compiled = environment.compile_owned_template("case".into(), "{{ probe() }}".into()).unwrap();
        assert_eq!(environment.template_from_owned(&compiled).render(()).unwrap(), "True");
    }

    #[test]
    fn deferred_recursive_loop_hook_controls_nested_execution() {
        for execute in [false, true] {
            for expression in ["{{ loop([1]) }}", "{{ 'X' ~ loop([1]) }}"] {
                let mut environment = Environment::new();
                environment.set_runtime_deferred_loop(move |state, body| {
                    if execute { body(state) } else { Ok(Value::from("skipped")) }
                });
                let source = format!("{{% for x in [0] recursive %}}{{% if x == 0 %}}{expression}{{% else %}}nested{{{{loop.depth}}}}{{% endif %}}{{% endfor %}}");
                let output = environment.template_from_str(&source).unwrap().render(crate::context!()).unwrap();
                let prefix = if expression.contains("'X'") { "X" } else { "" };
                assert_eq!(output, format!("{prefix}{}", if execute { "nested2" } else { "skipped" }));
            }
        }
    }

    #[test]
    fn deferred_recursive_loop_error_restores_outer_frame_and_capture() {
        let mut environment = Environment::new();
        environment.add_function("fail", || -> Result<String, Error> {
            Err(Error::new(ErrorKind::InvalidOperation, "nested error"))
        });
        environment.set_runtime_deferred_loop(|state, body| {
            assert!(body(state).unwrap_err().to_string().contains("nested error"));
            Ok(Value::from("caught"))
        });
        let source = "{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}:{{loop.depth}}{% else %}partial{{ fail() }}{% endif %}{% endfor %}";
        let output = environment.template_from_str(source).unwrap().render(crate::context!()).unwrap();
        assert_eq!(output, "caught:1");
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn deferred_recursive_loop_restores_depth_after_failed_include() {
        let mut environment = Environment::new();
        environment.set_recursion_limit(8);
        environment.add_template("included", "unreachable").unwrap();
        environment.set_runtime_deferred_loop(|state, body| {
            assert!(body(state).is_err());
            Ok(Value::from("caught"))
        });
        let source = "{% for x in [0] recursive %}{% if x == 0 %}{{ loop([1]) }}:{{loop.depth}}{% else %}{% include 'included' %}{% endif %}{% endfor %}";
        let output = environment.template_from_str(source).unwrap().render(crate::context!()).unwrap();
        assert_eq!(output, "caught:1");
    }

    #[derive(Debug)]
    struct Stream { trace: Arc<Mutex<Vec<usize>>>, error_at: Option<usize> }
    impl Object for Stream {
        fn repr(self: &Arc<Self>) -> ObjectRepr { ObjectRepr::Iterable }
        fn enumerate(self: &Arc<Self>) -> Enumerator {
            let trace=self.trace.clone();let error_at=self.error_at;
            Enumerator::Iter(Box::new((0..5).map(move |index| {
                trace.lock().unwrap().push(index);
                if error_at==Some(index) { Value::from(Error::new(ErrorKind::InvalidOperation,"iteration failed")) }
                else { Value::from(index) }
            })))
        }
    }

    #[test]
    fn unpacking_excess_consumes_only_one_extra_item() {
        let trace=Arc::new(Mutex::new(Vec::new()));
        let pair=Value::from_object(Stream { trace:trace.clone(),error_at:None });
        let environment=Environment::new();
        let result=environment.template_from_str("{% for a,b in pairs %}{{a}}{% endfor %}").unwrap()
            .render(crate::context!(pairs => vec![pair]));
        assert!(result.is_err());
        assert_eq!(*trace.lock().unwrap(),[0,1,2]);
    }

    #[test]
    fn unpacking_iterator_error_stops_before_body() {
        let trace=Arc::new(Mutex::new(Vec::new()));
        let pair=Value::from_object(Stream { trace:trace.clone(),error_at:Some(1) });
        let environment=Environment::new();
        let result=environment.template_from_str("{% for a,b in pairs %}{{a}}{% endfor %}").unwrap()
            .render(crate::context!(pairs => vec![pair]));
        assert!(result.unwrap_err().to_string().contains("iteration failed"));
        assert_eq!(*trace.lock().unwrap(),[0,1]);
    }

    #[test]
    fn filtered_length_hook_can_return_without_consuming_and_is_cached() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let calls = Arc::new(Mutex::new(0));
        let observed = calls.clone();
        let before_length = trace.clone();
        let stream = Value::from_object(Stream { trace: trace.clone(), error_at: None });
        let mut environment = Environment::new();
        environment.set_runtime_filtered_loop_length(move |_, _| {
            assert_eq!(*before_length.lock().unwrap(), [0, 1]);
            *observed.lock().unwrap() += 1;
            Ok(Some(Value::from(7)))
        });
        let output = environment.template_from_str("{% for x in xs if x > 0 %}{{loop.length}}:{{x}};{% endfor %}")
            .unwrap().render(crate::context!(xs => stream)).unwrap();
        assert_eq!(output, "7:1;7:2;7:3;7:4;");
        assert_eq!(*calls.lock().unwrap(), 1);
        assert_eq!(*trace.lock().unwrap(), [0, 1, 2, 3, 4]);
    }

    #[test]
    fn filtered_length_hook_error_precedes_remaining_consumption() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let stream = Value::from_object(Stream { trace: trace.clone(), error_at: None });
        let mut environment = Environment::new();
        environment.set_runtime_filtered_loop_length(|_, _| Err(Error::new(ErrorKind::InvalidOperation, "length failed")));
        let result = environment.template_from_str("{% for x in xs if x > 0 %}{{loop.length}}{% endfor %}")
            .unwrap().render(crate::context!(xs => stream));
        assert!(result.unwrap_err().to_string().contains("length failed"));
        assert_eq!(*trace.lock().unwrap(), [0, 1]);
    }

    #[test]
    fn filtered_length_hook_decline_uses_native_collection() {
        let calls = Arc::new(Mutex::new(0));
        let observed = calls.clone();
        let mut environment = Environment::new();
        environment.set_runtime_filtered_loop_length(move |_, _| {
            *observed.lock().unwrap() += 1;
            Ok(None)
        });
        let output = environment.template_from_str("{% for x in xs if x > 0 %}{{loop.length}}:{{x}};{% endfor %}")
            .unwrap().render(crate::context!(xs => vec![0, 1, 2])).unwrap();
        assert_eq!(output, "2:1;2:2;");
        assert_eq!(*calls.lock().unwrap(), 1);
    }

    #[test]
    fn filtered_length_hook_none_value_is_recomputed() {
        let calls = Arc::new(Mutex::new(0));
        let observed = calls.clone();
        let mut environment = Environment::new();
        environment.set_runtime_filtered_loop_length(move |_, _| {
            *observed.lock().unwrap() += 1;
            Ok(Some(Value::from(())))
        });
        let output = environment.template_from_str("{% for x in xs if x > 0 %}{{loop.length is none}};{% endfor %}")
            .unwrap().render(crate::context!(xs => vec![0, 1, 2])).unwrap();
        assert_eq!(output, "True;True;");
        assert_eq!(*calls.lock().unwrap(), 2);
    }
}

#[cfg(all(test, feature = "multi_template"))]
mod render_frame_depth_tests {
    use crate::{context, Environment, Error, ErrorKind, RuntimeRenderFrameKind as Kind, State};
    use std::sync::{Arc, Mutex};

    fn environment() -> Environment<'static> {
        let mut env = Environment::new();
        env.add_function("depth", |state: &State| state.render_frame_depth());
        env.add_template("inner", "{{depth()}}").unwrap();
        env.add_template("outer", "{{depth()}}{% include 'inner' %}{{depth()}}").unwrap();
        env.add_template("grand", "{% block b %}G{{depth()}}{% endblock %}").unwrap();
        env.add_template("middle", "{% extends 'grand' %}{% block b %}M{{depth()}}{{super()}}{% endblock %}").unwrap();
        env
    }

    #[test]
    fn root_include_and_macro_share_scoped_render_depth() {
        let env = environment();
        let source = "{{depth()}}{% include 'inner' %}{{depth()}}";
        assert_eq!(env.template_from_str(source).unwrap().render(context!()).unwrap(), "121");
        let nested = "{{depth()}}{% include 'outer' %}{{depth()}}";
        assert_eq!(env.template_from_str(nested).unwrap().render(context!()).unwrap(), "12321");
        #[cfg(feature = "macros")]
        {
            let source = "{{depth()}}{% macro m() %}{{depth()}}{% include 'inner' %}{% endmacro %}{{m()}}{{depth()}}";
            assert_eq!(env.template_from_str(source).unwrap().render(context!()).unwrap(), "1121");
        }
    }

    #[test]
    fn inheritance_blocks_and_super_add_scoped_render_frames() {
        let mut env = environment();
        let source = "{% extends 'middle' %}{% block b %}C{{depth()}}{{super()}}{% endblock %}";
        assert_eq!(env.template_from_str(source).unwrap().render(context!()).unwrap(), "C4M5G6");
        env.add_template("child", source).unwrap();
        let included = "{{depth()}}{% include 'child' %}{{depth()}}";
        assert_eq!(env.template_from_str(included).unwrap().render(context!()).unwrap(), "1C5M6G71");
    }

    #[test]
    fn reentrant_render_uses_an_independent_depth_counter() {
        let mut env = environment();
        env.add_function("nested", |state: &State| -> Result<String, Error> {
            let before = state.render_frame_depth();
            let nested = state.env().template_from_str("{{depth()}}{% include 'inner' %}{{depth()}}")?
                .render(context!())?;
            Ok(format!("{before}:{nested}:{}", state.render_frame_depth()))
        });
        assert_eq!(env.template_from_str("{{nested()}}").unwrap().render(context!()).unwrap(), "1:121:1");
    }

    #[test]
    fn frame_snapshots_preserve_activation_identity_and_kind() {
        let snapshots = Arc::new(Mutex::new(Vec::new()));
        let output = snapshots.clone();
        let mut env = environment();
        env.add_function("capture", move |state: &State| {
            output.lock().unwrap().push(state.render_frames());
            ""
        });
        env.add_template("tracked", "{{capture()}}").unwrap();
        let source = "{{capture()}}{% include 'tracked' %}{{capture()}}{% include 'tracked' %}{{capture()}}";
        env.template_from_str(source).unwrap().render(context!()).unwrap();
        let snapshots = snapshots.lock().unwrap();
        assert_eq!(snapshots.len(), 5);
        let root = snapshots[0][0].token;
        assert!(snapshots.iter().all(|frames| frames[0].token == root && frames[0].kind == Kind::Root));
        assert_eq!(snapshots[1][1].kind, Kind::Include);
        assert_eq!(snapshots[1][1].template, "tracked");
        assert_ne!(snapshots[1][1].token, snapshots[3][1].token);
        assert_eq!(snapshots[2].len(), 1);
        assert_eq!(snapshots[4].len(), 1);
    }

    #[test]
    fn frame_snapshots_identify_inheritance_block_and_super() {
        let snapshots = Arc::new(Mutex::new(Vec::new()));
        let output = snapshots.clone();
        let mut env = Environment::new();
        env.add_function("capture", move |state: &State| {
            output.lock().unwrap().push(state.render_frames());
            ""
        });
        env.add_template("grand", "{% block b %}{{capture()}}{% endblock %}").unwrap();
        env.add_template("middle", "{% extends 'grand' %}{% block b %}{{capture()}}{{super()}}{% endblock %}").unwrap();
        let source = "{% extends 'middle' %}{% block b %}{{capture()}}{{super()}}{% endblock %}";
        env.template_from_str(source).unwrap().render(context!()).unwrap();
        let snapshots = snapshots.lock().unwrap();
        assert_eq!(snapshots.len(), 3);
        let kinds: Vec<Vec<_>> = snapshots.iter().map(|frames| frames.iter().map(|frame| frame.kind).collect()).collect();
        assert_eq!(kinds, [
            vec![Kind::Root, Kind::Inheritance, Kind::Inheritance, Kind::Block],
            vec![Kind::Root, Kind::Inheritance, Kind::Inheritance, Kind::Block, Kind::Super],
            vec![Kind::Root, Kind::Inheritance, Kind::Inheritance, Kind::Block, Kind::Super, Kind::Super],
        ]);
        assert_eq!(snapshots[0][1].template, "middle");
        assert_eq!(snapshots[0][2].template, "grand");
        assert_eq!(snapshots[1][4].template, "middle");
        assert_eq!(snapshots[2][5].template, "grand");
        assert_eq!(snapshots[0][3].token, snapshots[2][3].token);
        assert_ne!(snapshots[1][4].token, snapshots[2][5].token);
    }

    #[test]
    fn failed_render_releases_all_frames_before_error_observer() {
        let observed = Arc::new(Mutex::new(Vec::new()));
        let output = observed.clone();
        let mut env = environment();
        env.add_function("fail", |state: &State| -> Result<String, Error> {
            assert_eq!(state.render_frame_depth(), 2);
            Err(Error::new(ErrorKind::InvalidOperation, "failed"))
        });
        env.add_template("bad", "{{fail()}}").unwrap();
        env.set_runtime_error_observer(move |state, _| output.lock().unwrap().push(state.render_frame_depth()));
        assert!(env.template_from_str("{% include 'bad' %}").unwrap().render(context!()).is_err());
        assert_eq!(*observed.lock().unwrap(), [0]);
        assert_eq!(env.template_from_str("{{depth()}}").unwrap().render(context!()).unwrap(), "1");
    }

    #[test]
    fn error_frame_observer_sees_nested_instruction_before_each_unwind() {
        let observed = Arc::new(Mutex::new(Vec::new()));
        let output = observed.clone();
        let mut env = environment();
        env.add_function("fail", || -> Result<String, Error> {
            Err(Error::new(ErrorKind::InvalidOperation, "failed"))
        });
        env.add_template("child", "inside\n{{ fail() }}").unwrap();
        env.set_runtime_error_frame_observer(move |state, _, line| {
            output.lock().unwrap().push((state.name().to_owned(), line, state.render_frame_depth()));
        });
        assert!(env.template_from_str("parent\n{% include 'child' %}").unwrap().render(context!()).is_err());
        assert_eq!(*observed.lock().unwrap(), [("child".to_owned(), 2, 2), ("<string>".to_owned(), 2, 1)]);
    }
}

// the cost of a single include against the stack limit.
#[cfg(feature = "multi_template")]
const INCLUDE_RECURSION_COST: usize = 10;

// the cost of a single macro call against the stack limit.
#[cfg(feature = "macros")]
const MACRO_RECURSION_COST: usize = 4;

/// A VM operation whose runtime hook may defer it past the current call.
#[derive(Clone)]
pub(crate) enum DeferredOperation {
    /// `super()` in the current block.
    Super,
    /// `self.<name>()`.
    #[cfg_attr(not(feature = "multi_template"), allow(dead_code))]
    SelfBlock(Arc<str>),
    /// A recursive `loop(...)` call.
    Loop { iterable: Value, target: u32, depth: usize },
}

/// Whether an import module exports native macros.
#[cfg(all(feature = "macros", feature = "multi_template"))]
fn module_has_macros(module: &Value) -> bool {
    module.downcast_object::<crate::vm::module_object::Module>().is_some_and(|module|
        module.exported_items().iter().any(|(_, value)| value.downcast_object_ref::<crate::vm::macro_object::Macro>().is_some()))
}

/// Record `operation` on the state while its deferral hook runs.
fn with_deferred<'t, 'e, R>(state: &mut State<'t, 'e>, operation: DeferredOperation,
    run: impl FnOnce(&mut State<'t, 'e>) -> R) -> R {
    let previous = state.pending_deferred.replace(Some(operation));
    let result = run(state);
    state.pending_deferred.replace(previous);
    result
}

/// A deferred operation detached from the activation that created it, with
/// its own copy of the frames and the owners of everything they borrow.
/// Created by [`State::detach_deferred`]; runs at most once.
pub struct DetachedActivation {
    detached: crate::vm::state::DetachedState,
    operation: DeferredOperation,
}

impl std::fmt::Debug for DetachedActivation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DetachedActivation").field("template", &self.detached.state.name()).finish()
    }
}

impl DetachedActivation {
    pub(crate) fn new(detached: crate::vm::state::DetachedState, operation: DeferredOperation) -> Self {
        Self { detached, operation }
    }

    /// Run the operation. `scope` receives the detached state and a function
    /// that executes the operation in it, so a host can install per-state
    /// context around the execution; it returns the operation's captured output.
    pub fn run(mut self, scope: impl for<'t, 'e> FnOnce(&mut State<'t, 'e>,
        &mut dyn FnMut(&mut State<'t, 'e>) -> Result<Value, Error>) -> Result<Value, Error>) -> Result<Value, Error> {
        let operation = self.operation.clone();
        scope(&mut self.detached.state, &mut |state| {
            let vm = Vm::new(state.env());
            let mut rv = String::new();
            let mut out = match state.env().fragment_joiner() {
                Some(joiner) => Output::with_fragments(&mut rv, joiner),
                None => Output::new(&mut rv),
            };
            match &operation {
                DeferredOperation::Super => vm.perform_super(state, &mut out, true),
                #[cfg(feature = "multi_template")]
                DeferredOperation::SelfBlock(name) => vm.perform_self_block(state, &mut out, name),
                #[cfg(not(feature = "multi_template"))]
                DeferredOperation::SelfBlock(_) => Err(Error::new(ErrorKind::InvalidOperation,
                    "self blocks need the multi_template feature")),
                DeferredOperation::Loop { iterable, target, depth } =>
                    vm.execute_deferred_loop(state, &mut out, iterable.clone(), *target, *depth),
            }
        })
    }
}

/// A copy of an activation detached for host code that runs template code
/// (such as a macro body) outside the state it was taken from, for example on
/// its own native coroutine. Created by [`State::detach_for_host`].
pub struct DetachedHostState {
    detached: crate::vm::state::DetachedState,
}

impl std::fmt::Debug for DetachedHostState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DetachedHostState").field("template", &self.detached.state.name()).finish()
    }
}

impl DetachedHostState {
    pub(crate) fn new(detached: crate::vm::state::DetachedState) -> Self {
        Self { detached }
    }

    /// Run host code with the detached state.
    pub fn run<R>(mut self, scope: impl for<'t, 'e> FnOnce(&mut State<'t, 'e>) -> R) -> R {
        scope(&mut self.detached.state)
    }
}

/// Helps to evaluate something.
#[cfg_attr(feature = "internal_debug", derive(Debug))]
pub struct Vm<'env> {
    env: &'env Environment<'env>,
}

#[derive(Debug)]
struct PreparedMethod { receiver: Value, name: String }
impl crate::value::Object for PreparedMethod {
    fn call(self: &std::sync::Arc<Self>, state: &State, args: &[Value]) -> Result<Value, Error> {
        self.receiver.call_method(state, &self.name, args)
    }
}

pub(crate) fn prepare_blocks<'env, 'template>(
    blocks: &'template BTreeMap<&'env str, Instructions<'env>>,
) -> BTreeMap<&'env str, BlockStack<'template, 'env>> {
    blocks
        .iter()
        .map(|(name, instr)| (*name, BlockStack::new(instr)))
        .collect()
}

fn get_or_lookup_local<'a, F>(vec: &mut [Option<&'a Value>], idx: u8, f: F) -> Option<&'a Value>
where
    F: FnOnce() -> Option<&'a Value>,
{
    if idx == !0 {
        f()
    } else if let Some(Some(rv)) = vec.get(idx as usize) {
        Some(*rv)
    } else {
        let val = some!(f());
        vec[idx as usize] = Some(val);
        Some(val)
    }
}

fn normalize_filter_test_name(name: &str) -> Cow<'_, str> {
    if name.as_bytes().iter().any(|b| b.is_ascii_whitespace()) {
        let mut normalized = String::with_capacity(name.len());
        normalized.extend(name.chars().filter(|c| !c.is_ascii_whitespace()));
        Cow::Owned(normalized)
    } else {
        Cow::Borrowed(name)
    }
}

impl<'env> Vm<'env> {
    /// Creates a new VM.
    pub fn new(env: &'env Environment<'env>) -> Vm<'env> {
        Vm { env }
    }

    /// Evaluates the given inputs.
    ///
    /// It returns both the last value left on the stack as well as the state
    /// at the end of the evaluation.
    pub fn eval<'template>(
        &self,
        instructions: &'template Instructions<'env>,
        root: Value,
        blocks: &'template BTreeMap<&'env str, Instructions<'env>>,
        out: &mut Output,
        auto_escape: AutoEscape,
    ) -> Result<(Option<Value>, State<'template, 'env>), Error> {
        self.eval_retaining(instructions, root, blocks, out, auto_escape, None)
    }

    /// Like [`Vm::eval`]; the state also retains the root template's
    /// allocation, which owns `instructions` and `blocks`.
    pub(crate) fn eval_retaining<'template>(
        &self,
        instructions: &'template Instructions<'env>,
        root: Value,
        blocks: &'template BTreeMap<&'env str, Instructions<'env>>,
        out: &mut Output,
        auto_escape: AutoEscape,
        retained: Option<&Arc<crate::loader::LoadedTemplate>>,
    ) -> Result<(Option<Value>, State<'template, 'env>), Error> {
        let mut state = State::new(
            Context::new_with_frame(self.env, ok!(Frame::new_checked(root))),
            auto_escape,
            instructions,
            prepare_blocks(blocks),
        );
        if let Some(template) = retained { state.retain_root_template(template); }
        state.runtime_context = self.env.runtime_context_event(&state, crate::RuntimeContextEvent::Root)?;
        match self.eval_state(&mut state, out, RuntimeRenderFrameKind::Root) {
            Ok(value) => Ok((value, state)),
            Err(error) => {
                self.env.observe_runtime_error(&state, &error);
                Err(error)
            }
        }
    }

    /// Evaluate a macro in a state.
    #[cfg(feature = "macros")]
    pub fn eval_macro(
        &self,
        state: &State,
        macro_id: usize,
        out: &mut Output,
        closure: Value,
        runtime_context: Value,
        caller: Option<Value>,
        captures: Vec<(&str, Value)>,
        args: Vec<Value>,
    ) -> Result<Option<Value>, Error> {
        let (instructions, pc, _, defining_blocks, blocks, auto_escape) = &state.macros[macro_id];
        let context_base = state.ctx.clone_base();
        let mut ctx = Context::new_with_frame(self.env, Frame::new(context_base));
        ok!(ctx.push_frame(Frame::new(closure)));
        if let Some(caller) = caller {
            ctx.store("caller", caller);
        }
        for (name, value) in captures { ctx.store(name, value); }
        ok!(ctx.incr_depth(state.ctx.depth() + MACRO_RECURSION_COST));
        self.do_eval(
            &mut State::new_for_macro(ctx, state, instructions, runtime_context, defining_blocks.clone(), blocks.clone(),
                *auto_escape),
            out,
            Stack::from(args),
            *pc,
        )
    }

    /// This is the actual evaluation loop that works with a specific context.
    #[inline(always)]
    fn eval_state(
        &self,
        state: &mut State<'_, 'env>,
        out: &mut Output,
        kind: RuntimeRenderFrameKind,
    ) -> Result<Option<Value>, Error> {
        let _frame = state.enter_render_frame(kind, state.name());
        self.do_eval(state, out, Stack::default(), 0)
    }

    /// Performs the actual evaluation, optionally with stack growth functionality.
    fn do_eval(
        &self,
        state: &mut State<'_, 'env>,
        out: &mut Output,
        stack: Stack,
        pc: u32,
    ) -> Result<Option<Value>, Error> {
        self.do_eval_with_recursion(state, out, stack, pc, None)
    }

    fn do_eval_with_recursion(
        &self, state: &mut State<'_, 'env>, out: &mut Output, stack: Stack, pc: u32,
        initial_recursion: Option<(u32, bool, usize)>,
    ) -> Result<Option<Value>, Error> {
        out.set_capture_wrapper(self.env.runtime_capture_wrapper());
        // An evaluated frame may fail while nested arguments still have
        // prepared dispatchers. Keep the caller's dispatch state separate.
        let pending = state.pending_calls.borrow().clone();
        let prepared = state.prepared_call.borrow().clone();
        let policy = state.call_policy.get();
        #[cfg(feature = "stacker")]
        let result = {
            stacker::maybe_grow(32 * 1024, 1024 * 1024, || {
                self.eval_impl(state, out, stack, pc, initial_recursion)
            })
        };
        #[cfg(not(feature = "stacker"))]
        let result = {
            self.eval_impl(state, out, stack, pc, initial_recursion)
        };
        let discarded_pending = state.pending_calls.replace(pending);
        let discarded_prepared = state.prepared_call.replace(prepared);
        state.call_policy.set(policy);
        // Drop foreign values after the RefCell borrows end: destructors can
        // call back into the renderer.
        drop((discarded_pending, discarded_prepared));
        result
    }

    #[inline]
    fn eval_impl(
        &self,
        state: &mut State<'_, 'env>,
        out: &mut Output,
        mut stack: Stack,
        mut pc: u32,
        initial_recursion: Option<(u32, bool, usize)>,
    ) -> Result<Option<Value>, Error> {
        let initial_auto_escape = state.auto_escape.get();
        let undefined_behavior = state.undefined_behavior();
        let strict_undefined = matches!(
            undefined_behavior,
            UndefinedBehavior::Strict | UndefinedBehavior::SemiStrict
        );
        let mut auto_escape_stack = vec![];
        let mut next_loop_recursion_jump = initial_recursion;
        let mut deferred_capture: Option<&'env str> = None;
        let mut loaded_filters = [None; MAX_LOCALS];
        let mut loaded_tests = [None; MAX_LOCALS];
        let mut active_filtered_inputs = Vec::new();

        // If we are extending we are holding the instructions of the target parent
        // template here.  This is used to detect multiple extends and the evaluation
        // uses these instructions when it makes it to the end of the instructions.
        #[cfg(feature = "multi_template")]
        let mut parent_instructions: Option<&Instructions<'env>> = None;
        #[cfg(feature = "multi_template")]
        let mut inherited_frames = Vec::new();

        macro_rules! recurse_loop {
            ($capture:expr, $loop_object:expr) => {{
                let Some(jump_target) = $loop_object.recurse_jump_target else {
                    bail!(Error::new(
                        ErrorKind::InvalidOperation,
                        "cannot recurse outside of recursive loop",
                    ))
                };
                // the way this works is that we remember the next instruction
                // as loop exit jump target.  Whenever a loop is pushed, it
                // memorizes the value in `next_loop_iteration_jump` to jump
                // to.
                next_loop_recursion_jump = Some((pc + 1, $capture, $loop_object.depth + 1));
                if $capture {
                    out.begin_capture(CaptureMode::Capture);
                }
                pc = jump_target;
                continue;
            }};
        }

        // looks nicer this way
        #[allow(clippy::while_let_loop)]
        loop {
            let instr = match state.instructions.get(pc) {
                Some(instr) => instr,
                #[cfg(not(feature = "multi_template"))]
                None => break,
                #[cfg(feature = "multi_template")]
                None => {
                    // when an extends statement appears in a template, when we hit the
                    // last instruction we need to check if parent instructions were
                    // stashed away (which means we found an extends tag which invoked
                    // `LoadBlocks`).  If we do find instructions, we reset back to 0
                    // from the new instructions.
                    state.instructions = match parent_instructions.take() {
                        Some(instr) => {
                            inherited_frames.push(state.enter_render_frame(RuntimeRenderFrameKind::Inheritance, instr.name()));
                            instr
                        },
                        None => break,
                    };
                    out.end_capture(AutoEscape::None)?;
                    pc = 0;
                    // because we swap out the instructions we also need to unload all
                    // the filters and tests to ensure that we are not accidentally
                    // reusing the local_ids for completely different filters.
                    loaded_filters = [None; MAX_LOCALS];
                    loaded_tests = [None; MAX_LOCALS];
                    continue;
                }
            };

            // if we only have two arguments that we pull from the stack, we
            // can assign them to a and b.  This slightly reduces the amount of
            // code bloat generated here.  Same with the error.
            let a;
            let b;
            let mut err;

            macro_rules! func_binop {
                ($method:ident) => {{
                    b = stack.pop();
                    a = stack.pop();
                    stack.push(ctx_ok!(state.env().runtime_operator(state, stringify!($method), &a, Some(&b))
                        .unwrap_or_else(|| ops::$method(&a, &b))));
                }};
            }

            macro_rules! op_binop {
                ($op:tt) => {{
                    b = stack.pop();
                    a = stack.pop();
                    ctx_ok!(undefined_behavior.assert_value_not_undefined(&a));
                    ctx_ok!(undefined_behavior.assert_value_not_undefined(&b));
                    stack.push(ctx_ok!(state.env().runtime_operator(state, stringify!($op), &a, Some(&b))
                        .unwrap_or_else(|| Ok(Value::from(a $op b)))));
                }};
            }

            macro_rules! bail {
                ($err:expr) => {{
                    err = $err;
                    process_err(&mut err, pc, state);
                    self.env.observe_runtime_error_frame(state, &err, state.instructions.get_line(pc).unwrap_or(1));
                    return Err(err);
                }};
            }

            macro_rules! ctx_ok {
                ($expr:expr) => {
                    match $expr {
                        Ok(rv) => rv,
                        Err(err) => bail!(err),
                    }
                };
            }

            macro_rules! assert_valid {
                ($expr:expr) => {{
                    let val = $expr;
                    match val.validate() {
                        Ok(val) => val,
                        Err(err) => bail!(err),
                    }
                }};
            }

            // if the fuel consumption feature is enabled, track the fuel
            // consumption here.
            #[cfg(feature = "fuel")]
            if let Some(ref tracker) = state.fuel_tracker {
                ctx_ok!(tracker.track(instr));
            }

            match instr {
                Instruction::Swap => {
                    a = stack.pop();
                    b = stack.pop();
                    stack.push(a);
                    stack.push(b);
                }
                Instruction::EmitRaw(val) => {
                    if self.env.has_runtime_constant_output_guard() && val.contains('\u{1}') {
                        if out.is_capturing() { out.mark_untrusted_control(); }
                        else { ctx_ok!(self.env.runtime_constant_output_guard(&Value::from(val.to_string()))); }
                    }
                    // this only produces a format error, no need to attach
                    // location information.
                    ok!(out.write_str(val).map_err(Error::from));
                    out.mark_constant();
                }
                Instruction::EmitConst(val) => {
                    if self.env.has_runtime_constant_output_guard() && val.contains('\u{1}') {
                        if out.is_capturing() { out.mark_untrusted_control(); }
                        else { ctx_ok!(self.env.runtime_constant_output_guard(&Value::from(val.to_string()))); }
                    }
                    ok!(out.write_str(val).map_err(Error::from)); out.mark_constant();
                }
                Instruction::EmitConstFragment(val) => {
                    ctx_ok!(self.env.runtime_constant_output_guard(val));
                    ctx_ok!(out.write_constant_fragment(val.clone()));
                }
                Instruction::OutputGroupBoundary => { ctx_ok!(out.finish_group()); }
                Instruction::PrepareEmit => { stack.push(ctx_ok!(self.env.prepare_output(state))); }
                Instruction::EmitPrepared => {
                    a = stack.pop();
                    b = stack.pop();
                    ctx_ok!(self.env.format_prepared(out, state, &a, &b));
                }
                Instruction::Emit => {
                    let value = stack.pop();
                    if self.env.is_default_formatter() {
                        if strict_undefined
                            && matches!(value.0, ValueRepr::Undefined(UndefinedType::Default))
                        {
                            bail!(Error::from(ErrorKind::UndefinedError));
                        }
                        ctx_ok!(write_escaped(out, state.auto_escape.get(), &value));
                    } else {
                        ctx_ok!(self.env.format(&value, state, out));
                    }
                }
                Instruction::EmitFilterBlock => {
                    let value = stack.pop();
                    if out.supports_fragments() {
                        ctx_ok!(out.write_fragment(value));
                    } else {
                        ctx_ok!(self.env.format(&value, state, out));
                    }
                }
                Instruction::StoreLocal(name) => {
                    state.ctx.store(name, stack.pop());
                }
                Instruction::PublishLocals(names) => {
                    let values = state.ctx.publish(names);
                    ctx_ok!(self.env.runtime_context_event(state, crate::RuntimeContextEvent::Publish(values)));
                }
                Instruction::PublishImports(names) => {
                    let values = state.ctx.publish_imports(names);
                    ctx_ok!(self.env.runtime_context_event(state, crate::RuntimeContextEvent::PublishImports(values)));
                }
                Instruction::Lookup(name) => {
                    let (value, root_binding) = state.ctx.load_instruction(name);
                    let value = assert_valid!(value.unwrap_or(Value::UNDEFINED));
                    stack.push(if root_binding { ctx_ok!(self.env.runtime_prepared_use(value)) } else { value });
                }
                #[cfg(feature = "multi_template")]
                Instruction::LookupImport(name, depth, line) => {
                    let exports = state.ctx.current_module_exports_mut().clone();
                    let hidden = self.env.respect_include_context()
                        && (name.starts_with('_') || state.ctx.current_imports_mut().contains(name));
                    let found = match exports {
                        Some(exports) => exports.get_item(&Value::from(*name)).ok().filter(|value| !value.is_undefined()),
                        None => state.ctx.current_locals_mut().get(name).filter(|_| !hidden).cloned(),
                    };
                    let value = match found {
                        Some(value) => value,
                        None => ctx_ok!(self.env.runtime_import_missing(state, stack.peek_depth(*depth), name, *line)),
                    };
                    stack.push(assert_valid!(value));
                }
                Instruction::CaptureRootBinding(name, missing) => {
                    let value = if *missing { None } else { state.ctx.resolve_current_base_prepared(name) };
                    let value = match value {
                        Some(value) => assert_valid!(value),
                        None => ctx_ok!(self.env.runtime_root_missing(state.ctx.current_base(), name)),
                    };
                    state.ctx.store_root_binding(name, value);
                }
                Instruction::CaptureSelfReference => {
                    let value = ctx_ok!(self.env.runtime_context_event(state, crate::RuntimeContextEvent::SelfReference));
                    state.ctx.store_root_binding("self", value);
                }
                #[cfg(feature = "multi_template")]
                Instruction::PrepareSelfBlock(name) => {
                    let selected = ctx_ok!(self.env.runtime_self_block_prepare(state, name));
                    // A host-provided `self.<name>` is an attribute read: await it like one.
                    let selected = if selected.is_undefined() { selected } else { ctx_ok!(self.env.runtime_await(state, selected)) };
                    stack.push(selected);
                }
                Instruction::PrepareFunction(name) => {
                    stack.push(state.ctx.load_prepared(name).unwrap_or_else(|| Value::from_object(MissingPreparedFunction)));
                }
                Instruction::PrepareRootCall(name) => {
                    let value = assert_valid!(state.ctx.load_prepared(name).unwrap_or_else(|| Value::from_object(MissingPreparedFunction)));
                    let value = ctx_ok!(self.env.runtime_prepared_use(value));
                    stack.prepare_root_call(name, value);
                }
                Instruction::PrepareBinding(name) => {
                    stack.push(assert_valid!(state.ctx.load_prepared(name).unwrap_or(Value::UNDEFINED)));
                }
                Instruction::PrepareCapture(name) => {
                    // A constructed filtered loop resolves the name after its
                    // host constructor ran (in push_loop).
                    if ctx_ok!(self.env.runtime_loop_constructed(state)) {
                        deferred_capture = Some(*name);
                        stack.push(Value::UNDEFINED);
                    } else {
                        stack.push(assert_valid!(state.ctx.load_prepared(name).unwrap_or(Value::UNDEFINED)));
                    }
                }
                Instruction::LookupFilteredCapture => {
                    let capture = state.ctx.current_loop().and_then(|loop_state| loop_state.object.filter_capture.lock().unwrap().clone())
                        .expect("filtered predicate has a capture");
                    stack.push(assert_valid!(capture));
                }
                Instruction::PrepareAttribute => {
                    stack.push(ctx_ok!(self.env.runtime_prepare_attribute()));
                }
                Instruction::PrepareAwait => {
                    ctx_ok!(self.env.runtime_prepare_await(state));
                }
                Instruction::GetAttrPrepared(name, is_method) => {
                    a = stack.pop();
                    b = stack.pop();
                    let looked_up = self.env.runtime_attribute(state, &b, &a, name);
                    let post_lookup = matches!(&looked_up, Ok(None)) && a.downcast_object_ref::<Loop>().is_some();
                    if matches!(&looked_up, Ok(None)) {
                      if let Some(controller) = a.downcast_object::<Loop>() {
                        if controller.filter_pc.is_some() && controller.delegate().is_none()
                            && matches!(*name, "last" | "nextitem" | "length" | "revindex" | "revindex0") {
                            ctx_ok!(self.env.runtime_loop_handoff(state, &a, name));
                        }
                        if controller.filter_pc.is_some() {
                            match *name {
                                "last" | "nextitem" => ctx_ok!(self.fill_filtered(state, &controller, false)),
                                "length" | "revindex" | "revindex0" => ctx_ok!(self.prepare_filtered_length(state, &controller)),
                                _ => {},
                            }
                        }
                      }
                    }
                    let value = match looked_up {
                        Ok(Some(value)) => Ok(value),
                        Ok(None) if *is_method => Ok(match a.as_object().and_then(|object| object.get_method(name)) {
                            Some(value) => value,
                            None => Value::from_object(PreparedMethod { receiver: a.clone(), name: (*name).to_owned() }),
                        }),
                        Ok(None) => match crate::vm::state::with_host_state(state, || a.get_attribute_fast(name)) {
                            Some(value) => Ok(value),
                            None => undefined_behavior.handle_undefined(a.is_undefined()),
                        },
                        Err(error) => Err(error),
                    };
                    let value = if post_lookup && value.as_ref().is_ok_and(|value| value.kind() != ValueKind::Invalid) {
                        value.and_then(|value| self.env.runtime_loop_post_lookup(state, &b, &a, Some(name), None, value))
                    } else { value };
                    self.env.runtime_release_attribute(b);
                    let value = assert_valid!(ctx_ok!(value));
                    stack.push(ctx_ok!(self.env.runtime_await(state, value)));
                }
                Instruction::GetAttr(name) => {
                    a = stack.pop();
                    if let Some(controller) = a.downcast_object::<Loop>() {
                        if controller.filter_pc.is_some() && controller.delegate().is_none()
                            && matches!(*name, "last" | "nextitem" | "length" | "revindex" | "revindex0") {
                            ctx_ok!(self.env.runtime_loop_handoff(state, &a, name));
                        }
                        if controller.filter_pc.is_some() {
                            match *name {
                                "last" | "nextitem" => ctx_ok!(self.fill_filtered(state, &controller, false)),
                                "length" | "revindex" | "revindex0" => ctx_ok!(self.prepare_filtered_length(state, &controller)),
                                _ => {},
                            }
                        }
                    }
                    // This is a common enough operation that it's interesting to consider a fast
                    // path here.  This is slightly faster than the regular attr lookup because we
                    // do not need to pass down the error object for the more common success case.
                    // Only when we cannot look up something, we start to consider the undefined
                    // special case.
                    let value = match crate::vm::state::with_host_state(state, || a.get_attribute_fast(name)) {
                        Some(value) => assert_valid!(value),
                        None => ctx_ok!(undefined_behavior.handle_undefined(a.is_undefined())),
                    };
                    stack.push(ctx_ok!(self.env.runtime_await(state, value)));
                }
                Instruction::PrepareMethod(name) => {
                    a = stack.pop();
                    let method = a.as_object().and_then(|object| object.get_method(name));
                    stack.push(match method {
                        Some(method) => ctx_ok!(self.env.runtime_await(state, assert_valid!(method))),
                        None => Value::from_object(PreparedMethod { receiver: a, name: (*name).to_owned() }),
                    });
                }
                Instruction::SetAttr(name) => {
                    b = stack.pop();
                    a = stack.pop();
                    if let Some(ns) = b.downcast_object_ref::<Namespace>() {
                        ns.set_value(name, a);
                    } else if let Some(object) = b.as_object() {
                        if !ctx_ok!(object.set_attribute(name, a)) {
                            bail!(Error::new(ErrorKind::InvalidOperation, "cannot assign to this object"));
                        }
                    } else {
                        bail!(Error::new(
                            ErrorKind::InvalidOperation,
                            format!("can only assign to namespaces, not {}", b.kind())
                        ));
                    }
                }
                Instruction::PrepareItem => {
                    stack.push(ctx_ok!(self.env.runtime_prepare_item()));
                }
                Instruction::GetItemPrepared => {
                    a = stack.pop();
                    b = stack.pop();
                    let method = stack.pop();
                    let looked_up = self.env.runtime_item(state, &method, &b, &a);
                    let post_lookup = matches!(&looked_up, Ok(None)) && b.downcast_object_ref::<Loop>().is_some();
                    let normalized_key = if post_lookup { ctx_ok!(self.env.runtime_loop_item_key(&method, &a)) } else { None };
                    let lookup_key = normalized_key.as_ref().unwrap_or(&a);
                    if matches!(&looked_up, Ok(None)) {
                      if let (Some(controller), Some(name)) = (b.downcast_object::<Loop>(), lookup_key.as_str()) {
                        if controller.filter_pc.is_some() && controller.delegate().is_none()
                            && matches!(name, "last" | "nextitem" | "length" | "revindex" | "revindex0") {
                            ctx_ok!(self.env.runtime_loop_handoff(state, &b, name));
                        }
                        if controller.filter_pc.is_some() {
                            match name {
                                "last" | "nextitem" => ctx_ok!(self.fill_filtered(state, &controller, false)),
                                "length" | "revindex" | "revindex0" => ctx_ok!(self.prepare_filtered_length(state, &controller)),
                                _ => {},
                            }
                        }
                      }
                    }
                    let value = match looked_up {
                        Ok(Some(value)) => Ok(value),
                        Ok(None) if post_lookup && matches!(lookup_key.as_str(), Some("changed" | "cycle")) => {
                            Ok(Value::from_object(PreparedMethod {
                                receiver: b.clone(), name: lookup_key.as_str().unwrap().to_owned(),
                            }))
                        },
                        Ok(None) => match crate::vm::state::with_host_state(state, || b.get_item_opt(lookup_key)) {
                            Some(value) => Ok(value),
                            None => undefined_behavior.handle_undefined(b.is_undefined()),
                        },
                        Err(error) => Err(error),
                    };
                    let value = if post_lookup && value.as_ref().is_ok_and(|value| value.kind() != ValueKind::Invalid) {
                        value.and_then(|value| self.env.runtime_loop_post_lookup(state, &method, &b, None, Some(&a), value))
                    } else { value };
                    self.env.runtime_release_item(method);
                    let value = assert_valid!(ctx_ok!(value));
                    stack.push(ctx_ok!(self.env.runtime_await(state, value)));
                }
                Instruction::GetItem => {
                    a = stack.pop();
                    b = stack.pop();
                    if let (Some(controller), Some(name)) = (b.downcast_object::<Loop>(), a.as_str()) {
                        if controller.filter_pc.is_some() && controller.delegate().is_none()
                            && matches!(name, "last" | "nextitem" | "length" | "revindex" | "revindex0") {
                            ctx_ok!(self.env.runtime_loop_handoff(state, &b, name));
                        }
                        if controller.filter_pc.is_some() {
                            match name {
                                "last" | "nextitem" => ctx_ok!(self.fill_filtered(state, &controller, false)),
                                "length" | "revindex" | "revindex0" => ctx_ok!(self.prepare_filtered_length(state, &controller)),
                                _ => {},
                            }
                        }
                    }
                    let value = match crate::vm::state::with_host_state(state, || b.get_item_opt(&a)) {
                        Some(value) => assert_valid!(value),
                        None => ctx_ok!(undefined_behavior.handle_undefined(b.is_undefined())),
                    };
                    stack.push(ctx_ok!(self.env.runtime_await(state, value)));
                }
                Instruction::Slice => {
                    let step = stack.pop();
                    let stop = stack.pop();
                    b = stack.pop();
                    a = stack.pop();
                    if a.is_undefined() && matches!(undefined_behavior, UndefinedBehavior::Strict) {
                        bail!(Error::from(ErrorKind::UndefinedError));
                    }
                    let bounds = ctx_ok!(state.env().runtime_tuple(vec![b.clone(), stop.clone(), step.clone()]));
                    stack.push(ctx_ok!(state.env().runtime_operator(state, "slice", &a, Some(&bounds))
                        .unwrap_or_else(|| ops::slice(a, b, stop, step))));
                }
                Instruction::LoadConst(value) => {
                    let resolved = match &value.0 {
                        ValueRepr::F64(number) if !number.is_finite() =>
                            self.env.runtime_nonfinite_constant(*number),
                        _ => None,
                    };
                    stack.push(match resolved { Some(result) => ctx_ok!(result), None => value.clone() });
                }
                Instruction::LoadRawConst(value) => { stack.push(value.clone()); }
                Instruction::BuildMap(pair_count) => {
                    let mut pairs = Vec::with_capacity(*pair_count);
                    stack.reverse_top(*pair_count * 2);
                    for _ in 0..*pair_count {
                        let key = stack.pop();
                        let value = stack.pop();
                        pairs.push((key, value));
                    }
                    stack.push(ctx_ok!(state.env().runtime_map(pairs)))
                }
                Instruction::BuildKwargs(pair_count) => {
                    let mut map = value_map_with_capacity(*pair_count);
                    stack.reverse_top(*pair_count * 2);
                    for _ in 0..*pair_count {
                        let key = stack.pop();
                        let value = stack.pop();
                        map.insert(key, value);
                    }
                    stack.push(Kwargs::wrap(map))
                }
                Instruction::MergeKwargs(count) => {
                    let mut kwargs_sources = Vec::from_iter((0..*count).map(|_| stack.pop()));
                    kwargs_sources.reverse();
                    stack.push(ctx_ok!(self.merge_kwargs(kwargs_sources)));
                }
                Instruction::BuildList(n) => {
                    let count = match n { Some(n) => *n, None => ctx_ok!(usize::try_from(stack.pop())) };
                    let mut v = Vec::with_capacity(untrusted_size_hint(count));
                    for _ in 0..count {
                        v.push(stack.pop());
                    }
                    v.reverse();
                    stack.push(ctx_ok!(state.env().runtime_list(v)))
                }
                Instruction::Increment => {
                    let count = ctx_ok!(usize::try_from(stack.pop()));
                    let next = ctx_ok!(count.checked_add(1).ok_or_else(||
                        Error::new(ErrorKind::InvalidOperation, "internal list count overflow")));
                    stack.push(Value::from(next));
                }
                Instruction::BuildTuple(count) => {
                    let mut values = Vec::with_capacity(untrusted_size_hint(*count));
                    for _ in 0..*count { values.push(stack.pop()); }
                    values.reverse();
                    stack.push(ctx_ok!(state.env().runtime_tuple(values)))
                }
                Instruction::InternSubscriptTuple | Instruction::InternArithmeticConstant => {
                    let value = stack.pop();
                    stack.push(ctx_ok!(state.env().runtime_subscript_tuple(state, value)));
                }
                Instruction::InternUniqueConstant(site) => {
                    let value = stack.pop();
                    stack.push(ctx_ok!(state.env().runtime_unique_constant(state, value, *site)));
                }
                Instruction::PrepareCall(policy) => {
                    let prepared = ctx_ok!(self.env.runtime_prepare_call(state, *policy));
                    state.pending_calls.borrow_mut().push(prepared);
                }
                Instruction::SetCallPolicy(policy) => {
                    state.call_policy.set(Some(*policy));
                    *state.prepared_call.borrow_mut() = state.pending_calls.borrow_mut().pop();
                }
                Instruction::UnpackList(count) => {
                    ctx_ok!(self.unpack_list(&mut stack, *count));
                }
                Instruction::UnpackLists(count) => {
                    let lists = Vec::from_iter((0..*count).map(|_| stack.pop()));
                    let mut len = 0;
                    for list in lists.into_iter().rev() {
                        for item in ctx_ok!(list.try_iter()) {
                            stack.push(assert_valid!(item));
                            len += 1;
                        }
                    }
                    stack.push(Value::from(len));
                }
                Instruction::Add => func_binop!(add),
                Instruction::Sub => func_binop!(sub),
                Instruction::Mul => func_binop!(mul),
                Instruction::Div => func_binop!(div),
                Instruction::IntDiv => func_binop!(int_div),
                Instruction::Rem => {
                    b = stack.pop();
                    a = stack.pop();
                    stack.push(ctx_ok!(state.env().runtime_operator(state, "rem", &a, Some(&b))
                        .or_else(|| state.env().string_format_operator(&a, &b))
                        .unwrap_or_else(|| ops::rem(&a, &b))));
                }
                Instruction::Pow => func_binop!(pow),
                Instruction::Eq => op_binop!(==),
                Instruction::Ne => op_binop!(!=),
                Instruction::Gt => op_binop!(>),
                Instruction::Gte => op_binop!(>=),
                Instruction::Lt => op_binop!(<),
                Instruction::Lte => op_binop!(<=),
                Instruction::Not => {
                    a = stack.pop();
                    stack.push(Value::from(!ctx_ok!(undefined_behavior.is_true(&a))));
                }
                Instruction::StringConcatMany(count) => {
                    let mut values = Vec::with_capacity(untrusted_size_hint(*count));
                    for _ in 0..*count { values.push(stack.pop()); }
                    values.reverse();
                    for value in &values { ctx_ok!(undefined_behavior.assert_value_not_undefined(value)); }
                    let result = if let Some(result) = state.env().runtime_concat(state, &values) {
                        ctx_ok!(result)
                    } else {
                        let mut iter = values.into_iter();
                        let mut result = iter.next().unwrap_or_default();
                        for value in iter {
                            result = ctx_ok!(state.env().runtime_operator(state, "concat", &result, Some(&value))
                                .unwrap_or_else(|| Ok(ops::string_concat(result, &value))));
                        }
                        result
                    };
                    stack.push(result);
                }
                Instruction::In => {
                    a = stack.pop();
                    b = stack.pop();
                    // the in-operator can fail if either side is undefined and
                    // we are in strict mode.
                    ctx_ok!(state.undefined_behavior().assert_iterable(&a));
                    ctx_ok!(state.undefined_behavior().assert_value_not_undefined(&b));
                    stack.push(ctx_ok!(state.env().runtime_operator(state, "contains", &b, Some(&a))
                        .unwrap_or_else(|| ops::contains(&a, &b))));
                }
                Instruction::CompareAndPreserve(op) => {
                    b = stack.pop();
                    a = stack.pop();
                    let op_name = match op { CompareOp::Eq => "==", CompareOp::Ne => "!=", CompareOp::Lt => "<", CompareOp::Lte => "<=",
                        CompareOp::Gt => ">", CompareOp::Gte => ">=", CompareOp::In => "contains", CompareOp::NotIn => "not_contains" };
                    let result = if let Some(result) = state.env().runtime_operator(state, op_name, &a, Some(&b)) {
                        ctx_ok!(result)
                    } else { Value::from(match op {
                        CompareOp::Eq => {
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&a));
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&b));
                            a == b
                        }
                        CompareOp::Ne => {
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&a));
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&b));
                            a != b
                        }
                        CompareOp::Lt => {
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&a));
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&b));
                            a < b
                        }
                        CompareOp::Lte => {
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&a));
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&b));
                            a <= b
                        }
                        CompareOp::Gt => {
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&a));
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&b));
                            a > b
                        }
                        CompareOp::Gte => {
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&a));
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&b));
                            a >= b
                        }
                        CompareOp::In | CompareOp::NotIn => {
                            ctx_ok!(undefined_behavior.assert_iterable(&b));
                            ctx_ok!(undefined_behavior.assert_value_not_undefined(&a));
                            let contains = ctx_ok!(ops::contains(&b, &a)).is_true();
                            if matches!(op, CompareOp::NotIn) {
                                !contains
                            } else {
                                contains
                            }
                        }
                    }) };
                    stack.push(b);
                    stack.push(result);
                }
                Instruction::Neg => {
                    a = stack.pop();
                    stack.push(ctx_ok!(state.env().runtime_operator(state, "neg", &a, None)
                        .unwrap_or_else(|| ops::neg(&a))));
                }
                Instruction::Pos => {
                    a = stack.pop();
                    stack.push(ctx_ok!(state.env().runtime_operator(state, "pos", &a, None)
                        .unwrap_or_else(|| if a.kind() == ValueKind::Number { Ok(a) }
                            else { Err(Error::new(ErrorKind::InvalidOperation, "cannot apply unary plus")) })));
                }
                Instruction::PushWith => {
                    ctx_ok!(state.ctx.push_frame(Frame::default()));
                }
                Instruction::PopFrame => {
                    state.ctx.pop_frame();
                }
                Instruction::PopLoopFrame => {
                    let mut l = state.ctx.pop_frame().current_loop.unwrap();
                    l.object.release_from_frame();
                    if let Some((target, end_capture, _)) = l.current_recursion_jump.take() {
                        if target == u32::MAX { return Ok(None); }
                        pc = target;
                        if end_capture {
                            stack.push(ctx_ok!(out.end_capture(state.auto_escape.get())));
                        }
                        continue;
                    }
                }
                #[cfg(feature = "macros")]
                Instruction::IsUndefined => {
                    a = stack.pop();
                    stack.push(Value::from(a.is_undefined()));
                }
                Instruction::PushLoop(flags) => {
                    a = stack.pop();
                    ctx_ok!(self.push_loop(state, a, *flags, pc, next_loop_recursion_jump.take(), None, None, None));
                }
                Instruction::PushFilteredLoop(entry, captured, recursive) => {
                    let capture = if *captured { Some(stack.pop()) } else { None };
                    a = stack.pop();
                    let flags = if *recursive { LOOP_FLAG_WITH_LOOP_VAR | LOOP_FLAG_RECURSIVE } else { LOOP_FLAG_WITH_LOOP_VAR };
                    ctx_ok!(self.push_loop(state, a, flags, pc, next_loop_recursion_jump.take(), Some(*entry), capture,
                        deferred_capture.take()));
                }
                Instruction::Iterate(jump_target) | Instruction::IterateFiltering(jump_target) => {
                    let guard = matches!(instr, Instruction::IterateFiltering(_))
                        .then(|| state.enter_filtered_loop());
                    let next = if self.env.has_runtime_loop_next() {
                        let frames = state.render_frames();
                        let depth = state.filtered_loop_depth();
                        ctx_ok!(self.env.runtime_loop_next(frames, depth, &mut || Ok(state.ctx.next_loop_item())))
                    } else { state.ctx.next_loop_item() };
                    match next {
                        Some(item) => {
                            let item = assert_valid!(item);
                            if self.env.has_runtime_loop_advance() {
                                let loop_state = state.ctx.current_loop().unwrap();
                                if loop_state.with_loop_var {
                                    let controller = loop_state.object.clone();
                                    ctx_ok!(self.env.runtime_loop_advance(state, &Value::from_dyn_object(controller)));
                                }
                            }
                            if let Some(guard) = guard { active_filtered_inputs.push(guard); }
                            stack.push(item);
                        },
                        None => {
                            pc = *jump_target;
                            continue;
                        }
                    };
                }
                Instruction::IterateFiltered(jump_target) => {
                    let controller = state.ctx.current_loop().unwrap().object.clone();
                    ctx_ok!(self.fill_filtered(state, &controller, false));
                    let next = if controller.delegate().is_some() {
                        // The host pulls accepted items; predicates run in a fork of this state.
                        let item = crate::vm::state::with_host_state(state, || controller.advance())
                            .unwrap_or_else(|error| Some(Value::from(error)));
                        if item.is_some() { state.ctx.clear_loop_locals(); }
                        item
                    } else { state.ctx.next_loop_item() };
                    match next {
                        Some(item) => {
                            let item = assert_valid!(item);
                            if self.env.has_runtime_loop_advance() {
                                let loop_state = state.ctx.current_loop().unwrap();
                                if loop_state.with_loop_var {
                                    let controller = loop_state.object.clone();
                                    ctx_ok!(self.env.runtime_loop_advance(state, &Value::from_dyn_object(controller)));
                                }
                            }
                            stack.push(item);
                        },
                        None => { pc = *jump_target; continue; }
                    }
                }
                Instruction::CompleteLoopIteration => {
                    state.ctx.current_loop().unwrap().completed_iteration = true;
                }
                Instruction::AcceptFilteredIteration => {
                    state.ctx.current_loop().unwrap().accepted_iteration = true;
                }
                Instruction::PushDidNotIterateFiltered => {
                    let policy = self.env.loop_else_on_completed_iteration();
                    stack.push(Value::from(
                        state.ctx.current_loop().unwrap().did_not_iterate_filtered(policy),
                    ));
                }
                Instruction::PushDidNotIterate => {
                    let policy = self.env.loop_else_on_completed_iteration();
                    stack.push(Value::from(
                        state.ctx.current_loop().unwrap().did_not_iterate(policy),
                    ));
                }
                Instruction::Jump(jump_target) => {
                    pc = *jump_target;
                    continue;
                }
                Instruction::JumpIfFalse(jump_target) => {
                    a = stack.pop();
                    if !ctx_ok!(undefined_behavior.is_true(&a)) {
                        pc = *jump_target;
                        continue;
                    }
                }
                Instruction::JumpIfFalseFiltered(jump_target) => {
                    a = stack.pop();
                    let accepted = ctx_ok!(undefined_behavior.is_true(&a));
                    active_filtered_inputs.pop().expect("filtered input scope is active");
                    if !accepted {
                        pc = *jump_target;
                        continue;
                    }
                }
                Instruction::JumpIfFalseOrPop(jump_target) => {
                    if !ctx_ok!(undefined_behavior.is_true(stack.peek())) {
                        pc = *jump_target;
                        continue;
                    } else {
                        stack.pop();
                    }
                }
                Instruction::JumpIfTrueOrPop(jump_target) => {
                    if ctx_ok!(undefined_behavior.is_true(stack.peek())) {
                        pc = *jump_target;
                        continue;
                    } else {
                        stack.pop();
                    }
                }
                Instruction::PushAutoEscape => {
                    a = stack.pop();
                    let saved = ctx_ok!(self.env.runtime_context_event(state, crate::RuntimeContextEvent::EnterAutoEscape(a.clone())));
                    auto_escape_stack.push((state.auto_escape.get(), state.eval_auto_escape.get(), saved));
                    let derived = ctx_ok!(self.derive_auto_escape(a, initial_auto_escape));
                    state.auto_escape.set(derived);
                    state.eval_auto_escape.set(derived);
                }
                Instruction::PopAutoEscape => {
                    let (escape, eval_escape, saved) = auto_escape_stack.pop().unwrap();
                    ctx_ok!(self.env.runtime_context_event(state, crate::RuntimeContextEvent::LeaveAutoEscape(saved)));
                    state.auto_escape.set(escape);
                    state.eval_auto_escape.set(eval_escape);
                }
                Instruction::BeginCapture(mode) => {
                    out.begin_capture(*mode);
                }
                Instruction::EndCapture => {
                    stack.push(ctx_ok!(out.end_capture(state.auto_escape.get())));
                }
                Instruction::ApplyFilter(name, arg_count, local_id) => {
                    let normalized_name = normalize_filter_test_name(name);
                    let filter =
                        ctx_ok!(get_or_lookup_local(&mut loaded_filters, *local_id, || {
                            state.env().get_filter(normalized_name.as_ref())
                        })
                        .ok_or_else(|| {
                            Error::new(
                                ErrorKind::UnknownFilter,
                                format!("filter {} is unknown", normalized_name.as_ref()),
                            )
                        }));
                    let args = stack.get_call_args(*arg_count);
                    let arg_count = args.len();
                    a = ctx_ok!(self.env.runtime_await(state, ctx_ok!(filter.call(state, args))));
                    stack.drop_top(arg_count);
                    stack.push(a);
                }
                Instruction::PerformTest(name, arg_count, local_id) => {
                    let normalized_name = normalize_filter_test_name(name);
                    let test = ctx_ok!(get_or_lookup_local(&mut loaded_tests, *local_id, || {
                        state.env().get_test(normalized_name.as_ref())
                    })
                    .ok_or_else(|| {
                        Error::new(
                            ErrorKind::UnknownTest,
                            format!("test {} is unknown", normalized_name.as_ref()),
                        )
                    }));
                    let args = stack.get_call_args(*arg_count);
                    let arg_count = args.len();
                    a = ctx_ok!(self.env.runtime_await(state, ctx_ok!(test.call(state, args))));
                    stack.drop_top(arg_count);
                    stack.push(if self.env.preserve_test_results() { a } else { Value::from(ctx_ok!(a.is_true_fallible())) });
                }
                Instruction::CallFunction(..) | Instruction::CallFunctionCaptured(..) => {
                    let (name, arg_count, capture) = match instr {
                        Instruction::CallFunction(name, count) => (*name, *count, false),
                        Instruction::CallFunctionCaptured(name) => (*name, Some(0), true),
                        _ => unreachable!(),
                    };
                    let prepared = stack.take_prepared_root_call(name);
                    let args = stack.get_call_args(arg_count);
                    let mut captured = None;
                    // super is a special function reserved for super-ing into blocks.
                    let mut awaited = false;
                    let rv = if name == "super" {
                        if !args.is_empty() {
                            bail!(Error::new(
                                ErrorKind::InvalidOperation,
                                "super() takes no arguments",
                            ));
                        }
                        if let Some(resolve) = self.env.runtime_deferred_super() {
                            awaited = true;
                            ctx_ok!(with_deferred(state, DeferredOperation::Super, |state| resolve(state, &mut |state| self.perform_super(state, out, true))))
                        } else {
                            ctx_ok!(self.perform_super(state, out, true))
                        }
                    } else if let Some(func) = prepared.or_else(|| state.lookup(name)) {
                        if func.downcast_object_ref::<MissingPreparedFunction>().is_some() {
                            bail!(Error::new(ErrorKind::UnknownFunction, format!("{name} is unknown")));
                        }
                        if capture { captured = Some(func.clone()); }
                        // calling loops is a special operation that starts the recursion process.
                        // this bypasses the actual `call` implementation which would just fail
                        // with an error.
                        if let Some(loop_object) = func.downcast_object_ref::<Loop>() {
                            let count = args.len();
                            let normalized = self.env.runtime_loop_recursion(state, &func, args, loop_object.recurse_jump_target.is_some());
                            let normalized = if let Some(normalized) = normalized {
                                Some(ctx_ok!(normalized))
                            } else if count != 1 {
                                bail!(Error::new(ErrorKind::InvalidOperation, "loop() takes one argument"));
                            } else { None };
                            if let Some(outcome) = normalized.as_ref().and_then(|value| value.downcast_object_ref::<LoopCallOutcome>()) {
                                let value = outcome.0.clone();
                                stack.drop_top(count);
                                if let Some(value) = captured.take() { stack.push(value); }
                                state.call_policy.set(None);
                                drop(state.prepared_call.borrow_mut().take());
                                stack.push(value);
                                pc += 1;
                                continue;
                            }
                            if let Some(resolve) = self.env.runtime_deferred_loop() {
                                let Some(target) = loop_object.recurse_jump_target else {
                                    bail!(Error::new(ErrorKind::InvalidOperation, "cannot recurse outside of recursive loop"));
                                };
                                let depth = loop_object.depth + 1;
                                let iterable = normalized.unwrap_or_else(|| args[0].clone());
                                stack.drop_top(count);
                                if let Some(value) = captured.take() { stack.push(value); }
                                state.call_policy.set(None);
                                drop(state.prepared_call.borrow_mut().take());
                                let value = ctx_ok!(with_deferred(state, DeferredOperation::Loop { iterable: iterable.clone(), target, depth },
                                    |state| resolve(state, &mut |state|
                                    self.execute_deferred_loop(state, out, iterable.clone(), target, depth))));
                                stack.push(value);
                                pc += 1;
                                continue;
                            }
                            if let Some(normalized) = normalized {
                                stack.drop_top(count);
                                if let Some(value) = captured.take() { stack.push(value); }
                                stack.push(normalized);
                                state.call_policy.set(None);
                                drop(state.prepared_call.borrow_mut().take());
                            }
                            recurse_loop!(true, loop_object);
                        } else {
                            ctx_ok!(func.call(state, args))
                        }
                    } else {
                        bail!(Error::new(
                            ErrorKind::UnknownFunction,
                            format!("{name} is unknown"),
                        ));
                    };
                    state.call_policy.set(None);
                    drop(state.prepared_call.borrow_mut().take());
                    let arg_count = args.len();
                    stack.drop_top(arg_count);
                    if let Some(value) = captured { stack.push(value); }
                    stack.push(if awaited { rv } else { ctx_ok!(self.env.runtime_await(state, rv)) });
                }
                Instruction::CallPreparedFunction(name, arg_count) => {
                    let args = stack.get_prepared_function_args(*arg_count);
                    let func = args[0].clone();
                    if func.downcast_object_ref::<MissingPreparedFunction>().is_some() {
                        bail!(Error::new(ErrorKind::UnknownFunction, format!("{name} is unknown")));
                    }
                    if let Some(loop_object) = func.downcast_object_ref::<Loop>() {
                        let count = args.len();
                        let normalized = self.env.runtime_loop_recursion(state, &func, &args[1..], loop_object.recurse_jump_target.is_some());
                        let normalized = if let Some(normalized) = normalized {
                            Some(ctx_ok!(normalized))
                        } else if count != 2 {
                            bail!(Error::new(ErrorKind::InvalidOperation, "loop() takes one argument"));
                        } else { None };
                        if let Some(outcome) = normalized.as_ref().and_then(|value| value.downcast_object_ref::<LoopCallOutcome>()) {
                            let value = outcome.0.clone();
                            stack.drop_top(count);
                            state.call_policy.set(None);
                            drop(state.prepared_call.borrow_mut().take());
                            stack.push(value);
                            pc += 1;
                            continue;
                        }
                        if let Some(resolve) = self.env.runtime_deferred_loop() {
                            let Some(target) = loop_object.recurse_jump_target else {
                                bail!(Error::new(ErrorKind::InvalidOperation, "cannot recurse outside of recursive loop"));
                            };
                            let depth = loop_object.depth + 1;
                            let iterable = normalized.unwrap_or_else(|| args[1].clone());
                            stack.drop_top(count);
                            state.call_policy.set(None);
                            drop(state.prepared_call.borrow_mut().take());
                            let value = ctx_ok!(with_deferred(state, DeferredOperation::Loop { iterable: iterable.clone(), target, depth },
                                |state| resolve(state, &mut |state|
                                self.execute_deferred_loop(state, out, iterable.clone(), target, depth))));
                            stack.push(value);
                            pc += 1;
                            continue;
                        }
                        if let Some(normalized) = normalized {
                            stack.drop_top(count);
                            stack.push(normalized);
                            state.call_policy.set(None);
                            drop(state.prepared_call.borrow_mut().take());
                        }
                        recurse_loop!(true, loop_object);
                    }
                    let rv = ctx_ok!(func.call(state, &args[1..]));
                    state.call_policy.set(None);
                    drop(state.prepared_call.borrow_mut().take());
                    let count = args.len();
                    stack.drop_top(count);
                    stack.push(ctx_ok!(self.env.runtime_await(state, rv)));
                }
                Instruction::RetainPreparedArgument => stack.retain_argument_below_prepared_function(),
                Instruction::BeginPreparedBindings => stack.begin_prepared_bindings(),
                Instruction::EndPreparedBindings => stack.end_prepared_bindings(),
                Instruction::CapturePreparedFactory(name) => {
                    let value = stack.peek().clone();
                    stack.insert_prepared_binding(name, value);
                }
                Instruction::CapturePreparedBinding(name) => {
                    let value = assert_valid!(state.ctx.load_prepared(name).unwrap_or(Value::UNDEFINED));
                    stack.insert_prepared_binding(name, value);
                }
                Instruction::CopyPreparedBinding(name, materialize) => {
                    let value = stack.prepared_binding(name).clone();
                    stack.push(if *materialize { ctx_ok!(self.env.runtime_prepared_use(value)) } else { value });
                }
                Instruction::MaterializePreparedFunction => {
                    let value = stack.pop();
                    stack.push(ctx_ok!(self.env.runtime_prepared_use(value)));
                }
                Instruction::CopyRetainedArgument => {
                    let value = stack.retained_argument().clone();
                    stack.push(ctx_ok!(self.env.runtime_prepared_use(value)));
                }
                Instruction::ReleaseRetainedArgument => stack.release_retained_argument(),
                Instruction::CallMethod(name, arg_count) => {
                    let args = stack.get_call_args(*arg_count);
                    let arg_count = args.len();
                    let called = args[0].call_method(state, name, &args[1..]);
                    state.call_policy.set(None);
                    drop(state.prepared_call.borrow_mut().take());
                    a = ctx_ok!(self.env.runtime_await(state, ctx_ok!(called)));
                    stack.drop_top(arg_count);
                    stack.push(a);
                }
                Instruction::CallObject(arg_count) => {
                    let args = stack.get_call_args(*arg_count);
                    let arg_count = args.len();
                    let called = args[0].call(state, &args[1..]);
                    state.call_policy.set(None);
                    drop(state.prepared_call.borrow_mut().take());
                    a = ctx_ok!(self.env.runtime_await(state, ctx_ok!(called)));
                    stack.drop_top(arg_count);
                    stack.push(a);
                }
                Instruction::PrepareOperator(factory) => {
                    stack.push(ctx_ok!(factory.call(state, &[])));
                }
                Instruction::CallPreparedOperator(operand_count) => {
                    let args = stack.get_call_args(Some(operand_count + 1));
                    a = ctx_ok!(args[0].call(state, &args[1..]));
                    stack.drop_top((*operand_count + 1) as usize);
                    stack.push(a);
                }
                Instruction::DupTop => {
                    stack.push(stack.peek().clone());
                }
                Instruction::DiscardTop => {
                    stack.pop();
                }
                Instruction::FastSuper => {
                    if let Some(resolve) = self.env.runtime_deferred_super() {
                        let value = ctx_ok!(with_deferred(state, DeferredOperation::Super, |state| resolve(state, &mut |state| self.perform_super(state, out, true))));
                        if self.env.is_default_formatter() {
                            if strict_undefined && matches!(value.0, ValueRepr::Undefined(UndefinedType::Default)) {
                                bail!(Error::from(ErrorKind::UndefinedError));
                            }
                            ctx_ok!(write_escaped(out, state.auto_escape.get(), &value));
                        } else {
                            ctx_ok!(self.env.format(&value, state, out));
                        }
                    } else {
                        ctx_ok!(self.perform_super(state, out, false));
                    }
                }
                Instruction::FastRecurse => {
                    if let Some(resolve) = self.env.runtime_deferred_loop() {
                        let Some(loop_state) = state.ctx.current_loop() else {
                            bail!(Error::new(ErrorKind::UnknownFunction, "loop is unknown"));
                        };
                        let Some(target) = loop_state.object.recurse_jump_target else {
                            bail!(Error::new(ErrorKind::InvalidOperation, "cannot recurse outside of recursive loop"));
                        };
                        let depth = loop_state.object.depth + 1;
                        let iterable = stack.pop();
                        let value = ctx_ok!(with_deferred(state, DeferredOperation::Loop { iterable: iterable.clone(), target, depth },
                            |state| resolve(state, &mut |state|
                            self.execute_deferred_loop(state, out, iterable.clone(), target, depth))));
                        if self.env.is_default_formatter() {
                            if strict_undefined && matches!(value.0, ValueRepr::Undefined(UndefinedType::Default)) {
                                bail!(Error::from(ErrorKind::UndefinedError));
                            }
                            ctx_ok!(write_escaped(out, state.auto_escape.get(), &value));
                        } else { ctx_ok!(self.env.format(&value, state, out)); }
                    } else {
                        match state.ctx.current_loop() {
                            Some(l) => recurse_loop!(false, &l.object),
                            None => bail!(Error::new(ErrorKind::UnknownFunction, "loop is unknown")),
                        }
                    }
                },
                // Explanation on the behavior of `LoadBlocks` and rendering of
                // inherited templates:
                //
                // MiniJinja inherits the behavior from Jinja2 where extending
                // loads the blocks (`LoadBlocks`) and the rest of the template
                // keeps executing but with output disabled, only at the end the
                // parent template is then invoked.  This has the effect that
                // you can still set variables or declare macros and that they
                // become visible in the blocks.
                //
                // This behavior has a few downsides.  First of all what happens
                // in the parent template overrides what happens in the child.
                // For instance if you declare a macro named `foo` after `{%
                // extends %}` and then a variable with that named is also set
                // in the parent template, then you won't be able to call that
                // macro in the body.
                //
                // The reason for this is that blocks unlike macros do not have
                // closures in Jinja2/MiniJinja.
                //
                // However for the common case this is convenient because it
                // lets you put some imports there and for as long as you do not
                // create name clashes this works fine.
                #[cfg(feature = "multi_template")]
                Instruction::LoadBlocks => {
                    a = stack.pop();
                    if parent_instructions.is_some() {
                        if let Some(error) = self.env.runtime_host_error(state, "TemplateRuntimeError", "extended multiple times") {
                            bail!(error);
                        }
                        bail!(Error::new(
                            ErrorKind::InvalidOperation,
                            "tried to extend a second time in a template"
                        ));
                    }
                    parent_instructions = Some(ctx_ok!(self.load_blocks(a, state)));
                    out.begin_capture(CaptureMode::Discard);
                }
                #[cfg(feature = "multi_template")]
                Instruction::Include(ignore_missing, with_context, export_locals) => {
                    a = stack.pop();
                    // An import keeps its body's fragments for the module.
                    if *export_locals { out.stash_current_capture_fragments(); }
                    // An import without context reuses the module of its template.
                    out.import_cacheable = *export_locals && !*with_context;
                    out.import_module = None;
                    out.import_template = None;
                    if !with_context && self.env.respect_include_context() {
                        let mut isolated = Context::new_with_frame(self.env, Frame::default());
                        ctx_ok!(isolated.incr_depth(state.ctx.depth()));
                        let previous = mem::replace(&mut state.ctx, isolated);
                        let result = self.perform_include_for_import(a, state, out, *ignore_missing, *export_locals, false);
                        let exports = if *export_locals && result.is_ok() {
                            Some((mem::take(state.ctx.current_locals_mut()), mem::take(state.ctx.current_imports_mut()), state.ctx.current_module_exports_mut().take()))
                        } else { None };
                        state.ctx = previous;
                        if let Some((exports, imported, public)) = exports {
                            state.ctx.current_locals_mut().extend(exports);
                            state.ctx.current_imports_mut().extend(imported);
                            *state.ctx.current_module_exports_mut() = public;
                        }
                        ctx_ok!(result);
                    } else if !export_locals && self.env.respect_include_context() {
                        // Includes can read the caller's bindings but own their
                        // assignments. Imports deliberately retain their module
                        // frame so ExportLocals can collect its public values.
                        ctx_ok!(state.ctx.push_frame(Frame::default()));
                        let result = self.perform_include(a, state, out, *ignore_missing);
                        state.ctx.pop_frame();
                        ctx_ok!(result);
                    } else {
                        ctx_ok!(self.perform_include_for_import(a, state, out, *ignore_missing, *export_locals, *with_context));
                    }
                    let assigned = matches!(state.instructions.get(pc + 1), Some(Instruction::EndCapture));
                    if let Some(module) = out.import_module.clone() {
                        // A reused module answers the names a `from` import reads.
                        if !assigned { out.import_module = None; }
                        *state.ctx.current_module_exports_mut() = Some(module);
                    } else if !assigned {
                        // A `from` import has no module value: cache one now.
                        if let Some(template) = out.import_template.take() {
                            let name = out.take_stashed_name().unwrap_or_default();
                            let module = self.module_from_frame(state, Value::from(""), name, Vec::new(), false);
                            #[cfg(feature = "macros")]
                            self.env.store_module(&template, module, state.id);
                            #[cfg(not(feature = "macros"))]
                            self.env.store_module(&template, module, 0);
                        }
                    }
                }
                #[cfg(feature = "multi_template")]
                Instruction::ExportLocals => {
                    let captured = stack.pop();
                    let fragments = out.take_stashed_fragments().unwrap_or_default();
                    let name = out.take_stashed_name().unwrap_or_default();
                    if let Some(module) = out.import_module.take() {
                        out.import_template = None;
                        drop(state.ctx.current_module_exports_mut().take());
                        stack.push(module);
                        pc += 1;
                        continue;
                    }
                    let cache_for = out.import_template.take();
                    let module = self.module_from_frame(state, captured, name, fragments, true);
                    ctx_ok!(self.env.runtime_module_created(state, &module));
                    if let Some(template) = cache_for {
                        #[cfg(feature = "macros")]
                        self.env.store_module(&template, module.clone(), state.id);
                        #[cfg(not(feature = "macros"))]
                        self.env.store_module(&template, module.clone(), 0);
                    }
                    stack.push(module);
                }
                #[cfg(feature = "multi_template")]
                Instruction::CheckRequiredBlock(name) => {
                    if parent_instructions.is_none() && !out.is_discarding() {
                        let length = state.blocks.get(name).map_or(0, |stack| stack.len());
                        if !ctx_ok!(self.env.runtime_required_block_check(state, name, length)) {
                            bail!(Error::new(ErrorKind::InvalidOperation, format!("Required block '{name}' not found")));
                        }
                    }
                }
                #[cfg(feature = "multi_template")]
                Instruction::CallBlock(name) => {
                    if parent_instructions.is_none() && !out.is_discarding() {
                        // Jinja calls a scoped block with context.derived(locals()).
                        let locals = state.ctx.template_locals();
                        let derived = ctx_ok!(self.env.runtime_context_event(state,
                            crate::RuntimeContextEvent::ScopedBlock(locals)));
                        let previous = (!derived.is_undefined())
                            .then(|| mem::replace(&mut state.runtime_context, derived));
                        let result = self.call_block(name, state, out);
                        if let Some(previous) = previous { state.runtime_context = previous; }
                        ctx_ok!(result);
                    }
                }
                #[cfg(feature = "multi_template")]
                Instruction::CallBlockUnscoped(name) => {
                    if parent_instructions.is_none() && !out.is_discarding() {
                        let suspended = state.ctx.suspend_nested_template_frames(false);
                        let result = self.call_block(name, state, out);
                        state.ctx.restore_nested_template_frames(suspended);
                        ctx_ok!(result);
                    }
                }
                #[cfg(feature = "multi_template")]
                Instruction::CallSelfBlock(name) => {
                    if parent_instructions.is_none() && !out.is_discarding() {
                        if let Some(resolve) = self.env.runtime_deferred_super() {
                            let value = ctx_ok!(with_deferred(state, DeferredOperation::SelfBlock(Arc::from(*name)), |state| resolve(state, &mut |state| self.perform_self_block(state, out, name))));
                            if self.env.is_default_formatter() {
                                if strict_undefined && matches!(value.0, ValueRepr::Undefined(UndefinedType::Default)) {
                                    bail!(Error::from(ErrorKind::UndefinedError));
                                }
                                ctx_ok!(write_escaped(out, state.auto_escape.get(), &value));
                            } else {
                                ctx_ok!(self.env.format(&value, state, out));
                            }
                        } else {
                            let suspended = state.ctx.suspend_nested_template_frames(true);
                            let result = self.call_block(name, state, out);
                            state.ctx.restore_nested_template_frames(suspended);
                            ctx_ok!(result);
                        }
                    }
                }
                #[cfg(feature = "multi_template")]
                Instruction::CallSelfBlockValue(name, policy, arg_count) => {
                    let args = stack.get_call_args(*arg_count).to_vec();
                    let selected = stack.peek_depth(args.len()).clone();
                    if !selected.is_undefined() {
                        let value = ctx_ok!(selected.call(state, &args));
                        let value = ctx_ok!(self.env.runtime_await(state, value));
                        stack.drop_top(args.len() + 1);
                        stack.push(value);
                        pc += 1;
                        continue;
                    }
                    let validation = self.env.runtime_self_block_arguments(state, &args, *policy == Some(true));
                    let prepared = policy.and_then(|_| state.take_prepared_call());
                    if policy.is_some() { state.take_call_policy(); }
                    ctx_ok!(validation);
                    if let Some(sandboxed) = policy {
                        ctx_ok!(self.env.runtime_self_block_guard(state, *sandboxed));
                    }
                    let value = if parent_instructions.is_some() {
                        Value::from("")
                    } else if let Some(resolve) = self.env.runtime_deferred_super() {
                        ctx_ok!(with_deferred(state, DeferredOperation::SelfBlock(Arc::from(*name)), |state| resolve(state, &mut |state| self.perform_self_block(state, out, name))))
                    } else {
                        ctx_ok!(self.perform_self_block(state, out, name))
                    };
                    drop(prepared);
                    stack.drop_top(args.len() + 1);
                    stack.push(value);
                }
                #[cfg(feature = "macros")]
                Instruction::BuildMacro(name, offset, flags) => {
                    ctx_ok!(self.build_macro(&mut stack, state, *offset, name, *flags));
                }
                Instruction::ReturnFilter => break,
                #[cfg(feature = "macros")]
                Instruction::Return => break,
                #[cfg(feature = "macros")]
                Instruction::Enclose(name) => {
                    // the first time we enclose a value, we need to create a closure
                    // and store it on the context, and add it to the closure tracker
                    // for cycle breaking.
                    if state.ctx.closure().is_none() {
                        let closure = Arc::new(Closure::default());
                        state.closure_tracker.track_closure(closure.clone());
                        state.ctx.reset_closure(Some(closure));
                    }
                    state.ctx.enclose(name);
                }
                #[cfg(feature = "macros")]
                Instruction::GetClosure => {
                    stack.push(
                        state
                            .ctx
                            .closure()
                            .map_or(Value::UNDEFINED, |x| Value::from_dyn_object(x.clone())),
                    );
                }
            }
            pc += 1;
        }

        Ok(stack.try_pop())
    }

    fn merge_kwargs(&self, values: Vec<Value>) -> Result<Value, Error> {
        if let Some(result) = self.env.runtime_keyword_sources(values.clone()) {
            return result;
        }
        let mut rv = ValueMap::new();
        for value in values {
            ok!(self.env.undefined_behavior().assert_iterable(&value));
            let iter = ok!(value
                .as_object()
                .filter(|x| x.repr() == ObjectRepr::Map)
                .and_then(|x| x.try_iter_pairs())
                .ok_or_else(|| {
                    Error::new(
                        ErrorKind::InvalidOperation,
                        format!(
                            "attempted to apply keyword arguments from non map (got {})",
                            value.kind()
                        ),
                    )
                }));
            for (key, value) in iter {
                rv.insert(key, value);
            }
        }
        Ok(Kwargs::wrap(rv))
    }

    #[cfg(feature = "multi_template")]
    fn perform_include(
        &self,
        name: Value,
        state: &mut State<'_, 'env>,
        out: &mut Output,
        ignore_missing: bool,
    ) -> Result<(), Error> {
        self.perform_include_for_import(name, state, out, ignore_missing, false, true)
    }

    #[cfg(feature = "multi_template")]
    fn perform_include_for_import(
        &self, name: Value, state: &mut State<'_, 'env>, out: &mut Output,
        ignore_missing: bool, export_locals: bool, with_context: bool,
    ) -> Result<(), Error> {
        // a template object of the host's engine renders there
        let include_foreign = |state: &State, target: &Value, out: &mut Output| -> Result<bool, Error> {
            if export_locals || target.as_object().is_none() { return Ok(false); }
            match ok!(self.env.runtime_include_foreign(state, target, with_context)) {
                Some(output) => {
                    ok!(out.write_str(&output).map_err(|_| Error::new(ErrorKind::WriteFailure, "write failed")));
                    Ok(true)
                }
                None => Ok(false),
            }
        };
        if ok!(include_foreign(state, &name, out)) { return Ok(()); }
        let foreign_name = match name.as_object() {
            Some(object) => ok!(object.template_name()),
            None => None,
        };
        let obj = name.as_object();
        let single_name = name.as_str().is_some() || foreign_name.is_some() || obj.is_none();
        let choices = obj.as_ref().filter(|_| !single_name)
            .and_then(|object| object.try_iter()).into_iter().flatten()
            .chain(single_name.then(|| name.clone()));

        let mut templates_tried = vec![];
        let mut candidates = 0usize;
        for choice in choices {
            candidates += 1;
            if ok!(include_foreign(state, &choice, out)) { return Ok(()); }
            let choice = ok!(choice.validate());
            let resolved = match choice.as_str() {
                Some(text) => Some(text.to_owned()),
                None => match choice.as_object() {
                    Some(object) => ok!(object.template_name()),
                    None => None,
                },
            };
            let name = ok!(resolved.as_deref().ok_or_else(|| Error::new(
                ErrorKind::InvalidOperation, "template name was not a string")));
            let tmpl = match state.get_template_for_execution(name) {
                Ok(tmpl) => tmpl,
                Err(err) => {
                    if err.kind() == ErrorKind::TemplateNotFound {
                        templates_tried.push(choice);
                    } else {
                        return Err(err);
                    }
                    continue;
                }
            };

            if export_locals && std::mem::take(&mut out.import_cacheable) {
                if let crate::template::CompiledTemplateRef::Adapted(loaded) = &tmpl.compiled {
                    if let Some((module, owner)) = self.env.cached_module(loaded) {
                        // Native macros run only in the state that defined them.
                        #[cfg(feature = "macros")]
                        let reusable = owner == state.id || !module_has_macros(&module);
                        #[cfg(not(feature = "macros"))]
                        let reusable = { let _ = owner; true };
                        if reusable {
                            out.import_module = Some(module);
                            return Ok(());
                        }
                    }
                    out.import_template = Some(loaded.clone());
                }
            }
            // SAFETY: the VM stores these references only in this state or
            // its nested macro states, which share the retained-template arena.
            let (new_instructions, new_blocks) = ok!(unsafe { state.retain_template_instructions(&tmpl) });
            if export_locals { out.set_stashed_name(tmpl.name()); }
            ok!(state.ctx.incr_depth(INCLUDE_RECURSION_COST));
            let frame_checkpoint = state.ctx.frame_checkpoint();
            let capture_depth = out.capture_depth();
            let old_escape = state.auto_escape.replace(tmpl.initial_auto_escape());
            // an included template has its own evaluation context
            let old_eval_escape = state.eval_auto_escape.replace(tmpl.initial_auto_escape());
            let old_instructions = mem::replace(&mut state.instructions, new_instructions);
            let old_blocks = mem::replace(&mut state.blocks, prepare_blocks(new_blocks));
            #[cfg(feature = "macros")]
            let old_defining_blocks = mem::replace(&mut state.defining_blocks, Arc::new(()));
            let old_context = state.runtime_context.clone();
            let old_scope = state.ctx.set_scope(crate::RuntimeScope::Root);
            // we need to make a copy of the loaded templates here as we want
            // to forget about the templates that an include triggered by the
            // time the include finishes.
            let old_loaded_templates = state.loaded_templates.clone();
            let mut old_base = None;
            #[cfg(feature = "macros")]
            let mut old_closure = None;
            #[cfg(feature = "macros")]
            let mut closure_taken = false;
            let rv: Result<Option<Value>, Error> = (|| {
                state.runtime_context = self.env.runtime_context_event(state, crate::RuntimeContextEvent::Include)?;
                let namespace = self.env.runtime_context_event(state, crate::RuntimeContextEvent::Namespace)?;
                if !namespace.is_undefined() { old_base = Some(state.ctx.replace_current_base(namespace)); }
                #[cfg(feature = "macros")]
                {
                    old_closure = state.ctx.take_closure();
                    closure_taken = true;
                }
                let result = self.eval_state(state, out, RuntimeRenderFrameKind::Include)?;
                if export_locals {
                    let values = state.ctx.current_locals_mut().iter().map(|(name, value)| ((*name).to_owned(), value.clone())).collect();
                    let exports = self.env.runtime_context_event(state, crate::RuntimeContextEvent::ModuleExports(values))?;
                    *state.ctx.current_module_exports_mut() = (!exports.is_undefined()).then_some(exports);
                }
                Ok(result)
            })();
            if rv.is_err() {
                state.ctx.restore_frame_checkpoint(frame_checkpoint);
                out.discard_captures_to(capture_depth);
            }
            #[cfg(feature = "macros")]
            if closure_taken { state.ctx.reset_closure(old_closure); }
            if let Some((base, bindings)) = old_base { state.ctx.restore_current_base(base, bindings); }
            state.ctx.decr_depth(INCLUDE_RECURSION_COST);
            state.ctx.set_scope(old_scope);
            state.runtime_context = old_context;
            state.loaded_templates = old_loaded_templates;
            state.auto_escape.set(old_escape);
            state.eval_auto_escape.set(old_eval_escape);
            state.instructions = old_instructions;
            state.blocks = old_blocks;
            #[cfg(feature = "macros")]
            { state.defining_blocks = old_defining_blocks; }
            ok!(rv.map_err(|err| {
                Error::new(
                    ErrorKind::BadInclude,
                    format!("error in \"{}\"", tmpl.name()),
                )
                .with_source(err)
            }));
            return Ok(());
        }
        // an empty list of candidates is an error too (Jinja's select_template)
        if (!templates_tried.is_empty() || (!single_name && candidates == 0)) && !ignore_missing {
            if let Some(error) = self.env.runtime_template_not_found(state, &templates_tried, !single_name) {
                return Err(error);
            }
            Err(Error::new(
                ErrorKind::TemplateNotFound,
                if templates_tried.len() == 1 {
                    format!(
                        "tried to include non-existing template {:?}",
                        templates_tried[0]
                    )
                } else {
                    format!(
                        "tried to include one of multiple templates, none of which existed {}",
                        Value::from(templates_tried)
                    )
                },
            ))
        } else {
            Ok(())
        }
    }

    fn perform_super(
        &self,
        state: &mut State<'_, 'env>,
        out: &mut Output,
        capture: bool,
    ) -> Result<Value, Error> {
        let name = ok!(state.current_block.ok_or_else(|| {
            Error::new(ErrorKind::InvalidOperation, "cannot super outside of block")
        }));

        let block_stack = state.blocks.get_mut(name).unwrap();
        if !block_stack.push() {
            return Err(Error::new(
                ErrorKind::InvalidOperation,
                "no parent block exists",
            ));
        }

        let capture_depth = out.capture_depth();
        let frame_checkpoint = state.ctx.frame_checkpoint();
        let auto_escape = state.auto_escape.get();
        let old_instructions = mem::replace(&mut state.instructions, block_stack.instructions());
        if let Err(error) = state.ctx.push_frame(Frame { scope: crate::RuntimeScope::Block, ..Frame::default() }) {
            state.instructions = old_instructions;
            state.blocks.get_mut(name).unwrap().pop();
            return Err(error);
        }
        if capture {
            out.begin_capture(CaptureMode::Capture);
        }
        let depth = state.blocks.get(name).unwrap().depth();
        let rv = match self.env.runtime_block_dispatch(state, name, depth, out) {
            Ok(true) => Ok(None),
            Ok(false) => self.eval_state(state, out, RuntimeRenderFrameKind::Super),
            Err(error) => Err(error),
        };
        if rv.is_err() {
            state.ctx.restore_frame_checkpoint(frame_checkpoint);
            // A failed scope can leave the foreign EvalContext mutated, just
            // as the source runtime does. Restore only VM lexical escaping.
            state.auto_escape.set(auto_escape);
        } else {
            state.ctx.pop_frame();
        }
        state.instructions = old_instructions;
        state.blocks.get_mut(name).unwrap().pop();

        if let Err(err) = rv {
            out.discard_captures_to(capture_depth);
            return Err(Error::new(ErrorKind::EvalBlock, "error in super block").with_source(err));
        }
        if capture {
            out.end_capture(state.auto_escape.get())
        } else {
            Ok(Value::UNDEFINED)
        }
    }

    #[cfg(feature = "multi_template")]
    fn load_blocks(
        &self,
        name: Value,
        state: &mut State<'_, 'env>,
    ) -> Result<&'env Instructions<'env>, Error> {
        let foreign_name = match name.as_object() {
            Some(object) => object.template_name()?,
            None => None,
        };
        let name = name.as_str().or(foreign_name.as_deref()).ok_or_else(|| Error::new(
            ErrorKind::InvalidOperation, "template name was not a string"))?;
        if state.loaded_templates.contains(&name) {
            return Err(Error::new(
                ErrorKind::InvalidOperation,
                format!("cycle in template inheritance. {name:?} was referenced more than once"),
            ));
        }
        let tmpl = ok!(state.get_template_for_execution(name));
        // SAFETY: the returned instructions and block references remain in
        // this state or in macro states sharing its retained-template arena.
        let (new_instructions, new_blocks) = ok!(unsafe { state.retain_template_instructions(&tmpl) });
        state.loaded_templates.insert(new_instructions.name());
        let mut appended = Vec::with_capacity(new_blocks.len());
        for (name, instr) in new_blocks.iter() {
            let stack = state.blocks.entry(name).or_default();
            stack.append_instructions(instr);
            appended.push((*name, stack.len() - 1));
            #[cfg(feature = "macros")]
            for (_, _, _, owner, blocks, _) in Arc::make_mut(&mut state.macros).iter_mut() {
                if Arc::ptr_eq(owner, &state.defining_blocks) {
                    blocks.entry(name).or_default().append_instructions(instr);
                }
            }
        }
        ok!(self.env.runtime_blocks_loaded(state, &appended));
        Ok(new_instructions)
    }

    #[cfg(feature = "multi_template")]
    fn perform_self_block(
        &self,
        state: &mut State<'_, 'env>,
        out: &mut Output,
        name: &str,
    ) -> Result<Value, Error> {
        let capture_depth = out.capture_depth();
        out.begin_capture(CaptureMode::Capture);
        let suspended = state.ctx.suspend_nested_template_frames(true);
        let result = self.call_block(name, state, out);
        state.ctx.restore_nested_template_frames(suspended);
        if let Err(error) = result {
            out.discard_captures_to(capture_depth);
            return Err(error);
        }
        out.end_capture(state.auto_escape.get())
    }

    #[cfg(feature = "multi_template")]
    /// The module of the import whose frame is current: its public locals (or
    /// the host's exports), the body's output and fragments. With `take`, the
    /// host's exports move into the module.
    #[cfg(feature = "multi_template")]
    fn module_from_frame(&self, state: &mut State<'_, 'env>, captured: Value, name: String,
        fragments: Vec<Value>, take: bool) -> Value {
        let exports = if take { state.ctx.current_module_exports_mut().take() }
            else { state.ctx.current_module_exports_mut().clone() };
        let imported = state.ctx.current_imports_mut().clone();
        let locals = state.ctx.current_locals_mut();
        let mut values = value_map_with_capacity(locals.len());
        for (key, value) in locals.iter() {
            if exports.is_some() { continue; }
            if self.env.respect_include_context() && (key.starts_with('_') || imported.contains(key)) { continue; }
            values.insert(Value::from(*key), value.clone());
        }
        Value::from_object(crate::vm::module_object::Module::new(values, captured, exports, name, fragments))
    }

    /// Render the definition at `depth` of block `name` for a host, in a fork
    /// of `state`, without asking the host's block dispatch again.
    #[cfg(feature = "multi_template")]
    pub(crate) fn render_block_for_host(&self, state: &State<'_, 'env>, name: &str, depth: usize) -> Result<Value, Error> {
        let mut fork = state.fork();
        let Some((name, stack)) = fork.blocks.get_key_value(name).map(|(key, stack)| (*key, stack.clone())) else {
            return Err(Error::new(ErrorKind::UnknownBlock, format!("block '{name}' not found")));
        };
        let mut stack = stack;
        if !stack.set_depth(depth) {
            return Err(Error::new(ErrorKind::UnknownBlock, format!("block '{name}' has no definition {depth}")));
        }
        let instructions = stack.instructions();
        fork.blocks.insert(name, stack);
        fork.current_block = Some(name);
        fork.instructions = instructions;
        ok!(fork.ctx.push_frame(Frame { scope: crate::RuntimeScope::Block, ..Frame::default() }));
        let mut rv = String::new();
        let mut out = match fork.env().fragment_joiner() {
            Some(joiner) => Output::with_fragments(&mut rv, joiner),
            None => Output::new(&mut rv),
        };
        out.begin_capture(CaptureMode::Capture);
        ok!(self.eval_state(&mut fork, &mut out, RuntimeRenderFrameKind::Block));
        out.end_capture(fork.auto_escape.get())
    }

    pub(crate) fn call_block(
        &self,
        name: &str,
        state: &mut State<'_, 'env>,
        out: &mut Output,
    ) -> Result<Option<Value>, Error> {
        if self.env.runtime_block_dispatch(state, name, 0, out)? { return Ok(None); }
        if let Some((name, block_stack)) = state.blocks.get_key_value(name) {
            let old_block = state.current_block.replace(name);
            let frame_checkpoint = state.ctx.frame_checkpoint();
            let capture_depth = out.capture_depth();
            let auto_escape = state.auto_escape.get();
            let old_instructions =
                mem::replace(&mut state.instructions, block_stack.instructions());
            let rv = match state.ctx.push_frame(Frame { scope: crate::RuntimeScope::Block, ..Frame::default() }) {
                Ok(()) => {
                    let result = self.eval_state(state, out, RuntimeRenderFrameKind::Block);
                    if result.is_err() {
                        state.ctx.restore_frame_checkpoint(frame_checkpoint);
                        state.auto_escape.set(auto_escape);
                        out.discard_captures_to(capture_depth);
                    } else {
                        state.ctx.pop_frame();
                    }
                    result
                }
                Err(error) => Err(error),
            };
            state.instructions = old_instructions;
            state.current_block = old_block;
            rv
        } else {
            Err(Error::new(
                ErrorKind::UnknownBlock,
                format!("block '{name}' not found"),
            ))
        }
    }

    fn derive_auto_escape(
        &self,
        value: Value,
        initial_auto_escape: AutoEscape,
    ) -> Result<AutoEscape, Error> {
        match (value.as_str(), value == Value::from(true)) {
            (Some("html"), _) => Ok(AutoEscape::Html),
            #[cfg(feature = "json")]
            (Some("json"), _) => Ok(AutoEscape::Json),
            (Some("none"), _) | (None, false) => Ok(AutoEscape::None),
            (None, true) => Ok(if matches!(initial_auto_escape, AutoEscape::None) {
                AutoEscape::Html
            } else {
                initial_auto_escape
            }),
            _ => Err(Error::new(
                ErrorKind::InvalidOperation,
                "invalid value to autoescape tag",
            )),
        }
    }

    /// Render the body of a recursive loop for `iterable` in `state`.
    pub(crate) fn recurse_loop(&self, state: &mut State<'_, 'env>, iterable: Value, target: u32, depth: usize)
        -> Result<Value, Error> {
        let mut rv = String::new();
        let mut out = match state.env().fragment_joiner() {
            Some(joiner) => Output::with_fragments(&mut rv, joiner),
            None => Output::new(&mut rv),
        };
        self.execute_deferred_loop(state, &mut out, iterable, target, depth)
    }

    fn execute_deferred_loop(&self, state: &mut State<'_, 'env>, out: &mut Output,
        iterable: Value, target: u32, depth: usize) -> Result<Value, Error> {
        let frame_checkpoint = state.ctx.frame_checkpoint();
        let capture_depth = out.capture_depth();
        let auto_escape = state.auto_escape.get();
        out.begin_capture(CaptureMode::Capture);
        let result = self.do_eval_with_recursion(state, out, Stack::from(vec![iterable]),
            target, Some((u32::MAX, false, depth)));
        if let Err(error) = result {
            state.ctx.restore_frame_checkpoint(frame_checkpoint);
            state.auto_escape.set(auto_escape);
            out.discard_captures_to(capture_depth);
            return Err(error);
        }
        out.end_capture(state.auto_escape.get())
    }

    fn push_loop(
        &self,
        state: &mut State<'_, 'env>,
        iterable: Value,
        flags: u8,
        pc: u32,
        current_recursion_jump: Option<(u32, bool, usize)>,
        filter_pc: Option<u32>,
        filter_capture: Option<Value>,
        deferred_capture: Option<&'env str>,
    ) -> Result<(), Error> {
        // Jinja's filter generator resolves the predicate's context names once,
        // before it iterates the source; template variables stay live. A host
        // constructor for the loop object runs first, so names then resolve
        // after the start handoff.
        let constructed = filter_pc.is_some() && flags & LOOP_FLAG_WITH_LOOP_VAR != 0
            && ok!(self.env.runtime_loop_constructed(state));
        let mut predicate_bindings = match filter_pc {
            Some(entry) if !constructed => ok!(self.resolve_predicate_names(state, entry, 0)),
            _ => Vec::new(),
        };
        let iter = ok!(state
            .undefined_behavior()
            .assert_iterable(&iterable)
            .and_then(|_| {
                let metadata = flags & LOOP_FLAG_WITH_LOOP_VAR != 0;
                match self.env.runtime_loop_iterator(state, &iterable, metadata, filter_pc.is_some() || flags & LOOP_FLAG_FILTERED_INPUT != 0) {
                    Some(result) => result.map(crate::value::ValueIter::from_loop_iterator),
                    None => iterable.try_iter_for_loop(metadata && filter_pc.is_none()),
                }
            })
            .map_err(|mut err| {
                if let Some((jump_instr, _, _)) = current_recursion_jump {
                    // When a recursion error happens, we need to process the error at both the
                    // recursion call site and the loop definition.  This is because the error
                    // happens at the recursion call site, but the loop definition is where the
                    // recursion is defined.  This helps provide a more helpful error message.
                    // For better reporting we basically turn this around.  jump_pc - 1 is the
                    // location of FastRecurse or CallFunction.
                    process_err(&mut err, pc, state);
                    let mut call_err = Error::new(
                        ErrorKind::InvalidOperation,
                        "cannot recurse because of non-iterable value",
                    );
                    process_err(&mut call_err, jump_instr - 1, state);
                    call_err.with_source(err)
                } else {
                    err
                }
            }));
        // Recursion follows the called loop, which can be an outer loop held
        // through an alias. An ordinary nested loop always begins at depth 0.
        let depth = current_recursion_jump.map_or(0, |(_, _, depth)| depth);
        state.ctx.push_frame(Frame {
            scope: crate::RuntimeScope::Loop,
            current_loop: Some(LoopState::new(
                iter,
                iterable,
                depth,
                flags & LOOP_FLAG_WITH_LOOP_VAR != 0,
                (flags & LOOP_FLAG_RECURSIVE != 0).then_some(pc),
                current_recursion_jump,
                filter_pc,
                filter_capture,
            )),
            ..Frame::default()
        })?;
        if let Some(entry) = filter_pc {
            let controller = state.ctx.current_loop().unwrap().object.clone();
            if constructed {
                ok!(self.env.runtime_loop_handoff(state, &Value::from_dyn_object(controller.clone()), "start"));
                predicate_bindings = ok!(self.resolve_predicate_names(state, entry, 1));
                if let Some(name) = deferred_capture {
                    let value = state.ctx.load_prepared(name).unwrap_or(Value::UNDEFINED);
                    *controller.filter_capture.lock().unwrap() = Some(ok!(value.validate()));
                }
            }
            if !predicate_bindings.is_empty() {
                *controller.predicate_bindings.lock().unwrap() = predicate_bindings;
            }
            if !constructed {
                ok!(self.env.runtime_loop_handoff(state, &Value::from_dyn_object(controller), "start"));
            }
        } else if flags & LOOP_FLAG_WITH_LOOP_VAR != 0 {
            // A loop object exists from the start: its host may build it now.
            let controller = state.ctx.current_loop().unwrap().object.clone();
            ok!(self.env.runtime_loop_handoff(state, &Value::from_dyn_object(controller), "start"));
        }
        Ok(())
    }

    /// The context names a filter predicate looks up, in order of first use.
    fn predicate_names(&self, state: &State<'_, 'env>, entry: u32) -> Vec<&'env str> {
        let mut stored = Vec::new();
        let mut names = Vec::new();
        let mut pc = entry;
        while let Some(instruction) = state.instructions.get(pc) {
            match instruction {
                Instruction::ReturnFilter => break,
                Instruction::StoreLocal(name) => stored.push(*name),
                Instruction::Lookup(name) if !stored.contains(name) && !names.contains(name) => names.push(*name),
                _ => {}
            }
            pc += 1;
        }
        names
    }

    /// The predicate's context names, from the base of the frame `skip` frames
    /// below the topmost (the loop's own frame, once pushed, has none).
    fn resolve_predicate_names(&self, state: &State<'_, 'env>, entry: u32, skip: usize) -> Result<Vec<(String, Value)>, Error> {
        let mut bindings = Vec::new();
        let base = state.ctx.base_below(skip);
        for name in self.predicate_names(state, entry) {
            if state.ctx.binds_locally(name) { continue; }
            let value = match state.ctx.resolve_base_prepared(base, name) {
                Some(value) => value,
                None => ok!(self.env.runtime_root_missing(base, name)),
            };
            bindings.push((name.to_owned(), value));
        }
        Ok(bindings)
    }

    fn prepare_filtered_length(&self, state: &mut State<'_, 'env>, controller: &Arc<Loop>) -> Result<(), Error> {
        if controller.delegate().is_some() { return Ok(()); }
        if self.env.has_runtime_filtered_loop_length_with_input() {
            if controller.filtered_has_length() { return Ok(()); }
            let loop_value = Value::from_dyn_object(controller.clone());
            let mut next = || {
                self.fill_filtered_for_input(state, controller, false, true)?;
                controller.take_filtered_input_from_host()
            };
            if let Some(length) = self.env.runtime_filtered_loop_length_with_input(&loop_value, &mut next)? {
                controller.filtered_supply_length(length);
                return Ok(());
            }
        }
        if self.env.has_runtime_filtered_loop_length() {
            if controller.filtered_has_length() { return Ok(()); }
            let loop_value = Value::from_dyn_object(controller.clone());
            if let Some(length) = self.env.runtime_filtered_loop_length(state, &loop_value)? {
                controller.filtered_supply_length(length);
                return Ok(());
            }
        }
        self.fill_filtered(state, controller, true)
    }

    fn fill_filtered(&self, state: &mut State<'_, 'env>, controller: &Loop, all: bool) -> Result<(), Error> {
        // A loop handed to a host evaluates predicates when the host pulls.
        if controller.delegate().is_some() { return Ok(()); }
        self.fill_filtered_for_input(state, controller, all, false)
    }

    /// Accept the next item of a filtered loop for its host delegate.
    pub(crate) fn fill_filtered_from_host(&self, state: &mut State<'_, 'env>, controller: &Loop) -> Result<(), Error> {
        self.fill_filtered_for_input(state, controller, false, false)
    }

    fn fill_filtered_for_input(&self, state: &mut State<'_, 'env>, controller: &Loop, all: bool,
        input_only: bool) -> Result<(), Error> {
        let Some(entry) = controller.filter_pc else { return Ok(()); };
        let _filtered = state.enter_filtered_loop();
        while all || if input_only { controller.filtered_input_buffered()? == 0 }
            else { controller.filtered_buffered()? == 0 } {
            let candidate = if self.env.has_runtime_loop_next() {
                self.env.runtime_loop_next(state.render_frames(), state.filtered_loop_depth(),
                    &mut || controller.filtered_next_raw())
            } else { controller.filtered_next_raw() };
            let candidate = match candidate {
                Ok(candidate) => candidate,
                Err(error) => { controller.filtered_close_after_error()?; return Err(error); }
            };
            let Some(candidate) = candidate else { break; };
            let bound = controller.predicate_bindings.lock().unwrap().clone();
            let frame = if bound.is_empty() { Frame::default() } else {
                let names = self.predicate_names(state, entry);
                Context::frame_with_root_bindings(bound.into_iter()
                    .filter_map(|(name, value)| names.iter().find(|candidate| **candidate == name).map(|name| (*name, value)))
                    .collect())
            };
            state.ctx.push_frame(frame)?;
            let previous = std::mem::replace(&mut state.callback_context_limit, Some(1));
            let result = self.do_eval(state, &mut Output::null(), Stack::from(vec![candidate.clone()]), entry);
            state.callback_context_limit = previous;
            state.ctx.pop_frame();
            let accepted = result.and_then(|value| value.ok_or_else(|| Error::new(ErrorKind::InvalidOperation,
                "filter returned no value"))).and_then(|value| state.undefined_behavior().is_true(&value));
            match accepted {
                Ok(true) => controller.filtered_push(candidate)?,
                Ok(false) => {},
                Err(error) => { controller.filtered_close_after_error()?; return Err(error); }
            }
        }
        if all { controller.filtered_set_length()?; }
        Ok(())
    }

    fn unpack_list(&self, stack: &mut Stack, count: usize) -> Result<(), Error> {
        let top = stack.pop();
        if let Some(result) = self.env.runtime_unpack(&top, count) {
            use crate::environment::RuntimeUnpack;
            match result {
                Ok(RuntimeUnpack::Values(values)) if values.len() == count => {
                    for value in values { stack.push(value.validate()?); }
                    stack.reverse_top(count);
                    return Ok(());
                }
                Ok(RuntimeUnpack::Values(_)) => return Err(Error::new(ErrorKind::InvalidOperation,
                    "foreign unpack handler returned the wrong number of values")),
                Ok(RuntimeUnpack::TooFew(actual)) if actual < count => return Err(self.env.unpack_mismatch(count, Some(actual))),
                Ok(RuntimeUnpack::TooFew(_)) => return Err(Error::new(ErrorKind::InvalidOperation,
                    "foreign unpack handler reported an invalid short count")),
                Ok(RuntimeUnpack::TooMany) => return Err(self.env.unpack_mismatch(count, None)),
                Err(error) => return Err(self.env.unpack_iterator_error(&top, error)),
            }
        }
        let mut iter = match top.as_object().and_then(|x| x.try_iter()) {
            Some(iter) => iter,
            None => return Err(self.env.unpack_iterator_error(&top,
                Error::new(ErrorKind::CannotUnpack, "value is not iterable"))),
        };

        for actual in 0..count {
            match iter.next() {
                Some(item) => match item.validate() {
                    Ok(value) => stack.push(value),
                    Err(error) if actual == 0 => return Err(self.env.unpack_iterator_error(&top, error)),
                    Err(error) => return Err(error),
                },
                None => return Err(self.env.unpack_mismatch(count, Some(actual))),
            }
        }
        if let Some(item) = iter.next() {
            if let Err(error) = item.validate() {
                return Err(if count == 0 { self.env.unpack_iterator_error(&top, error) } else { error });
            }
            return Err(self.env.unpack_mismatch(count, None));
        }
        stack.reverse_top(count);
        Ok(())
    }

    #[cfg(feature = "macros")]
    fn build_macro(
        &self,
        stack: &mut Stack,
        state: &mut State,
        offset: u32,
        name: &str,
        flags: u8,
    ) -> Result<(), Error> {
        use crate::{compiler::instructions::{MACRO_CALLER, MACRO_VARARGS, MACRO_KWARGS, MACRO_CALL_BLOCK}, vm::macro_object::Macro};

        let arg_spec: Vec<Value> = stack.pop().try_iter().unwrap().collect();
        let argument_names = state.env().runtime_tuple(arg_spec.clone())?;
        let closure = stack.pop();
        let macro_ref_id = state.macros.len();
        let reference = Arc::new(());
        // A macro belongs to this template's block chain, but its own block
        // lookup starts at the most-derived implementation even if the macro
        // was defined while an enclosing super() call was active.
        let blocks = state.blocks.iter().map(|(&name, stack)| (name, stack.from_start())).collect();
        Arc::make_mut(&mut state.macros).push((state.instructions, offset, reference.clone(), state.defining_blocks.clone(), blocks,
            state.auto_escape.get()));
        let implementation = Arc::new(Macro {
            name: if (flags & MACRO_CALL_BLOCK) != 0 { Value::from(()) } else { Value::from(name) },
            arg_spec,
            argument_names,
            macro_ref_id,
            reference,
            state_id: state.id,
            closure,
            runtime_context: state.runtime_context.clone(),
            caller_reference: (flags & MACRO_CALLER) != 0,
            capture_varargs: (flags & MACRO_VARARGS) != 0,
            capture_kwargs: (flags & MACRO_KWARGS) != 0,
            foreign: std::sync::OnceLock::new(),
        });
        let value = Value::from_dyn_object(implementation.clone());
        if let Some(foreign) = self.env.runtime_macro_value(state, &value) {
            let _ = implementation.foreign.set(foreign?);
            state.closure_tracker.track_macro(implementation.clone());
        }
        stack.push(value);
        Ok(())
    }
}

#[inline(never)]
#[cold]
fn process_err(err: &mut Error, pc: u32, state: &State) {
    // only attach line information if the error does not have line info yet.
    if err.line().is_none() {
        if let Some(span) = state.instructions.get_span(pc) {
            err.set_filename_and_span(state.instructions.name(), span);
        } else if let Some(lineno) = state.instructions.get_line(pc) {
            err.set_filename_and_line(state.instructions.name(), lineno);
        }
    }
    // only attach debug info if we don't have one yet and we are in debug mode.
    #[cfg(feature = "debug")]
    {
        if state.env().debug() && err.debug_info().is_none() {
            err.attach_debug_info(state.make_debug_info(pc, state.instructions));
        }
    }
}

#[cfg(test)]
mod output_preparation_tests {
    use crate::{Environment, Error, Value};
    use std::sync::{Arc, atomic::{AtomicUsize, Ordering}};

    #[test]
    fn captures_before_evaluating_the_expression() {
        let counter = Arc::new(AtomicUsize::new(0));
        let mut environment = Environment::new();
        let prepare = counter.clone();
        environment.set_output_preparer(move |_| Ok(Value::from(prepare.load(Ordering::Relaxed))));
        let change = counter.clone();
        environment.add_function("change", move || { change.store(1, Ordering::Relaxed); "new" });
        environment.set_prepared_formatter(|output, _, value, prepared|
            output.write_str(&format!("{prepared}:{value}")).map_err(Error::from));
        let result = environment.template_from_str("{{ change() }}").unwrap().render(()).unwrap();
        assert_eq!(result, "0:new");
        assert_eq!(counter.load(Ordering::Relaxed), 1);
    }
}

#[cfg(all(test, feature = "multi_template"))]
mod self_reference_tests {
    use crate::{Environment, Error, ErrorKind, RuntimeContextEvent, Value};
    use std::sync::{Arc, Mutex};

    #[test]
    fn resolves_overridden_self_before_arguments_and_keeps_native_fallback() {
        for fail in [false, true] {
            let events = Arc::new(Mutex::new(Vec::new()));
            let mut env = Environment::new();
            env.set_runtime_root_missing(|_, _| Ok(Value::UNDEFINED));
            let observed = events.clone();
            env.set_runtime_context_handler(move |_, event| {
                if matches!(event, RuntimeContextEvent::SelfReference) {
                    observed.lock().unwrap().push("self");
                    Ok(Value::from("reference"))
                } else { Ok(Value::UNDEFINED) }
            });
            let observed = events.clone();
            env.add_function("argument", move || {
                observed.lock().unwrap().push("argument");
                7i64
            });
            let observed = events.clone();
            env.set_runtime_self_block_prepare(move |state, name| {
                assert_eq!(name, "body");
                assert_eq!(state.lookup("self").unwrap().as_str(), Some("reference"));
                observed.lock().unwrap().push("lookup");
                if fail { return Err(Error::new(ErrorKind::InvalidOperation, "lookup failed")); }
                let called = observed.clone();
                Ok(Some(Value::from_function(move |value: i64| {
                    called.lock().unwrap().push("call");
                    format!("custom:{value}")
                })))
            });
            let result = env.render_str("{{ self.body(argument()) }}", ());
            if fail {
                assert!(result.is_err());
                assert_eq!(*events.lock().unwrap(), ["self", "lookup"]);
            } else {
                assert_eq!(result.unwrap(), "custom:7");
                assert_eq!(*events.lock().unwrap(), ["self", "lookup", "argument", "call"]);
            }
        }
        let env = Environment::new();
        assert_eq!(env.render_str("{% if false %}{% block body %}BODY{% endblock %}{% endif %}{{self.body()}}", ()).unwrap(), "BODY");
    }
}

#[cfg(all(test, feature = "multi_template"))]
mod import_export_tests {
    use crate::{Environment, RuntimeContextEvent, Value};

    #[test]
    fn imports_without_context_reuse_the_module_while_the_template_is_valid() {
        use std::sync::{Arc, atomic::{AtomicBool, AtomicUsize, Ordering}};
        let loads = Arc::new(AtomicUsize::new(0));
        let ticks = Arc::new(AtomicUsize::new(0));
        let valid = Arc::new(AtomicBool::new(true));
        let mut env = Environment::new();
        let counted = loads.clone();
        env.set_loader(move |_| { counted.fetch_add(1, Ordering::Relaxed); Ok(Some("{% set n = tick() %}".to_owned())) });
        let ticked = ticks.clone();
        env.add_function("tick", move || ticked.fetch_add(1, Ordering::Relaxed) + 1);
        let current = valid.clone();
        env.set_loaded_template_validator(move |_| Ok(current.load(Ordering::Relaxed)));
        env.enable_shared_loaded_cache();
        let source = "{% import 'lib' as a %}{% import 'lib' as b %}{% from 'lib' import n %}{{ a.n }}|{{ b.n }}|{{ n }}";
        let compiled = env.compile_owned_template("case".into(), source.into()).unwrap();
        assert_eq!(env.template_from_owned(&compiled).render(()).unwrap(), "1|1|1");
        assert_eq!((loads.load(Ordering::Relaxed), ticks.load(Ordering::Relaxed)), (1, 1));
        // An invalid entry is loaded again and gets a new module.
        valid.store(false, Ordering::Relaxed);
        assert_eq!(env.template_from_owned(&compiled).render(()).unwrap(), "2|3|4");
        assert_eq!(loads.load(Ordering::Relaxed), 4);
    }

    #[test]
    fn scoped_block_offers_its_visible_variables_to_the_host() {
        use std::sync::{Arc, Mutex};
        let seen = Arc::new(Mutex::new(Vec::new()));
        let mut env = Environment::new();
        let observed = seen.clone();
        env.set_runtime_context_handler(move |_, event| {
            if let RuntimeContextEvent::ScopedBlock(values) = event {
                observed.lock().unwrap().push(values.into_iter().map(|(name, _)| name).collect::<Vec<_>>());
            }
            Ok(Value::UNDEFINED)
        });
        let source = "{% set y = 3 %}{% for x in [1] %}{% block a scoped %}{{ x }}{% endblock %}{% block b %}{% endblock %}{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap().render(()).unwrap(), "1");
        assert_eq!(*seen.lock().unwrap(), [vec!["loop".to_owned(), "x".to_owned(), "y".to_owned()]]);
    }

    #[test]
    fn blocks_render_for_a_host_inside_and_outside_the_render() {
        use std::sync::{Arc, Mutex};
        let loaded = Arc::new(Mutex::new(Vec::new()));
        let inside = Arc::new(Mutex::new(Vec::new()));
        let mut env = Environment::new();
        env.add_template("base", "{{ probe() }}{% block body %}BASE {{ who }}{% endblock %}").unwrap();
        let seen = loaded.clone();
        env.set_runtime_blocks_loaded(move |_, blocks| {
            seen.lock().unwrap().extend(blocks.iter().map(|(name, depth)| ((*name).to_owned(), *depth)));
            Ok(())
        });
        let rendered = inside.clone();
        env.add_function("probe", move |state: &crate::State| -> Result<String, crate::Error> {
            let child = crate::with_host_state(state, || crate::render_block_in_host_state("body", 0))?;
            let parent = crate::with_host_state(state, || crate::render_block_in_host_state("body", 1))?;
            rendered.lock().unwrap().push(format!("{child}|{parent}"));
            Ok(String::new())
        });
        let source = "{% extends 'base' %}{% block body %}CHILD {{ who }}{% endblock %}";
        let template = env.template_from_str(source).unwrap();
        assert_eq!(template.render(crate::context!(who => "x")).unwrap(), "CHILD x");
        assert_eq!(*loaded.lock().unwrap(), [("body".to_owned(), 1)]);
        assert_eq!(*inside.lock().unwrap(), ["CHILD x|BASE x"]);
        // Outside a render: a new state of the template with the given root.
        let standalone = env.template_from_str("{% block greet %}hi {{ who }}{% endblock %}").unwrap();
        assert_eq!(standalone.render_block_with_root("greet", 0, crate::context!(who => "y")).unwrap().to_string(), "hi y");
        assert!(standalone.render_block_with_root("missing", 0, crate::context!()).is_err());
    }

    #[test]
    fn imported_module_keeps_name_body_fragments_and_a_host_object() {
        use std::sync::{Arc, Mutex};
        let seen = Arc::new(Mutex::new(Vec::new()));
        let mut env = Environment::new();
        env.add_template("library", "A{{ value }}B{% set first = 1 %}").unwrap();
        let observed = seen.clone();
        env.set_runtime_module_created(move |_, module| {
            let module = module.downcast_object::<crate::RuntimeModule>().unwrap();
            observed.lock().unwrap().push((module.template_name().to_owned(), module.captured().to_string(),
                module.exported_items().len()));
            // A host object takes over attribute reads.
            module.set_foreign_value(Value::from_iter([("first", Value::from("host"))]));
            Ok(())
        });
        let output = env.template_from_str("{% import 'library' as m %}{{ m.first }}|{{ m }}").unwrap()
            .render(crate::context!(value => "X")).unwrap();
        assert_eq!(output, "host|AXB");
        assert_eq!(*seen.lock().unwrap(), [("library".to_owned(), "AXB".to_owned(), 1)]);
    }

    #[test]
    fn completed_exports_are_authoritative_and_retain_native_macros() {
        for context in ["with context", "without context"] {
            let mut env = Environment::new();
            env.set_respect_include_context(true);
            env.set_runtime_context_handler(|_, event| Ok(match event {
                RuntimeContextEvent::ModuleExports(values) => {
                    let body = values.into_iter().find(|(name, _)| name == "body").unwrap().1;
                    Value::from_iter([("_hidden", Value::from("explicit")), ("renamed", body)])
                }
                _ => Value::UNDEFINED,
            }));
            env.add_template("module", "{% set public='omitted' %}{% macro body() %}M{% endmacro %}").unwrap();
            let source = format!("{{% import 'module' as m {context} %}}{{{{m._hidden}}}}|{{{{m.renamed()}}}}|{{{{m.public|default('absent')}}}}");
            assert_eq!(env.render_str(&source, ()).unwrap(), "explicit|M|absent");
        }
    }

    #[test]
    fn private_and_imported_bindings_are_excluded_only_in_compatibility_mode() {
        let cases = [
            ("{% set _hidden='private' %}", "{{ m._hidden is defined }}", "False", "True"),
            ("{% macro _hidden() %}private{% endmacro %}", "{{ m._hidden is defined }}", "False", "True"),
            ("{% import 'dependency' as item %}", "{{ m.item is defined }}", "False", "True"),
            ("{% from 'dependency' import item %}", "{{ m.item is defined }}", "False", "True"),
            ("{% set item='first' %}{% import 'dependency' as item %}", "{{ m.item is defined }}", "False", "True"),
            ("{% import 'dependency' as item %}{% set item='last' %}", "{{ m.item }}", "last", "last"),
        ];
        for (module, expression, compatible, default) in cases {
            for mode in [false, true] {
                let mut env = Environment::new();
                env.set_respect_include_context(mode);
                env.set_runtime_context_handler(|_, _| Ok(Value::UNDEFINED));
                env.add_template("dependency", "{% set item='dependency' %}").unwrap();
                env.add_template("module", module).unwrap();
                let source = format!("{{% import 'module' as m %}}{expression}");
                assert_eq!(env.render_str(&source, ()).unwrap(), if mode { compatible } else { default }, "{module}, compatibility={mode}");
            }
        }
    }
}

#[cfg(test)]
mod internal_counter_tests {
    use crate::{Environment,Error,ErrorKind};

    #[test]
    fn filtered_loop_counter_does_not_call_foreign_arithmetic() {
        let mut env=Environment::new();
        env.set_runtime_operator(|_,_,_,_| Err(Error::new(ErrorKind::InvalidOperation,"foreign arithmetic called")));
        let output=env.template_from_str("{% for v in [1,2] if true %}{{ v }}{% endfor %}").unwrap().render(()).unwrap();
        assert_eq!(output,"12");
    }
}

#[cfg(all(test, feature = "loop_controls"))]
mod loop_else_policy_tests {
    use crate::Environment;

    #[test]
    fn interrupted_iterations_preserve_jinja_else_semantics() {
        for control in ["break", "continue"] {
            let source = format!("{{% for v in [1] %}}{{{{ v }}}}{{% {control} %}}{{% else %}}E{{% endfor %}}");
            let mut env = Environment::new();
            assert_eq!(env.template_from_str(&source).unwrap().render(()).unwrap(), "1");
            env.set_loop_else_on_completed_iteration(true);
            assert_eq!(env.template_from_str(&source).unwrap().render(()).unwrap(), "1E");
        }
        let mut env = Environment::new();
        env.set_loop_else_on_completed_iteration(true);
        assert_eq!(env.template_from_str("{% for v in [1,2] %}{{ v }}{% if v == 2 %}{% break %}{% endif %}{% else %}E{% endfor %}").unwrap().render(()).unwrap(), "12");
    }
}

#[cfg(all(test, feature = "multi_template"))]
mod include_scope_tests {
    use crate::{context, Environment, Error, Value};
    use crate::value::{Enumerator, Object, ObjectRepr};
    use std::sync::Arc;

    #[derive(Debug)]
    struct ForeignName(&'static str);
    impl Object for ForeignName {
        fn repr(self: &Arc<Self>) -> ObjectRepr { ObjectRepr::Iterable }
        fn enumerate(self: &Arc<Self>) -> Enumerator {
            Enumerator::Values(self.0.chars().map(|ch| Value::from(ch.to_string())).collect())
        }
        fn template_name(self: &Arc<Self>) -> Result<Option<String>, Error> {
            Ok(Some(self.0.to_owned()))
        }
    }

    #[test]
    fn without_context_is_opt_in_and_restores_caller_scope() {
        let mut env = Environment::new();
        env.add_template("part", "{{ value|default('U') }}").unwrap();
        let source = "{% include 'part' without context %}|{{ value }}";
        assert_eq!(env.template_from_str(source).unwrap().render(context!(value => "X")).unwrap(), "X|X");
        env.set_respect_include_context(true);
        assert_eq!(env.template_from_str(source).unwrap().render(context!(value => "X")).unwrap(), "U|X");
        env.add_global("value", "G");
        assert_eq!(env.template_from_str(source).unwrap().render(context!(value => "X")).unwrap(), "G|X");
    }

    #[test]
    fn foreign_template_names_are_not_iterated_as_characters() {
        let mut env = Environment::new();
        env.add_template("part", "X").unwrap();
        let name = Value::from_object(ForeignName("part"));
        let candidates = Value::from(vec![Value::from_object(ForeignName("absent")), name.clone()]);
        for value in [name, candidates] {
            assert_eq!(env.template_from_str("{% include selected %}").unwrap().render(context!(selected => value)).unwrap(), "X");
        }
    }

    #[test]
    fn include_assignments_are_local_but_imports_export_their_values() {
        let mut env = Environment::new();
        env.add_template("part", "{% set value='inner' %}{{ value }}").unwrap();
        let source = "{% include 'part' %}|{{ value }}";
        // Preserve MiniJinja's existing policy unless compatibility is enabled.
        assert_eq!(env.template_from_str(source).unwrap().render(context!(value => "outer")).unwrap(), "inner|inner");
        env.set_respect_include_context(true);
        for (source, expected) in [
            (source, "inner|outer"),
            ("{% for value in [1,2] %}{% include 'part' %}|{{ value }};{% endfor %}|{{ value }}", "inner|1;inner|2;|outer"),
            ("{% import 'part' as m %}{{ m.value }}|{{ value }}", "inner|outer"),
            ("{% from 'part' import value as imported %}{{ imported }}|{{ value }}", "inner|outer"),
        ] {
            assert_eq!(env.template_from_str(source).unwrap().render(context!(value => "outer")).unwrap(), expected);
        }
    }
}

// Jinja semantics the language partition probes pin; the expected
// values are Jinja2 3.1.6's output for the same templates.
#[cfg(all(test, feature = "multi_template", feature = "macros"))]
mod jinja_partition_tests {
    use crate::{context, Environment, Error, ErrorKind, Value};
    use std::sync::{Arc, Mutex};

    fn render(env: &Environment, source: &str, ctx: Value) -> Result<String, Error> {
        env.template_from_str(source)?.render(ctx)
    }

    #[test]
    fn with_values_are_evaluated_in_the_enclosing_scope() {
        let env = Environment::new();
        let source = "{% set a = 'outer' %}{% with a = 'inner', b = a %}{{ a }}/{{ b }}{% endwith %}{{ a }}";
        assert_eq!(render(&env, source, context!()).unwrap(), "inner/outerouter");
    }

    #[test]
    fn call_block_macro_is_anonymous_and_if_without_else_keeps_its_line() {
        let env = Environment::new();
        let source = "{% macro m() %}[{{ caller.name }}]{% endmacro %}{% call m() %}x{% endcall %}";
        assert_eq!(render(&env, source, context!()).unwrap(), "[None]");
        let mut env = Environment::new();
        env.add_function("f", |value: Value| value.silent_undefined_line().map(i64::from).unwrap_or(-1));
        assert_eq!(render(&env, "\n{{ f('x' if flag) }}", context!(flag => false)).unwrap(), "\n2");
    }

    #[test]
    fn macro_result_safety_follows_the_callers_evaluation_context() {
        let mut env = Environment::new();
        env.set_auto_escape_callback(|_| crate::AutoEscape::Html);
        // the body's text is lexical; nested results are escaped again when
        // the caller's context no longer autoescapes (Jinja: 2'1&#39;0&amp;#39;)
        let source = "{% macro m(n) %}{{ n }}'{% if n > 0 %}{{ m(n - 1) }}{% endif %}{% endmacro %}\
                      {{ m(2) }}|{% autoescape false %}{{ m(2) }}{% endautoescape %}";
        assert_eq!(render(&env, source, context!()).unwrap(), "2'1'0'|2'1&#x27;0&amp;#x27;");
    }

    #[test]
    fn empty_candidate_list_and_missing_templates_reach_the_host() {
        let mut env = Environment::new();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let recorded = seen.clone();
        env.set_runtime_template_not_found(move |_, tried, select| {
            recorded.lock().unwrap().push((tried.len(), select));
            Error::new(ErrorKind::TemplateNotFound, "host")
        });
        assert!(render(&env, "{% include [] %}", context!()).is_err());
        assert!(render(&env, "{% include 'absent' %}", context!()).is_err());
        assert_eq!(render(&env, "{% include [] ignore missing %}ok", context!()).unwrap(), "ok");
        assert_eq!(*seen.lock().unwrap(), vec![(0, true), (1, false)]);
    }

    #[test]
    fn host_errors_and_foreign_includes_go_through_the_host() {
        let mut env = Environment::new();
        env.add_template("base", "B").unwrap();
        env.set_runtime_host_error(|_, class, message| Error::new(ErrorKind::InvalidOperation, format!("{class}: {message}")));
        let error = render(&env, "{% extends 'base' %}{% extends 'base' %}", context!()).unwrap_err();
        assert_eq!(error.detail(), Some("TemplateRuntimeError: extended multiple times"));
        env.set_runtime_include_foreign(|_, target, with_context| {
            Ok(target.as_object().map(|_| format!("foreign:{with_context}")))
        });
        let target = Value::from_object(vec![Value::from("x")]);
        assert_eq!(render(&env, "{% include t %}|{% include t without context %}", context!(t => target)).unwrap(),
                   "foreign:true|foreign:false");
    }

    #[test]
    fn recursive_filtered_loops_run_predicates_between_bodies() {
        let env = Environment::new();
        let trace = Arc::new(Mutex::new(Vec::new()));
        let mut env = env;
        let predicate = trace.clone();
        env.add_function("p", move |value: i64| { predicate.lock().unwrap().push(format!("p{value}")); value % 2 == 1 });
        let body = trace.clone();
        env.add_function("mark", move |value: i64| { body.lock().unwrap().push(format!("b{value}")); String::new() });
        let source = "{% for x in items if p(x) recursive %}{{ x }}{{ mark(x) }}{% endfor %}";
        assert_eq!(render(&env, source, context!(items => vec![1, 2, 3])).unwrap(), "13");
        assert_eq!(*trace.lock().unwrap(), ["p1", "b1", "p2", "p3", "b3"]);
    }
}
