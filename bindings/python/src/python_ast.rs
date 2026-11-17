//! Lower extension-generated Jinja nodes into the native compiler AST.
//!
//! This adapter owns every string used by instructions. The Python parser is
//! still the frontend for this route; neither Python code generation nor a
//! Python template renderer is invoked here.
use crate::python_bridge::Bridge;
use minijinja::machinery::{ast as a, Span, TemplateAst};
use minijinja::value::Object;
use minijinja::{Error, ErrorKind, State, Value};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use std::collections::BTreeMap;
use std::sync::Arc;

/// Rust strings cannot contain isolated UTF-16 surrogates. Keep such source
/// literals as foreign values through the existing AST adapter. Selection is
/// conservative; the original-language parser still validates the grammar.
#[pyfunction]
pub(crate) fn requires_foreign_literals(source: &str) -> bool {
    let bytes = source.as_bytes();
    bytes.windows(6).any(|s| s[0] == b'\\' && s[1] == b'u'
        && matches!(s[2], b'd' | b'D') && s[3].is_ascii_hexdigit() && s[3] >= b'8'
        && s[4..].iter().all(u8::is_ascii_hexdigit))
        || bytes.windows(10).any(|s| s[..6] == *b"\\U0000"
            && matches!(s[6], b'd' | b'D') && s[7].is_ascii_hexdigit() && s[7] >= b'8'
            && s[8..].iter().all(u8::is_ascii_hexdigit))
}

enum Field {
    Node(Box<Node>),
    List(Vec<Field>),
    Text(String),
    Bool(bool),
    None,
    Constant(Value),
}

struct Node {
    kind: String,
    fields: BTreeMap<String, Field>,
    span: Span,
    constant_id: u32,
    bridge: Arc<Bridge>,
}

fn invalid(message: impl Into<String>) -> Error {
    Error::new(ErrorKind::InvalidOperation, message.into())
}

impl Field {
    fn node(&self) -> Result<&Node, Error> {
        if let Self::Node(node) = self { Ok(node) } else { Err(invalid("expected an AST node")) }
    }
    fn list(&self) -> Result<&[Field], Error> {
        if let Self::List(items) = self { Ok(items) } else { Err(invalid("expected an AST node list")) }
    }
}

impl Node {
    fn capture(value: &Bound<'_, PyAny>, bridge: &Arc<Bridge>, originals: &Bound<'_, PyDict>, depth: usize) -> PyResult<Self> {
        if depth > 150 {
            return Err(pyo3::exceptions::PyRecursionError::new_err("extension syntax tree is too deeply nested"));
        }
        let kind = value.get_type().name()?.to_string();
        let index = u32::try_from(originals.len()).map_err(|_| pyo3::exceptions::PyOverflowError::new_err("syntax tree too large"))?;
        originals.set_item(index, value)?;
        let line = value.getattr("lineno")?.extract::<Option<u16>>()?.unwrap_or(1);
        let mut fields = BTreeMap::new();
        for name in value.getattr("fields")?.extract::<Vec<String>>()? {
            let field = value.getattr(name.as_str())?;
            let captured = if kind == "Const" && name == "value" {
                Field::Constant(bridge.value(&field)?)
            } else {
                Self::capture_field(&field, bridge, originals, depth + 1)?
            };
            fields.insert(name, captured);
        }
        // Jinja AST nodes have lines but no columns or byte offsets. Preserve
        // that information without inventing a location from the node index.
        Ok(Self { kind, fields, constant_id: index,
            span: Span { start_line: line, end_line: line, ..Span::default() }, bridge: bridge.clone() })
    }

