//! Jinja2 builtins for the browser engine.
//!
//! The Python binding executes Jinja's own filters on Python objects; the
//! browser engine has no Python, so this module reproduces Jinja2 3.1 and
//! CPython semantics over MiniJinja values: output conversion (`str`/`repr`),
//! the filters whose signature or behavior differs from MiniJinja's, Python
//! string/dict methods and printf-style `%` formatting. Each item names the
//! Jinja2 or CPython behavior it follows; `scripts/wasm_jinja_conformance.py`
//! compares them with Jinja2 itself.

use std::cmp::Ordering;
use std::fmt::Write as _;
use std::sync::Arc;

use minijinja::value::{Enumerator, Kwargs, Object, ObjectRepr, Rest, Value, ValueKind};
use minijinja::{AutoEscape, Environment, Error, ErrorKind, State};

pub(crate) fn register(env: &mut Environment<'_>) {
    env.set_formatter(|output, state, value| {
        // `RichText.__html__`: its XML, never escaped.
        if let Some(xml) = crate::value::rich_xml(value) { return output.write_str(xml).map_err(Error::from); }
        let text = if value.is_safe() { value.to_string() } else { py_str(value) };
        let text = if state.auto_escape() != AutoEscape::None && !value.is_safe() { html_escape(&text) } else { text };
        output.write_str(&text).map_err(Error::from)
    });
    env.set_string_format_operator(|format, arguments| {
        // A tuple literal is a sequence in this VM, so sequences split like
        // Python tuples; mappings feed `%(name)s` conversions.
        let arguments = match arguments.kind() {
            ValueKind::Seq | ValueKind::Iterable => FormatArgs::Positional(arguments.try_iter()?.collect()),
            ValueKind::Map => FormatArgs::Mapping(arguments.clone()),
            _ => FormatArgs::Positional(vec![arguments.clone()]),
        };
        Ok(Value::from(printf(&format.to_string(), &arguments)?))
    });
    env.set_unknown_method_callback(|_state, value, method, args| {
        match python_method(value, method, args)? {
            Some(result) => Ok(result),
            None => docxtplrs::template::py_like_method(value, method, args),
        }
    });
    // `escaped`: Jinja2 tests for `__html__`, which safe strings and RichText have.
    env.add_test("escaped", |value: Value| value.is_safe() || crate::value::rich_xml(&value).is_some());
    env.add_filter("attr", attr);
    env.add_filter("dictsort", dictsort);
    env.add_filter("e", escape);
    env.add_filter("escape", escape);
    env.add_filter("float", float);
    env.add_filter("format", format);
    env.add_filter("indent", indent);
    env.add_filter("int", int);
    env.add_filter("items", items);
    env.add_filter("join", join);
    env.add_filter("max", |state: &State, value: Value, args: Rest<Value>, kwargs: Kwargs| extreme(state, value, args, kwargs, Ordering::Greater));
    env.add_filter("min", |state: &State, value: Value, args: Rest<Value>, kwargs: Kwargs| extreme(state, value, args, kwargs, Ordering::Less));
    env.add_filter("pprint", |value: Value| Value::from(pformat(&value, 0, 80)));
    env.add_filter("replace", replace);
    env.add_filter("round", round);
    env.add_filter("sum", sum);
    env.add_filter("title", |value: Value| title(&py_str(&value)));
    env.add_filter("tojson", tojson);
    env.add_filter("truncate", truncate);
    env.add_filter("urlize", urlize);
    env.add_filter("wordwrap", wordwrap);
    // Tuple literals, macro `varargs` and keyword merging, as in Python.
    env.set_runtime_container_factory(|container| Ok(match container {
        minijinja::RuntimeContainer::List(items) => Value::from(items),
        minijinja::RuntimeContainer::Tuple(items) => tuple(items),
        minijinja::RuntimeContainer::Map(pairs) => pairs.into_iter().collect(),
        minijinja::RuntimeContainer::KeywordSources(sources) => {
            let mut merged: Vec<(String, Value)> = Vec::new();
            for source in sources {
                if source.kind() != ValueKind::Map {
                    return Err(type_error(format!("attempted to apply keyword arguments from non map (got {})", source.kind())));
                }
                for key in source.try_iter()? {
                    let item = source.get_item(&key)?;
                    let key = key.to_string();
                    if merged.iter().any(|(existing, _)| existing == &key) {
                        return Err(Error::new(ErrorKind::TooManyArguments, format!("got multiple values for keyword argument '{key}'")));
                    }
                    merged.push((key, item));
                }
            }
            Value::from(merged.into_iter().collect::<Kwargs>())
        }
    }));
    // Jinja2's `cycler(*items)`; the compat crate's version takes one list.
    env.add_function("cycler", |items: Rest<Value>| Value::from_object(docxtplrs::template::Cycler::new(items.0)));
}

// ----------------------------------------------------------------- tuples

/// A Python tuple: a sequence whose `repr` uses parentheses.
#[derive(Debug, Clone)]
pub(crate) struct Tuple(pub(crate) Vec<Value>);

impl Object for Tuple {
    fn repr(self: &Arc<Self>) -> ObjectRepr { ObjectRepr::Seq }
    fn get_value(self: &Arc<Self>, key: &Value) -> Option<Value> {
        let index = key.as_i64()?;
        let index = if index < 0 { self.0.len() as i64 + index } else { index };
        self.0.get(usize::try_from(index).ok()?).cloned()
    }
    fn enumerate(self: &Arc<Self>) -> Enumerator { Enumerator::Seq(self.0.len()) }
}

fn tuple(items: Vec<Value>) -> Value { Value::from_object(Tuple(items)) }

// ------------------------------------------------------------ str / repr

/// CPython `float.__repr__`: shortest round-trip digits, scientific notation
/// outside `1e-4 <= |x| < 1e16`.
pub(crate) fn py_float(value: f64) -> String {
    if value.is_nan() { return "nan".into(); }
    if value.is_infinite() { return if value < 0.0 { "-inf".into() } else { "inf".into() }; }
    if value == 0.0 { return if value.is_sign_negative() { "-0.0".into() } else { "0.0".into() }; }
    let scientific = format!("{value:e}");
    let (mantissa, exponent) = scientific.split_once('e').expect("exponent");
    let exponent: i32 = exponent.parse().expect("exponent digits");
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..16).contains(&exponent) {
        let point = exponent + 1;
        let body = if point <= 0 {
            format!("0.{}{}", "0".repeat((-point) as usize), digits)
        } else if point as usize >= digits.len() {
            format!("{}{}.0", digits, "0".repeat(point as usize - digits.len()))
        } else {
            format!("{}.{}", &digits[..point as usize], &digits[point as usize..])
        };
        format!("{sign}{body}")
    } else {
        let mantissa = if digits.len() > 1 { format!("{}.{}", &digits[..1], &digits[1..]) } else { digits };
        format!("{sign}{mantissa}e{}{:02}", if exponent < 0 { '-' } else { '+' }, exponent.abs())
    }
}

