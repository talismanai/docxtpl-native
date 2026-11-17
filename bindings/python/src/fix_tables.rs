//! Table-grid repair on the caller-visible lxml document tree.
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

/// Rendered XML ready for lxml's recover mode. That mode drops entity
/// references after the first error and stray `&`/`<`, so ill-formed text is
/// repaired first (`docxtplrs::recover`) and nothing is lost: an intentional
/// divergence from docxtpl, which parses the raw string. Well-formed input and
/// anything that is not a Python `str` are returned as they are.
pub(crate) fn repair_text<'py>(xml: &Bound<'py, PyAny>) -> Bound<'py, PyAny> {
    match xml.extract::<String>() {
        Ok(text) if !docxtplrs::recover::is_well_formed(&text) => {
            pyo3::types::PyString::new(xml.py(), &docxtplrs::recover::recover(&text)).into_any()
        }
        _ => xml.clone(),
    }
}

#[pyfunction]
pub(crate) fn fix_exposed_tables<'py>(xml: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = xml.py();
    let xml = &repair_text(xml);
    let etree = super::LXML_ETREE_MODULE.get_or_try_init(py, || {
        super::find_loaded_module(py, pyo3::intern!(py, "lxml.etree"))?
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("lxml is required for the exposed document model"))
    })?.bind(py);
    let options = PyDict::new(py);
    options.set_item("recover", true)?;
    let parser = etree.getattr(pyo3::intern!(py, "XMLParser"))?.call((), Some(&options))?;
    let options = PyDict::new(py);
    options.set_item("parser", parser)?;
    let tree = etree.getattr(pyo3::intern!(py, "fromstring"))?.call((xml,), Some(&options))?;
    let uri: String = tree.getattr(pyo3::intern!(py, "nsmap"))?.get_item("w")?.extract()?;
    let tag = |name: &str| format!("{{{uri}}}{name}");
    let (tbl, grid, col, tr, tc, pr, span, width, val) = (tag("tbl"), tag("tblGrid"), tag("gridCol"), tag("tr"), tag("tc"), tag("tcPr"), tag("gridSpan"), tag("w"), tag("val"));
    let builtins = py.import("builtins")?;
    let float = builtins.getattr(pyo3::intern!(py, "float"))?;
    let int = builtins.getattr(pyo3::intern!(py, "int"))?;
    let as_float = |value: &Bound<'py, PyAny>| -> PyResult<f64> { float.call1((value,))?.extract() };
    let int_string = |value: f64| -> PyResult<Bound<'py, PyAny>> { Ok(int.call1((value,))?.str()?.into_any()) };
    for table in tree.call_method1(pyo3::intern!(py, "iter"), (&tbl,))?.try_iter()? {
        let table = table?;
        let table_grid = table.call_method1(pyo3::intern!(py, "find"), (&grid,))?;
        let columns = table_grid.call_method1(pyo3::intern!(py, "findall"), (&col,))?.cast_into::<PyList>()?;
        let mut add = 0usize;
        for row in table.call_method1(pyo3::intern!(py, "iter"), (&tr,))?.try_iter()? {
            let count = row?.call_method1(pyo3::intern!(py, "findall"), (&tc,))?.len()?;
            if columns.len() + add < count { add = count - columns.len(); }
        }
        if add > 0 {
            let mut total_width = 0.0;
            for column in columns.iter() {
                let first = column.call_method1(pyo3::intern!(py, "get"), (&width,))?;
                if !first.is_none() { total_width += as_float(&column.call_method1(pyo3::intern!(py, "get"), (&width,))?)?; }
            }
            if total_width > 0.0 {
                let old_average = total_width / columns.len() as f64;
                let new_average = total_width / (columns.len() + add) as f64;
                for column in columns.iter() {
                    let old = as_float(&column.call_method1(pyo3::intern!(py, "get"), (&width,))?)?;
                    column.call_method1(pyo3::intern!(py, "set"), (&width, int_string(old * new_average / old_average)?))?;
                }
                for _ in 0..add {
                    let attributes = PyDict::new(py);
                    attributes.set_item(&width, int_string(new_average)?)?;
                    etree.call_method1(pyo3::intern!(py, "SubElement"), (&table_grid, &col, attributes))?;
                }
            }
        }
        let columns = table_grid.call_method1(pyo3::intern!(py, "findall"), (&col,))?.cast_into::<PyList>()?;
        let mut max_cells = 0usize;
        for row in table.call_method1(pyo3::intern!(py, "iter"), (&tr,))?.try_iter()? {
            let cells = row?.call_method1(pyo3::intern!(py, "findall"), (&tc,))?;
            let mut count = int.call1((0,))?;
            for cell in cells.try_iter()? {
                let cell = cell?;
                let cell_pr = cell.call_method1(pyo3::intern!(py, "find"), (&pr,))?;
                let grid_span = if cell_pr.is_none() { py.None().into_bound(py) } else { cell_pr.call_method1(pyo3::intern!(py, "find"), (&span,))? };
                let n = if grid_span.is_none() { int.call1((1,))? } else { int.call1((grid_span.call_method1(pyo3::intern!(py, "get"), (&val,))?,))? };
                count = count.call_method1(pyo3::intern!(py, "__add__"), (n,))?;
            }
            if count.gt(max_cells)? {
                // Values larger than the number of grid columns require no removal.
                max_cells = if count.ge(columns.len())? { columns.len() } else { count.extract()? };
            }
        }
        let remove = columns.len() - max_cells;
        if remove > 0 {
            let mut removed_width = 0.0;
            for column in columns.iter().skip(columns.len() - remove) {
                removed_width += as_float(&column.call_method1(pyo3::intern!(py, "get"), (&width,))?)?;
                table_grid.call_method1(pyo3::intern!(py, "remove"), (column,))?;
            }
            let remaining = table_grid.call_method1(pyo3::intern!(py, "findall"), (&col,))?.cast_into::<PyList>()?;
            let extra = if remaining.is_empty() { 0.0 } else {
                // Python int() rejects non-finite results and may retain arbitrarily large values.
                int.call1((removed_width / remaining.len() as f64,))?.extract::<f64>()?
            };
            for column in remaining.iter() {
                let value = as_float(&column.call_method1(pyo3::intern!(py, "get"), (&width,))?)?;
                column.call_method1(pyo3::intern!(py, "set"), (&width, int_string(value + extra)?))?;
            }
        }
    }
    Ok(tree)
}