    fn capture_field(value: &Bound<'_, PyAny>, bridge: &Arc<Bridge>, originals: &Bound<'_, PyDict>, depth: usize) -> PyResult<Field> {
        if depth > 150 {
            return Err(pyo3::exceptions::PyRecursionError::new_err("extension syntax tree is too deeply nested"));
        }
        if value.is_none() { return Ok(Field::None); }
        if value.is_instance_of::<PyString>() { return Ok(Field::Text(value.extract()?)); }
        if value.is_instance_of::<pyo3::types::PyBool>() { return Ok(Field::Bool(value.extract()?)); }
        if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
            return value.try_iter()?.map(|item| Self::capture_field(&item?, bridge, originals, depth + 1))
                .collect::<PyResult<Vec<_>>>().map(Field::List);
        }
        Ok(Field::Node(Box::new(Self::capture(value, bridge, originals, depth)?)))
    }

    fn field(&self, name: &str) -> Result<&Field, Error> {
        self.fields.get(name).ok_or_else(|| invalid(format!("missing {}.{name}", self.kind)))
    }
    fn text(&self, name: &str) -> Result<&str, Error> {
        match self.field(name)? { Field::Text(text) => Ok(text), _ => Err(invalid("expected AST string")) }
    }
    fn boolean(&self, name: &str) -> Result<bool, Error> {
        match self.field(name)? { Field::Bool(value) => Ok(*value), _ => Err(invalid("expected AST boolean")) }
    }
    fn child(&self, name: &str) -> Result<&Node, Error> { self.field(name)?.node() }
    fn expr_field(&self, name: &str) -> Result<a::Expr<'_>, Error> { self.child(name)?.expr() }
    fn optional(&self, name: &str) -> Result<Option<a::Expr<'_>>, Error> {
        match self.field(name)? { Field::None => Ok(None), field => field.node()?.expr().map(Some) }
    }
    fn expressions(&self, name: &str) -> Result<Vec<a::Expr<'_>>, Error> {
        self.field(name)?.list()?.iter().map(|field| field.node()?.expr()).collect()
    }
    fn statements(&self, name: &str) -> Result<Vec<a::Stmt<'_>>, Error> {
        let mut result = Vec::new();
        for field in self.field(name)?.list()? { field.node()?.statements_into(&mut result)?; }
        Ok(result)
    }
    fn sp<T>(&self, value: T) -> a::Spanned<T> { a::Spanned::new(value, self.span) }
    fn unsupported(&self) -> Error { invalid(format!("native AST adapter does not support {} yet", self.kind)) }

    fn validate_control_flow(&self, in_loop: bool) -> Result<(), Error> {
        if matches!(self.kind.as_str(), "Break" | "Continue") && !in_loop {
            return Err(invalid("extension AST loop control outside a loop"));
        }
        let in_loop = in_loop && !matches!(self.kind.as_str(), "Macro" | "CallBlock" | "Block");
        fn visit(field: &Field, in_loop: bool) -> Result<(), Error> {
            match field {
                Field::Node(node) => node.validate_control_flow(in_loop),
                Field::List(fields) => { for field in fields { visit(field, in_loop)?; } Ok(()) },
                _ => Ok(()),
            }
        }
        for (name, field) in &self.fields {
            visit(field, in_loop || (self.kind == "For" && name == "body"))?;
        }
        Ok(())
    }

    fn args(&self) -> Result<Vec<a::CallArg<'_>>, Error> {
        let mut result = self.expressions("args")?.into_iter().map(a::CallArg::Pos).collect::<Vec<_>>();
        for item in self.field("kwargs")?.list()? {
            let item = item.node()?;
            result.push(a::CallArg::Kwarg(item.text("key")?, item.expr_field("value")?));
        }
        if let Some(expr) = self.optional("dyn_args")? { result.push(a::CallArg::PosSplat(expr)); }
        if let Some(expr) = self.optional("dyn_kwargs")? { result.push(a::CallArg::KwargSplat(expr)); }
        Ok(result)
    }

    fn expr(&self) -> Result<a::Expr<'_>, Error> {
        use a::{BinOpKind as B, CompareOpKind as C, Expr as E};
        Ok(match self.kind.as_str() {
            "Name" => E::Var(self.sp(a::Var { id: self.text("name")? })),
            "Const" => match self.field("value")? {
                Field::Constant(value) => E::Const(self.sp(a::Const { value: value.clone() })),
                _ => return Err(invalid("invalid AST constant")),
            },
            "Tuple" | "List" => E::List(self.sp(a::List { is_tuple: self.kind == "Tuple", items: self.expressions("items")? })),
            "Dict" => {
                let mut keys = Vec::new(); let mut values = Vec::new();
                for field in self.field("items")?.list()? {
                    let pair = field.node()?; keys.push(pair.expr_field("key")?); values.push(pair.expr_field("value")?);
                }
                E::Map(self.sp(a::Map { keys, values }))
            },
            "Neg" | "Pos" | "Not" => E::UnaryOp(self.sp(a::UnaryOp {
                op: match self.kind.as_str() { "Neg" => a::UnaryOpKind::Neg, "Pos" => a::UnaryOpKind::Pos, _ => a::UnaryOpKind::Not },
                expr: self.expr_field("node")?,
            })),
            "Add" | "Sub" | "Mul" | "Div" | "FloorDiv" | "Mod" | "Pow" | "And" | "Or" => E::BinOp(self.sp(a::BinOp {
                op: match self.kind.as_str() { "Add" => B::Add, "Sub" => B::Sub, "Mul" => B::Mul, "Div" => B::Div,
                    "FloorDiv" => B::FloorDiv, "Mod" => B::Rem, "Pow" => B::Pow, "And" => B::ScAnd, _ => B::ScOr },
                left: self.expr_field("left")?, right: self.expr_field("right")?,
            })),
            "Concat" => E::Concat(self.sp(a::Concat { values: self.expressions("nodes")? })),
            "Compare" => {
                let mut ops = Vec::new();
                for field in self.field("ops")?.list()? {
                    let item = field.node()?;
                    let op = match item.text("op")? { "eq" => C::Eq, "ne" => C::Ne, "lt" => C::Lt, "lteq" => C::Lte,
                        "gt" => C::Gt, "gteq" => C::Gte, "in" => C::In, "notin" => C::NotIn, _ => return Err(item.unsupported()) };
                    ops.push(a::CompareOp { op, expr: item.expr_field("expr")? });
                }
                E::Compare(self.sp(a::Compare { expr: self.expr_field("expr")?, ops }))
            },
            "CondExpr" => E::IfExpr(self.sp(a::IfExpr { test_expr: self.expr_field("test")?,
                true_expr: self.expr_field("expr1")?, false_expr: self.optional("expr2")? })),
            "Getattr" => E::GetAttr(self.sp(a::GetAttr { expr: self.expr_field("node")?, name: self.text("attr")? })),
            "NSRef" => E::GetAttr(self.sp(a::GetAttr { expr: E::Var(self.sp(a::Var { id: self.text("name")? })), name: self.text("attr")? })),
            "Getitem" => {
                let arg = self.child("arg")?;
                if arg.kind == "Slice" {
                    E::Slice(self.sp(a::Slice { expr: self.expr_field("node")?, start: arg.optional("start")?, stop: arg.optional("stop")?, step: arg.optional("step")? }))
                } else { E::GetItem(self.sp(a::GetItem { expr: self.expr_field("node")?, subscript_expr: arg.expr()? })) }
            },
            "Call" => E::Call(self.sp(a::Call { expr: self.expr_field("node")?, args: self.args()? })),
            "Filter" => E::Filter(self.sp(a::Filter { name: self.text("name")?, expr: self.optional("node")?, args: self.args()? })),
            "Test" => E::Test(self.sp(a::Test { name: self.text("name")?, expr: self.expr_field("node")?, args: self.args()? })),
            "ExtensionAttribute" | "EnvironmentAttribute" => {
                let lookup = AttributeLookup { bridge: self.bridge.clone(), attribute: self.text("name")?.to_owned(),
                    extension: if self.kind == "ExtensionAttribute" { Some(self.text("identifier")?.to_owned()) } else { None } };
                E::Call(self.sp(a::Call { expr: E::Const(self.sp(a::Const { value: Value::from_object(lookup) })), args: Vec::new() }))
            },
            _ => return Err(self.unsupported()),
        })
    }

    fn assignment(&self) -> Result<a::Expr<'_>, Error> {
        match self.kind.as_str() {
            "Name" | "NSRef" => self.expr(),
            "Tuple" => {
                let items = self.field("items")?.list()?.iter().map(|item| item.node()?.assignment()).collect::<Result<_, _>>()?;
                Ok(a::Expr::List(self.sp(a::List { is_tuple: true, items })))
            },
            _ => Err(invalid("invalid assignment target in extension AST")),
        }
    }

    fn statements_into<'a>(&'a self, result: &mut Vec<a::Stmt<'a>>) -> Result<(), Error> {
        use a::Stmt as S;
        let statement = match self.kind.as_str() {
            "Output" => {
                for field in self.field("nodes")?.list()? {
                    let node = field.node()?;
                    result.push(if node.kind == "TemplateData" {
                        S::EmitRaw(node.sp(a::EmitRaw { raw: node.text("data")? }))
                    } else { S::EmitExpr(node.sp(a::EmitExpr { expr: node.expr()?, constant_id: Some(node.constant_id) })) });
                }
                return Ok(());
            },
            "Assign" => S::Set(self.sp(a::Set { target: self.child("target")?.assignment()?, expr: self.expr_field("node")? })),
            "AssignBlock" => S::SetBlock(self.sp(a::SetBlock { target: self.child("target")?.assignment()?, filter: self.optional("filter")?, body: self.statements("body")? })),
            "If" => {
                let mut otherwise = self.statements("else_")?;
                for field in self.field("elif_")?.list()?.iter().rev() {
                    let item = field.node()?;
                    otherwise = vec![S::IfCond(item.sp(a::IfCond { expr: item.expr_field("test")?, true_body: item.statements("body")?, false_body: otherwise }))];
                }
                S::IfCond(self.sp(a::IfCond { expr: self.expr_field("test")?, true_body: self.statements("body")?, false_body: otherwise }))
            },
            "For" => S::ForLoop(self.sp(a::ForLoop { target: self.child("target")?.assignment()?, iter: self.expr_field("iter")?,
                filter_expr: self.optional("test")?, recursive: self.boolean("recursive")?, body: self.statements("body")?, else_body: self.statements("else_")? })),
            "With" => {
                let targets = self.field("targets")?.list()?; let values = self.field("values")?.list()?;
                if targets.len() != values.len() { return Err(invalid("with targets and values differ in length")); }
                let assignments = targets.iter().zip(values).map(|(target, value)| Ok((target.node()?.assignment()?, value.node()?.expr()?))).collect::<Result<_, Error>>()?;
                S::WithBlock(self.sp(a::WithBlock { assignments, body: self.statements("body")? }))
            },
            "Scope" => S::WithBlock(self.sp(a::WithBlock { assignments: Vec::new(), body: self.statements("body")? })),
            "Block" => {
                S::Block(self.sp(a::Block { name: self.text("name")?, required: self.boolean("required")?, scoped: self.boolean("scoped")?, body: self.statements("body")? }))
            },
            "Import" => S::Import(self.sp(a::Import {
                expr: self.expr_field("template")?,
                name: a::Expr::Var(self.sp(a::Var { id: self.text("target")? })),
                with_context: self.boolean("with_context")?,
            })),
            "FromImport" => {
                let mut names = Vec::new();
                for field in self.field("names")?.list()? {
                    let (name, alias) = match field {
                        Field::Text(name) => (name.as_str(), None),
                        Field::List(pair) if pair.len() == 2 => match (&pair[0], &pair[1]) {
                            (Field::Text(name), Field::Text(alias)) => (name.as_str(), Some(alias.as_str())),
                            _ => return Err(invalid("invalid imported name and alias")),
                        },
                        _ => return Err(invalid("invalid imported name")),
                    };
                    let variable = |id| a::Expr::Var(self.sp(a::Var { id }));
                    names.push((variable(name), alias.map(variable)));
                }
                S::FromImport(self.sp(a::FromImport { expr: self.expr_field("template")?,
                    names, with_context: self.boolean("with_context")? }))
            },
            "Extends" => S::Extends(self.sp(a::Extends { name: self.expr_field("template")? })),
            "Include" => S::Include(self.sp(a::Include { name: self.expr_field("template")?, ignore_missing: self.boolean("ignore_missing")?, with_context: self.boolean("with_context")? })),
            "Macro" => {
                let args = self.expressions("args")?; let defaults = self.expressions("defaults")?;
                if defaults.len() > args.len() || !args.iter().all(|arg| matches!(arg, a::Expr::Var(_))) {
                    return Err(invalid("invalid macro arguments in extension AST"));
                }
                S::Macro(self.sp(a::Macro { name: self.text("name")?, args, defaults, body: self.statements("body")? }))
            },
            "FilterBlock" => S::FilterBlock(self.sp(a::FilterBlock { filter: self.expr_field("filter")?, body: self.statements("body")? })),
            "ScopedEvalContextModifier" => {
                let options = self.field("options")?.list()?;
                if options.len() != 1 || options[0].node()?.text("key")? != "autoescape" { return Err(self.unsupported()); }
                S::AutoEscape(self.sp(a::AutoEscape { enabled: options[0].node()?.expr_field("value")?, body: self.statements("body")? }))
            },
            "CallBlock" => {
                let a::Expr::Call(call) = self.expr_field("call")? else { return Err(invalid("call block requires a call expression")); };
                let args = self.expressions("args")?; let defaults = self.expressions("defaults")?;
                if defaults.len() > args.len() || !args.iter().all(|arg| matches!(arg, a::Expr::Var(_))) {
                    return Err(invalid("invalid call block arguments in extension AST"));
                }
                S::CallBlock(self.sp(a::CallBlock { call, macro_decl: self.sp(a::Macro { name: "caller", args, defaults, body: self.statements("body")? }) }))
            },
            "Break" => S::Break(self.sp(a::Break)),
            "Continue" => S::Continue(self.sp(a::Continue)),
            "ExprStmt" => match self.expr_field("node")? { a::Expr::Call(call) => S::Do(self.sp(a::Do { call })), _ => return Err(self.unsupported()) },
            _ => return Err(self.unsupported()),
        };
        result.push(statement);
        Ok(())
    }
}