/// CPython `str.__repr__`.
pub(crate) fn py_repr_str(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => { out.push('\\'); out.push(c); }
            c if (c as u32) < 0x20 || (0x7f..=0xa0).contains(&(c as u32)) || c == '\u{ad}' => {
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c if matches!(c as u32, 0x2028 | 0x2029 | 0xfeff) => { let _ = write!(out, "\\u{:04x}", c as u32); }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn number_str(value: &Value) -> Option<String> {
    if value.kind() != ValueKind::Number { return None; }
    if value.is_integer() { return Some(value.to_string()); }
    f64::try_from(value.clone()).ok().map(py_float)
}

/// Python `str(value)` for template data.
pub(crate) fn py_str(value: &Value) -> String {
    match value.kind() {
        ValueKind::Undefined => String::new(),
        ValueKind::String => value.to_string(),
        ValueKind::Number => number_str(value).unwrap_or_default(),
        ValueKind::Seq | ValueKind::Map | ValueKind::Iterable => py_repr(value),
        _ => value.to_string(),
    }
}

/// Python `repr(value)` for template data.
pub(crate) fn py_repr(value: &Value) -> String {
    match value.kind() {
        ValueKind::Undefined => String::new(),
        ValueKind::String => py_repr_str(value.as_str().unwrap_or_default()),
        ValueKind::Number => number_str(value).unwrap_or_default(),
        ValueKind::Seq | ValueKind::Iterable => {
            let items: Vec<String> = value.try_iter().map(|iter| iter.map(|item| py_repr(&item)).collect()).unwrap_or_default();
            if value.downcast_object_ref::<Tuple>().is_some() {
                if items.len() == 1 { format!("({},)", items[0]) } else { format!("({})", items.join(", ")) }
            } else {
                format!("[{}]", items.join(", "))
            }
        }
        ValueKind::Map => {
            let mut parts = Vec::new();
            if let Ok(keys) = value.try_iter() {
                for key in keys {
                    let item = value.get_item(&key).unwrap_or_default();
                    parts.push(format!("{}: {}", py_repr(&key), py_repr(&item)));
                }
            }
            format!("{{{}}}", parts.join(", "))
        }
        _ => value.to_string(),
    }
}

/// `pprint.pformat(value, width)`: sorted dictionaries and wrapped containers.
fn pformat(value: &Value, indent: usize, width: usize) -> String {
    let flat = sorted_repr(value);
    if flat.chars().count() + indent <= width { return flat; }
    match value.kind() {
        ValueKind::Seq | ValueKind::Iterable => {
            let tuple = value.downcast_object_ref::<Tuple>().is_some();
            let items: Vec<Value> = value.try_iter().map(Iterator::collect).unwrap_or_default();
            let (open, close) = if tuple { ("(", if items.len() == 1 { ",)" } else { ")" }) } else { ("[", "]") };
            let inner: Vec<String> = items.iter().map(|item| pformat(item, indent + 1, width)).collect();
            format!("{open}{}{close}", inner.join(&format!(",\n{}", " ".repeat(indent + 1))))
        }
        ValueKind::Map => {
            let mut entries: Vec<(Value, Value)> = value.try_iter().map(|keys| keys.map(|key| {
                let item = value.get_item(&key).unwrap_or_default();
                (key, item)
            }).collect()).unwrap_or_default();
            entries.sort_by(|a, b| py_cmp(&a.0, &b.0));
            let inner: Vec<String> = entries.iter().map(|(key, item)| {
                let key = sorted_repr(key);
                let child = indent + 1 + key.chars().count() + 2;
                format!("{key}: {}", pformat(item, child, width))
            }).collect();
            format!("{{{}}}", inner.join(&format!(",\n{}", " ".repeat(indent + 1))))
        }
        _ => flat,
    }
}

fn sorted_repr(value: &Value) -> String {
    match value.kind() {
        ValueKind::Map => {
            let mut entries: Vec<(Value, Value)> = value.try_iter().map(|keys| keys.map(|key| {
                let item = value.get_item(&key).unwrap_or_default();
                (key, item)
            }).collect()).unwrap_or_default();
            entries.sort_by(|a, b| py_cmp(&a.0, &b.0));
            let parts: Vec<String> = entries.iter().map(|(k, v)| format!("{}: {}", sorted_repr(k), sorted_repr(v))).collect();
            format!("{{{}}}", parts.join(", "))
        }
        ValueKind::Seq | ValueKind::Iterable => {
            let items: Vec<String> = value.try_iter().map(|iter| iter.map(|item| sorted_repr(&item)).collect()).unwrap_or_default();
            if value.downcast_object_ref::<Tuple>().is_some() {
                if items.len() == 1 { format!("({},)", items[0]) } else { format!("({})", items.join(", ")) }
            } else { format!("[{}]", items.join(", ")) }
        }
        _ => py_repr(value),
    }
}

/// `markupsafe.escape`.
pub(crate) fn html_escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&#34;"),
            '\'' => out.push_str("&#39;"),
            c => out.push(c),
        }
    }
    out
}

/// Python ordering for the values `sort`/`max`/`min` compare.
fn py_cmp(a: &Value, b: &Value) -> Ordering {
    match (a.kind(), b.kind()) {
        (ValueKind::Number | ValueKind::Bool, ValueKind::Number | ValueKind::Bool) => {
            let x = f64::try_from(a.clone()).unwrap_or(0.0);
            let y = f64::try_from(b.clone()).unwrap_or(0.0);
            x.partial_cmp(&y).unwrap_or(Ordering::Equal)
        }
        (ValueKind::String, ValueKind::String) => a.as_str().cmp(&b.as_str()),
        (ValueKind::Seq, ValueKind::Seq) => {
            let x: Vec<Value> = a.try_iter().map(Iterator::collect).unwrap_or_default();
            let y: Vec<Value> = b.try_iter().map(Iterator::collect).unwrap_or_default();
            for (p, q) in x.iter().zip(&y) {
                let order = py_cmp(p, q);
                if order != Ordering::Equal { return order; }
            }
            x.len().cmp(&y.len())
        }
        _ => a.cmp(b),
    }
}

fn type_error(message: impl Into<String>) -> Error { Error::new(ErrorKind::InvalidOperation, message.into()) }

/// Jinja positional-or-keyword parameter `name` at `index` of `args`.
fn param(args: &[Value], kwargs: &Kwargs, index: usize, name: &str) -> Result<Option<Value>, Error> {
    let keyword: Option<Value> = kwargs.get(name)?;
    match (args.get(index), keyword) {
        (Some(_), Some(_)) => Err(Error::new(ErrorKind::TooManyArguments, format!("multiple values for argument '{name}'"))),
        (Some(value), None) => Ok(Some(value.clone())),
        (None, keyword) => Ok(keyword),
    }
}

fn check_arity(args: &[Value], kwargs: &Kwargs, max: usize) -> Result<(), Error> {
    kwargs.assert_all_used()?;
    if args.len() > max { return Err(Error::new(ErrorKind::TooManyArguments, "too many arguments")); }
    Ok(())
}

/// `environment.getitem`/`getattr` chain used by `attribute=` (`a.b.0`).
fn attribute_path(value: &Value, path: &str) -> Result<Value, Error> {
    let mut current = value.clone();
    for part in path.split('.') {
        current = match part.parse::<i64>() {
            Ok(index) => current.get_item(&Value::from(index))?,
            Err(_) => current.get_attr(part)?,
        };
    }
    Ok(current)
}

fn lowered(value: Value, case_sensitive: bool) -> Value {
    if !case_sensitive && value.kind() == ValueKind::String { Value::from(value.as_str().unwrap_or_default().to_lowercase()) } else { value }
}

// ---------------------------------------------------------------- filters

/// `attr(obj, name)`: attribute lookup only; mapping keys are not attributes.
fn attr(value: Value, name: String) -> Result<Value, Error> {
    if value.kind() == ValueKind::Map && value.as_object().is_none_or(|o| o.repr() == ObjectRepr::Map) {
        return Ok(Value::UNDEFINED);
    }
    Ok(value.get_attr(&name).unwrap_or(Value::UNDEFINED))
}

/// `dictsort(value, case_sensitive=False, by='key', reverse=False)`.
fn dictsort(value: Value, args: Rest<Value>, kwargs: Kwargs) -> Result<Value, Error> {
    let case_sensitive = param(&args, &kwargs, 0, "case_sensitive")?.is_some_and(|v| v.is_true());
    let by = param(&args, &kwargs, 1, "by")?.map(|v| v.to_string()).unwrap_or_else(|| "key".into());
    let reverse = param(&args, &kwargs, 2, "reverse")?.is_some_and(|v| v.is_true());
    check_arity(&args, &kwargs, 3)?;
    let position = match by.as_str() {
        "key" => 0,
        "value" => 1,
        _ => return Err(type_error("You can only sort by either \"key\" or \"value\"")),
    };
    let mut pairs = item_pairs(&value)?;
    pairs.sort_by(|a, b| {
        let (x, y) = if position == 0 { (&a.0, &b.0) } else { (&a.1, &b.1) };
        py_cmp(&lowered(x.clone(), case_sensitive), &lowered(y.clone(), case_sensitive))
    });
    if reverse { pairs.reverse(); }
    Ok(Value::from(pairs.into_iter().map(|(k, v)| tuple(vec![k, v])).collect::<Vec<_>>()))
}

fn item_pairs(value: &Value) -> Result<Vec<(Value, Value)>, Error> {
    if value.kind() != ValueKind::Map { return Err(type_error("Can only get item pairs from a mapping.")); }
    Ok(value.try_iter()?.map(|key| {
        let item = value.get_item(&key).unwrap_or_default();
        (key, item)
    }).collect())
}

/// `escape(value)`: markupsafe escaping that leaves safe strings unchanged.
fn escape(value: Value) -> Value {
    if value.is_safe() { return value; }
    if let Some(xml) = crate::value::rich_xml(&value) { return Value::from_safe_string(xml.to_owned()); }
    Value::from_safe_string(html_escape(&py_str(&value)))
}

/// Python `float(text)`, accepting surrounding whitespace and underscores
/// between digits.
fn parse_py_float(text: &str) -> Option<f64> {
    let text = text.trim();
    if text.is_empty() || text.starts_with('_') || text.ends_with('_') || text.contains("__") { return None; }
    let cleaned: String = text.chars().filter(|&c| c != '_').collect();
    let lower = cleaned.to_ascii_lowercase();
    let unsigned = lower.trim_start_matches(['+', '-']);
    if !matches!(unsigned, "inf" | "infinity" | "nan") && !unsigned.chars().all(|c| c.is_ascii_digit() || matches!(c, '.' | 'e' | '+' | '-')) {
        return None;
    }
    cleaned.parse::<f64>().ok()
}

/// `float(value, default=0.0)`.
fn float(value: Value, args: Rest<Value>, kwargs: Kwargs) -> Result<Value, Error> {
    let default = param(&args, &kwargs, 0, "default")?.unwrap_or(Value::from(0.0));
    check_arity(&args, &kwargs, 1)?;
    let parsed = match value.kind() {
        ValueKind::Number | ValueKind::Bool => f64::try_from(value.clone()).ok(),
        ValueKind::String => parse_py_float(value.as_str().unwrap_or_default()),
        _ => None,
    };
    Ok(parsed.map(Value::from).unwrap_or(default))
}

