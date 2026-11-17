use std::cell::RefCell;
use std::collections::BTreeMap;
use std::mem;
use std::sync::Arc;
use crate::error::Error;
use crate::template::{ArithmeticCodegenMode, CallPolicyFactory, ConstantArgument, ConstantArgumentFunc, ConstantArithmeticFunc, ConstantOutputFunc, JinjaArithmeticPolicy, JinjaConstAttributeLookup, JinjaConstAttributeRelease, OperatorCallFactory};

use crate::compiler::ast;
use crate::compiler::instructions::{
    CompareOp, Instruction, Instructions, LocalId, LOOP_FLAG_FILTERED_INPUT, LOOP_FLAG_RECURSIVE, LOOP_FLAG_WITH_LOOP_VAR,
    MAX_LOCALS,
};
use crate::compiler::tokens::Span;
use crate::output::CaptureMode;
use crate::value::ops::neg;
use crate::value::{Kwargs, UndefinedType, Value, ValueMap, ValueRepr};

// Collect variable reads in Jinja's symbol-visitor order. Calls, filters,
// tests and slices need their own callback and dispatch preparation.
fn collect_argument_names<'a>(expr: &ast::Expr<'a>, names: &mut Vec<&'a str>) -> bool {
    let mut visit = |expr: &ast::Expr<'a>| collect_argument_names(expr, names);
    match expr {
        ast::Expr::Var(var) => {
            if var.id == "loop" { return false; }
            if !names.contains(&var.id) { names.push(var.id); }
            true
        }
        ast::Expr::Const(_) => true,
        ast::Expr::UnaryOp(node) => visit(&node.expr),
        ast::Expr::BinOp(node) => visit(&node.left) && visit(&node.right),
        ast::Expr::Concat(node) => node.values.iter().all(&mut visit),
        ast::Expr::Compare(node) => visit(&node.expr) && node.ops.iter().all(|op| visit(&op.expr)),
        ast::Expr::IfExpr(node) => visit(&node.test_expr) && visit(&node.true_expr)
            && node.false_expr.as_ref().is_none_or(&mut visit),
        ast::Expr::GetAttr(node) => visit(&node.expr),
        ast::Expr::GetItem(node) => visit(&node.expr) && visit(&node.subscript_expr),
        ast::Expr::List(node) => node.items.iter().all(&mut visit),
        ast::Expr::Map(node) => node.keys.iter().zip(&node.values).all(|(key, value)| visit(key) && visit(value)),
        ast::Expr::Slice(_) | ast::Expr::Call(_) | ast::Expr::Filter(_) | ast::Expr::Test(_) => false,
    }
}

fn captured_argument_names<'a>(call: &ast::Call<'a>, name: &str) -> Option<Vec<&'a str>> {
    let mut names = Vec::new();
    // Jinja visits positional and keyword arguments before dynamic splats,
    // even when a splat appears earlier in source order.
    for category in 0..4 {
        for arg in &call.args {
            let expr = match (category, arg) {
                (0, ast::CallArg::Pos(expr)) | (1, ast::CallArg::Kwarg(_, expr))
                | (2, ast::CallArg::PosSplat(expr)) | (3, ast::CallArg::KwargSplat(expr)) => expr,
                _ => continue,
            };
            if !collect_argument_names(expr, &mut names) { return None; }
        }
    }
    names.contains(&name).then_some(names)
}

fn captures_argument(call: &ast::Call<'_>, name: &str) -> bool {
    captured_argument_names(call, name).is_some()
}

#[cfg(test)]
use similar_asserts::assert_eq;

#[cfg(feature = "macros")]
type Caller<'source> = ast::Spanned<ast::Macro<'source>>;

#[cfg(not(feature = "macros"))]
type Caller<'source> = std::marker::PhantomData<&'source ()>;

/// For the first `MAX_LOCALS` filters/tests, an ID is returned for faster lookups from the stack.
fn get_local_id<'source>(ids: &mut BTreeMap<&'source str, LocalId>, name: &'source str) -> LocalId {
    if let Some(id) = ids.get(name) {
        *id
    } else if ids.len() >= MAX_LOCALS {
        !0
    } else {
        let next_id = ids.len() as LocalId;
        ids.insert(name, next_id);
        next_id
    }
}

fn streamable_for_target(target: &ast::Expr<'_>) -> bool {
    match target {
        ast::Expr::Var(_) => true,
        ast::Expr::List(items) => items.items.iter().all(streamable_for_target),
        _ => false,
    }
}

fn compare_op(op: ast::CompareOpKind) -> CompareOp {
    match op {
        ast::CompareOpKind::Eq => CompareOp::Eq,
        ast::CompareOpKind::Ne => CompareOp::Ne,
        ast::CompareOpKind::Lt => CompareOp::Lt,
        ast::CompareOpKind::Lte => CompareOp::Lte,
        ast::CompareOpKind::Gt => CompareOp::Gt,
        ast::CompareOpKind::Gte => CompareOp::Gte,
        ast::CompareOpKind::In => CompareOp::In,
        ast::CompareOpKind::NotIn => CompareOp::NotIn,
    }
}

/// Represents an open block of code that does not yet have updated
/// jump targets.
enum PendingBlock {
    Branch {
        jump_instr: u32,
    },
    Loop {
        iter_instr: u32,
        jump_instrs: Vec<u32>,
    },
    ScBool {
        jump_instrs: Vec<u32>,
    },
}

const CODEGEN_POOL_MAX_ITEMS: usize = 64;
const CODEGEN_POOLED_MAX_CAPACITY: usize = 64;

thread_local! {
    static PENDING_BLOCK_POOL: RefCell<Vec<Vec<PendingBlock>>> = const { RefCell::new(Vec::new()) };
    static SPAN_STACK_POOL: RefCell<Vec<Vec<Span>>> = const { RefCell::new(Vec::new()) };
}

#[inline(always)]
fn take_pending_block_buffer() -> Vec<PendingBlock> {
    let mut buf = PENDING_BLOCK_POOL
        .with(|pool| pool.borrow_mut().pop())
        .unwrap_or_else(|| Vec::with_capacity(8));
    buf.clear();
    buf
}

#[inline(always)]
fn take_span_stack_buffer() -> Vec<Span> {
    let mut buf = SPAN_STACK_POOL
        .with(|pool| pool.borrow_mut().pop())
        .unwrap_or_else(|| Vec::with_capacity(8));
    buf.clear();
    buf
}

#[inline(always)]
fn recycle_pending_block_buffer(mut buf: Vec<PendingBlock>) {
    if buf.capacity() <= CODEGEN_POOLED_MAX_CAPACITY {
        buf.clear();
        PENDING_BLOCK_POOL.with(|pool| {
            let mut pool = pool.borrow_mut();
            if pool.len() < CODEGEN_POOL_MAX_ITEMS {
                pool.push(buf);
            }
        });
    }
}

#[inline(always)]
fn recycle_span_stack_buffer(mut buf: Vec<Span>) {
    if buf.capacity() <= CODEGEN_POOLED_MAX_CAPACITY {
        buf.clear();
        SPAN_STACK_POOL.with(|pool| {
            let mut pool = pool.borrow_mut();
            if pool.len() < CODEGEN_POOL_MAX_ITEMS {
                pool.push(buf);
            }
        });
    }
}

/// Provides a convenient interface to creating instructions for the VM.
pub struct CodeGenerator<'source> {
    root_symbol_prologue: bool,
    suppressed_root_call: Option<(u32, u32)>,
    prepare_output: bool,
    prepare_attribute: bool,
    prepare_item: bool,
    prepare_await: bool,
    lazy_filtered_predicates: bool,
    fragment_boundaries: bool,
    constant_output: Option<Arc<ConstantOutputFunc>>,
    constant_argument: Option<Arc<ConstantArgumentFunc>>,
    constant_arithmetic: Option<Arc<ConstantArithmeticFunc>>,
    arithmetic_codegen_mode: Option<Arc<ArithmeticCodegenMode>>,
    jinja_arithmetic_policy: Option<Arc<JinjaArithmeticPolicy>>,
    jinja_const_attribute_lookup: Option<Arc<JinjaConstAttributeLookup>>,
    jinja_const_item_lookup: Option<Arc<JinjaConstAttributeLookup>>,
    jinja_const_attribute_release: Option<Arc<JinjaConstAttributeRelease>>,
    selected_arithmetic_operators: BTreeMap<(u32, u32), Option<Value>>,
    preoptimized_arithmetic: BTreeMap<(u32, u32), (Value, bool)>,
    operator_call_factory: Option<Arc<OperatorCallFactory>>,
    call_policy_factory: Option<Arc<CallPolicyFactory>>,
    constant_autoescape: Option<bool>,
    error: Option<Error>,
    native_optimizations: bool,
    publish_locals: bool,
    instructions: Instructions<'source>,
    compilation_key: Option<String>,
    blocks: BTreeMap<&'source str, Instructions<'source>>,
    block_order: Vec<&'source str>,
    pending_block: Vec<PendingBlock>,
    current_line: u16,
    span_stack: Vec<Span>,
    filter_local_ids: BTreeMap<&'source str, LocalId>,
    test_local_ids: BTreeMap<&'source str, LocalId>,
    raw_template_bytes: usize,
}