impl TemplateAst for Node {
    fn lower(&self) -> Result<a::Stmt<'_>, Error> {
        if self.kind != "Template" { return Err(invalid("extension AST root must be Template")); }
        self.validate_control_flow(false)?;
        Ok(a::Stmt::Template(self.sp(a::Template { children: self.statements("body")? })))
    }
}

struct AttributeLookup { bridge: Arc<Bridge>, extension: Option<String>, attribute: String }
impl std::fmt::Debug for AttributeLookup {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("ExtensionAttributeLookup") }
}
impl Object for AttributeLookup {
    fn call(self: &Arc<Self>, _state: &State, _args: &[Value]) -> Result<Value, Error> {
        Python::attach(|py| -> PyResult<Value> {
            let environment = self.bridge.config.bind(py).getattr("environment")?;
            let owner = match &self.extension { Some(id) => environment.getattr("extensions")?.get_item(id)?, None => environment };
            self.bridge.value(&owner.getattr(self.attribute.as_str())?)
        }).map_err(|error| self.bridge.remember(error))
    }
}

pub fn take_tree(bridge: &Arc<Bridge>, name: &str) -> Result<Option<Box<dyn TemplateAst>>, Error> {
    Python::attach(|py| -> PyResult<Option<Box<dyn TemplateAst>>> {
        let config = bridge.config.bind(py);
        let tree = config.getattr("_compile_trees")?.call_method1("pop", (name, py.None()))?;
        if tree.is_none() { return Ok(None); }
        let originals = PyDict::new(py);
        let node = Node::capture(&tree, bridge, &originals, 0)?;
        config.getattr("_compile_ast_nodes")?.set_item(name, originals)?;
        Ok(Some(Box::new(node)))
    }).map_err(|error| bridge.remember(error))
}