/// Python `int(text, base)`.
fn parse_py_int(text: &str, base: u32) -> Option<i128> {
    let text = text.trim();
    let (negative, digits) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text.strip_prefix('+').unwrap_or(text)),
    };
    let lower = digits.to_ascii_lowercase();
    let (base, digits) = match base {
        0 => match lower.get(..2) {
            Some("0x") => (16, &lower[2..]),
            Some("0o") => (8, &lower[2..]),
            Some("0b") => (2, &lower[2..]),
            _ if lower.len() > 1 && lower.starts_with('0') && lower.chars().any(|c| c != '0' && c != '_') => return None,
            _ => (10, &lower[..]),
        },
        16 => (16, lower.strip_prefix("0x").unwrap_or(&lower)),
        8 => (8, lower.strip_prefix("0o").unwrap_or(&lower)),
        2 => (2, lower.strip_prefix("0b").unwrap_or(&lower)),
        base => (base, &lower[..]),
    };
    let digits = digits.strip_prefix('_').unwrap_or(digits);
    if digits.is_empty() || digits.ends_with('_') || digits.contains("__") { return None; }
    let cleaned: String = digits.chars().filter(|&c| c != '_').collect();
    let magnitude = i128::from_str_radix(&cleaned, base).ok()?;
    Some(if negative { -magnitude } else { magnitude })
}

/// `int(value, default=0, base=10)`, including Jinja's `int(float(value))`
/// fallback so `"42.23"|int` is 42.
fn int(value: Value, args: Rest<Value>, kwargs: Kwargs) -> Result<Value, Error> {
    let default = param(&args, &kwargs, 0, "default")?.unwrap_or(Value::from(0));
    let base = param(&args, &kwargs, 1, "base")?.and_then(|v| v.as_i64()).unwrap_or(10);
    check_arity(&args, &kwargs, 2)?;
    let base = u32::try_from(base).map_err(|_| type_error("int() base must be >= 2 and <= 36, or 0"))?;
    let direct = match value.kind() {
        ValueKind::String => parse_py_int(value.as_str().unwrap_or_default(), base),
        ValueKind::Bool => Some(i128::from(value.is_true())),
        ValueKind::Number if value.is_integer() => value.as_i64().map(i128::from),
        ValueKind::Number => f64::try_from(value.clone()).ok().filter(|f| f.is_finite()).map(|f| f.trunc() as i128),
        _ => None,
    };
    if let Some(number) = direct { return Ok(Value::from(number)); }
    let via_float = match value.kind() {
        ValueKind::String => parse_py_float(value.as_str().unwrap_or_default()),
        _ => None,
    };
    match via_float {
        Some(f) if f.is_finite() => Ok(Value::from(f.trunc() as i128)),
        Some(f) if f.is_nan() => Err(type_error("cannot convert float NaN to integer")),
        Some(_) => Err(type_error("cannot convert float infinity to integer")),
        None => Ok(default),
    }
}

/// `format(value, *args, **kwargs)`: `str(value) % (kwargs or args)`.
fn format(value: Value, args: Rest<Value>, kwargs: Kwargs) -> Result<String, Error> {
    let keys: Vec<String> = kwargs.args().map(str::to_owned).collect();
    if !args.is_empty() && !keys.is_empty() {
        return Err(type_error("can't handle positional and keyword arguments at the same time"));
    }
    let arguments = if keys.is_empty() {
        FormatArgs::Positional(args.0.clone())
    } else {
        let map: Vec<(Value, Value)> = keys.iter().map(|key| Ok((Value::from(key.as_str()), kwargs.get::<Value>(key)?))).collect::<Result<_, Error>>()?;
        FormatArgs::Mapping(map.into_iter().collect())
    };
    kwargs.assert_all_used()?;
    printf(&py_str(&value), &arguments)
}

/// `indent(s, width=4, first=False, blank=False)`.
fn indent(value: Value, args: Rest<Value>, kwargs: Kwargs) -> Result<String, Error> {
    let width = param(&args, &kwargs, 0, "width")?;
    let first = param(&args, &kwargs, 1, "first")?.is_some_and(|v| v.is_true());
    let blank = param(&args, &kwargs, 2, "blank")?.is_some_and(|v| v.is_true());
    check_arity(&args, &kwargs, 3)?;
    let indention = match width {
        Some(width) if width.kind() == ValueKind::String => width.to_string(),
        Some(width) => " ".repeat(usize::try_from(width.as_i64().unwrap_or(4)).unwrap_or(0)),
        None => "    ".into(),
    };
    let text = py_str(&value) + "\n";
    let lines = splitlines(&text);
    let mut result = if blank {
        lines.join(&format!("\n{indention}"))
    } else {
        let mut iter = lines.into_iter();
        let mut result = iter.next().unwrap_or_default();
        let rest: Vec<String> = iter.map(|line| if line.is_empty() { line } else { format!("{indention}{line}") }).collect();
        if !rest.is_empty() { result.push('\n'); result.push_str(&rest.join("\n")); }
        result
    };
    if first { result = format!("{indention}{result}"); }
    Ok(result)
}

/// Python `str.splitlines()` line boundaries.
fn splitlines(text: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut current = String::new();
    let mut chars = text.chars().peekable();
    while let Some(ch) = chars.next() {
        match ch {
            '\r' => {
                if chars.peek() == Some(&'\n') { chars.next(); }
                lines.push(std::mem::take(&mut current));
            }
            '\n' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}' => {
                lines.push(std::mem::take(&mut current));
            }
            c => current.push(c),
        }
    }
    if !current.is_empty() { lines.push(current); }
    lines
}

/// `items(value)`: `(key, value)` tuples; undefined yields nothing.
fn items(value: Value) -> Result<Value, Error> {
    if value.is_undefined() { return Ok(Value::from(Vec::<Value>::new())); }
    Ok(Value::from(item_pairs(&value)?.into_iter().map(|(k, v)| tuple(vec![k, v])).collect::<Vec<_>>()))
}

/// `join(value, d='', attribute=None)`.
fn join(state: &State, value: Value, args: Rest<Value>, kwargs: Kwargs) -> Result<Value, Error> {
    let separator = param(&args, &kwargs, 0, "d")?.unwrap_or_else(|| Value::from(""));
    let attribute = param(&args, &kwargs, 1, "attribute")?.map(|v| v.to_string());
    check_arity(&args, &kwargs, 2)?;
    if value.is_undefined() { return Ok(Value::from("")); }
    let mut items = Vec::new();
    for item in value.try_iter()? {
        items.push(match &attribute { Some(path) => attribute_path(&item, path)?, None => item });
    }
    // With autoescape, markup (anything with `__html__`) among the separator
    // and the items makes the result markup, the other pieces escaped.
    let markup = |v: &Value| v.is_safe() || crate::value::rich_xml(v).is_some();
    if state.auto_escape() != AutoEscape::None && (markup(&separator) || items.iter().any(markup)) {
        let piece = |v: &Value| if markup(v) { v.to_string() } else { html_escape(&py_str(v)) };
        let parts: Vec<String> = items.iter().map(piece).collect();
        return Ok(Value::from_safe_string(parts.join(&piece(&separator))));
    }
    let parts: Vec<String> = items.iter().map(py_str).collect();
    Ok(Value::from(parts.join(&py_str(&separator))))
}

/// `max`/`min(value, case_sensitive=False, attribute=None)`: the first
/// extreme item, compared case-insensitively unless requested.
fn extreme(_state: &State, value: Value, args: Rest<Value>, kwargs: Kwargs, wanted: Ordering) -> Result<Value, Error> {
    let case_sensitive = param(&args, &kwargs, 0, "case_sensitive")?.is_some_and(|v| v.is_true());
    let attribute = param(&args, &kwargs, 1, "attribute")?.map(|v| v.to_string());
    check_arity(&args, &kwargs, 2)?;
    let mut best: Option<(Value, Value)> = None;
    for item in value.try_iter()? {
        let key = match &attribute { Some(path) => attribute_path(&item, path)?, None => item.clone() };
        let key = lowered(key, case_sensitive);
        let replace = match &best { None => true, Some((current, _)) => py_cmp(&key, current) == wanted };
        if replace { best = Some((key, item)); }
    }
    Ok(best.map(|(_, item)| item).unwrap_or(Value::UNDEFINED))
}

/// `replace(s, old, new, count=None)`.
fn replace(value: Value, old: Value, new: Value, count: Option<Value>) -> Result<Value, Error> {
    let text = py_str(&value);
    let (old, new) = (py_str(&old), py_str(&new));
    let limit = count.and_then(|c| c.as_i64()).filter(|&c| c >= 0).map(|c| c as usize);
    let replaced = match limit { Some(n) => text.replacen(&old, &new, n), None => text.replace(&old, &new) };
    Ok(if value.is_safe() { Value::from_safe_string(replaced) } else { Value::from(replaced) })
}

