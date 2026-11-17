use std::ptr::addr_of_mut;
use std::sync::Arc;
use std::{fmt, io};

use crate::error::{Error, ErrorKind};
use crate::utils::AutoEscape;
use crate::value::Value;

/// How should output be captured?
#[derive(Debug, Clone, Copy, Eq, PartialEq)]
#[cfg_attr(feature = "unstable_machinery_serde", derive(serde::Serialize))]
pub enum CaptureMode {
    Capture,
    #[allow(unused)]
    Discard,
}

/// An abstraction over [`fmt::Write`](std::fmt::Write) for the rendering.
///
/// This is a utility type used in the engine which can be written into like one
/// can write into an [`std::fmt::Write`] value.  It's primarily used internally
/// in the engine but it's also passed to the custom formatter function.
pub struct Output<'a> {
    w: *mut (dyn fmt::Write + 'a),
    target: *mut (dyn fmt::Write + 'a),
    capture_stack: Vec<Option<FragmentCapture>>,
    root_fragment_capture: bool,
    join_fragments: Option<&'a FragmentJoiner>,
    capture_wrapper: Option<Arc<CaptureWrapper>>,
    // Keep the fragments of the capture at this depth when it ends.
    fragments_at_depth: Option<usize>,
    stashed_fragments: Option<Vec<Value>>,
    stashed_name: Option<String>,
    // An import without context: the module to reuse, or the template whose
    // module the import creates.
    pub(crate) import_module: Option<crate::value::Value>,
    pub(crate) import_cacheable: bool,
    pub(crate) import_template: Option<Arc<crate::loader::LoadedTemplate>>,
}

/// Combines formatted output fragments at a capture boundary.
pub type FragmentJoiner = dyn Fn(Vec<Value>, AutoEscape) -> Result<Value, Error> + Send + Sync;
pub(crate) type CaptureWrapper = dyn Fn(Value) -> Result<Value, Error> + Send + Sync;

#[derive(Default)]
struct FragmentCapture {
    text: String,
    fragments: Vec<Value>,
    constant_fragments: Vec<Value>,
    has_constant: bool,
    untrusted_control: bool,
    restricted_control: bool,
}

impl FragmentCapture {
    fn push_constant(&mut self, value: Value) {
        if !self.text.is_empty() {
            self.constant_fragments.push(Value::from(std::mem::take(&mut self.text)));
        }
        self.constant_fragments.push(value);
        self.has_constant = true;
    }

    fn finish_group(&mut self, joiner: &FragmentJoiner) -> Result<(), Error> {
        if self.constant_fragments.is_empty() {
            if self.has_constant || !self.text.is_empty() {
                self.fragments.push(Value::from(std::mem::take(&mut self.text)));
            }
        } else {
            if !self.text.is_empty() {
                self.constant_fragments.push(Value::from(std::mem::take(&mut self.text)));
            }
            let values = std::mem::take(&mut self.constant_fragments);
            self.fragments.push(joiner(values, AutoEscape::None)?);
        }
        self.has_constant = false;
        Ok(())
    }
}

impl<'a> Output<'a> {
    /// Creates a new output.
    pub(crate) fn new(w: &'a mut (dyn fmt::Write + 'a)) -> Self {
        Self {
            w,
            target: w,
            capture_stack: Vec::new(),
            root_fragment_capture: false,
            join_fragments: None,
            capture_wrapper: None,
            fragments_at_depth: None,
            stashed_fragments: None,
            stashed_name: None,
            import_module: None,
            import_cacheable: false,
            import_template: None,
        }
    }

    /// Creates an output that may preserve values across capture boundaries.
    pub(crate) fn with_fragments(w: &'a mut (dyn fmt::Write + 'a), joiner: &'a FragmentJoiner) -> Self {
        Self { w, target: w, capture_stack: Vec::new(), root_fragment_capture: false,
            join_fragments: Some(joiner), capture_wrapper: None,
            fragments_at_depth: None, stashed_fragments: None, stashed_name: None,
            import_module: None, import_cacheable: false, import_template: None }
    }

    /// Creates a null output that writes nowhere.
    pub(crate) fn null() -> Self {
        // The null writer also has a single entry on the discarding capture
        // stack.  In fact, `w` is more or less useless here as we always
        // shadow it.  This is done so that `is_discarding` returns true.
        Self {
            w: NullWriter::get_mut(),
            target: NullWriter::get_mut(),
            capture_stack: vec![None],
            root_fragment_capture: false,
            join_fragments: None,
            capture_wrapper: None,
            fragments_at_depth: None,
            stashed_fragments: None,
            stashed_name: None,
            import_module: None,
            import_cacheable: false,
            import_template: None,
        }
    }

    /// Begins capturing into a string or discard.
    pub(crate) fn begin_capture(&mut self, mode: CaptureMode) {
        self.capture_stack.push(match mode {
            CaptureMode::Capture => Some(FragmentCapture::default()),
            CaptureMode::Discard => None,
        });
        self.retarget();
    }

    pub(crate) fn capture_depth(&self) -> usize { self.capture_stack.len() }

    pub(crate) fn discard_captures_to(&mut self, depth: usize) {
        self.capture_stack.truncate(depth);
        self.retarget();
    }

    /// Retain root fragments without treating the root as an intermediate capture.
    pub(crate) fn begin_root_fragments(&mut self) {
        debug_assert!(self.capture_stack.is_empty());
        self.begin_capture(CaptureMode::Capture);
        self.root_fragment_capture = true;
    }

    /// Ends capturing and returns the captured string as value.
    /// Keep the fragments of the current capture, as a list, when it ends.
    pub(crate) fn stash_current_capture_fragments(&mut self) {
        self.fragments_at_depth = Some(self.capture_stack.len());
    }

    /// Remember the name of the template an import is executing.
    pub(crate) fn set_stashed_name(&mut self, name: &str) { self.stashed_name = Some(name.to_owned()); }

    pub(crate) fn take_stashed_name(&mut self) -> Option<String> { self.stashed_name.take() }

    /// The fragments kept by [`Output::stash_current_capture_fragments`].
    pub(crate) fn take_stashed_fragments(&mut self) -> Option<Vec<Value>> {
        self.stashed_fragments.take()
    }

    pub(crate) fn end_capture(&mut self, auto_escape: AutoEscape) -> Result<Value, Error> {
        let stash = self.fragments_at_depth == Some(self.capture_stack.len());
        if stash { self.fragments_at_depth = None; }
        let (rv, untrusted_control) = if let Some(mut captured) = self.capture_stack.pop().unwrap() {
            // The old target pointed into the popped capture.
            self.retarget();
            if let Some(joiner) = self.join_fragments {
                captured.finish_group(joiner)?;
            }
            if stash {
                self.stashed_fragments = Some(if captured.fragments.is_empty() {
                    if captured.text.is_empty() { Vec::new() }
                    else if !matches!(auto_escape, AutoEscape::None) { vec![Value::from_safe_string(captured.text.clone())] }
                    else { vec![Value::from(captured.text.clone())] }
                } else { captured.fragments.clone() });
            }
            let rv = if captured.fragments.is_empty() {
                if !matches!(auto_escape, AutoEscape::None) {
                    Value::from_safe_string(captured.text)
                } else {
                    Value::from(captured.text)
                }
            } else {
                let joiner = self.join_fragments.ok_or_else(|| Error::new(
                    ErrorKind::InvalidOperation, "output fragment joiner unavailable"));
                joiner?(captured.fragments, auto_escape)?
            };
            (rv, captured.untrusted_control)
        } else {
            (Value::UNDEFINED, false)
        };
        self.retarget();
        if untrusted_control {
            if let Some(wrapper) = &self.capture_wrapper { return wrapper(rv); }
        }
        Ok(rv)
    }

    /// Finish the outer fragment capture without flattening its values.
    /// Nested captures still use the configured joiner and capture wrapper.
    pub(crate) fn end_root_fragments(&mut self) -> Result<Vec<Value>, Error> {
        if self.capture_stack.len() != 1 {
            return Err(Error::new(ErrorKind::InvalidOperation, "expected one root fragment capture"));
        }
        let joiner = self.join_fragments.ok_or_else(|| Error::new(
            ErrorKind::InvalidOperation, "output fragment capture is disabled"))?;
        let mut captured = self.capture_stack.pop().unwrap().ok_or_else(|| Error::new(
            ErrorKind::InvalidOperation, "root fragment capture is discarding"))?;
        self.retarget();
        self.root_fragment_capture = false;
        captured.finish_group(joiner)?;
        Ok(captured.fragments)
    }

    pub(crate) fn set_capture_wrapper(&mut self, wrapper: Option<Arc<CaptureWrapper>>) {
        self.capture_wrapper = wrapper;
    }

    /// Whether output is being retained in an intermediate capture.
    pub fn is_capturing(&self) -> bool {
        matches!(self.capture_stack.last(), Some(Some(_)))
            && (!self.root_fragment_capture || self.capture_stack.len() > 1)
    }

    /// Whether the current target retains the unjoined outer fragment stream.
    pub fn is_unjoined_fragment_root(&self) -> bool {
        self.root_fragment_capture && self.capture_stack.len() == 1
            && matches!(self.capture_stack.last(), Some(Some(_)))
    }

    /// Whether a nested capture emitted restricted control text in this render.
    pub fn root_has_restricted_capture(&self) -> bool {
        self.root_fragment_capture
            && matches!(self.capture_stack.first(), Some(Some(capture)) if capture.restricted_control)
    }

    /// Mark the active capture for host wrapping when it is finished.
    pub fn mark_untrusted_control(&mut self) {
        if let Some(Some(capture)) = self.capture_stack.last_mut() {
            capture.untrusted_control = true;
        }
    }

    /// Preserve the guard for a nested capture containing restricted content.
    pub fn mark_restricted_control(&mut self) {
        if self.root_fragment_capture && self.capture_stack.len() > 1
            && matches!(self.capture_stack.last(), Some(Some(_))) {
            if let Some(Some(root)) = self.capture_stack.first_mut() {
                root.restricted_control = true;
            }
        }
        self.mark_untrusted_control();
    }

    fn retarget(&mut self) {
        self.target = match self.capture_stack.last_mut() {
            Some(Some(stream)) => &mut stream.text,
            Some(None) => NullWriter::get_mut(),
            None => self.w,
        };
    }

    #[inline(always)]
    fn target(&mut self) -> &mut dyn fmt::Write {
        // SAFETY: this is safe because we carefully maintain the capture stack
        // to update self.target whenever it's modified
        unsafe { &mut *self.target }
    }

    /// Returns `true` if the output is discarding.
    #[inline(always)]
    #[allow(unused)]
    pub(crate) fn is_discarding(&self) -> bool {
        matches!(self.capture_stack.last(), Some(None))
    }

    /// Writes some data to the underlying buffer contained within this output.
    #[inline]
    pub fn write_str(&mut self, s: &str) -> fmt::Result {
        self.target().write_str(s)
    }

    /// Append an already formatted foreign value without converting it to UTF-8.
    ///
    /// The value is joined at the current capture boundary. It is discarded
    /// inside a discard capture and rejected when fragment capture is disabled.
    pub fn write_fragment(&mut self, value: Value) -> Result<(), Error> {
        if self.is_discarding() { return Ok(()); }
        if self.join_fragments.is_none() {
            return Err(Error::new(ErrorKind::InvalidOperation, "output fragment capture is disabled"));
        }
        self.finish_group()?;
        match self.capture_stack.last_mut() {
            Some(Some(capture)) => { capture.fragments.push(value); Ok(()) }
            _ => Err(Error::new(ErrorKind::InvalidOperation, "output fragment requires a capture")),
        }
    }

    /// Append compile-time output to the current constant group.
    pub(crate) fn write_constant_fragment(&mut self, value: Value) -> Result<(), Error> {
        if self.is_discarding() { return Ok(()); }
        if self.join_fragments.is_none() {
            return Err(Error::new(ErrorKind::InvalidOperation, "output fragment capture is disabled"));
        }
        match self.capture_stack.last_mut() {
            Some(Some(capture)) => { capture.push_constant(value); Ok(()) }
            _ => Err(Error::new(ErrorKind::InvalidOperation, "output fragment requires a capture")),
        }
    }

    /// Mark output from a compile-time constant, including the empty string.
    pub(crate) fn mark_constant(&mut self) {
        if self.join_fragments.is_some() {
            if let Some(Some(capture)) = self.capture_stack.last_mut() {
                capture.has_constant = true;
            }
        }
    }

    /// Finish an adjacent constant group without joining the whole capture.
    pub(crate) fn finish_group(&mut self) -> Result<(), Error> {
        if let (Some(joiner), Some(Some(capture))) = (self.join_fragments, self.capture_stack.last_mut()) {
            capture.finish_group(joiner)?;
        }
        Ok(())
    }

    /// Whether this output can retain foreign fragments at its current target.
    pub fn supports_fragments(&self) -> bool {
        self.join_fragments.is_some() && !self.capture_stack.is_empty()
    }

    /// Writes some formatted information into this instance.
    #[inline]
    pub fn write_fmt(&mut self, a: fmt::Arguments<'_>) -> fmt::Result {
        self.target().write_fmt(a)
    }
}