impl<'source> CodeGenerator<'source> {
    /// Creates a new code generator.
    pub fn new(file: &'source str, source: &'source str) -> CodeGenerator<'source> {
        CodeGenerator {
            root_symbol_prologue: false,
            suppressed_root_call: None,
            prepare_output: false,
            prepare_attribute: false,
            prepare_item: false,
            prepare_await: false,
            lazy_filtered_predicates: true,
            fragment_boundaries: false,
            constant_output: None,
            constant_argument: None,
            constant_arithmetic: None,
            arithmetic_codegen_mode: None,
            jinja_arithmetic_policy: None,
            jinja_const_attribute_lookup: None,
            jinja_const_item_lookup: None,
            jinja_const_attribute_release: None,
            selected_arithmetic_operators: BTreeMap::new(),
            preoptimized_arithmetic: BTreeMap::new(),
            operator_call_factory: None,
            call_policy_factory: None,
            constant_autoescape: None,
            error: None,
            native_optimizations: true,
            publish_locals: false,
            instructions: Instructions::new(file, source),
            compilation_key: None,
            blocks: BTreeMap::new(),
            block_order: Vec::new(),
            pending_block: take_pending_block_buffer(),
            current_line: 0,
            span_stack: take_span_stack_buffer(),
            filter_local_ids: BTreeMap::new(),
            test_local_ids: BTreeMap::new(),
            raw_template_bytes: 0,
        }
    }

    /// Select native folding versus original-runtime instruction dispatch.
    pub fn set_native_optimizations(&mut self, enabled: bool) { self.native_optimizations = enabled; }
    pub(crate) fn set_root_symbol_prologue(&mut self, enabled: bool) { self.root_symbol_prologue = enabled; }
    pub(crate) fn set_prepare_attribute(&mut self, enabled: bool) { self.prepare_attribute = enabled; }
    pub(crate) fn set_prepare_item(&mut self, enabled: bool) { self.prepare_item = enabled; }
    pub(crate) fn set_prepare_await(&mut self, enabled: bool) { self.prepare_await = enabled; }
    pub(crate) fn set_lazy_filtered_predicates(&mut self, enabled: bool) { self.lazy_filtered_predicates = enabled; }

    /// Emit assignment publication events for a foreign runtime context.
    pub fn set_publish_locals(&mut self, enabled: bool) { self.publish_locals = enabled; }

    pub(crate) fn set_constant_output(&mut self, convert: Option<Arc<ConstantOutputFunc>>, autoescape: Option<bool>) {
        self.constant_output = convert; self.constant_autoescape = autoescape;
    }
    pub(crate) fn set_constant_argument(&mut self, convert: Option<Arc<ConstantArgumentFunc>>) {
        self.constant_argument = convert;
    }
    pub(crate) fn set_constant_arithmetic(&mut self, convert: Option<Arc<ConstantArithmeticFunc>>) {
        self.constant_arithmetic = convert;
    }
    pub(crate) fn set_arithmetic_codegen_mode(&mut self, mode: Option<Arc<ArithmeticCodegenMode>>) {
        self.arithmetic_codegen_mode = mode;
    }
    pub(crate) fn set_jinja_arithmetic_policy(&mut self, policy: Option<Arc<JinjaArithmeticPolicy>>) {
        self.jinja_arithmetic_policy = policy;
    }
    pub(crate) fn set_jinja_const_attribute_lookup(&mut self, lookup: Option<Arc<JinjaConstAttributeLookup>>) {
        self.jinja_const_attribute_lookup = lookup;
    }
    pub(crate) fn set_jinja_const_item_lookup(&mut self, lookup: Option<Arc<JinjaConstAttributeLookup>>) {
        self.jinja_const_item_lookup = lookup;
    }
    pub(crate) fn set_jinja_const_attribute_release(&mut self, release: Option<Arc<JinjaConstAttributeRelease>>) {
        self.jinja_const_attribute_release = release;
    }

    pub(crate) fn set_operator_call_factory(&mut self, factory: Option<Arc<OperatorCallFactory>>) {
        self.operator_call_factory = factory;
    }
    pub(crate) fn set_call_policy_factory(&mut self, factory: Option<Arc<CallPolicyFactory>>) {
        self.call_policy_factory = factory;
    }

    pub(crate) fn take_error(&mut self) -> Option<Error> { self.error.take() }

    pub(crate) fn set_prepare_output(&mut self, enabled: bool) { self.prepare_output = enabled; }
    pub(crate) fn set_fragment_boundaries(&mut self, enabled: bool) { self.fragment_boundaries = enabled; }

    /// Sets the current location's line.
    pub fn set_line(&mut self, lineno: u16) {
        self.current_line = lineno;
    }

    /// Sets line from span.
    pub fn set_line_from_span(&mut self, span: Span) {
        self.set_line(span.start_line);
    }

    /// Pushes a span to the stack
    pub fn push_span(&mut self, span: Span) {
        self.span_stack.push(span);
        self.set_line_from_span(span);
    }

    /// Pops a span from the stack.
    pub fn pop_span(&mut self) {
        self.span_stack.pop();
    }

    /// Add a simple instruction with the current location.
    pub fn add(&mut self, instr: Instruction<'source>) -> u32 {
        if let Some(span) = self.span_stack.last() {
            if span.start_line == self.current_line {
                return self.instructions.add_with_span(instr, *span);
            }
        }
        self.instructions.add_with_line(instr, self.current_line)
    }

    /// Add a simple instruction with other location.
    pub fn add_with_span(&mut self, instr: Instruction<'source>, span: Span) -> u32 {
        self.instructions.add_with_span(instr, span)
    }

    /// Returns the next instruction index.
    pub fn next_instruction(&self) -> u32 {
        self.instructions.len() as u32
    }

    /// Creates a sub generator.
    #[cfg(feature = "multi_template")]
    fn new_subgenerator(&self) -> CodeGenerator<'source> {
        let mut sub = CodeGenerator::new(self.instructions.name(), self.instructions.source());
        sub.instructions.set_metadata(self.instructions.metadata().cloned());
        sub.compilation_key = self.compilation_key.clone();
        sub.root_symbol_prologue = self.root_symbol_prologue;
        sub.native_optimizations = self.native_optimizations;
        sub.publish_locals = self.publish_locals;
        sub.prepare_output = self.prepare_output;
        sub.prepare_attribute = self.prepare_attribute;
        sub.prepare_item = self.prepare_item;
        sub.prepare_await = self.prepare_await;
        sub.lazy_filtered_predicates = self.lazy_filtered_predicates;
        sub.fragment_boundaries = self.fragment_boundaries;
        sub.constant_output = self.constant_output.clone();
        sub.constant_argument = self.constant_argument.clone();
        sub.constant_arithmetic = self.constant_arithmetic.clone();
        sub.arithmetic_codegen_mode = self.arithmetic_codegen_mode.clone();
        sub.jinja_arithmetic_policy = self.jinja_arithmetic_policy.clone();
        sub.jinja_const_attribute_lookup = self.jinja_const_attribute_lookup.clone();
        sub.jinja_const_item_lookup = self.jinja_const_item_lookup.clone();
        sub.jinja_const_attribute_release = self.jinja_const_attribute_release.clone();
        sub.operator_call_factory = self.operator_call_factory.clone();
        sub.call_policy_factory = self.call_policy_factory.clone();
        sub.constant_autoescape = self.constant_autoescape;
        sub.current_line = self.current_line;
        sub.span_stack = self.span_stack.last().copied().into_iter().collect();
        sub
    }

    pub(crate) fn set_template_metadata(&mut self, metadata: Option<Value>) {
        self.instructions.set_metadata(metadata);
    }

    pub(crate) fn instructions_metadata(&self) -> Option<&Value> { self.instructions.metadata() }

    pub(crate) fn set_compilation_key(&mut self, key: Option<String>) { self.compilation_key = key; }
    fn callback_name(&self) -> &str { self.compilation_key.as_deref().unwrap_or(self.instructions.name()) }

    /// Finishes a sub generator and syncs it back.
    #[cfg(feature = "multi_template")]
    fn finish_subgenerator(&mut self, mut sub: CodeGenerator<'source>) -> Instructions<'source> {
        if let Some(error) = sub.take_error() { self.error = Some(error); }
        self.current_line = sub.current_line;
        self.block_order.append(&mut sub.block_order);
        let (instructions, blocks) = sub.finish();
        self.blocks.extend(blocks);
        instructions
    }

    /// Starts a for loop
    pub fn start_for_loop(&mut self, with_loop_var: bool, recursive: bool) {
        let mut flags = 0;
        if with_loop_var {
            flags |= LOOP_FLAG_WITH_LOOP_VAR;
        }
        if recursive {
            flags |= LOOP_FLAG_RECURSIVE;
        }
        self.add(Instruction::PushLoop(flags));
        let instr = self.add(Instruction::Iterate(!0));
        self.pending_block.push(PendingBlock::Loop {
            iter_instr: instr,
            jump_instrs: Vec::new(),
        });
    }

    fn start_for_filter_input(&mut self, without_metadata: bool) {
        self.add(Instruction::PushLoop(if without_metadata { LOOP_FLAG_FILTERED_INPUT } else { 0 }));
        let instr = self.add(Instruction::IterateFiltering(!0));
        self.pending_block.push(PendingBlock::Loop { iter_instr: instr, jump_instrs: Vec::new() });
    }

    /// Ends the open for loop
    pub fn end_for_loop(&mut self, push_did_not_iterate: bool) {
        self.end_for_loop_inner(push_did_not_iterate, false);
    }

    fn end_for_loop_inner(&mut self, push_did_not_iterate: bool, filtered: bool) {
        if let Some(PendingBlock::Loop {
            iter_instr,
            jump_instrs,
        }) = self.pending_block.pop()
        {
            if self.fragment_boundaries { self.add(Instruction::OutputGroupBoundary); }
            self.add(Instruction::CompleteLoopIteration);
            self.add(Instruction::Jump(iter_instr));
            let loop_end = self.next_instruction();
            if push_did_not_iterate {
                self.add(if filtered { Instruction::PushDidNotIterateFiltered }
                         else { Instruction::PushDidNotIterate });
            };
            self.add(Instruction::PopLoopFrame);
            for instr in jump_instrs.into_iter().chain(Some(iter_instr)) {
                match self.instructions.get_mut(instr) {
                    Some(&mut Instruction::Iterate(ref mut jump_target))
                    | Some(&mut Instruction::IterateFiltering(ref mut jump_target))
                    | Some(&mut Instruction::IterateFiltered(ref mut jump_target))
                    | Some(&mut Instruction::Jump(ref mut jump_target)) => {
                        *jump_target = loop_end;
                    }
                    _ => unreachable!(),
                }
            }
        } else {
            unreachable!()
        }
    }

    /// Begins an if conditional
    pub fn start_if(&mut self) {
        let jump_instr = self.add(Instruction::JumpIfFalse(!0));
        self.pending_block.push(PendingBlock::Branch { jump_instr });
    }

    fn start_if_filtered(&mut self) {
        let jump_instr = self.add(Instruction::JumpIfFalseFiltered(!0));
        self.pending_block.push(PendingBlock::Branch { jump_instr });
    }

    /// Begins an else conditional
    pub fn start_else(&mut self) {
        let jump_instr = self.add(Instruction::Jump(!0));
        self.end_condition(jump_instr + 1);
        self.pending_block.push(PendingBlock::Branch { jump_instr });
    }

    /// Closes the current if block.
    pub fn end_if(&mut self) {
        self.end_condition(self.next_instruction());
    }

    /// Starts a short-circuited bool block.
    pub fn start_sc_bool(&mut self) {
        self.pending_block.push(PendingBlock::ScBool {
            jump_instrs: Vec::new(),
        });
    }

    /// Emits a short-circuited bool operator.
    pub fn sc_bool(&mut self, and: bool) {
        if let Some(&mut PendingBlock::ScBool {
            ref mut jump_instrs,
        }) = self.pending_block.last_mut()
        {
            jump_instrs.push(self.instructions.add(if and {
                Instruction::JumpIfFalseOrPop(!0)
            } else {
                Instruction::JumpIfTrueOrPop(!0)
            }));
        } else {
            unreachable!();
        }
    }

    /// Ends a short-circuited bool block.
    pub fn end_sc_bool(&mut self) {
        let end = self.next_instruction();
        if let Some(PendingBlock::ScBool { jump_instrs }) = self.pending_block.pop() {
            for instr in jump_instrs {
                match self.instructions.get_mut(instr) {
                    Some(&mut Instruction::JumpIfFalseOrPop(ref mut target))
                    | Some(&mut Instruction::JumpIfTrueOrPop(ref mut target)) => {
                        *target = end;
                    }
                    _ => unreachable!(),
                }
            }
        }
    }

    fn end_condition(&mut self, new_jump_instr: u32) {
        match self.pending_block.pop() {
            Some(PendingBlock::Branch { jump_instr }) => {
                match self.instructions.get_mut(jump_instr) {
                    Some(&mut Instruction::JumpIfFalse(ref mut target))
                    | Some(&mut Instruction::JumpIfFalseFiltered(ref mut target))
                    | Some(&mut Instruction::Jump(ref mut target)) => {
                        *target = new_jump_instr;
                    }
                    _ => {}
                }
            }
            _ => unreachable!(),
        }
    }

    /// Compiles a statement.
    pub fn compile_stmt(&mut self, stmt: &ast::Stmt<'source>) {
        if self.error.is_some() { return; }
        let boundary = self.fragment_boundaries && !matches!(stmt,
            ast::Stmt::Template(_) | ast::Stmt::EmitExpr(_) | ast::Stmt::EmitRaw(_));
        if boundary { self.add(Instruction::OutputGroupBoundary); }
        match stmt {
            ast::Stmt::Template(t) => {
                self.set_line_from_span(t.span());
                if self.root_symbol_prologue {
                    use crate::compiler::root_symbols::RootLoadKind;
                    for symbol in crate::compiler::root_symbols::root_symbol_loads(stmt) {
                        match symbol.load {
                            RootLoadKind::Resolve => self.add(Instruction::CaptureRootBinding(symbol.name, false)),
                            RootLoadKind::Missing => self.add(Instruction::CaptureRootBinding(symbol.name, true)),
                            RootLoadKind::SelfReference => self.add(Instruction::CaptureSelfReference),
                        };
                    }
                }
                for node in &t.children {
                    self.compile_stmt(node);
                }
            }
            ast::Stmt::EmitExpr(expr) => {
                self.compile_emit_expr(expr);
            }
            ast::Stmt::EmitRaw(raw) => {
                self.set_line_from_span(raw.span());
                self.add(Instruction::EmitRaw(raw.raw));
                self.raw_template_bytes += raw.raw.len();
            }
            ast::Stmt::ForLoop(for_loop) => {
                self.compile_for_loop(for_loop);
            }
            ast::Stmt::IfCond(if_cond) => {
                self.compile_if_stmt(if_cond);
            }
            ast::Stmt::WithBlock(with_block) => {
                self.set_line_from_span(with_block.span());
                // As Jinja: every value is evaluated in the enclosing scope,
                // so `{% with a = 1, b = a %}` binds the outer `a` to `b`.
                for (_, expr) in &with_block.assignments {
                    self.compile_expr(expr);
                }
                self.add(Instruction::PushWith);
                for (target, _) in with_block.assignments.iter().rev() {
                    self.compile_assignment(target);
                }
                for node in &with_block.body {
                    self.compile_stmt(node);
                }
                self.add(Instruction::PopFrame);
            }
            ast::Stmt::Set(set) => {
                self.set_line_from_span(set.span());
                self.compile_expr(&set.expr);
                self.compile_assignment(&set.target);
                self.publish_assignment(&set.target);
            }
            ast::Stmt::SetBlock(set_block) => {
                self.set_line_from_span(set_block.span());
                self.add(Instruction::BeginCapture(CaptureMode::Capture));
                for node in &set_block.body {
                    self.compile_stmt(node);
                }
                self.add(Instruction::EndCapture);
                if let Some(ref filter) = set_block.filter {
                    self.compile_expr(filter);
                }
                self.compile_assignment(&set_block.target);
                self.publish_assignment(&set_block.target);
            }
            ast::Stmt::AutoEscape(auto_escape) => {
                self.set_line_from_span(auto_escape.span());
                if self.publish_locals { self.add(Instruction::PushWith); }
                self.compile_expr(&auto_escape.enabled);
                self.add(Instruction::PushAutoEscape);
                let previous_autoescape = self.constant_autoescape;
                self.constant_autoescape = auto_escape.enabled.as_const().map(|value| value.is_true());
                for node in &auto_escape.body {
                    self.compile_stmt(node);
                }
                self.add(Instruction::PopAutoEscape);
                if self.publish_locals { self.add(Instruction::PopFrame); }
                self.constant_autoescape = previous_autoescape;
            }
            ast::Stmt::FilterBlock(filter_block) => {
                self.set_line_from_span(filter_block.span());
                self.add(Instruction::BeginCapture(CaptureMode::Capture));
                for node in &filter_block.body {
                    self.compile_stmt(node);
                }
                self.add(Instruction::EndCapture);
                self.compile_expr(&filter_block.filter);
                self.add(Instruction::EmitFilterBlock);
            }
            #[cfg(feature = "multi_template")]
            ast::Stmt::Block(block) => {
                self.compile_block(block);
            }
            #[cfg(feature = "multi_template")]
            ast::Stmt::Import(import) => {
                self.add(Instruction::BeginCapture(CaptureMode::Capture));
                self.add(Instruction::PushWith);
                self.compile_expr(&import.expr);
                self.add_with_span(Instruction::Include(false, import.with_context, true), import.span());
                self.add(Instruction::EndCapture);
                self.add(Instruction::ExportLocals);
                self.add(Instruction::PopFrame);
                self.compile_assignment(&import.name);
                if self.publish_locals {
                    if let ast::Expr::Var(name) = &import.name {
                        self.add(Instruction::PublishImports(vec![name.id]));
                    }
                }
            }
            #[cfg(feature = "multi_template")]
            ast::Stmt::FromImport(from_import) => {
                self.add(Instruction::BeginCapture(CaptureMode::Discard));
                self.add(Instruction::PushWith);
                self.compile_expr(&from_import.expr);
                self.add(Instruction::DupTop);
                self.add_with_span(Instruction::Include(false, from_import.with_context, true), from_import.span());
                for (index, (name, _)) in from_import.names.iter().enumerate() {
                    if let ast::Expr::Var(name) = name {
                        self.add_with_span(Instruction::LookupImport(name.id, index, from_import.span().start_line), name.span());
                    } else {
                        self.compile_expr(name);
                    }
                }
                self.add(Instruction::PopFrame);
                for (name, alias) in from_import.names.iter().rev() {
                    self.compile_assignment(alias.as_ref().unwrap_or(name));
                }
                if self.publish_locals {
                    let names = from_import.names.iter().filter_map(|(name, alias)| {
                        if let ast::Expr::Var(name) = alias.as_ref().unwrap_or(name) { Some(name.id) }
                        else { None }
                    }).collect();
                    self.add(Instruction::PublishImports(names));
                }
                self.add(Instruction::DiscardTop);
                self.add(Instruction::EndCapture);
            }
            #[cfg(feature = "multi_template")]
            ast::Stmt::Extends(extends) => {
                self.set_line_from_span(extends.span());
                self.compile_expr(&extends.name);
                self.add_with_span(Instruction::LoadBlocks, extends.span());
            }
            #[cfg(feature = "multi_template")]
            ast::Stmt::Include(include) => {
                self.set_line_from_span(include.span());
                self.compile_expr(&include.name);
                self.add_with_span(Instruction::Include(include.ignore_missing, include.with_context, false), include.span());
            }
            #[cfg(feature = "macros")]
            ast::Stmt::Macro(macro_decl) => {
                self.compile_macro(macro_decl);
            }
            #[cfg(feature = "macros")]
            ast::Stmt::CallBlock(call_block) => {
                self.compile_call_block(call_block);
            }
            #[cfg(feature = "loop_controls")]
            ast::Stmt::Continue(cont) => {
                self.set_line_from_span(cont.span());
                for pending_block in self.pending_block.iter().rev() {
                    if let PendingBlock::Loop { iter_instr, .. } = pending_block {
                        self.add(Instruction::Jump(*iter_instr));
                        break;
                    }
                }
            }
            #[cfg(feature = "loop_controls")]
            ast::Stmt::Break(brk) => {
                self.set_line_from_span(brk.span());
                let instr = self.add(Instruction::Jump(0));
                for pending_block in self.pending_block.iter_mut().rev() {
                    if let &mut PendingBlock::Loop {
                        ref mut jump_instrs,
                        ..
                    } = pending_block
                    {
                        jump_instrs.push(instr);
                        break;
                    }
                }
            }
            ast::Stmt::Do(do_tag) => {
                self.compile_do(do_tag);
            }
        }
        if boundary { self.add(Instruction::OutputGroupBoundary); }
    }

    #[cfg(feature = "multi_template")]
    fn compile_block(&mut self, block: &ast::Spanned<ast::Block<'source>>) {
        self.set_line_from_span(block.span());
        self.block_order.push(block.name);
        let mut sub = self.new_subgenerator();
        for node in &block.body {
            sub.compile_stmt(node);
        }
        let instructions = self.finish_subgenerator(sub);
        self.blocks.insert(block.name, instructions);
        if block.required { self.add(Instruction::CheckRequiredBlock(block.name)); }
        self.add(if block.scoped { Instruction::CallBlock(block.name) }
                 else { Instruction::CallBlockUnscoped(block.name) });
    }

    #[cfg(feature = "macros")]
    fn compile_macro_expression(&mut self, macro_decl: &ast::Spanned<ast::Macro<'source>>, call_block: bool) {
        use crate::compiler::instructions::{MACRO_CALLER, MACRO_VARARGS, MACRO_KWARGS, MACRO_CALL_BLOCK};
        self.set_line_from_span(macro_decl.span());
        let instr = self.add(Instruction::Jump(!0));
        // Bind every parameter first. Defaults can reference earlier bound
        // parameters and must run from left to right at invocation time.
        for arg in macro_decl.args.iter().rev() {
            self.compile_assignment(arg);
        }
        for (arg, default) in macro_decl.args.iter().skip(macro_decl.args.len()-macro_decl.defaults.len()).zip(&macro_decl.defaults) {
            self.compile_expr(arg);
            self.add(Instruction::IsUndefined);
            self.start_if();
            self.compile_expr(default);
            self.compile_assignment(arg);
            self.end_if();
        }
        for node in &macro_decl.body {
            self.compile_stmt(node);
        }
        self.add(Instruction::Return);
        let mut undeclared = crate::compiler::meta::find_macro_closure(macro_decl);
        let caller_reference = undeclared.remove("caller");
        let capture_varargs = undeclared.remove("varargs");
        let capture_kwargs = undeclared.remove("kwargs");
        let macro_instr = self.next_instruction();
        for name in &undeclared {
            self.add(Instruction::Enclose(name));
        }
        self.add(Instruction::GetClosure);
        self.add(Instruction::LoadConst(Value::from_object(
            macro_decl
                .args
                .iter()
                .map(|x| match x {
                    ast::Expr::Var(var) => Value::from(var.id),
                    _ => unreachable!(),
                })
                .collect::<Vec<Value>>(),
        )));
        let mut flags = 0;
        if caller_reference {
            flags |= MACRO_CALLER;
        }
        if capture_varargs { flags |= MACRO_VARARGS; }
        if capture_kwargs { flags |= MACRO_KWARGS; }
        if call_block { flags |= MACRO_CALL_BLOCK; }
        self.add(Instruction::BuildMacro(macro_decl.name, instr + 1, flags));
        if let Some(&mut Instruction::Jump(ref mut target)) = self.instructions.get_mut(instr) {
            *target = macro_instr;
        } else {
            unreachable!();
        }
    }

    #[cfg(feature = "macros")]
    fn compile_macro(&mut self, macro_decl: &ast::Spanned<ast::Macro<'source>>) {
        self.compile_macro_expression(macro_decl, false);
        self.add(Instruction::StoreLocal(macro_decl.name));
        if self.publish_locals { self.add(Instruction::PublishLocals(vec![macro_decl.name])); }
    }

    #[cfg(feature = "macros")]
    fn compile_call_block(&mut self, call_block: &ast::Spanned<ast::CallBlock<'source>>) {
        self.compile_call(&call_block.call, Some(&call_block.macro_decl));
        self.add(Instruction::Emit);
    }

    fn compile_do(&mut self, do_tag: &ast::Spanned<ast::Do<'source>>) {
        self.compile_call(&do_tag.call, None);
    }

    fn compile_if_stmt(&mut self, if_cond: &ast::Spanned<ast::IfCond<'source>>) {
        self.set_line_from_span(if_cond.span());
        self.push_span(if_cond.expr.span());
        self.compile_expr(&if_cond.expr);
        self.start_if();
        self.pop_span();
        for node in &if_cond.true_body {
            self.compile_stmt(node);
        }
        if !if_cond.false_body.is_empty() {
            self.start_else();
            for node in &if_cond.false_body {
                self.compile_stmt(node);
            }
        }
        self.end_if();
    }

    fn compile_emit_expr(&mut self, expr: &ast::Spanned<ast::EmitExpr<'source>>) {
        if let Some(convert) = &self.constant_output {
            let span = expr.expr.span();
            let (start, end) = expr.constant_id.map(|id| (id as usize, id as usize))
                .unwrap_or((span.start_offset as usize, span.end_offset as usize));
            match convert(self.callback_name(), self.instructions.source(),
                start, end, self.constant_autoescape) {
                Ok(Some(output)) => {
                    match output {
                        crate::ConstantOutput::Text(text) => {
                            self.raw_template_bytes += text.len();
                            self.add(Instruction::EmitConst(text));
                        }
                        crate::ConstantOutput::Fragment(value) => {
                            self.add(Instruction::EmitConstFragment(value));
                        }
                    }
                    return;
                }
                Ok(None) => {},
                Err(error) => { self.error = Some(error); return; },
            }
        }
        if self.prepare_output {
            self.push_span(expr.expr.span());
            self.add(Instruction::PrepareEmit);
            self.compile_expr(&expr.expr);
            self.add(Instruction::EmitPrepared);
            self.pop_span();
            return;
        }
        if let ast::Expr::Call(call) = &expr.expr {
            self.set_line_from_span(expr.expr.span());
            match call.identify_call() {
                ast::CallType::Function(name) => {
                    if name == "super" && call.args.is_empty() {
                        self.add_with_span(Instruction::FastSuper, call.span());
                        return;
                    } else if name == "loop" && call.args.len() == 1 {
                        self.compile_call_args(std::slice::from_ref(&call.args[0]), 0, None);
                        self.add_with_span(Instruction::FastRecurse, call.span());
                        return;
                    }
                }
                #[cfg(feature = "multi_template")]
                ast::CallType::Block(name) => {
                    // The direct-output instruction bypasses the host call
                    // guard. A configured policy must use the value path.
                    if call.args.is_empty() && self.call_policy_factory.is_none() {
                        self.add(Instruction::CallSelfBlock(name));
                        return;
                    }
                }
                _ => {}
            }
        }
        self.push_span(expr.expr.span());
        self.compile_expr(&expr.expr);
        self.add(Instruction::Emit);
        self.pop_span();
    }

    fn compile_for_loop(&mut self, for_loop: &ast::Spanned<ast::ForLoop<'source>>) {
        self.set_line_from_span(for_loop.span());
        let with_loop_var = for_loop.recursive || crate::compiler::meta::requires_loop_metadata(&for_loop.body);

        // The predicate is a VM subroutine so it runs only when the loop needs
        // its next accepted item, as Jinja's generator does: at advance, at a
        // lookahead or length read, or when a host that received the loop
        // advances it. More complex targets and else retain the materializing
        // path until qualified. Without lazy_filtered_predicates only simple
        // predicates and bodies that keep `loop` local use it, and recursive
        // loops (each `loop(...)` filters its own input lazily) need it.
        let supports_metadata = if with_loop_var && for_loop.filter_expr.is_some()
            && (!for_loop.recursive || self.lazy_filtered_predicates)
            && for_loop.else_body.is_empty() && matches!(for_loop.target, ast::Expr::Var(_)) {
            self.lazy_filtered_predicates || crate::compiler::meta::supports_filtered_loop_metadata(&for_loop.body)
        } else { false };
        if supports_metadata {
            if let Some(ref predicate) = for_loop.filter_expr {
                let mut filter_capture = None;
                let simple_test = match (&for_loop.target, predicate) {
                    (ast::Expr::Var(target), ast::Expr::Test(test)) =>
                        test.args.is_empty() && matches!(&test.expr, ast::Expr::Var(value) if value.id == target.id),
                    (ast::Expr::Var(target), ast::Expr::Call(call)) =>
                        matches!(&call.expr, ast::Expr::Var(_)) && matches!(call.args.as_slice(),
                            [ast::CallArg::Pos(ast::Expr::Var(value))] if value.id == target.id),
                    (ast::Expr::Var(target), ast::Expr::BinOp(compare)) => {
                        let pair = |left: &ast::Expr<'source>, right: &ast::Expr<'source>| {
                            if !matches!(left, ast::Expr::Var(value) if value.id == target.id) { return None; }
                            match right {
                                ast::Expr::Const(_) => Some(None),
                                ast::Expr::Var(value) if value.id != target.id && value.id != "loop"
                                    && (matches!(&for_loop.iter, ast::Expr::Var(_))
                                        || matches!(&for_loop.iter, ast::Expr::Call(call)
                                            if matches!(&call.expr, ast::Expr::Var(factory)
                                                if factory.id != "super" && factory.id != "loop")
                                                && (captures_argument(call, value.id)
                                                    || call.args.iter().all(|argument| {
                                                        let expr = match argument {
                                                            ast::CallArg::Pos(expr) | ast::CallArg::Kwarg(_, expr) |
                                                            ast::CallArg::PosSplat(expr) | ast::CallArg::KwargSplat(expr) => expr,
                                                        };
                                                        !crate::compiler::meta::expression_references(expr, value.id)
                                                    }))))
                                    => Some(Some(value.id)),
                                _ => None,
                            }
                        };
                        if matches!(compare.op, ast::BinOpKind::Eq | ast::BinOpKind::Ne |
                            ast::BinOpKind::Lt | ast::BinOpKind::Lte | ast::BinOpKind::Gt | ast::BinOpKind::Gte) {
                            let matched = pair(&compare.left, &compare.right).or_else(|| pair(&compare.right, &compare.left));
                            filter_capture = matched.flatten();
                            matched.is_some()
                        } else { false }
                    },
                    _ => false,
                };
                // a recursive call pushes only the new iterable: no capture
                if for_loop.recursive { filter_capture = None; }
                if (simple_test || self.lazy_filtered_predicates)
                    && !crate::compiler::meta::expression_references(predicate, "loop") {
                    let source_start = self.next_instruction();
                    let prepared_factory = match (&for_loop.iter, filter_capture) {
                        (ast::Expr::Call(call), Some(_)) if !call.args.is_empty() => match &call.expr {
                            ast::Expr::Var(factory) if factory.id != "super" && factory.id != "loop" => Some(factory.id),
                            _ => None,
                        },
                        _ => None,
                    };
                    let argument_names = match (&for_loop.iter, filter_capture) {
                        (ast::Expr::Call(call), Some(name)) => captured_argument_names(call, name),
                        _ => None,
                    };
                    let retained_argument = argument_names.is_some();
                    let multiple_bindings = argument_names.as_ref().is_some_and(|names| names.len() > 1);
                    if let Some(factory) = prepared_factory {
                        self.add(Instruction::PrepareFunction(factory));
                        self.add(Instruction::DupTop);
                        if retained_argument {
                            let name = filter_capture.expect("retained argument has a capture");
                            if multiple_bindings {
                                self.add(Instruction::BeginPreparedBindings);
                                self.add(Instruction::CapturePreparedFactory(factory));
                                for argument_name in argument_names.as_ref().unwrap() {
                                    if *argument_name != factory {
                                        self.add(Instruction::CapturePreparedBinding(argument_name));
                                    }
                                }
                                self.add(Instruction::CopyPreparedBinding(name, false));
                            } else if factory == name { self.add(Instruction::DupTop); }
                            else { self.add(Instruction::PrepareBinding(name)); }
                            self.add(Instruction::RetainPreparedArgument);
                            self.add(Instruction::MaterializePreparedFunction);
                        }
                    }
                    let argument_start = self.next_instruction();
                    let old_suppressed_call = self.suppressed_root_call;
                    if prepared_factory.is_some() || matches!((&for_loop.iter, filter_capture),
                        (ast::Expr::Call(call), Some(name)) if call.args.is_empty()
                            && matches!(&call.expr, ast::Expr::Var(factory) if factory.id == name)) {
                        if let ast::Expr::Call(call) = &for_loop.iter {
                            let span = call.span();
                            self.suppressed_root_call = Some((span.start_offset, span.end_offset));
                        }
                    }
                    self.compile_expr(&for_loop.iter);
                    self.suppressed_root_call = old_suppressed_call;
                    if retained_argument { self.add(Instruction::ReleaseRetainedArgument); }
                    if multiple_bindings { self.add(Instruction::EndPreparedBindings); }
                    if let Some(factory) = prepared_factory {
                        if retained_argument {
                            let name = filter_capture.expect("retained argument has a capture");
                            for offset in argument_start..self.next_instruction() {
                                if let Some(instruction @ Instruction::Lookup(_)) = self.instructions.get_mut(offset) {
                                    if let Instruction::Lookup(argument) = instruction {
                                        if multiple_bindings && argument_names.as_ref().unwrap().contains(argument) {
                                            *instruction = Instruction::CopyPreparedBinding(argument, true);
                                        } else if !multiple_bindings && *argument == name {
                                            *instruction = Instruction::CopyRetainedArgument;
                                        }
                                    }
                                }
                            }
                        }
                        for offset in (source_start..self.next_instruction()).rev() {
                            if let Some(instruction @ Instruction::CallFunction(_, _)) = self.instructions.get_mut(offset) {
                                if let Instruction::CallFunction(called, count) = instruction {
                                    if *called == factory {
                                        *instruction = Instruction::CallPreparedFunction(factory, *count);
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    if let Some(name) = filter_capture {
                        if let Some(factory) = prepared_factory {
                            self.add(Instruction::Swap);
                            if retained_argument {
                                self.add(Instruction::DiscardTop);
                                self.add(Instruction::Swap);
                            } else if factory != name {
                                self.add(Instruction::DiscardTop);
                                self.add(Instruction::PrepareCapture(name));
                            }
                        } else if matches!(&for_loop.iter, ast::Expr::Var(source) if source.id == name) {
                            self.add(Instruction::DupTop);
                        } else if matches!(&for_loop.iter, ast::Expr::Call(call)
                            if call.args.is_empty() && matches!(&call.expr, ast::Expr::Var(factory) if factory.id == name)) {
                            for offset in source_start..self.next_instruction() {
                                if let Some(instruction @ Instruction::CallFunction(_, Some(0))) = self.instructions.get_mut(offset) {
                                    if matches!(instruction, Instruction::CallFunction(called, Some(0)) if *called == name) {
                                        *instruction = Instruction::CallFunctionCaptured(name);
                                    }
                                }
                            }
                            self.add(Instruction::Swap);
                        } else {
                            self.add(Instruction::PrepareCapture(name));
                        }
                    }
                    let skip = self.add(Instruction::Jump(!0));
                    let entry = self.next_instruction();
                    self.compile_assignment(&for_loop.target);
                    self.compile_expr(predicate);
                    if let Some(captured) = filter_capture {
                        for offset in entry..self.next_instruction() {
                            if let Some(instruction @ Instruction::Lookup(_)) = self.instructions.get_mut(offset) {
                                if matches!(instruction, Instruction::Lookup(name) if *name == captured) {
                                    *instruction = Instruction::LookupFilteredCapture;
                                }
                            }
                        }
                    }
                    self.add(Instruction::ReturnFilter);
                    let start = self.next_instruction();
                    if let Some(Instruction::Jump(target)) = self.instructions.get_mut(skip) { *target = start; }
                    self.add(Instruction::PushFilteredLoop(entry, filter_capture.is_some(), for_loop.recursive));
                    let iter_instr = self.add(Instruction::IterateFiltered(!0));
                    self.pending_block.push(PendingBlock::Loop { iter_instr, jump_instrs: Vec::new() });
                    self.compile_assignment(&for_loop.target);
                    for node in &for_loop.body { self.compile_stmt(node); }
                    self.end_for_loop(false);
                    return;
                }
            }
        }

        // A loop whose body does not use the special loop object can evaluate
        // each predicate immediately before its body, without materializing
        // the filtered input. Metadata demand is scoped to this loop body.
        let can_stream = if for_loop.filter_expr.is_some() && !for_loop.recursive
            && streamable_for_target(&for_loop.target)
        {
            !with_loop_var
        } else {
            false
        };
        if can_stream {
            if let Some(ref filter_expr) = for_loop.filter_expr {
                self.push_span(for_loop.iter.span());
                self.compile_expr(&for_loop.iter);
                self.start_for_filter_input(true);
                self.pop_span();
                let iter_instr = match self.pending_block.last() {
                    Some(PendingBlock::Loop { iter_instr, .. }) => *iter_instr,
                    _ => unreachable!(),
                };
                self.compile_assignment(&for_loop.target);
                self.push_span(filter_expr.span());
                self.compile_expr(filter_expr);
                self.add(Instruction::JumpIfFalseFiltered(iter_instr));
                self.pop_span();
                if !for_loop.else_body.is_empty() {
                    self.add(Instruction::AcceptFilteredIteration);
                }
                for node in &for_loop.body {
                    self.compile_stmt(node);
                }
                self.end_for_loop_inner(!for_loop.else_body.is_empty(), true);
                if !for_loop.else_body.is_empty() {
                    self.start_if();
                    for node in &for_loop.else_body { self.compile_stmt(node); }
                    self.end_if();
                }
                return;
            }
        }

        // filter expressions work like a nested for loop without
        // the special loop variable. in one loop, the condition is checked and
        // passing items accumulated into a list. in the second, that list is
        // iterated over normally
        if let Some(ref filter_expr) = for_loop.filter_expr {
            self.add(Instruction::LoadConst(Value::from(0usize)));
            self.push_span(filter_expr.span());
            self.compile_expr(&for_loop.iter);
            self.start_for_filter_input(false);
            self.add(Instruction::DupTop);
            self.compile_assignment(&for_loop.target);
            self.compile_expr(filter_expr);
            self.start_if_filtered();
            self.add(Instruction::Swap);
            self.add(Instruction::Increment);
            self.start_else();
            self.add(Instruction::DiscardTop);
            self.end_if();
            self.pop_span();
            self.end_for_loop(false);
            self.add(Instruction::BuildList(None));
            self.start_for_loop(with_loop_var, for_loop.recursive);
        } else {
            self.push_span(for_loop.iter.span());
            self.compile_expr(&for_loop.iter);
            self.start_for_loop(with_loop_var, for_loop.recursive);
            self.pop_span();
        }

        self.compile_assignment(&for_loop.target);
        for node in &for_loop.body {
            self.compile_stmt(node);
        }
        self.end_for_loop(!for_loop.else_body.is_empty());
        if !for_loop.else_body.is_empty() {
            self.start_if();
            for node in &for_loop.else_body {
                self.compile_stmt(node);
            }
            self.end_if();
        };
    }

    fn publish_assignment(&mut self, expr: &ast::Expr<'source>) {
        if !self.publish_locals { return; }
        fn names<'a>(expr: &ast::Expr<'a>, out: &mut std::collections::BTreeSet<&'a str>) {
            match expr {
                ast::Expr::Var(var) => { out.insert(var.id); }
                ast::Expr::List(list) => for item in &list.items { names(item, out); },
                _ => {},
            }
        }
        let mut assigned = std::collections::BTreeSet::new();
        names(expr, &mut assigned);
        if !assigned.is_empty() { self.add(Instruction::PublishLocals(assigned.into_iter().collect())); }
    }

    /// Compiles an assignment expression.
    pub fn compile_assignment(&mut self, expr: &ast::Expr<'source>) {
        match expr {
            ast::Expr::Var(var) => {
                self.add(Instruction::StoreLocal(var.id));
            }
            ast::Expr::List(list) => {
                self.push_span(list.span());
                self.add(Instruction::UnpackList(list.items.len()));
                for expr in &list.items {
                    self.compile_assignment(expr);
                }
                self.pop_span();
            }
            ast::Expr::GetAttr(attr) => {
                self.push_span(attr.span());
                self.compile_expr(&attr.expr);
                self.add(Instruction::SetAttr(attr.name));
            }
            _ => unreachable!(),
        }
    }

    fn contains_nonfinite_literal(expr: &ast::Expr<'source>) -> bool {
        match expr {
            ast::Expr::Const(value) => matches!(value.value.0,
                crate::value::ValueRepr::F64(number) if !number.is_finite()),
            ast::Expr::UnaryOp(value) => Self::contains_nonfinite_literal(&value.expr),
            ast::Expr::BinOp(value) => Self::contains_nonfinite_literal(&value.left)
                || Self::contains_nonfinite_literal(&value.right),
            _ => false,
        }
    }

    fn numeric_arithmetic(expr: &ast::Expr<'source>) -> bool {
        match expr {
            ast::Expr::Const(value) => matches!(value.value.kind(),
                crate::value::ValueKind::Number | crate::value::ValueKind::Bool),
            ast::Expr::UnaryOp(value) => matches!(value.op, ast::UnaryOpKind::Neg | ast::UnaryOpKind::Pos)
                && Self::numeric_arithmetic(&value.expr),
            ast::Expr::BinOp(value) => matches!(value.op, ast::BinOpKind::Add | ast::BinOpKind::Sub |
                ast::BinOpKind::Mul | ast::BinOpKind::Div | ast::BinOpKind::FloorDiv |
                ast::BinOpKind::Rem | ast::BinOpKind::Pow)
                && Self::numeric_arithmetic(&value.left) && Self::numeric_arithmetic(&value.right),
            _ => false,
        }
    }

    fn variable_access_chain(expr: &ast::Expr<'source>) -> bool {
        match expr {
            ast::Expr::Var(_) => true,
            ast::Expr::GetAttr(value) => Self::variable_access_chain(&value.expr),
            ast::Expr::GetItem(value) => Self::variable_access_chain(&value.expr)
                && Self::jinja_item_key(&value.subscript_expr),
            _ => false,
        }
    }

    fn jinja_item_key(expr: &ast::Expr<'source>) -> bool {
        matches!(expr, ast::Expr::Const(value) if value.value.kind() == crate::value::ValueKind::String)
            || Self::jinja_arithmetic_tree(expr)
    }

    fn jinja_arithmetic_tree(expr: &ast::Expr<'source>) -> bool {
        match expr {
            ast::Expr::Var(_) => true,
            ast::Expr::GetAttr(value) => Self::variable_access_chain(&value.expr),
            ast::Expr::GetItem(value) => Self::variable_access_chain(&value.expr)
                && Self::jinja_item_key(&value.subscript_expr),
            ast::Expr::Const(value) => matches!(value.value.kind(),
                crate::value::ValueKind::Number | crate::value::ValueKind::Bool),
            ast::Expr::UnaryOp(value) => matches!(value.op, ast::UnaryOpKind::Neg | ast::UnaryOpKind::Pos)
                && Self::jinja_arithmetic_tree(&value.expr),
            ast::Expr::BinOp(value) => matches!(value.op, ast::BinOpKind::Add | ast::BinOpKind::Sub |
                ast::BinOpKind::Mul | ast::BinOpKind::Div | ast::BinOpKind::FloorDiv |
                ast::BinOpKind::Rem | ast::BinOpKind::Pow)
                && Self::jinja_arithmetic_tree(&value.left) && Self::jinja_arithmetic_tree(&value.right),
            _ => false,
        }
    }

    // CPython folds the generated arithmetic after Jinja has visited the
    // operators. Preserve those compile-time policy decisions in visit order.
    fn select_arithmetic_operators(&mut self, expr: &ast::Expr<'source>) {
        let (token, unary, children): (&str, bool, Vec<&ast::Expr<'source>>) = match expr {
            ast::Expr::UnaryOp(value) => (match value.op {
                ast::UnaryOpKind::Neg => "-", ast::UnaryOpKind::Pos => "+", _ => return,
            }, true, vec![&value.expr]),
            ast::Expr::BinOp(value) => (match value.op {
                ast::BinOpKind::Add => "+", ast::BinOpKind::Sub => "-", ast::BinOpKind::Mul => "*",
                ast::BinOpKind::Div => "/", ast::BinOpKind::FloorDiv => "//",
                ast::BinOpKind::Rem => "%", ast::BinOpKind::Pow => "**", _ => return,
            }, false, vec![&value.left, &value.right]),
            _ => return,
        };
        let Some(factory) = &self.operator_call_factory else { return; };
        let selected = match factory(self.callback_name(), token, unary) {
            Ok(value) => value,
            Err(error) => { self.error = Some(error); return; }
        };
        let span = expr.span();
        self.selected_arithmetic_operators.insert((span.start_offset, span.end_offset), selected);
        for child in children {
            self.select_arithmetic_operators(child);
            if self.error.is_some() { return; }
        }
    }

    fn clear_arithmetic_operators(&mut self, expr: &ast::Expr<'source>) {
        let span = expr.span();
        self.selected_arithmetic_operators.remove(&(span.start_offset, span.end_offset));
        match expr {
            ast::Expr::UnaryOp(value) => self.clear_arithmetic_operators(&value.expr),
            ast::Expr::BinOp(value) => {
                self.clear_arithmetic_operators(&value.left);
                self.clear_arithmetic_operators(&value.right);
            }
            _ => {}
        }
    }

    fn jinja_arithmetic_enabled(&mut self) -> bool {
        let Some(policy) = &self.jinja_arithmetic_policy else { return false; };
        match policy(self.callback_name(), None, false, self.constant_autoescape) {
            Ok(Some(_)) => true,
            Ok(None) => false,
            Err(error) => { self.error = Some(error); false }
        }
    }

    // Jinja's optimizer visits children first, then attempts as_const on the
    // resulting node. A failed child is still revisited by its parent's
    // as_const, after the parent's interception policy has been consulted.
    fn jinja_arithmetic_as_const(&mut self, expr: &ast::Expr<'source>) -> Option<(Value, bool)> {
        let span = expr.span();
        if let Some(value) = self.preoptimized_arithmetic.get(&(span.start_offset, span.end_offset)) {
            return Some(value.clone());
        }
        let access = match expr {
            ast::Expr::GetAttr(value) => Some((&self.jinja_const_attribute_lookup, &value.expr)),
            ast::Expr::GetItem(value) => Some((&self.jinja_const_item_lookup, &value.expr)),
            _ => None,
        };
        if let Some((lookup, base)) = access {
            let lookup = lookup.as_ref()?;
            let captured = match lookup() {
                Ok(Some(captured)) => captured,
                Ok(None) => return None,
                Err(error) => { self.error = Some(error); return None; }
            };
            self.jinja_arithmetic_as_const(base);
            if let Some(release) = &self.jinja_const_attribute_release { release(captured); }
            // Accepted access chains always end in a variable, so they
            // cannot produce a compile-time value.
            return None;
        }
        let (operator, token, unary, left, right) = match expr {
            ast::Expr::Const(value) if matches!(value.value.kind(),
                crate::value::ValueKind::Number | crate::value::ValueKind::Bool) => return Some((value.value.clone(), false)),
            ast::Expr::UnaryOp(value) => {
                let (operator, token) = match value.op {
                    ast::UnaryOpKind::Neg => ("neg", "-"), ast::UnaryOpKind::Pos => ("pos", "+"), _ => return None,
                };
                (operator, token, true, &value.expr, None)
            }
            ast::Expr::BinOp(value) => {
                let (operator, token) = match value.op {
                    ast::BinOpKind::Add => ("add", "+"), ast::BinOpKind::Sub => ("sub", "-"),
                    ast::BinOpKind::Mul => ("mul", "*"), ast::BinOpKind::Div => ("div", "/"),
                    ast::BinOpKind::FloorDiv => ("int_div", "//"), ast::BinOpKind::Rem => ("rem", "%"),
                    ast::BinOpKind::Pow => ("pow", "**"), _ => return None,
                };
                (operator, token, false, &value.left, Some(&value.right))
            }
            _ => return None,
        };
        let policy = self.jinja_arithmetic_policy.as_ref()?;
        match policy(self.callback_name(), Some(token), unary, self.constant_autoescape) {
            Ok(Some(false)) => {},
            Ok(_) => return None,
            Err(error) => { self.error = Some(error); return None; }
        }
        let left = self.jinja_arithmetic_as_const(left)?.0;
        let right = match right {
            Some(expr) => Some(self.jinja_arithmetic_as_const(expr)?.0),
            None => None,
        };
        let convert = self.constant_arithmetic.as_ref()?;
        match convert(self.callback_name(), operator, &left, right.as_ref(), self.constant_autoescape,
                      Self::contains_nonfinite_literal(expr), true, true) {
            Ok(value) => value,
            Err(error) => { self.error = Some(error); None }
        }
    }

    fn optimize_jinja_arithmetic(&mut self, expr: &ast::Expr<'source>) -> Option<(Value, bool)> {
        let span = expr.span();
        if let Some(value) = self.preoptimized_arithmetic.get(&(span.start_offset, span.end_offset)) {
            return Some(value.clone());
        }
        match expr {
            ast::Expr::UnaryOp(value) => { self.optimize_jinja_arithmetic(&value.expr); }
            ast::Expr::GetAttr(value) => { self.optimize_jinja_arithmetic(&value.expr); }
            ast::Expr::GetItem(value) => {
                self.optimize_jinja_arithmetic(&value.expr);
                if self.error.is_some() { return None; }
                self.optimize_jinja_arithmetic(&value.subscript_expr);
            }
            ast::Expr::BinOp(value) => {
                self.optimize_jinja_arithmetic(&value.left);
                if self.error.is_some() { return None; }
                self.optimize_jinja_arithmetic(&value.right);
            }
            _ => {}
        }
        if self.error.is_some() { return None; }
        let value = self.jinja_arithmetic_as_const(expr)?;
        if !matches!(expr, ast::Expr::Const(_)) {
            self.clear_preoptimized_arithmetic(expr);
            self.preoptimized_arithmetic.insert((span.start_offset, span.end_offset), value.clone());
        }
        Some(value)
    }

    fn constant_arithmetic_value(&mut self, expr: &ast::Expr<'source>, nonfinite_source: bool, only_jinja: bool) -> Option<(Value, bool)> {
        let span = expr.span();
        if let Some(value) = self.preoptimized_arithmetic.get(&(span.start_offset, span.end_offset)) {
            return Some(value.clone());
        }
        let selected = self.selected_arithmetic_operators.get(&(span.start_offset, span.end_offset));
        if matches!(selected, Some(Some(_))) { return None; }
        let preselected = selected.is_some();
        let (operator, left, right) = match expr {
            ast::Expr::Const(value) if matches!(value.value.kind(),
                crate::value::ValueKind::Number | crate::value::ValueKind::Bool) => return Some((value.value.clone(), false)),
            ast::Expr::UnaryOp(value) => {
                let operator = match value.op {
                    ast::UnaryOpKind::Neg => "neg", ast::UnaryOpKind::Pos => "pos", _ => return None,
                };
                (operator, self.constant_arithmetic_value(&value.expr, nonfinite_source, only_jinja)?.0, None)
            }
            ast::Expr::BinOp(value) => {
                let operator = match value.op {
                    ast::BinOpKind::Add => "add", ast::BinOpKind::Sub => "sub",
                    ast::BinOpKind::Mul => "mul", ast::BinOpKind::Div => "div",
                    ast::BinOpKind::FloorDiv => "int_div", ast::BinOpKind::Rem => "rem",
                    ast::BinOpKind::Pow => "pow", _ => return None,
                };
                let left = self.constant_arithmetic_value(&value.left, nonfinite_source, only_jinja)?.0;
                let right = self.constant_arithmetic_value(&value.right, nonfinite_source, only_jinja)?.0;
                (operator, left, Some(right))
            }
            _ => return None,
        };
        let convert = self.constant_arithmetic.as_ref()?;
        match convert(self.callback_name(), operator, &left, right.as_ref(), self.constant_autoescape, nonfinite_source, only_jinja, preselected) {
            Ok(value) => {
                if only_jinja {
                    if let Some(ref value) = value {
                        self.clear_preoptimized_arithmetic(expr);
                        self.preoptimized_arithmetic.insert((span.start_offset, span.end_offset), value.clone());
                    }
                }
                value
            }
            Err(error) => { self.error = Some(error); None }
        }
    }

    fn clear_preoptimized_arithmetic(&mut self, expr: &ast::Expr<'source>) {
        let span = expr.span();
        self.preoptimized_arithmetic.remove(&(span.start_offset, span.end_offset));
        match expr {
            ast::Expr::UnaryOp(value) => self.clear_preoptimized_arithmetic(&value.expr),
            ast::Expr::GetAttr(value) => self.clear_preoptimized_arithmetic(&value.expr),
            ast::Expr::GetItem(value) => {
                self.clear_preoptimized_arithmetic(&value.expr);
                self.clear_preoptimized_arithmetic(&value.subscript_expr);
            }
            ast::Expr::BinOp(value) => {
                self.clear_preoptimized_arithmetic(&value.left);
                self.clear_preoptimized_arithmetic(&value.right);
            }
            _ => {}
        }
    }

    fn emit_folded_arithmetic(&mut self, expr: &ast::Expr<'source>, value: Value, raw: bool) {
        self.set_line_from_span(expr.span());
        let finite = !matches!(value.0, crate::value::ValueRepr::F64(number) if !number.is_finite());
        let unique = raw && matches!(value.0, crate::value::ValueRepr::F64(number) if number.is_nan());
        self.add(if raw { Instruction::LoadRawConst(value) } else { Instruction::LoadConst(value) });
        if unique { self.add(Instruction::InternUniqueConstant(expr.span().start_offset)); }
        else if finite || raw { self.add(Instruction::InternArithmeticConstant); }
    }

    /// Compiles an expression.
    pub fn compile_expr(&mut self, expr: &ast::Expr<'source>) {
        if self.error.is_some() { return; }
        let span = expr.span();
        if let Some((value, raw)) = self.preoptimized_arithmetic.remove(&(span.start_offset, span.end_offset)) {
            self.emit_folded_arithmetic(expr, value, raw);
            return;
        }
        let nonfinite_source = Self::contains_nonfinite_literal(expr);
        if self.constant_arithmetic.is_some()
            && (matches!(expr, ast::Expr::BinOp(_) | ast::Expr::UnaryOp(_) | ast::Expr::GetAttr(_) | ast::Expr::GetItem(_)) || nonfinite_source)
            && Self::jinja_arithmetic_tree(expr) {
            let jinja_enabled = self.jinja_arithmetic_enabled();
            if self.error.is_some() { return; }
            if jinja_enabled {
                if let Some((value, raw)) = self.optimize_jinja_arithmetic(expr) {
                    self.clear_preoptimized_arithmetic(expr);
                    self.emit_folded_arithmetic(expr, value, raw);
                    return;
                }
                if self.error.is_some() { return; }
            } else if Self::numeric_arithmetic(expr) && (matches!(expr, ast::Expr::BinOp(_)) || nonfinite_source) {
                let key = (span.start_offset, span.end_offset);
                if !self.selected_arithmetic_operators.contains_key(&key)
                    && self.arithmetic_codegen_mode.as_ref().is_some_and(|mode| mode(self.callback_name())) {
                    self.select_arithmetic_operators(expr);
                    if self.error.is_some() { return; }
                }
                if let Some((value, raw)) = self.constant_arithmetic_value(expr, nonfinite_source, false) {
                    self.clear_arithmetic_operators(expr);
                    self.clear_preoptimized_arithmetic(expr);
                    self.emit_folded_arithmetic(expr, value, raw);
                    return;
                }
                if self.error.is_some() { return; }
            }
        }
        // try to do constant folding
        if let Some(v) = expr.as_const().filter(|_| self.native_optimizations || matches!(expr, ast::Expr::Const(_))) {
            self.set_line_from_span(expr.span());
            self.add(Instruction::LoadConst(v.clone()));
            if !self.native_optimizations && matches!(expr, ast::Expr::Const(_))
                && matches!(v.kind(), crate::value::ValueKind::Number | crate::value::ValueKind::String)
                && !matches!(v.0, crate::value::ValueRepr::F64(number) if !number.is_finite()) {
                self.add(Instruction::InternArithmeticConstant);
            }
            return;
        }

        match expr {
            ast::Expr::Var(v) => {
                self.set_line_from_span(v.span());
                self.add(Instruction::Lookup(v.id));
            }
            ast::Expr::Const(_) => unreachable!(), // handled by constant folding
            ast::Expr::Slice(s) => {
                self.push_span(s.span());
                self.compile_expr(&s.expr);
                if let Some(ref start) = s.start {
                    self.compile_expr(start);
                } else {
                    self.add(Instruction::LoadConst(Value::from(())));
                }
                if let Some(ref stop) = s.stop {
                    self.compile_expr(stop);
                } else {
                    self.add(Instruction::LoadConst(Value::from(())));
                }
                if let Some(ref step) = s.step {
                    self.compile_expr(step);
                } else {
                    self.add(Instruction::LoadConst(Value::from(())));
                }
                self.add(Instruction::Slice);
                self.pop_span();
            }
            ast::Expr::UnaryOp(c) => {
                self.set_line_from_span(c.span());
                let token = match c.op { ast::UnaryOpKind::Pos => Some("+"), ast::UnaryOpKind::Neg => Some("-"), _ => None };
                if let Some(token) = token {
                    if self.compile_intercepted_operator(token, true, &[&c.expr], c.span()) { return; }
                }
                let intern_signed = !self.native_optimizations && matches!(&c.expr,
                    ast::Expr::Const(value) if value.value.kind() == crate::value::ValueKind::Number
                    && !matches!(value.value.0, crate::value::ValueRepr::F64(number) if !number.is_finite()));
                match c.op {
                    ast::UnaryOpKind::Pos => {
                        self.compile_expr(&c.expr);
                        self.add_with_span(Instruction::Pos, c.span());
                        if intern_signed { self.add(Instruction::InternArithmeticConstant); }
                    }
                    ast::UnaryOpKind::Not => {
                        self.compile_expr(&c.expr);
                        self.add(Instruction::Not);
                    }
                    ast::UnaryOpKind::Neg => {
                        // common case: negative numbers.  In that case we
                        // directly negate them if this is possible without
                        // an error.
                        if let ast::Expr::Const(ref c) = c.expr {
                            if self.native_optimizations {
                                if let Ok(negated) = neg(&c.value) {
                                    self.add(Instruction::LoadConst(negated));
                                    return;
                                }
                            }
                        }
                        self.compile_expr(&c.expr);
                        self.add_with_span(Instruction::Neg, c.span());
                        if intern_signed { self.add(Instruction::InternArithmeticConstant); }
                    }
                }
            }
            ast::Expr::BinOp(c) => {
                self.compile_bin_op(c);
            }
            ast::Expr::Concat(c) => {
                for value in &c.values { self.compile_expr(value); }
                self.add_with_span(Instruction::StringConcatMany(c.values.len()), c.span());
            }
            ast::Expr::Compare(c) => {
                self.compile_compare(c);
            }
            ast::Expr::IfExpr(i) => {
                self.set_line_from_span(i.span());
                self.compile_expr(&i.test_expr);
                self.start_if();
                self.compile_expr(&i.true_expr);
                self.start_else();
                if let Some(ref false_expr) = i.false_expr {
                    self.compile_expr(false_expr);
                } else {
                    // special behavior: missing false block have a silent undefined
                    // to permit special casing.  This is for compatibility also with
                    // what Jinja2 does.
                    self.add(Instruction::LoadConst(
                        ValueRepr::Undefined(UndefinedType::Silent(i.span().start_line)).into(),
                    ));
                }
                self.end_if();
            }
            ast::Expr::Filter(f) => {
                self.push_span(f.span());
                if self.prepare_await { self.add(Instruction::PrepareAwait); }
                if let Some(ref expr) = f.expr {
                    self.compile_expr(expr);
                }
                let arg_count = self.compile_call_args(&f.args, 1, None);
                let local_id = get_local_id(&mut self.filter_local_ids, f.name);
                self.add(Instruction::ApplyFilter(f.name, arg_count, local_id));
                self.pop_span();
            }
            ast::Expr::Test(f) => {
                self.push_span(f.span());
                if self.prepare_await { self.add(Instruction::PrepareAwait); }
                self.compile_expr(&f.expr);
                let arg_count = self.compile_call_args(&f.args, 1, None);
                let local_id = get_local_id(&mut self.test_local_ids, f.name);
                self.add(Instruction::PerformTest(f.name, arg_count, local_id));
                self.pop_span();
            }
            ast::Expr::GetAttr(g) => {
                self.push_span(g.span());
                if self.prepare_await { self.add(Instruction::PrepareAwait); }
                if self.prepare_attribute { self.add(Instruction::PrepareAttribute); }
                self.compile_expr(&g.expr);
                self.add(if self.prepare_attribute { Instruction::GetAttrPrepared(g.name, false) }
                         else { Instruction::GetAttr(g.name) });
                self.pop_span();
            }
            ast::Expr::GetItem(g) => {
                self.push_span(g.span());
                if self.prepare_await { self.add(Instruction::PrepareAwait); }
                if self.prepare_item { self.add(Instruction::PrepareItem); }
                self.compile_expr(&g.expr);
                let key_start = self.next_instruction();
                self.compile_expr(&g.subscript_expr);
                let key_end = self.next_instruction();
                if !self.native_optimizations {
                    if let ast::Expr::List(items) = &g.subscript_expr {
                        // Non-finite values may resolve to a foreign runtime
                        // global and therefore are not compiled tuple constants.
                        let finite = |value: &Value| !matches!(value.0,
                            crate::value::ValueRepr::F64(number) if !number.is_finite());
                        if items.is_tuple && items.items.iter().all(|item| {
                            match item {
                                ast::Expr::Const(value) => matches!(value.value.kind(),
                                    crate::value::ValueKind::None | crate::value::ValueKind::Bool |
                                    crate::value::ValueKind::Number | crate::value::ValueKind::String)
                                    && finite(&value.value),
                                ast::Expr::UnaryOp(unary) if matches!(unary.op,
                                    ast::UnaryOpKind::Neg | ast::UnaryOpKind::Pos) =>
                                    matches!(&unary.expr, ast::Expr::Const(value)
                                        if value.value.kind() == crate::value::ValueKind::Number && finite(&value.value)),
                                _ => false,
                            }
                        }) && (key_start..key_end).all(|index| matches!(self.instructions.get(index),
                            Some(Instruction::LoadConst(_) | Instruction::InternArithmeticConstant |
                                 Instruction::Neg | Instruction::Pos | Instruction::BuildTuple(_)))) {
                            self.add(Instruction::InternSubscriptTuple);
                        }
                    }
                }
                self.add(if self.prepare_item { Instruction::GetItemPrepared } else { Instruction::GetItem });
                self.pop_span();
            }
            ast::Expr::Call(c) => {
                self.compile_call(c, None);
            }
            ast::Expr::List(l) => {
                self.set_line_from_span(l.span());
                for item in &l.items {
                    self.compile_expr(item);
                }
                if l.is_tuple && !self.native_optimizations {
                    self.add(Instruction::BuildTuple(l.items.len()));
                } else {
                    self.add(Instruction::BuildList(Some(l.items.len())));
                }
            }
            ast::Expr::Map(m) => {
                self.set_line_from_span(m.span());
                assert_eq!(m.keys.len(), m.values.len());
                for (key, value) in m.keys.iter().zip(m.values.iter()) {
                    self.compile_expr(key);
                    self.compile_expr(value);
                }
                self.add(Instruction::BuildMap(m.keys.len()));
            }
        }
    }

    fn preoptimize_call_arguments(&mut self, args: &[ast::CallArg<'source>]) {
        if self.constant_arithmetic.is_none() { return; }
        let jinja_enabled = self.jinja_arithmetic_enabled();
        if self.error.is_some() { return; }
        for arg in args {
            let expr = match arg {
                ast::CallArg::Pos(expr) | ast::CallArg::PosSplat(expr) |
                ast::CallArg::Kwarg(_, expr) | ast::CallArg::KwargSplat(expr) => expr,
            };
            if !matches!(expr, ast::Expr::BinOp(_) | ast::Expr::UnaryOp(_) | ast::Expr::GetAttr(_) | ast::Expr::GetItem(_)) { continue; }
            if !(if jinja_enabled { Self::jinja_arithmetic_tree(expr) } else { Self::numeric_arithmetic(expr) }) { continue; }
            let span = expr.span();
            let nonfinite = Self::contains_nonfinite_literal(expr);
            let folded = if jinja_enabled { self.optimize_jinja_arithmetic(expr) }
                else { self.constant_arithmetic_value(expr, nonfinite, true) };
            if let Some(value) = folded {
                self.preoptimized_arithmetic.insert((span.start_offset, span.end_offset), value);
            }
            if self.error.is_some() { return; }
        }
    }

    fn select_call_policy(&mut self) -> Option<bool> {
        let factory = self.call_policy_factory.as_ref()?;
        match factory(self.callback_name()) {
            Ok(policy) => Some(policy),
            Err(error) => { self.error = Some(error); None }
        }
    }

    fn compile_call(
        &mut self,
        c: &ast::Spanned<ast::Call<'source>>,
        caller: Option<&Caller<'source>>,
    ) {
        self.push_span(c.span());
        if self.prepare_await { self.add(Instruction::PrepareAwait); }
        match c.identify_call() {
            ast::CallType::Function(name) => {
                let folded = if caller.is_none() && c.args.len() == 1 {
                    match &c.args[0] {
                        ast::CallArg::Pos(ast::Expr::Filter(filter)) if filter.args.is_empty() => {
                            match (&self.constant_argument, &filter.expr) {
                                (Some(convert), Some(ast::Expr::Const(value))) => {
                                    match convert(self.callback_name(), filter.name, &value.value, self.constant_autoescape) {
                                        Ok(value) => value,
                                        Err(error) => { self.error = Some(error); None }
                                    }
                                }
                                _ => None,
                            }
                        }
                        _ => None,
                    }
                } else { None };
                self.preoptimize_call_arguments(&c.args);
                let policy = self.select_call_policy();
                if let Some(policy) = policy { self.add(Instruction::PrepareCall(policy)); }
                let span = c.span();
                if self.root_symbol_prologue && name != "super" && name != "loop"
                    && self.suppressed_root_call != Some((span.start_offset, span.end_offset)) {
                    self.add(Instruction::PrepareRootCall(name));
                }
                let arg_count = if let Some(value) = folded {
                    self.emit_constant_argument(value);
                    Some(1)
                } else { self.compile_call_args(&c.args, 0, caller) };
                if let Some(policy) = policy { self.add(Instruction::SetCallPolicy(policy)); }
                self.add(Instruction::CallFunction(name, arg_count));
            }
            #[cfg(feature = "multi_template")]
            ast::CallType::Block(name) => {
                let policy = self.select_call_policy();
                if let Some(policy) = policy { self.add(Instruction::PrepareCall(policy)); }
                self.add(Instruction::PrepareSelfBlock(name));
                let arg_count = self.compile_call_args(&c.args, 0, caller);
                if let Some(policy) = policy { self.add(Instruction::SetCallPolicy(policy)); }
                self.add(Instruction::CallSelfBlockValue(name, policy, arg_count));
            }
            ast::CallType::Method(expr, name) => {
                self.preoptimize_call_arguments(&c.args);
                let policy = self.select_call_policy();
                if let Some(policy) = policy { self.add(Instruction::PrepareCall(policy)); }
                if self.prepare_await { self.add(Instruction::PrepareAwait); }
                if self.prepare_attribute { self.add(Instruction::PrepareAttribute); }
                self.compile_expr(expr);
                self.add(if self.prepare_attribute { Instruction::GetAttrPrepared(name, true) }
                         else { Instruction::PrepareMethod(name) });
                let arg_count = self.compile_call_args(&c.args, 1, caller);
                if let Some(policy) = policy { self.add(Instruction::SetCallPolicy(policy)); }
                self.add(Instruction::CallObject(arg_count));
            }
            ast::CallType::Object(expr) => {
                self.preoptimize_call_arguments(&c.args);
                let policy = self.select_call_policy();
                if let Some(policy) = policy { self.add(Instruction::PrepareCall(policy)); }
                self.compile_expr(expr);
                let arg_count = self.compile_call_args(&c.args, 1, caller);
                if let Some(policy) = policy { self.add(Instruction::SetCallPolicy(policy)); }
                self.add(Instruction::CallObject(arg_count));
            }
        };
        self.pop_span();
    }

    fn emit_constant_argument(&mut self, value: ConstantArgument) {
        match value {
            ConstantArgument::Value(value) => { self.add(Instruction::LoadConst(value)); }
            ConstantArgument::List(values) => {
                let count = values.len();
                for value in values { self.emit_constant_argument(value); }
                self.add(Instruction::BuildList(Some(count)));
            }
            ConstantArgument::Tuple(values) => {
                let count = values.len();
                for value in values { self.emit_constant_argument(value); }
                self.add(Instruction::BuildTuple(count));
            }
            ConstantArgument::Map(values) => {
                let count = values.len();
                for (key, value) in values { self.emit_constant_argument(key); self.emit_constant_argument(value); }
                self.add(Instruction::BuildMap(count));
            }
            ConstantArgument::Factory(factory) => {
                self.add(Instruction::LoadConst(factory));
                self.add(Instruction::CallObject(Some(1)));
            }
        }
    }

    fn compile_call_args(
        &mut self,
        args: &[ast::CallArg<'source>],
        extra_args: usize,
        caller: Option<&Caller<'source>>,
    ) -> Option<u16> {
        let mut pending_args = extra_args;
        let mut num_args_batches = 0;
        let mut has_kwargs = caller.is_some();
        let mut static_kwargs = caller.is_none();

        for arg in args {
            match arg {
                ast::CallArg::Pos(expr) => {
                    self.compile_expr(expr);
                    pending_args += 1;
                }
                ast::CallArg::PosSplat(expr) => {
                    if pending_args > 0 {
                        self.add(Instruction::BuildList(Some(pending_args)));
                        pending_args = 0;
                        num_args_batches += 1;
                    }
                    self.compile_expr(expr);
                    num_args_batches += 1;
                }
                ast::CallArg::Kwarg(_, expr) => {
                    if !matches!(expr, ast::Expr::Const(_)) {
                        static_kwargs = false;
                    }
                    has_kwargs = true;
                }
                ast::CallArg::KwargSplat(_) => {
                    static_kwargs = false;
                    has_kwargs = true;
                }
            }
        }

        // Keep keyword source wrappers outside positional batches.  A foreign
        // runtime may materialize a batch as a list, which would otherwise
        // erase the marker and turn **kwargs into a positional dictionary.
        let unpacked_before_kwargs = has_kwargs && num_args_batches > 0;
        if unpacked_before_kwargs {
            if pending_args > 0 {
                self.add(Instruction::BuildList(Some(pending_args)));
                num_args_batches += 1;
            }
            self.add(Instruction::UnpackLists(num_args_batches));
            num_args_batches = 0;
            pending_args = 0;
        }

        if has_kwargs {
            let mut pending_kwargs = 0;
            let mut num_kwargs_batches = 0;
            let mut collected_kwargs = ValueMap::new();
            for arg in args {
                match arg {
                    ast::CallArg::Kwarg(key, value) => {
                        if static_kwargs {
                            if let ast::Expr::Const(c) = value {
                                collected_kwargs.insert(Value::from(*key), c.value.clone());
                            } else {
                                unreachable!();
                            }
                        } else {
                            self.add(Instruction::LoadConst(Value::from(*key)));
                            self.compile_expr(value);
                            pending_kwargs += 1;
                        }
                    }
                    ast::CallArg::KwargSplat(expr) => {
                        if pending_kwargs > 0 {
                            self.add(Instruction::BuildKwargs(pending_kwargs));
                            num_kwargs_batches += 1;
                            pending_kwargs = 0;
                        }
                        self.compile_expr(expr);
                        num_kwargs_batches += 1;
                    }
                    ast::CallArg::Pos(_) | ast::CallArg::PosSplat(_) => {}
                }
            }

            if !collected_kwargs.is_empty() {
                self.add(Instruction::LoadConst(Kwargs::wrap(collected_kwargs)));
            } else {
                // The conditions above guarantee that if we collect static kwargs
                // we cannot enter this block (single kwargs batch, no caller).

                #[cfg(feature = "macros")]
                {
                    if let Some(caller) = caller {
                        self.add(Instruction::LoadConst(Value::from("caller")));
                        self.compile_macro_expression(caller, true);
                        pending_kwargs += 1
                    }
                }
                if num_kwargs_batches > 0 {
                    if pending_kwargs > 0 {
                        self.add(Instruction::BuildKwargs(pending_kwargs));
                        num_kwargs_batches += 1;
                    }
                    self.add(Instruction::MergeKwargs(num_kwargs_batches));
                } else {
                    self.add(Instruction::BuildKwargs(pending_kwargs));
                }
            }
            pending_args += 1;
        }

        if unpacked_before_kwargs {
            // UnpackLists left the positional count immediately below the
            // keyword wrapper; put the wrapper in the argument list and
            // adjust the dynamic count consumed by the call instruction.
            self.add(Instruction::Swap);
            self.add(Instruction::Increment);
            None
        } else if num_args_batches > 0 {
            if pending_args > 0 {
                self.add(Instruction::BuildList(Some(pending_args)));
                num_args_batches += 1;
            }
            self.add(Instruction::UnpackLists(num_args_batches));
            None
        } else {
            assert!(pending_args as u16 as usize == pending_args);
            Some(pending_args as u16)
        }
    }

    fn compile_compare(&mut self, c: &ast::Spanned<ast::Compare<'source>>) {
        self.push_span(c.span());
        self.compile_expr(&c.expr);
        let mut cleanup_jumps = Vec::new();
        for (idx, op) in c.ops.iter().enumerate() {
            self.compile_expr(&op.expr);
            if idx + 1 == c.ops.len() {
                self.emit_compare(op.op);
            } else {
                self.add(Instruction::CompareAndPreserve(compare_op(op.op)));
                cleanup_jumps.push(self.add(Instruction::JumpIfFalseOrPop(!0)));
            }
        }
        if !cleanup_jumps.is_empty() {
            let jump_end = self.add(Instruction::Jump(!0));
            let cleanup_start = self.next_instruction();
            self.add(Instruction::Swap);
            self.add(Instruction::DiscardTop);
            let end = self.next_instruction();
            for instr in cleanup_jumps {
                match self.instructions.get_mut(instr) {
                    Some(&mut Instruction::JumpIfFalseOrPop(ref mut target)) => {
                        *target = cleanup_start;
                    }
                    _ => unreachable!(),
                }
            }
            match self.instructions.get_mut(jump_end) {
                Some(&mut Instruction::Jump(ref mut target)) => {
                    *target = end;
                }
                _ => unreachable!(),
            }
        }
        self.pop_span();
    }

    fn emit_compare(&mut self, op: ast::CompareOpKind) {
        self.add(match op {
            ast::CompareOpKind::Eq => Instruction::Eq,
            ast::CompareOpKind::Ne => Instruction::Ne,
            ast::CompareOpKind::Lt => Instruction::Lt,
            ast::CompareOpKind::Lte => Instruction::Lte,
            ast::CompareOpKind::Gt => Instruction::Gt,
            ast::CompareOpKind::Gte => Instruction::Gte,
            ast::CompareOpKind::In | ast::CompareOpKind::NotIn => Instruction::In,
        });
        if matches!(op, ast::CompareOpKind::NotIn) {
            self.add(Instruction::Not);
        }
    }

    fn compile_intercepted_operator(&mut self, token: &str, unary: bool, operands: &[&ast::Expr<'source>], span: Span) -> bool {
        let Some(factory) = &self.operator_call_factory else { return false; };
        let selected = match self.selected_arithmetic_operators.remove(&(span.start_offset, span.end_offset)) {
            Some(value) => Ok(value),
            None => factory(self.callback_name(), token, unary),
        };
        let factory = match selected {
            Ok(Some(factory)) => factory,
            Ok(None) => return false,
            Err(error) => { self.error = Some(error); return true; }
        };
        self.add_with_span(Instruction::PrepareOperator(factory), span);
        for operand in operands { self.compile_expr(operand); }
        self.add_with_span(Instruction::CallPreparedOperator(operands.len() as u16), span);
        true
    }

    fn compile_bin_op(&mut self, c: &ast::Spanned<ast::BinOp<'source>>) {
        let token = match c.op {
            ast::BinOpKind::Add => Some("+"), ast::BinOpKind::Sub => Some("-"),
            ast::BinOpKind::Mul => Some("*"), ast::BinOpKind::Div => Some("/"),
            ast::BinOpKind::FloorDiv => Some("//"), ast::BinOpKind::Rem => Some("%"),
            ast::BinOpKind::Pow => Some("**"), _ => None,
        };
        if let Some(token) = token {
            if self.compile_intercepted_operator(token, false, &[&c.left, &c.right], c.span()) { return; }
        }
        self.push_span(c.span());
        let instr = match c.op {
            ast::BinOpKind::Eq => Instruction::Eq,
            ast::BinOpKind::Ne => Instruction::Ne,
            ast::BinOpKind::Lt => Instruction::Lt,
            ast::BinOpKind::Lte => Instruction::Lte,
            ast::BinOpKind::Gt => Instruction::Gt,
            ast::BinOpKind::Gte => Instruction::Gte,
            ast::BinOpKind::ScAnd | ast::BinOpKind::ScOr => {
                self.start_sc_bool();
                self.compile_expr(&c.left);
                self.sc_bool(matches!(c.op, ast::BinOpKind::ScAnd));
                self.compile_expr(&c.right);
                self.end_sc_bool();
                self.pop_span();
                return;
            }
            ast::BinOpKind::Add => Instruction::Add,
            ast::BinOpKind::Sub => Instruction::Sub,
            ast::BinOpKind::Mul => Instruction::Mul,
            ast::BinOpKind::Div => Instruction::Div,
            ast::BinOpKind::FloorDiv => Instruction::IntDiv,
            ast::BinOpKind::Rem => Instruction::Rem,
            ast::BinOpKind::Pow => Instruction::Pow,
            ast::BinOpKind::In => Instruction::In,
        };
        self.compile_expr(&c.left);
        self.compile_expr(&c.right);
        self.add(instr);
        self.pop_span();
    }

    /// Returns the size hint for buffers.
    ///
    /// This is a proposal for the initial buffer size when rendering directly to a string.
    pub fn buffer_size_hint(&self) -> usize {
        // for now the assumption is made that twice the bytes of template code without
        // control structures, rounded up to the next power of two is a good default.  The
        // round to the next power of two is chosen because the underlying vector backing
        // strings prefers powers of two.
        (self.raw_template_bytes * 2).next_power_of_two()
    }

    pub(crate) fn block_order(&self) -> &[&'source str] { &self.block_order }

    /// Converts the compiler into the instructions.
    pub fn finish(
        mut self,
    ) -> (
        Instructions<'source>,
        BTreeMap<&'source str, Instructions<'source>>,
    ) {
        assert!(self.pending_block.is_empty());
        recycle_pending_block_buffer(mem::take(&mut self.pending_block));
        recycle_span_stack_buffer(mem::take(&mut self.span_stack));
        (self.instructions, self.blocks)
    }
}


#[cfg(test)]
mod filtered_loop_tests {
    use std::sync::{Arc, Mutex};
    use crate::Environment;

    fn run(source: &str) -> (String, Vec<String>) {
        run_with_policy(source, false)
    }

    fn run_with_policy(source: &str, completed_iteration_policy: bool) -> (String, Vec<String>) {
        let trace = Arc::new(Mutex::new(Vec::<String>::new()));
        let mut env = Environment::new();
        env.set_loop_else_on_completed_iteration(completed_iteration_policy);
        let filter_trace = trace.clone();
        env.add_test("selected", move |value: i64| {
            filter_trace.lock().unwrap().push(format!("filter:{value}"));
            value % 2 == 1
        });
        let body_trace = trace.clone();
        env.add_function("emit", move |value: i64| {
            body_trace.lock().unwrap().push(format!("emit:{value}"));
            value
        });
        let output = env.template_from_str(source).unwrap()
            .render(crate::context!(values => vec![1, 2, 3], loopy => "L")).unwrap();
        let observed = trace.lock().unwrap().clone();
        (output, observed)
    }

    #[test]
    fn filtered_iteration_interleaves_predicate_and_body() {
        let (output, trace) = run("{% for x in values if x is selected %}{{ emit(x) }}{% endfor %}");
        assert_eq!(output, "13");
        assert_eq!(trace, ["filter:1", "emit:1", "filter:2", "filter:3", "emit:3"]);
    }

    #[test]
    fn filtered_metadata_looks_ahead_at_the_access_point() {
        for (field, expected) in [("last", "1:False;3:True;"),
                                  ("length", "1:2;3:2;"),
                                  ("nextitem|default('end')", "1:3;3:end;")] {
            let source = format!("{{% for x in values if x is selected %}}{{{{ emit(x) }}}}:{{{{ loop.{field} }}}};{{% endfor %}}");
            let (output, trace) = run(&source);
            assert_eq!(output, expected);
            assert_eq!(trace, ["filter:1", "emit:1", "filter:2", "filter:3", "emit:3"]);
        }
    }

    #[test]
    fn unrelated_identifier_or_literal_does_not_eagerly_filter() {
        for (source, expected) in [
            ("{% for x in values if x is selected %}{{ emit(x) }}{{ loopy }}{% endfor %}", "1L3L"),
            ("{% for x in values if x is selected %}{{ emit(x) }}loop{% endfor %}", "1loop3loop"),
        ] {
            let (output, trace) = run(source);
            assert_eq!(output, expected);
            assert_eq!(trace, ["filter:1", "emit:1", "filter:2", "filter:3", "emit:3"]);
        }
    }

    #[test]
    fn filtered_else_interleaves_and_handles_no_matching_values() {
        for policy in [false, true] {
            let (output, trace) = run_with_policy(
                "{% for x in values if x is selected %}{{ emit(x) }}{% else %}E{% endfor %}",policy);
            assert_eq!(output, "13");
            assert_eq!(trace, ["filter:1", "emit:1", "filter:2", "filter:3", "emit:3"]);
            let (output, trace) = run_with_policy(
                "{% for x in values if x is selected and x > 3 %}{{ emit(x) }}{% else %}E{% endfor %}",policy);
            assert_eq!(output, "E");
            assert_eq!(trace, ["filter:1", "filter:2", "filter:3"]);
        }
    }

    #[test]
    fn filtered_destructuring_streams_including_nested_targets() {
        let trace=Arc::new(Mutex::new(Vec::<String>::new()));
        let mut env=Environment::new();
        let selection=trace.clone();
        env.add_test("selected",move |value: i64| {
            selection.lock().unwrap().push(format!("filter:{value}"));
            value%2==1
        });
        let emissions=trace.clone();
        env.add_function("emit",move |value: i64| {
            emissions.lock().unwrap().push(format!("emit:{value}"));value
        });
        let source="{% for x, (y,z) in values if x is selected %}{{ emit(x) }}{% else %}E{% endfor %}";
        let result=env.template_from_str(source).unwrap().render(crate::context!(
            values => vec![(1,(2,3)),(2,(4,5)),(3,(6,7))])).unwrap();
        assert_eq!(result,"13");
        assert_eq!(*trace.lock().unwrap(),["filter:1","emit:1","filter:2","filter:3","emit:3"]);
    }

    #[cfg(feature = "loop_controls")]
    #[test]
    fn filtered_else_respects_break_and_continue_policy() {
        for control in ["break", "continue"] {
            let source=format!("{{% for x in values if x is selected %}}{{{{ emit(x) }}}}{{% {control} %}}{{% else %}}E{{% endfor %}}");
            let (normal, _) = run_with_policy(&source, false);
            let (jinja, _) = run_with_policy(&source, true);
            if control == "break" { assert_eq!(normal,"1"); assert_eq!(jinja,"1E"); }
            else { assert_eq!(normal,"13"); assert_eq!(jinja,"13E"); }
        }
    }

    #[test]
    fn filtered_loop_index_preserves_lazy_iteration() {
        let (output, trace) = run("{% for x in values if x is selected %}{{ emit(x) }}:{{ loop.index }};{% endfor %}");
        assert_eq!(output, "1:1;3:2;");
        assert_eq!(trace, ["filter:1", "emit:1", "filter:2", "filter:3", "emit:3"]);
    }

    #[test]
    fn filtered_loop_item_last_defers_lookahead_until_access() {
        let (output, trace) = run("{% for x in values if x is selected %}{{ emit(x) }}:{{ loop['last'] }};{% endfor %}");
        assert_eq!(output, "1:False;3:True;");
        assert_eq!(trace, ["filter:1", "emit:1", "filter:2", "filter:3", "emit:3"]);
    }

    #[test]
    fn unrelated_loop_reference_does_not_force_eager_filtering() {
        let (output, trace) = run("{% if false %}{{ loop }}{% endif %}{% for x in values if x is selected %}{{ emit(x) }}{% endfor %}");
        assert_eq!(output, "13");
        assert_eq!(trace, ["filter:1", "emit:1", "filter:2", "filter:3", "emit:3"]);
    }

    #[test]
    fn predicate_test_name_containing_loop_is_not_a_loop_reference() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let mut env = Environment::new();
        let observed = trace.clone();
        env.add_test("loopy", move |value: i64| {
            observed.lock().unwrap().push(format!("filter:{value}"));
            value % 2 == 1
        });
        let observed = trace.clone();
        env.add_function("emit", move |value: i64| {
            observed.lock().unwrap().push(format!("emit:{value}"));
            value
        });
        let source = "{% for x in values if x is loopy %}{{ loop.index }}:{{ emit(x) }};{% endfor %}";
        assert_eq!(env.render_str(source, crate::context!(values => vec![1, 2, 3])).unwrap(), "1:1;2:3;");
        assert_eq!(*trace.lock().unwrap(), ["filter:1", "emit:1", "filter:2", "filter:3", "emit:3"]);
    }

    #[test]
    fn factory_argument_name_containing_filter_variable_does_not_force_eager_filtering() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let mut env = Environment::new();
        let observed = trace.clone();
        env.add_function("values", move |_argument: i64| {
            let observed = observed.clone();
            crate::Value::make_iterable(move || {
                let observed = observed.clone();
                (1..=3).inspect(move |value| observed.lock().unwrap().push(format!("yield:{value}")))
            })
        });
        let observed = trace.clone();
        env.add_function("emit", move |value: i64| {
            observed.lock().unwrap().push(format!("emit:{value}"));
            value
        });
        let source = "{% for x in values(threshold_extra) if x > threshold %}{{ loop.index }}:{{ emit(x) }};{% endfor %}";
        assert_eq!(env.render_str(source, crate::context!(threshold => 1, threshold_extra => 7)).unwrap(), "1:2;2:3;");
        assert_eq!(*trace.lock().unwrap(), ["yield:1", "yield:2", "emit:2", "yield:3", "emit:3"]);
    }

    #[cfg(feature = "custom_syntax")]
    #[test]
    fn unrelated_loop_reference_with_custom_delimiters_keeps_filter_lazy() {
        let trace = Arc::new(Mutex::new(Vec::new()));
        let mut env = Environment::new();
        env.set_syntax(crate::syntax::SyntaxConfig::builder()
            .variable_delimiters("[[", "]]").build().unwrap());
        let observed = trace.clone();
        env.add_test("selected", move |value: i64| {
            observed.lock().unwrap().push(format!("filter:{value}"));
            value % 2 == 1
        });
        let observed = trace.clone();
        env.add_function("emit", move |value: i64| {
            observed.lock().unwrap().push(format!("emit:{value}"));
            value
        });
        let source = "{% if false %}[[ loop ]]{% endif %}{% for x in values if x is selected %}[[ emit(x) ]]{% endfor %}";
        assert_eq!(env.template_from_str(source).unwrap()
            .render(crate::context!(values => vec![1, 2, 3])).unwrap(), "13");
        assert_eq!(*trace.lock().unwrap(), ["filter:1", "emit:1", "filter:2", "filter:3", "emit:3"]);
    }

    #[cfg(feature = "loop_controls")]
    #[test]
    fn filtered_break_does_not_consume_remaining_items() {
        let (output, trace) = run("{% for x in values if x is selected %}{{ emit(x) }}{% break %}{% endfor %}");
        assert_eq!(output, "1");
        assert_eq!(trace, ["filter:1", "emit:1"]);
    }
}