/// `round(value, precision=0, method='common')`; `common` is Python's
/// correctly rounded, ties-to-even `round`.
fn round(value: Value, args: Rest<Value>, kwargs: Kwargs) -> Result<Value, Error> {
    let precision = param(&args, &kwargs, 0, "precision")?.and_then(|v| v.as_i64()).unwrap_or(0);
    let method = param(&args, &kwargs, 1, "method")?.map(|v| v.to_string()).unwrap_or_else(|| "common".into());
    check_arity(&args, &kwargs, 2)?;
    if !matches!(method.as_str(), "common" | "ceil" | "floor") {
        return Err(type_error("method must be common, ceil or floor"));
    }
    if method == "common" && value.is_integer() && precision >= 0 { return Ok(value); }
    let number = f64::try_from(value.clone()).map_err(|_| type_error("round() requires a number"))?;
    let factor = 10f64.powi(i32::try_from(precision).unwrap_or(0));
    let rounded = match method.as_str() {
        "ceil" => (number * factor).ceil() / factor,
        "floor" => (number * factor).floor() / factor,
        _ if precision >= 0 => format!("{number:.*}", precision as usize).parse().unwrap_or(number),
        _ => {
            let scaled = number / 10f64.powi(i32::try_from(-precision).unwrap_or(0));
            let even = format!("{scaled:.0}").parse::<f64>().unwrap_or(scaled);
            even * 10f64.powi(i32::try_from(-precision).unwrap_or(0))
        }
    };
    Ok(Value::from(rounded))
}

/// Python `+` for the operand kinds `sum` meets: integers stay exact while
/// they fit, other numbers become floats, sequences concatenate.
fn add(left: &Value, right: &Value) -> Result<Value, Error> {
    match (left.kind(), right.kind()) {
        (ValueKind::Number | ValueKind::Bool, ValueKind::Number | ValueKind::Bool) => {
            let integral = |v: &Value| v.kind() == ValueKind::Bool || v.is_integer();
            if integral(left) && integral(right) {
                let a = i128::try_from(left.clone()).unwrap_or(i128::from(left.is_true()));
                let b = i128::try_from(right.clone()).unwrap_or(i128::from(right.is_true()));
                if let Some(total) = a.checked_add(b) { return Ok(Value::from(total)); }
            }
            Ok(Value::from(f64::try_from(left.clone())? + f64::try_from(right.clone())?))
        }
        (ValueKind::Seq, ValueKind::Seq) => Ok(Value::from(left.try_iter()?.chain(right.try_iter()?).collect::<Vec<_>>())),
        _ => Err(type_error(format!("unsupported operand type(s) for +: '{}' and '{}'", left.kind(), right.kind()))),
    }
}

/// `sum(iterable, attribute=None, start=0)`.
fn sum(values: Value, kwargs: Kwargs) -> Result<Value, Error> {
    let path: Option<String> = kwargs.get("attribute")?;
    let start: Option<Value> = kwargs.get("start")?;
    kwargs.assert_all_used()?;
    let mut total = start.unwrap_or(Value::from(0));
    for item in values.try_iter()? {
        let item = match &path { Some(path) => attribute_path(&item, path)?, None => item };
        total = add(&total, &item)?;
    }
    Ok(total)
}

/// Jinja's `title`: capitalize after `-`, whitespace and opening brackets.
fn title(text: &str) -> String {
    let separator = |c: char| c == '-' || c.is_whitespace() || matches!(c, '(' | '{' | '[' | '<');
    let mut out = String::with_capacity(text.len());
    let mut start = true;
    for ch in text.chars() {
        if separator(ch) { out.push(ch); start = true; continue; }
        if start { out.extend(ch.to_uppercase()); } else { out.extend(ch.to_lowercase()); }
        start = false;
    }
    out
}

/// `tojson(value, indent=None)`: `json.dumps(sort_keys=True)` with HTML-safe
/// escapes, as a safe string.
fn tojson(value: Value, args: Rest<Value>, kwargs: Kwargs) -> Result<Value, Error> {
    let indent = param(&args, &kwargs, 0, "indent")?.and_then(|v| v.as_i64()).map(|v| usize::try_from(v).unwrap_or(0));
    check_arity(&args, &kwargs, 1)?;
    let mut out = String::new();
    write_json(&mut out, &value, indent, 0)?;
    let out = out.replace('<', "\\u003c").replace('>', "\\u003e").replace('&', "\\u0026").replace('\'', "\\u0027");
    Ok(Value::from_safe_string(out))
}

fn write_json_string(out: &mut String, text: &str) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if c.is_ascii() && (c as u32) >= 0x20 => out.push(c),
            c => {
                let mut buffer = [0u16; 2];
                for unit in c.encode_utf16(&mut buffer) { let _ = write!(out, "\\u{unit:04x}"); }
            }
        }
    }
    out.push('"');
}

fn write_json(out: &mut String, value: &Value, indent: Option<usize>, level: usize) -> Result<(), Error> {
    let newline = |out: &mut String, level: usize| if let Some(width) = indent { out.push('\n'); out.push_str(&" ".repeat(width * level)); };
    let item_separator = if indent.is_some() { "," } else { ", " };
    match value.kind() {
        ValueKind::Undefined | ValueKind::None => out.push_str("null"),
        ValueKind::Bool => out.push_str(if value.is_true() { "true" } else { "false" }),
        ValueKind::Number => {
            if value.is_integer() { out.push_str(&value.to_string()); } else {
                let f = f64::try_from(value.clone())?;
                out.push_str(&if f.is_nan() { "NaN".into() } else if f.is_infinite() { if f < 0.0 { "-Infinity".into() } else { "Infinity".into() } } else { py_float(f) });
            }
        }
        ValueKind::String => write_json_string(out, value.as_str().unwrap_or_default()),
        ValueKind::Seq | ValueKind::Iterable => {
            let items: Vec<Value> = value.try_iter()?.collect();
            if items.is_empty() { out.push_str("[]"); return Ok(()); }
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 { out.push_str(item_separator); }
                newline(out, level + 1);
                write_json(out, item, indent, level + 1)?;
            }
            newline(out, level);
            out.push(']');
        }
        ValueKind::Map => {
            let mut entries: Vec<(String, Value)> = value.try_iter()?.map(|key| {
                let item = value.get_item(&key).unwrap_or_default();
                (py_str(&key), item)
            }).collect();
            if entries.is_empty() { out.push_str("{}"); return Ok(()); }
            entries.sort_by(|a, b| a.0.cmp(&b.0));
            out.push('{');
            for (index, (key, item)) in entries.iter().enumerate() {
                if index > 0 { out.push_str(item_separator); }
                newline(out, level + 1);
                write_json_string(out, key);
                out.push_str(": ");
                write_json(out, item, indent, level + 1)?;
            }
            newline(out, level);
            out.push('}');
        }
        _ => return Err(type_error(format!("Object of type {} is not JSON serializable", value.kind()))),
    }
    Ok(())
}

/// `truncate(s, length=255, killwords=False, end='...', leeway=5)`.
fn truncate(value: Value, args: Rest<Value>, kwargs: Kwargs) -> Result<String, Error> {
    let length = param(&args, &kwargs, 0, "length")?.and_then(|v| v.as_i64()).unwrap_or(255);
    let killwords = param(&args, &kwargs, 1, "killwords")?.is_some_and(|v| v.is_true());
    let end = param(&args, &kwargs, 2, "end")?.map(|v| py_str(&v)).unwrap_or_else(|| "...".into());
    let leeway = param(&args, &kwargs, 3, "leeway")?.and_then(|v| v.as_i64()).unwrap_or(5);
    check_arity(&args, &kwargs, 4)?;
    let text = py_str(&value);
    let chars: Vec<char> = text.chars().collect();
    let end_len = end.chars().count() as i64;
    if length < end_len { return Err(type_error(format!("expected length >= {end_len}, got {length}"))); }
    if leeway < 0 { return Err(type_error(format!("expected leeway >= 0, got {leeway}"))); }
    if chars.len() as i64 <= length + leeway { return Ok(text); }
    let cut: String = chars[..(length - end_len) as usize].iter().collect();
    if killwords { return Ok(cut + &end); }
    let kept = cut.rsplit_once(' ').map_or(cut.as_str(), |(head, _)| head);
    Ok(format!("{kept}{end}"))
}

/// `wordwrap(s, width=79, break_long_words=True, wrapstring=None,
/// break_on_hyphens=True)` over Python's `textwrap` algorithm.
fn wordwrap(value: Value, args: Rest<Value>, kwargs: Kwargs) -> Result<String, Error> {
    let width = param(&args, &kwargs, 0, "width")?.and_then(|v| v.as_i64()).unwrap_or(79);
    let break_long = param(&args, &kwargs, 1, "break_long_words")?.is_none_or(|v| v.is_true());
    let wrapstring = param(&args, &kwargs, 2, "wrapstring")?.map(|v| py_str(&v)).unwrap_or_else(|| "\n".into());
    let break_on_hyphens = param(&args, &kwargs, 3, "break_on_hyphens")?.is_none_or(|v| v.is_true());
    check_arity(&args, &kwargs, 4)?;
    if width <= 0 { return Err(type_error(format!("invalid width {width} (must be > 0)"))); }
    let lines: Vec<String> = splitlines(&py_str(&value)).iter()
        .map(|line| textwrap(line, width as usize, break_long, break_on_hyphens).join(&wrapstring)).collect();
    Ok(lines.join(&wrapstring))
}