impl fmt::Write for Output<'_> {
    #[inline]
    fn write_str(&mut self, s: &str) -> fmt::Result {
        fmt::Write::write_str(self.target(), s)
    }

    #[inline]
    fn write_char(&mut self, c: char) -> fmt::Result {
        fmt::Write::write_char(self.target(), c)
    }

    #[inline]
    fn write_fmt(&mut self, args: fmt::Arguments<'_>) -> fmt::Result {
        fmt::Write::write_fmt(self.target(), args)
    }
}

pub struct NullWriter;

impl NullWriter {
    /// Returns a reference to the null writer.
    pub fn get_mut() -> &'static mut NullWriter {
        static mut NULL_WRITER: NullWriter = NullWriter;
        // SAFETY: this is safe as the null writer is a ZST
        unsafe { &mut *addr_of_mut!(NULL_WRITER) }
    }
}

impl fmt::Write for NullWriter {
    #[inline]
    fn write_str(&mut self, _s: &str) -> fmt::Result {
        Ok(())
    }

    #[inline]
    fn write_char(&mut self, _c: char) -> fmt::Result {
        Ok(())
    }
}

pub struct WriteWrapper<W> {
    pub w: W,
    pub err: Option<io::Error>,
}

impl<W> WriteWrapper<W> {
    /// Replaces the given error with the held error if available.
    pub fn take_err(&mut self, original: Error) -> Error {
        self.err
            .take()
            .map(|io_err| {
                Error::new(ErrorKind::WriteFailure, "I/O error during rendering")
                    .with_source(io_err)
            })
            .unwrap_or(original)
    }
}

