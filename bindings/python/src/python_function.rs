//! Identify an unchanged standard function without bypassing user overrides.
use pyo3::prelude::*;
use pyo3::types::{PyFunction, PyModule};

/// Identity and code snapshot for a dependency algorithm replaced in Rust.
/// A changed function is a user callback, including changes made in place.
pub(crate) struct StandardFunction {
    function: Py<PyAny>,
    code: Py<PyAny>,
    module: Py<PyModule>,
    name: String,
}

impl StandardFunction {
    pub(crate) fn capture(module: &Bound<'_, PyModule>, name: &str) -> PyResult<Self> {
        let function = module.getattr(name)?;
        Self::capture_value(module, name, &function)
    }

    pub(crate) fn capture_value(
        module: &Bound<'_, PyModule>,
        name: &str,
        function: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        Ok(Self {
            code: function.getattr("__code__")?.unbind(),
            function: function.clone().unbind(),
            module: module.clone().unbind(),
            name: name.into(),
        })
    }

    pub(crate) fn matches(&self, function: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = function.py();
        is_standard_function(
            function,
            self.function.bind(py),
            self.code.bind(py),
            self.module.bind(py),
            &self.name,
        )
    }

    /// A captured function whose code was replaced is a user callback, even
    /// when its module and function-name metadata still look standard.
    pub(crate) fn has_replaced_code(&self, function: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = function.py();
        if !function.is(self.function.bind(py)) {
            return Ok(false);
        }
        Ok(!function.getattr("__code__")?.is(self.code.bind(py)))
    }
}

/// Bind an unchanged fixed Python signature using live defaults and argument
/// names. Object comparison stays in the binding; no Python function executes.
pub(crate) fn bind_fixed<'py>(
    function: &Bound<'py, PyAny>,
    args: &Bound<'py, pyo3::types::PyTuple>,
    kwargs: &Bound<'py, pyo3::types::PyDict>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    use pyo3::types::PyTuple;
    let code = function.getattr("__code__")?;
    let count: usize = code.getattr("co_argcount")?.extract()?;
    let names = code.getattr("co_varnames")?.cast_into::<PyTuple>()?;
    let parameters = names.iter().take(count).collect::<Vec<_>>();
    let mut values: Vec<_> = (0..count).map(|i| args.get_item(i).ok()).collect();
    for pair in crate::python_keywords::snapshot(kwargs)?.iter() {
        let key = pair.get_item(0)?;
        let Some(index) = crate::python_keywords::parameter_index(&key, &parameters)? else {
            return Err(crate::python_keywords::argument_error(
                function,
                &key,
                "() got an unexpected keyword argument '",
            )?);
        };
        if values[index].is_some() {
            return Err(crate::python_keywords::argument_error(
                function,
                &key,
                "() got multiple values for argument '",
            )?);
        }
        values[index] = Some(pair.get_item(1)?);
    }
    let defaults = function.getattr("__defaults__")?;
    let defaults = if defaults.is_none() {
        None
    } else {
        Some(defaults.cast::<PyTuple>()?)
    };
    let ndefaults = defaults.map_or(0, |v| v.len());
    if args.len() > count {
        let signature = if ndefaults == 0 {
            count.to_string()
        } else {
            format!("from {} to {count}", count as isize - ndefaults as isize)
        };
        let suffix = if count == 1 && ndefaults == 0 {
            ""
        } else {
            "s"
        };
        let verb = if args.len() == 1 { "was" } else { "were" };
        return Err(crate::python_keywords::function_error(
            function,
            &format!(
                "() takes {signature} positional argument{suffix} but {} {verb} given",
                args.len()
            ),
        )?);
    }
    if let Some(defaults) = defaults {
        for i in 0..count.min(ndefaults) {
            let slot = count - count.min(ndefaults) + i;
            if values[slot].is_none() {
                values[slot] = Some(defaults.get_item(ndefaults - count.min(ndefaults) + i)?);
            }
        }
    }
    let missing = values
        .iter()
        .enumerate()
        .filter(|(_, v)| v.is_none())
        .map(|(i, _)| {
            parameters[i]
                .str()
                .map(|v| format!("'{}'", v.to_string_lossy()))
        })
        .collect::<PyResult<Vec<_>>>()?;
    if !missing.is_empty() {
        let names = match missing.len() {
            1 => missing[0].clone(),
            2 => missing.join(" and "),
            _ => format!(
                "{}, and {}",
                missing[..missing.len() - 1].join(", "),
                missing.last().unwrap()
            ),
        };
        return Err(crate::python_keywords::function_error(
            function,
            &format!(
                "() missing {} required positional argument{}: {names}",
                missing.len(),
                if missing.len() == 1 { "" } else { "s" }
            ),
        )?);
    }
    Ok(values.into_iter().map(Option::unwrap).collect())
}

/// Read live guard metadata without invoking the Python import hook per call.
pub(crate) fn binding(py: Python<'_>) -> PyResult<Bound<'_, PyModule>> {
    match crate::find_loaded_module(py, pyo3::intern!(py, "docxtpl_native.bridge"))? {
        Some(module) => Ok(module.into_bound(py).cast_into::<PyModule>()?),
        None => py.import("docxtpl_native.bridge"),
    }
}

pub(crate) fn is_standard_function(
    function: &Bound<'_, PyAny>,
    original: &Bound<'_, PyAny>,
    original_code: &Bound<'_, PyAny>,
    module: &Bound<'_, PyModule>,
    function_name: &str,
) -> PyResult<bool> {
    if !function.is(original) || !function.is_exact_instance_of::<PyFunction>() {
        return Ok(false);
    }
    let py = function.py();
    let code = function.getattr(pyo3::intern!(py, "__code__"))?;
    Ok(code.is(original_code)
        && function
            .getattr(pyo3::intern!(py, "__globals__"))?
            .is(&module.dict())
        && code
            .getattr(pyo3::intern!(py, "co_filename"))?
            .eq(module.getattr(pyo3::intern!(py, "__file__"))?)?
        && code
            .getattr(pyo3::intern!(py, "co_name"))?
            .eq(function_name)?)
}