fn textwrap_chunks(text: &str, break_on_hyphens: bool) -> Vec<String> {
    let pattern = if break_on_hyphens {
        r#"([\t\n\x0b\x0c\r ]+|(?<=[\w!"'&.,?])-{2,}(?=\w)|[^\t\n\x0b\x0c\r ]+?(?:-(?:(?<=[^\d\W]{2}-)|(?<=[^\d\W]-[^\d\W]-))(?=[^\d\W]-?[^\d\W])|(?=[\t\n\x0b\x0c\r ]|\z)|(?<=[\w!"'&.,?])(?=-{2,}\w)))"#
    } else {
        r"([\t\n\x0b\x0c\r ]+)"
    };
    let regex = fancy_regex::Regex::new(pattern).expect("textwrap pattern");
    let mut chunks = Vec::new();
    let mut last = 0;
    for found in regex.find_iter(text).flatten() {
        if found.start() > last { chunks.push(text[last..found.start()].to_owned()); }
        if !found.as_str().is_empty() { chunks.push(found.as_str().to_owned()); }
        last = found.end();
    }
    if last < text.len() { chunks.push(text[last..].to_owned()); }
    chunks
}

fn textwrap(text: &str, width: usize, break_long: bool, break_on_hyphens: bool) -> Vec<String> {
    let mut chunks = textwrap_chunks(text, break_on_hyphens);
    chunks.reverse();
    let length = |s: &str| s.chars().count();
    let mut lines = Vec::new();
    while !chunks.is_empty() {
        let mut line: Vec<String> = Vec::new();
        let mut current = 0;
        if chunks.last().is_some_and(|c| c.trim().is_empty()) && !lines.is_empty() { chunks.pop(); }
        while let Some(chunk) = chunks.last() {
            let size = length(chunk);
            if current + size <= width { current += size; line.push(chunks.pop().expect("chunk")); } else { break; }
        }
        if chunks.last().is_some_and(|c| length(c) > width) {
            let space_left = if width < 1 { 1 } else { width - current };
            if break_long {
                let chunk: Vec<char> = chunks.last().expect("chunk").chars().collect();
                let mut end = space_left;
                if break_on_hyphens && chunk.len() > space_left {
                    let head = &chunk[..space_left];
                    if let Some(hyphen) = head.iter().rposition(|&c| c == '-') {
                        if hyphen > 0 && chunk[..hyphen].iter().any(|&c| c != '-') { end = hyphen + 1; }
                    }
                }
                line.push(chunk[..end].iter().collect());
                *chunks.last_mut().expect("chunk") = chunk[end..].iter().collect();
            } else if line.is_empty() {
                line.push(chunks.pop().expect("chunk"));
            }
        }
        if line.last().is_some_and(|c| c.trim().is_empty()) { line.pop(); }
        if !line.is_empty() { lines.push(line.concat()); }
    }
    lines
}