impl<W: io::Write> fmt::Write for WriteWrapper<W> {
    #[inline]
    fn write_str(&mut self, s: &str) -> fmt::Result {
        self.w.write_all(s.as_bytes()).map_err(|e| {
            self.err = Some(e);
            fmt::Error
        })
    }

    #[inline]
    fn write_char(&mut self, c: char) -> fmt::Result {
        self.w
            .write_all(c.encode_utf8(&mut [0; 4]).as_bytes())
            .map_err(|e| {
                self.err = Some(e);
                fmt::Error
            })
    }
}

#[cfg(test)]
mod fragment_tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    #[test]
    fn unjoined_root_is_not_an_intermediate_capture() {
        let observed = Arc::new(Mutex::new(Vec::new()));
        let calls = observed.clone();
        let mut environment = crate::Environment::new();
        environment.set_formatter(move |output, _state, value| {
            calls.lock().unwrap().push((output.is_capturing(), output.is_unjoined_fragment_root()));
            output.write_str(&value.to_string()).map_err(crate::Error::from)
        });
        let template = environment.template_from_str("{{ a }}{% set x %}{{ b }}{% endset %}{{ x }}").unwrap();
        let joiner = |values: Vec<Value>, _: AutoEscape| {
            Ok(Value::from(values.iter().map(ToString::to_string).collect::<String>()))
        };
        let fragments = template.render_unjoined_fragments(crate::context!(a => "a", b => "b"), &joiner).unwrap();
        assert_eq!(fragments.iter().map(ToString::to_string).collect::<String>(), "ab");
        assert_eq!(*observed.lock().unwrap(), [(false, true), (true, false), (false, true)]);
    }

    #[test]
    fn root_distinguishes_untrusted_and_restricted_capture() {
        let joiner = |values: Vec<Value>, _: AutoEscape| {
            Ok(Value::from(values.iter().map(ToString::to_string).collect::<String>()))
        };
        let mut sink = String::new();
        let mut output = Output::with_fragments(&mut sink, &joiner);
        output.begin_root_fragments();
        assert!(output.is_unjoined_fragment_root());
        assert!(!output.root_has_restricted_capture());
        output.begin_capture(CaptureMode::Capture);
        assert!(!output.is_unjoined_fragment_root());
        output.mark_untrusted_control();
        assert!(!output.root_has_restricted_capture());
        output.mark_restricted_control();
        output.end_capture(AutoEscape::None).unwrap();
        assert!(output.is_unjoined_fragment_root());
        assert!(output.root_has_restricted_capture());
    }

    #[test]
    fn unjoined_root_still_runs_constant_guard() {
        let observed = Arc::new(Mutex::new(Vec::new()));
        let calls = observed.clone();
        let mut environment = crate::Environment::new();
        environment.set_runtime_constant_output_guard(move |value| {
            calls.lock().unwrap().push(value.to_string());
            Ok(())
        });
        let template = environment.template_from_str("\u{1}root{% set x %}\u{1}inner{% endset %}").unwrap();
        let joiner = |values: Vec<Value>, _: AutoEscape| {
            Ok(Value::from(values.iter().map(ToString::to_string).collect::<String>()))
        };
        let fragments = template.render_unjoined_fragments((), &joiner).unwrap();
        assert_eq!(fragments.iter().map(ToString::to_string).collect::<String>(), "\u{1}root");
        assert_eq!(*observed.lock().unwrap(), ["\u{1}root"]);
    }

    #[test]
    fn wraps_only_marked_captures_and_keeps_nested_boundaries() {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let observed = calls.clone();
        let mut sink = String::new();
        let mut output = Output::new(&mut sink);
        output.set_capture_wrapper(Some(Arc::new(move |value| {
            observed.lock().unwrap().push(value.to_string());
            Ok(Value::from(format!("wrapped:{value}")))
        })));
        output.begin_capture(CaptureMode::Capture);
        output.write_str("outer").unwrap();
        output.begin_capture(CaptureMode::Capture);
        output.write_str("inner").unwrap();
        output.mark_untrusted_control();
        let inner = output.end_capture(AutoEscape::None).unwrap();
        assert_eq!(inner.as_str(), Some("wrapped:inner"));
        let outer = output.end_capture(AutoEscape::None).unwrap();
        assert_eq!(outer.as_str(), Some("outer"));
        assert_eq!(*calls.lock().unwrap(), ["inner"]);
    }

    #[test]
    fn preserves_order_and_nested_capture_boundaries() {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let seen = calls.clone();
        let joiner = move |values: Vec<Value>, escape: AutoEscape| -> Result<Value, Error> {
            seen.lock().unwrap().push((values.len(), matches!(escape, AutoEscape::Html)));
            Ok(Value::from(values.into_iter().map(|value| value.to_string()).collect::<String>()))
        };
        let mut target = String::new();
        let mut output = Output::with_fragments(&mut target, &joiner);
        output.begin_capture(CaptureMode::Capture);
        output.write_str("A").unwrap();
        output.write_fragment(Value::from(1)).unwrap();
        output.begin_capture(CaptureMode::Capture);
        output.write_str("B").unwrap();
        output.write_fragment(Value::from(2)).unwrap();
        output.write_str("C").unwrap();
        let inner = output.end_capture(AutoEscape::Html).unwrap();
        output.write_fragment(inner).unwrap();
        output.write_str("D").unwrap();
        let result = output.end_capture(AutoEscape::None).unwrap();
        assert_eq!(result.as_str(), Some("A1B2CD"));
        assert_eq!(*calls.lock().unwrap(), [(3, true), (4, false)]);
        assert_eq!(target, "");
    }

    #[test]
    fn plain_discard_and_failure_preserve_capture_state() {
        let calls = Arc::new(Mutex::new(0));
        let seen = calls.clone();
        let joiner = move |values: Vec<Value>, _: AutoEscape| -> Result<Value, Error> {
            *seen.lock().unwrap() += 1;
            if values.iter().any(|value| value.as_i64() == Some(2)) {
                Err(Error::new(ErrorKind::InvalidOperation, "join failed"))
            } else {
                Ok(Value::from(values.into_iter().map(|value| value.to_string()).collect::<String>()))
            }
        };
        let mut target = String::new();
        let mut output = Output::with_fragments(&mut target, &joiner);
        output.begin_capture(CaptureMode::Capture);
        output.write_str("before").unwrap();
        output.begin_capture(CaptureMode::Discard);
        output.write_fragment(Value::from(1)).unwrap();
        output.end_capture(AutoEscape::None).unwrap();
        output.begin_capture(CaptureMode::Capture);
        output.write_fragment(Value::from(2)).unwrap();
        assert!(output.end_capture(AutoEscape::None).is_err());
        output.write_str("after").unwrap();
        assert_eq!(output.end_capture(AutoEscape::None).unwrap().as_str(), Some("beforeafter"));
        assert_eq!(*calls.lock().unwrap(), 2);
        assert!(output.write_fragment(Value::from(3)).is_err());
    }

    #[test]
    fn template_opt_in_delays_join_until_capture_finishes() {
        let mut env = crate::Environment::new();
        env.set_formatter(|output, _state, value| {
            if value.kind() == crate::value::ValueKind::Number {
                output.write_fragment(value.clone())
            } else {
                output.write_str(&value.to_string()).map_err(Error::from)
            }
        });
        let calls = Arc::new(Mutex::new(0));
        let seen = calls.clone();
        let joiner = move |values: Vec<Value>, _: AutoEscape| -> Result<Value, Error> {
            *seen.lock().unwrap() += 1;
            Ok(Value::from(values.into_iter().map(|value| value.to_string()).collect::<String>()))
        };
        let template = env.template_from_str("A{% set x %}B{{ 7 }}C{% endset %}{{ x }}D{{ 8 }}").unwrap();
        let result = template.render_fragments((), &joiner).unwrap();
        assert_eq!(result.as_str(), Some("AB7CD8"));
        assert_eq!(*calls.lock().unwrap(), 2);
    }

    #[test]
    fn root_fragments_keep_values_distinct_after_nested_capture() {
        let mut env = crate::Environment::new();
        env.set_formatter(|output, _state, value| {
            if value.kind() == crate::value::ValueKind::Number {
                output.write_fragment(value.clone())
            } else {
                output.write_str(&value.to_string()).map_err(Error::from)
            }
        });
        let calls = Arc::new(Mutex::new(0));
        let seen = calls.clone();
        let joiner = move |values: Vec<Value>, _: AutoEscape| -> Result<Value, Error> {
            *seen.lock().unwrap() += 1;
            Ok(Value::from(values.into_iter().map(|value| value.to_string()).collect::<String>()))
        };
        let template = env.template_from_str("A{{ first }}{% set inner %}B{{ second }}C{% endset %}{{ inner }}D{{ third }}").unwrap();
        let fragments = template.render_unjoined_fragments(crate::context!(first => 7, second => 8, third => 9), &joiner).unwrap();
        assert_eq!(fragments.len(), 4);
        assert_eq!(fragments[0].as_str(), Some("A"));
        assert_eq!(fragments[1].as_i64(), Some(7));
        assert_eq!(fragments[2].as_str(), Some("B8CD"));
        assert_eq!(fragments[3].as_i64(), Some(9));
        assert_eq!(*calls.lock().unwrap(), 1);
    }

    #[test]
    fn empty_root_fragment_capture_is_empty() {
        let env = crate::Environment::new();
        let joiner = |_: Vec<Value>, _: AutoEscape| -> Result<Value, Error> {
            panic!("empty root capture must not be joined")
        };
        let template = env.template_from_str("").unwrap();
        assert!(template.render_unjoined_fragments((), &joiner).unwrap().is_empty());
    }

    #[test]
    fn root_fragments_distinguish_equal_display_text_and_typed_value() {
        #[derive(Debug)]
        struct Tagged;
        impl std::fmt::Display for Tagged {
            fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                output.write_str("\u{1}DTPLD0\u{1}")
            }
        }
        impl crate::value::Object for Tagged {
            fn render(self: &Arc<Self>, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                std::fmt::Display::fmt(self.as_ref(), output)
            }
        }

        let mut env = crate::Environment::new();
        env.set_formatter(|output, _state, value| {
            if value.downcast_object_ref::<Tagged>().is_some() {
                output.write_fragment(value.clone())
            } else {
                output.write_str(&value.to_string()).map_err(Error::from)
            }
        });
        let joiner = |values: Vec<Value>, _: AutoEscape| -> Result<Value, Error> {
            Ok(Value::from(values.into_iter().map(|value| value.to_string()).collect::<String>()))
        };
        let template = env.template_from_str("{{ text }}{{ tagged }}").unwrap();
        let fragments = template.render_unjoined_fragments(crate::context!(
            text => "\u{1}DTPLD0\u{1}", tagged => Value::from_object(Tagged)
        ), &joiner).unwrap();
        assert_eq!(fragments.len(), 2);
        assert_eq!(fragments[0].as_str(), Some("\u{1}DTPLD0\u{1}"));
        assert!(fragments[1].downcast_object_ref::<Tagged>().is_some());
        assert_eq!(fragments[0].to_string(), fragments[1].to_string());
    }

    #[test]
    fn safe_string_provenance_survives_string_filter_and_output() {
        let mut env = crate::Environment::new();
        env.set_auto_escape_callback(|_| AutoEscape::Html);
        env.set_formatter(|output, state, value| {
            if value.string_provenance().is_some() {
                output.write_fragment(value.clone())
            } else {
                crate::escape_formatter(output, state, value)
            }
        });
        let joiner = |values: Vec<Value>, _: AutoEscape| -> Result<Value, Error> {
            Ok(Value::from(values.into_iter().map(|value| value.to_string()).collect::<String>()))
        };
        let original = Value::from_safe_string_with_provenance("<tag>".into(), 17);
        assert_eq!(original.kind(), crate::value::ValueKind::String);
        assert!(original.is_safe());
        assert_eq!(original.clone().string_provenance(), Some(17));
        assert_eq!(Value::from_safe_string_with_provenance(String::new(), u32::MAX).string_provenance(), Some(u32::MAX));
        assert_eq!(Value::from_safe_string("<tag>".into()).string_provenance(), None);
        let template = env.template_from_str("{{ fake }}{{ image|string }}").unwrap();
        let fragments = template.render_unjoined_fragments(crate::context!(
            fake => "<tag>", image => original
        ), &joiner).unwrap();
        assert_eq!(fragments.len(), 2);
        assert_eq!(fragments[0].as_str(), Some("&lt;tag&gt;"));
        assert_eq!(fragments[1].as_str(), Some("<tag>"));
        assert_eq!(fragments[1].string_provenance(), Some(17));
    }

    #[cfg(feature = "multi_template")]
    #[test]
    fn root_fragments_cross_include_without_flattening() {
        let mut env = crate::Environment::new();
        env.set_formatter(|output, _state, value| {
            if value.kind() == crate::value::ValueKind::Number {
                output.write_fragment(value.clone())
            } else {
                output.write_str(&value.to_string()).map_err(Error::from)
            }
        });
        env.add_template("part", "B{{ inner }}C").unwrap();
        let joiner = |values: Vec<Value>, _: AutoEscape| -> Result<Value, Error> {
            Ok(Value::from(values.into_iter().map(|value| value.to_string()).collect::<String>()))
        };
        let template = env.template_from_str("A{% include 'part' %}D{{ outer }}").unwrap();
        let fragments = template.render_unjoined_fragments(crate::context!(inner => 7, outer => 8), &joiner).unwrap();
        assert_eq!(fragments.len(), 4);
        assert_eq!(fragments[0].as_str(), Some("AB"));
        assert_eq!(fragments[1].as_i64(), Some(7));
        assert_eq!(fragments[2].as_str(), Some("CD"));
        assert_eq!(fragments[3].as_i64(), Some(8));
    }
}