/// `urlize(value, trim_url_limit=None, nofollow=False, target=None,
/// rel=None, extra_schemes=None)` with the default `noopener` policy.
fn urlize(value: Value, args: Rest<Value>, kwargs: Kwargs) -> Result<String, Error> {
    let trim = param(&args, &kwargs, 0, "trim_url_limit")?.and_then(|v| v.as_i64());
    let nofollow = param(&args, &kwargs, 1, "nofollow")?.is_some_and(|v| v.is_true());
    let target = param(&args, &kwargs, 2, "target")?.filter(|v| !v.is_none()).map(|v| py_str(&v));
    let rel = param(&args, &kwargs, 3, "rel")?.filter(|v| !v.is_none()).map(|v| py_str(&v));
    let extra: Vec<String> = param(&args, &kwargs, 4, "extra_schemes")?.filter(|v| !v.is_none())
        .map(|v| v.try_iter().map(|i| i.map(|s| py_str(&s)).collect()).unwrap_or_default()).unwrap_or_default();
    check_arity(&args, &kwargs, 5)?;
    let mut rel_parts: Vec<String> = rel.unwrap_or_default().split_whitespace().map(str::to_owned).collect();
    if nofollow { rel_parts.push("nofollow".into()); }
    rel_parts.push("noopener".into());
    rel_parts.sort();
    rel_parts.dedup();
    let rel_attr = if rel_parts.is_empty() { String::new() } else { format!(" rel=\"{}\"", html_escape(&rel_parts.join(" "))) };
    let target_attr = target.map(|t| format!(" target=\"{}\"", html_escape(&t))).unwrap_or_default();
    let trim_url = |url: &str| match trim {
        Some(limit) if url.chars().count() as i64 > limit => format!("{}...", url.chars().take(limit.max(0) as usize).collect::<String>()),
        _ => url.to_owned(),
    };
    let http = fancy_regex::Regex::new(r"(?ix)^(
        (https?://|www\.)(([\w%-]+\.)+)?([a-z]{2,63}|xn--[\w%]{2,59})
        | ([\w%-]{2,63}\.)+(com|net|int|edu|gov|org|info|mil)
        | (https?://)((([\d]{1,3})(\.[\d]{1,3}){3})|(\[([\da-f]{0,4}:){2}([\da-f]{0,4}:?){1,6}]))
        )(?::[\d]{1,5})?(?:[/?\#]\S*)?$").expect("url pattern");
    let email = fancy_regex::Regex::new(r"^\S+@\w[\w.-]*\.\w+$").expect("email pattern");
    let lead = fancy_regex::Regex::new(r"^([(<]|&lt;)+").expect("lead");
    let trail = fancy_regex::Regex::new(r"([)>.,\n]|&gt;)+$").expect("trail");
    let escaped = if value.is_safe() || crate::value::rich_xml(&value).is_some() { value.to_string() } else { html_escape(&py_str(&value)) };
    let mut out = String::new();
    for word in split_keep_whitespace(&escaped) {
        if word.chars().all(char::is_whitespace) { out.push_str(&word); continue; }
        let (mut head, mut middle, mut tail) = (String::new(), word.clone(), String::new());
        if let Ok(Some(m)) = lead.find(&middle) { head = m.as_str().to_owned(); middle = middle[m.end()..].to_owned(); }
        if [")", ">", ".", ",", "\n", "&gt;"].iter().any(|s| middle.ends_with(s)) {
            if let Ok(Some(m)) = trail.find(&middle) { tail = m.as_str().to_owned(); middle.truncate(m.start()); }
        }
        for (start, end) in [("(", ")"), ("<", ">"), ("&lt;", "&gt;")] {
            let starts = middle.matches(start).count();
            if starts <= middle.matches(end).count() { continue; }
            for _ in 0..starts.min(tail.matches(end).count()) {
                let index = tail.find(end).expect("end char") + end.len();
                middle.push_str(&tail[..index]);
                tail = tail[index..].to_owned();
            }
        }
        let is_match = |re: &fancy_regex::Regex, s: &str| re.is_match(s).unwrap_or(false);
        if is_match(&http, &middle) {
            let href = if middle.starts_with("https://") || middle.starts_with("http://") { middle.clone() } else { format!("https://{middle}") };
            middle = format!("<a href=\"{href}\"{rel_attr}{target_attr}>{}</a>", trim_url(&middle));
        } else if middle.starts_with("mailto:") && is_match(&email, &middle[7..]) {
            middle = format!("<a href=\"{middle}\">{}</a>", &middle[7..]);
        } else if middle.contains('@') && !middle.starts_with("www.") && !middle.contains(':') && is_match(&email, &middle) {
            middle = format!("<a href=\"mailto:{middle}\">{middle}</a>");
        } else {
            for scheme in &extra {
                if &middle != scheme && middle.starts_with(scheme.as_str()) {
                    middle = format!("<a href=\"{middle}\"{rel_attr}{target_attr}>{middle}</a>");
                }
            }
        }
        out.push_str(&head);
        out.push_str(&middle);
        out.push_str(&tail);
    }
    Ok(out)
}

fn split_keep_whitespace(text: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut current = String::new();
    let mut in_space = false;
    for ch in text.chars() {
        let space = ch.is_whitespace();
        if space != in_space && !current.is_empty() { parts.push(std::mem::take(&mut current)); }
        in_space = space;
        current.push(ch);
    }
    if !current.is_empty() { parts.push(current); }
    parts
}

// ---------------------------------------------------------------- printf

enum FormatArgs {
    Positional(Vec<Value>),
    Mapping(Value),
}

/// Python printf-style `format % arguments`.
fn printf(format: &str, arguments: &FormatArgs) -> Result<String, Error> {
    let chars: Vec<char> = format.chars().collect();
    let mut out = String::new();
    let mut position = 0usize;
    let mut index = 0usize;
    let positional: &[Value] = match arguments { FormatArgs::Positional(values) => values, FormatArgs::Mapping(_) => &[] };
    let next_arg = |position: &mut usize| -> Result<Value, Error> {
        let value = positional.get(*position).cloned().ok_or_else(|| type_error("not enough arguments for format string"))?;
        *position += 1;
        Ok(value)
    };
    while index < chars.len() {
        if chars[index] != '%' { out.push(chars[index]); index += 1; continue; }
        index += 1;
        let mut mapped: Option<Value> = None;
        if chars.get(index) == Some(&'(') {
            let mut depth = 1;
            let start = index + 1;
            index += 1;
            while index < chars.len() && depth > 0 {
                match chars[index] { '(' => depth += 1, ')' => depth -= 1, _ => {} }
                index += 1;
            }
            let key: String = chars[start..index - 1].iter().collect();
            let FormatArgs::Mapping(map) = arguments else { return Err(type_error("format requires a mapping")); };
            mapped = Some(map.get_item(&Value::from(key.as_str())).ok().filter(|v| !v.is_undefined())
                .ok_or_else(|| Error::new(ErrorKind::InvalidOperation, format!("KeyError: {}", py_repr_str(&key))))?);
        }
        let mut flags = String::new();
        while let Some(&c) = chars.get(index) {
            if matches!(c, '-' | '+' | ' ' | '#' | '0') { flags.push(c); index += 1; } else { break; }
        }
        let mut width: Option<usize> = None;
        if chars.get(index) == Some(&'*') {
            width = Some(next_arg(&mut position)?.as_i64().unwrap_or(0).max(0) as usize);
            index += 1;
        } else {
            let start = index;
            while chars.get(index).is_some_and(char::is_ascii_digit) { index += 1; }
            if index > start { width = chars[start..index].iter().collect::<String>().parse().ok(); }
        }
        let mut precision: Option<usize> = None;
        if chars.get(index) == Some(&'.') {
            index += 1;
            if chars.get(index) == Some(&'*') {
                precision = Some(next_arg(&mut position)?.as_i64().unwrap_or(0).max(0) as usize);
                index += 1;
            } else {
                let start = index;
                while chars.get(index).is_some_and(char::is_ascii_digit) { index += 1; }
                precision = Some(chars[start..index].iter().collect::<String>().parse().unwrap_or(0));
            }
        }
        while chars.get(index).is_some_and(|c| matches!(c, 'h' | 'l' | 'L')) { index += 1; }
        let Some(&conversion) = chars.get(index) else { return Err(type_error("incomplete format")); };
        index += 1;
        if conversion == '%' { out.push('%'); continue; }
        let value = match mapped.take() {
            Some(value) => value,
            None if matches!(arguments, FormatArgs::Mapping(_)) => match arguments { FormatArgs::Mapping(map) => map.clone(), _ => unreachable!() },
            None => next_arg(&mut position)?,
        };
        let body = convert(&value, conversion, &flags, precision)?;
        let numeric = matches!(conversion, 'd' | 'i' | 'u' | 'f' | 'F' | 'e' | 'E' | 'g' | 'G' | 'x' | 'X' | 'o');
        out.push_str(&pad(body, width, &flags, numeric));
    }
    if let FormatArgs::Positional(values) = arguments {
        if position < values.len() { return Err(type_error("not all arguments converted during string formatting")); }
    }
    Ok(out)
}

fn convert(value: &Value, conversion: char, flags: &str, precision: Option<usize>) -> Result<String, Error> {
    let sign = |negative: bool| if negative { "-" } else if flags.contains('+') { "+" } else if flags.contains(' ') { " " } else { "" };
    let number = || -> Result<f64, Error> {
        match value.kind() {
            ValueKind::Number | ValueKind::Bool => Ok(f64::try_from(value.clone())?),
            _ => Err(type_error(format!("must be real number, not {}", value.kind()))),
        }
    };
    let integer = || -> Result<i128, Error> {
        match value.kind() {
            ValueKind::Bool => Ok(i128::from(value.is_true())),
            ValueKind::Number if value.is_integer() => Ok(i128::try_from(value.clone())?),
            ValueKind::Number => Ok(f64::try_from(value.clone())?.trunc() as i128),
            _ => Err(type_error(format!("%{conversion} format: a real number is required, not {}", value.kind()))),
        }
    };
    Ok(match conversion {
        's' => { let s = py_str(value); match precision { Some(p) => s.chars().take(p).collect(), None => s } }
        'r' | 'a' => { let s = py_repr(value); match precision { Some(p) => s.chars().take(p).collect(), None => s } }
        'c' => match value.kind() {
            ValueKind::String => value.to_string(),
            _ => char::from_u32(u32::try_from(integer()?).unwrap_or(0)).map(String::from).unwrap_or_default(),
        },
        'd' | 'i' | 'u' => { let n = integer()?; format!("{}{}", sign(n < 0), n.unsigned_abs()) }
        'x' | 'X' | 'o' => {
            let n = integer()?;
            let digits = match conversion { 'x' => format!("{:x}", n.unsigned_abs()), 'X' => format!("{:X}", n.unsigned_abs()), _ => format!("{:o}", n.unsigned_abs()) };
            let prefix = if flags.contains('#') { match conversion { 'x' => "0x", 'X' => "0X", _ => "0o" } } else { "" };
            format!("{}{prefix}{digits}", sign(n < 0))
        }
        'f' | 'F' => { let f = number()?; let p = precision.unwrap_or(6); special(f, conversion).unwrap_or_else(|| format!("{}{:.*}", sign(f.is_sign_negative() && f != 0.0 || f.to_bits() == (-0.0f64).to_bits()), p, f.abs())) }
        'e' | 'E' => {
            let f = number()?;
            let p = precision.unwrap_or(6);
            special(f, conversion).unwrap_or_else(|| {
                let text = format!("{:.*e}", p, f.abs());
                let (mantissa, exponent) = text.split_once('e').expect("exponent");
                let exponent: i32 = exponent.parse().expect("exponent");
                let body = format!("{mantissa}e{}{:02}", if exponent < 0 { '-' } else { '+' }, exponent.abs());
                let body = if conversion == 'E' { body.to_uppercase() } else { body };
                format!("{}{body}", sign(f.is_sign_negative()))
            })
        }
        'g' | 'G' => {
            let f = number()?;
            special(f, conversion).unwrap_or_else(|| {
                let p = precision.unwrap_or(6).max(1);
                let exponent = if f == 0.0 { 0 } else { format!("{:.*e}", p - 1, f.abs()).split_once('e').expect("e").1.parse::<i32>().expect("exp") };
                let body = if exponent < -4 || exponent >= p as i32 {
                    let text = format!("{:.*e}", p - 1, f.abs());
                    let (mantissa, exp) = text.split_once('e').expect("e");
                    let mantissa = if flags.contains('#') { mantissa.to_owned() } else { trim_zeros(mantissa) };
                    let exp: i32 = exp.parse().expect("exp");
                    format!("{mantissa}e{}{:02}", if exp < 0 { '-' } else { '+' }, exp.abs())
                } else {
                    let decimals = (p as i32 - 1 - exponent).max(0) as usize;
                    let text = format!("{:.*}", decimals, f.abs());
                    if flags.contains('#') { text } else { trim_zeros(&text) }
                };
                let body = if conversion == 'G' { body.to_uppercase() } else { body };
                format!("{}{body}", sign(f.is_sign_negative()))
            })
        }
        other => return Err(type_error(format!("unsupported format character '{other}'"))),
    })
}

fn special(f: f64, conversion: char) -> Option<String> {
    let text = if f.is_nan() { "nan" } else if f.is_infinite() { if f < 0.0 { "-inf" } else { "inf" } } else { return None };
    Some(if conversion.is_ascii_uppercase() { text.to_uppercase() } else { text.to_owned() })
}

fn trim_zeros(text: &str) -> String {
    if !text.contains('.') { return text.to_owned(); }
    text.trim_end_matches('0').trim_end_matches('.').to_owned()
}

fn pad(body: String, width: Option<usize>, flags: &str, numeric: bool) -> String {
    let Some(width) = width else { return body };
    let length = body.chars().count();
    if length >= width { return body; }
    let fill = width - length;
    if flags.contains('-') { return format!("{body}{}", " ".repeat(fill)); }
    if flags.contains('0') && numeric {
        let (sign, digits) = match body.chars().next() { Some(c @ ('-' | '+' | ' ')) => (c.to_string(), body[1..].to_owned()), _ => (String::new(), body.clone()) };
        let (prefix, digits) = match digits.get(..2) { Some(p @ ("0x" | "0X" | "0o")) => (p.to_owned(), digits[2..].to_owned()), _ => (String::new(), digits) };
        return format!("{sign}{prefix}{}{digits}", "0".repeat(fill));
    }
    format!("{}{body}", " ".repeat(fill))
}

// ---------------------------------------------------------------- methods

/// CPython `str`/`dict` methods missing from or differing in the core.
fn python_method(value: &Value, method: &str, args: &[Value]) -> Result<Option<Value>, Error> {
    if value.kind() == ValueKind::Map && value.as_object().is_none_or(|o| o.downcast_ref::<Tuple>().is_none()) {
        return Ok(match method {
            "items" if args.is_empty() => Some(Value::from(item_pairs(value)?.into_iter().map(|(k, v)| tuple(vec![k, v])).collect::<Vec<_>>())),
            _ => None,
        });
    }
    if value.kind() == ValueKind::Seq {
        return Ok(match method {
            "index" | "count" => {
                let needle = args.first().ok_or_else(|| type_error(format!("{method}() takes exactly one argument")))?;
                let items: Vec<Value> = value.try_iter()?.collect();
                let matches = |item: &Value| py_cmp(item, needle) == Ordering::Equal && item.kind() == needle.kind()
                    || (item.kind() == ValueKind::Number && needle.kind() == ValueKind::Number && py_cmp(item, needle) == Ordering::Equal);
                if method == "count" { Some(Value::from(items.iter().filter(|i| matches(i)).count())) } else {
                    Some(Value::from(items.iter().position(matches).ok_or_else(|| type_error(format!("{} is not in list", py_repr(needle))))?))
                }
            }
            _ => None,
        });
    }
    if value.kind() != ValueKind::String { return Ok(None); }
    let text = value.as_str().unwrap_or_default();
    let (args, kwargs): (&[Value], Option<Kwargs>) = match args.last().map(|last| Kwargs::try_from(last.clone())) {
        Some(Ok(kwargs)) => (&args[..args.len() - 1], Some(kwargs)),
        _ => (args, None),
    };
    let arg = |i: usize| args.get(i).filter(|v| !v.is_none()).map(py_str);
    let width = |i: usize| args.get(i).and_then(Value::as_i64).unwrap_or(0).max(0) as usize;
    let fill = |i: usize| arg(i).and_then(|s| s.chars().next()).unwrap_or(' ');
    let count = text.chars().count();
    let result = match method {
        // `str.strip(chars=None)`: any character of `chars`, or whitespace
        // as `str.isspace` sees it (which adds U+001C..U+001F to Unicode's).
        "strip" | "lstrip" | "rstrip" => {
            let chars = arg(0);
            let strip = |c: char| match &chars {
                Some(chars) => chars.contains(c),
                None => c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c),
            };
            Value::from(match method {
                "strip" => text.trim_matches(strip),
                "lstrip" => text.trim_start_matches(strip),
                _ => text.trim_end_matches(strip),
            })
        }
        "swapcase" => Value::from(text.chars().flat_map(|c| if c.is_uppercase() { c.to_lowercase().collect::<Vec<_>>() } else { c.to_uppercase().collect() }).collect::<String>()),
        "casefold" => Value::from(text.to_lowercase()),
        "ljust" => Value::from(format!("{text}{}", fill(1).to_string().repeat(width(0).saturating_sub(count)))),
        "rjust" => Value::from(format!("{}{text}", fill(1).to_string().repeat(width(0).saturating_sub(count)))),
        "removeprefix" => Value::from(text.strip_prefix(arg(0).unwrap_or_default().as_str()).unwrap_or(text)),
        "removesuffix" => Value::from(text.strip_suffix(arg(0).unwrap_or_default().as_str()).unwrap_or(text)),
        "isupper" => Value::from(text.chars().any(char::is_alphabetic) && !text.chars().any(char::is_lowercase)),
        "islower" => Value::from(text.chars().any(char::is_alphabetic) && !text.chars().any(char::is_uppercase)),
        "isspace" => Value::from(!text.is_empty() && text.chars().all(char::is_whitespace)),
        "isalnum" => Value::from(!text.is_empty() && text.chars().all(char::is_alphanumeric)),
        "isnumeric" | "isdecimal" => Value::from(!text.is_empty() && text.chars().all(char::is_numeric)),
        "istitle" => Value::from(title_like(text)),
        "partition" | "rpartition" => {
            let separator = arg(0).ok_or_else(|| type_error("partition() takes exactly one argument"))?;
            let found = if method == "partition" { text.split_once(separator.as_str()) } else { text.rsplit_once(separator.as_str()) };
            let parts = match found {
                Some((head, tail)) => vec![head.to_owned(), separator, tail.to_owned()],
                None if method == "partition" => vec![text.to_owned(), String::new(), String::new()],
                None => vec![String::new(), String::new(), text.to_owned()],
            };
            tuple(parts.into_iter().map(Value::from).collect())
        }
        "rsplit" => {
            let limit = args.get(1).and_then(Value::as_i64).unwrap_or(-1);
            let mut parts: Vec<String> = match arg(0) {
                None => {
                    let words: Vec<&str> = text.split_whitespace().collect();
                    if limit < 0 || words.len() <= limit as usize + 1 { words.into_iter().map(str::to_owned).collect() } else {
                        let trimmed = text.trim_end();
                        let mut rest = trimmed;
                        let mut tail = Vec::new();
                        for _ in 0..limit {
                            let cut = rest.rfind(char::is_whitespace).expect("separator");
                            tail.push(rest[cut..].trim_start().to_owned());
                            rest = rest[..cut].trim_end();
                        }
                        tail.push(rest.to_owned());
                        tail.reverse();
                        tail
                    }
                }
                Some(separator) => {
                    let pieces: Vec<&str> = if limit < 0 { text.rsplit(separator.as_str()).collect() } else { text.rsplitn(limit as usize + 1, separator.as_str()).collect() };
                    pieces.into_iter().rev().map(str::to_owned).collect()
                }
            };
            if parts.is_empty() { parts.push(String::new()); }
            Value::from(parts)
        }
        "rfind" | "rindex" => {
            let needle = arg(0).unwrap_or_default();
            match text.rfind(needle.as_str()) {
                Some(byte) => Value::from(text[..byte].chars().count() as i64),
                None if method == "rfind" => Value::from(-1),
                None => return Err(type_error("substring not found")),
            }
        }
        "index" => {
            let needle = arg(0).unwrap_or_default();
            match text.find(needle.as_str()) {
                Some(byte) => Value::from(text[..byte].chars().count() as i64),
                None => return Err(type_error("substring not found")),
            }
        }
        "splitlines" => Value::from(splitlines(text)),
        "split" => {
            let limit = args.get(1).and_then(Value::as_i64).unwrap_or(-1);
            let parts: Vec<String> = match arg(0) {
                None => {
                    let mut parts = Vec::new();
                    let mut rest = text.trim_start();
                    while !rest.is_empty() {
                        if limit >= 0 && parts.len() as i64 == limit { parts.push(rest.to_owned()); break; }
                        let end = rest.find(char::is_whitespace).unwrap_or(rest.len());
                        parts.push(rest[..end].to_owned());
                        rest = rest[end..].trim_start();
                    }
                    parts
                }
                Some(separator) if separator.is_empty() => return Err(type_error("empty separator")),
                Some(separator) if limit < 0 => text.split(separator.as_str()).map(str::to_owned).collect(),
                Some(separator) => text.splitn(limit as usize + 1, separator.as_str()).map(str::to_owned).collect(),
            };
            Value::from(parts)
        }
        "center" => {
            let total = width(0);
            if total <= count { Value::from(text) } else {
                let margin = total - count;
                // CPython puts the extra fill on the right unless the margin and width are both odd.
                let left = margin / 2 + (margin & total & 1);
                let c = fill(1).to_string();
                Value::from(format!("{}{text}{}", c.repeat(left), c.repeat(margin - left)))
            }
        }
        "format" => Value::from(str_format(text, args, kwargs.as_ref())?),
        "title" => Value::from(py_title(text)),
        _ => return Ok(None),
    };
    Ok(Some(result))
}

/// CPython `str.title`: every run of letters starts uppercase.
fn py_title(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut previous_cased = false;
    for ch in text.chars() {
        if ch.is_alphabetic() {
            if previous_cased { out.extend(ch.to_lowercase()); } else { out.extend(ch.to_uppercase()); }
            previous_cased = true;
        } else { out.push(ch); previous_cased = false; }
    }
    out
}

/// CPython `str.format` with the common format-spec mini-language.
fn str_format(template: &str, args: &[Value], kwargs: Option<&Kwargs>) -> Result<String, Error> {
    let chars: Vec<char> = template.chars().collect();
    let mut out = String::new();
    let mut auto = 0usize;
    let mut index = 0;
    while index < chars.len() {
        match chars[index] {
            '{' if chars.get(index + 1) == Some(&'{') => { out.push('{'); index += 2; }
            '}' if chars.get(index + 1) == Some(&'}') => { out.push('}'); index += 2; }
            '{' => {
                let start = index + 1;
                let mut depth = 1;
                index += 1;
                while index < chars.len() && depth > 0 {
                    match chars[index] { '{' => depth += 1, '}' => depth -= 1, _ => {} }
                    index += 1;
                }
                if depth > 0 { return Err(type_error("Single '{' encountered in format string")); }
                let field: String = chars[start..index - 1].iter().collect();
                let (field, spec) = field.split_once(':').map_or((field.as_str(), ""), |(f, s)| (f, s));
                let (field, conversion) = field.split_once('!').map_or((field, None), |(f, c)| (f, Some(c)));
                let name_end = field.find(['.', '[']).unwrap_or(field.len());
                let (name, mut rest) = field.split_at(name_end);
                let mut value = if name.is_empty() {
                    let value = args.get(auto).cloned().ok_or_else(|| type_error(format!("Replacement index {auto} out of range for positional args tuple")))?;
                    auto += 1;
                    value
                } else if let Ok(position) = name.parse::<usize>() {
                    args.get(position).cloned().ok_or_else(|| type_error(format!("Replacement index {position} out of range for positional args tuple")))?
                } else {
                    kwargs.and_then(|k| k.get::<Value>(name).ok()).ok_or_else(|| Error::new(ErrorKind::InvalidOperation, format!("KeyError: {}", py_repr_str(name))))?
                };
                while !rest.is_empty() {
                    if let Some(after) = rest.strip_prefix('.') {
                        let end = after.find(['.', '[']).unwrap_or(after.len());
                        value = value.get_attr(&after[..end])?;
                        rest = &after[end..];
                    } else if let Some(after) = rest.strip_prefix('[') {
                        let end = after.find(']').ok_or_else(|| type_error("Missing ']' in format string"))?;
                        let key = &after[..end];
                        value = match key.parse::<i64>() { Ok(i) => value.get_item(&Value::from(i))?, Err(_) => value.get_item(&Value::from(key))? };
                        rest = &after[end + 1..];
                    } else { break; }
                }
                let value = match conversion { Some("r") | Some("a") => Value::from(py_repr(&value)), Some("s") => Value::from(py_str(&value)), _ => value };
                out.push_str(&format_spec(&value, spec)?);
            }
            '}' => return Err(type_error("Single '}' encountered in format string")),
            c => { out.push(c); index += 1; }
        }
    }
    if let Some(kwargs) = kwargs { let _ = kwargs.assert_all_used(); }
    Ok(out)
}

/// Python `format(value, spec)` for strings and numbers.
fn format_spec(value: &Value, spec: &str) -> Result<String, Error> {
    if spec.is_empty() { return Ok(py_str(value)); }
    let chars: Vec<char> = spec.chars().collect();
    let mut i = 0;
    let (mut fill, mut align) = (' ', None);
    if chars.len() >= 2 && matches!(chars[1], '<' | '>' | '^' | '=') { fill = chars[0]; align = Some(chars[1]); i = 2; }
    else if matches!(chars.first(), Some('<' | '>' | '^' | '=')) { align = Some(chars[0]); i = 1; }
    let mut sign = '-';
    if let Some(&c @ ('+' | '-' | ' ')) = chars.get(i) { sign = c; i += 1; }
    let alternate = chars.get(i) == Some(&'#');
    if alternate { i += 1; }
    if chars.get(i) == Some(&'0') && align.is_none() { fill = '0'; align = Some('='); i += 1; }
    let start = i;
    while chars.get(i).is_some_and(char::is_ascii_digit) { i += 1; }
    let width: usize = chars[start..i].iter().collect::<String>().parse().unwrap_or(0);
    let grouping = match chars.get(i) { Some(&c @ (',' | '_')) => { i += 1; Some(c) } _ => None };
    let mut precision = None;
    if chars.get(i) == Some(&'.') {
        i += 1;
        let start = i;
        while chars.get(i).is_some_and(char::is_ascii_digit) { i += 1; }
        precision = chars[start..i].iter().collect::<String>().parse::<usize>().ok();
    }
    let kind = chars.get(i).copied();
    let numeric = value.kind() == ValueKind::Number || value.kind() == ValueKind::Bool;
    let (negative, mut body) = if numeric && kind != Some('s') {
        let f = f64::try_from(value.clone())?;
        let integer = (value.is_integer() || value.kind() == ValueKind::Bool) && !matches!(kind, Some('f' | 'F' | 'e' | 'E' | 'g' | 'G' | '%'));
        let digits = match kind {
            Some('d') | Some('n') | None if integer => i128::try_from(value.clone()).unwrap_or(f as i128).unsigned_abs().to_string(),
            Some('x') => format!("{}{:x}", if alternate { "0x" } else { "" }, i128::try_from(value.clone()).unwrap_or(0).unsigned_abs()),
            Some('X') => format!("{}{:X}", if alternate { "0X" } else { "" }, i128::try_from(value.clone()).unwrap_or(0).unsigned_abs()),
            Some('o') => format!("{}{:o}", if alternate { "0o" } else { "" }, i128::try_from(value.clone()).unwrap_or(0).unsigned_abs()),
            Some('b') => format!("{}{:b}", if alternate { "0b" } else { "" }, i128::try_from(value.clone()).unwrap_or(0).unsigned_abs()),
            Some('%') => format!("{:.*}%", precision.unwrap_or(6), f.abs() * 100.0),
            Some('f' | 'F') => format!("{:.*}", precision.unwrap_or(6), f.abs()),
            Some('e' | 'E') => {
                let text = format!("{:.*e}", precision.unwrap_or(6), f.abs());
                let (m, e) = text.split_once('e').expect("exponent");
                let e: i32 = e.parse().expect("exponent");
                let body = format!("{m}e{}{:02}", if e < 0 { '-' } else { '+' }, e.abs());
                if kind == Some('E') { body.to_uppercase() } else { body }
            }
            Some('g' | 'G') | None => match precision {
                Some(p) => convert(&Value::from(f.abs()), kind.unwrap_or('g'), "", Some(p))?,
                None => py_float(f.abs()),
            },
            Some(other) => return Err(type_error(format!("Unknown format code '{other}' for number"))),
        };
        (f.is_sign_negative() && f != 0.0, digits)
    } else {
        let text = py_str(value);
        (false, match precision { Some(p) => text.chars().take(p).collect(), None => text })
    };
    if let Some(separator) = grouping {
        let (integer, fraction) = body.split_once('.').map_or((body.as_str(), None), |(a, b)| (a, Some(b)));
        let digits: Vec<char> = integer.chars().collect();
        let mut grouped = String::new();
        for (index, digit) in digits.iter().enumerate() {
            if index > 0 && (digits.len() - index) % 3 == 0 { grouped.push(separator); }
            grouped.push(*digit);
        }
        body = match fraction { Some(f) => format!("{grouped}.{f}"), None => grouped };
    }
    let sign_text = if !numeric { "" } else if negative { "-" } else { match sign { '+' => "+", ' ' => " ", _ => "" } };
    let length = sign_text.chars().count() + body.chars().count();
    let padding = width.saturating_sub(length);
    let pad = |n: usize| fill.to_string().repeat(n);
    Ok(match align.unwrap_or(if numeric { '>' } else { '<' }) {
        '<' => format!("{sign_text}{body}{}", pad(padding)),
        '^' => format!("{}{sign_text}{body}{}", pad(padding / 2), pad(padding - padding / 2)),
        '=' => format!("{sign_text}{}{body}", pad(padding)),
        _ => format!("{}{sign_text}{body}", pad(padding)),
    })
}

fn title_like(text: &str) -> bool { text.chars().any(char::is_alphabetic) && py_title(text) == text }

#[cfg(test)]
mod tests {
    use minijinja::{Environment, context};

    fn render(source: &str) -> Result<String, minijinja::Error> {
        let mut env = Environment::new();
        super::register(&mut env);
        env.render_str(source, context! {
            items => vec![context! { price => 1500.5, n => context! { v => 2 } }, context! { price => 800, n => context! { v => 3 } }],
            ints => vec![1, 2, 3], lists => vec![vec![1], vec![2]],
        })
    }

    #[test]
    fn sum_matches_jinja2() {
        // Expected values from jinja2.Environment().from_string(...).render(...).
        assert_eq!(render("{{ items|sum(attribute='price') }}").unwrap(), "2300.5");
        assert_eq!(render("{{ items|sum(attribute='n.v') }}").unwrap(), "5");
        assert_eq!(render("{{ ints|sum }}|{{ ints|sum(start=10) }}").unwrap(), "6|16");
        assert_eq!(render("{{ lists|sum(start=[]) }}").unwrap(), "[1, 2]");
        assert!(render("{{ ints|sum(other=1) }}").is_err());
    }

    #[test]
    // 3.14159 is a sample with six significant digits, not an approximation of PI.
    #[allow(clippy::approx_constant)]
    fn python_float_repr() {
        // repr(x) in CPython.
        for (value, expected) in [(1e20, "1e+20"), (1e16, "1e+16"), (1e15, "1000000000000000.0"), (0.0001, "0.0001"),
            (0.00001, "1e-05"), (3.14159, "3.14159"), (1.5e-7, "1.5e-07"), (-2.5, "-2.5"), (0.1 + 0.2, "0.30000000000000004")] {
            assert_eq!(super::py_float(value), expected);
        }
    }

    #[test]
    fn python_string_repr() {
        assert_eq!(super::py_repr_str("a'b"), "\"a'b\"");
        assert_eq!(super::py_repr_str("a'b\""), "'a\\'b\"'");
        assert_eq!(super::py_repr_str("ç\n\u{1}"), "'ç\\n\\x01'");
    }
}
