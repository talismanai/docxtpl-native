"""Python protocols for the native template runtime; no template rendering here.

This deliberately retains Python references. Environment subclasses and options
whose semantics are not implemented fail explicitly instead of being discarded.
"""
from __future__ import annotations

import builtins as _BUILTINS
from collections.abc import Callable, Generator, Iterator
from types import FunctionType as _FunctionType
from types import MethodType as _METHOD_TYPE
from typing import NoReturn

import jinja2.async_utils as _ASYNC_UTILS_MODULE
import jinja2.environment as _ENVIRONMENT_MODULE
import jinja2.nodes as _NODES_MODULE
import jinja2.runtime as _RUNTIME_MODULE
import jinja2.sandbox as _SANDBOX_MODULE
import jinja2.utils as _UTILS_MODULE
from jinja2 import Environment, Undefined
from jinja2.defaults import DEFAULT_TESTS
from jinja2.nodes import EvalContext
from jinja2.runtime import Context as _Context

# Names the native extension reads from this module's globals.
_NATIVE_GLOBALS = (Undefined, DEFAULT_TESTS, _METHOD_TYPE, _NODES_MODULE)

_PARSER_METHODS = tuple(
    (name, getattr(Environment, name), getattr(getattr(Environment, name), '__code__', None))
    for name in ('parse', '_parse', '_tokenize')
)


_SYNTAX_OPTIONS = ("block_start_string", "block_end_string", "variable_start_string", "variable_end_string",
                   "comment_start_string", "comment_end_string", "line_statement_prefix", "line_comment_prefix")


def _uses_standard_parser(environment: Environment) -> bool:
    for name, original, code in _PARSER_METHODS:
        actual = getattr(environment, name)
        function = getattr(actual, '__func__', actual)
        if (function is not original or getattr(function, '__code__', None) is not code
                or code is None or code.co_filename != _ENVIRONMENT_MODULE.__file__
                or code.co_name != name):
            return False
    return True

# Original implementation identity: replaced methods remain user callbacks.
_CONTEXT_CALL = _Context.call
_CONTEXT_CALL_CODE = _CONTEXT_CALL.__code__ if type(_CONTEXT_CALL) is _FunctionType else None
_PASS_ARG_FROM_OBJ = getattr(_UTILS_MODULE._PassArg.from_obj, '__func__', None)
_PASS_ARG_FROM_OBJ_CODE = _PASS_ARG_FROM_OBJ.__code__ if type(_PASS_ARG_FROM_OBJ) is _FunctionType else None
_OBJECT_NEW = object.__new__
_BUILTIN_NEXT = _BUILTINS.next
_MACRO_TYPE = _RUNTIME_MODULE.Macro
_TEMPLATE_REFERENCE_TYPE = _RUNTIME_MODULE.TemplateReference
_TEMPLATE_REFERENCE_INIT = _TEMPLATE_REFERENCE_TYPE.__init__
_TEMPLATE_REFERENCE_INIT_CODE = getattr(_TEMPLATE_REFERENCE_INIT, '__code__', None)
_RUNTIME_LOOP_TYPE = _RUNTIME_MODULE.LoopContext
_RUNTIME_ASYNC_LOOP_TYPE = _RUNTIME_MODULE.AsyncLoopContext
_RUNTIME_LOOP_REPR = _RUNTIME_LOOP_TYPE.__repr__
_ASYNC_ADAPTER_TYPE = _ASYNC_UTILS_MODULE._IteratorToAsyncIterator
_ASYNC_ADAPTER_ANEXT = getattr(_ASYNC_ADAPTER_TYPE, '__anext__', None)
_ASYNC_ADAPTER_ANEXT_CODE = _ASYNC_ADAPTER_ANEXT.__code__ if type(_ASYNC_ADAPTER_ANEXT) is _FunctionType else None
_ASYNC_ADAPTER_INIT = getattr(_ASYNC_ADAPTER_TYPE, '__init__', None)
_ASYNC_ADAPTER_INIT_CODE = _ASYNC_ADAPTER_INIT.__code__ if type(_ASYNC_ADAPTER_INIT) is _FunctionType else None
_ASYNC_ADAPTER_AITER = getattr(_ASYNC_ADAPTER_TYPE, '__aiter__', None)
_ASYNC_ADAPTER_AITER_CODE = _ASYNC_ADAPTER_AITER.__code__ if type(_ASYNC_ADAPTER_AITER) is _FunctionType else None


def _loop_code_snapshots(cls: type) -> tuple[tuple[object, object], ...]:
    snapshots: list[tuple[object, object]] = []
    for member in cls.__dict__.values():
        if type(member) is property:
            functions = (member.fget, member.fset, member.fdel)
        elif type(member) is staticmethod or type(member) is classmethod:
            functions = (member.__func__,)
        else:
            functions = (member,)
        for function in functions:
            if type(function) is _FunctionType:
                snapshots.append((function, function.__code__))
    return tuple(snapshots)


_RUNTIME_LOOP_CLASS_ITEMS = tuple(_RUNTIME_LOOP_TYPE.__dict__.items())
_RUNTIME_ASYNC_LOOP_CLASS_ITEMS = tuple(_RUNTIME_ASYNC_LOOP_TYPE.__dict__.items())
_RUNTIME_LOOP_CLASS_CODES = _loop_code_snapshots(_RUNTIME_LOOP_TYPE)
_RUNTIME_ASYNC_LOOP_CLASS_CODES = _loop_code_snapshots(_RUNTIME_ASYNC_LOOP_TYPE)
_RUNTIME_LOOP_CHANGED = _RUNTIME_LOOP_TYPE.changed
_RUNTIME_LOOP_CHANGED_CODE = _RUNTIME_LOOP_CHANGED.__code__ if type(_RUNTIME_LOOP_CHANGED) is _FunctionType else None
_RUNTIME_LOOP_CYCLE = _RUNTIME_LOOP_TYPE.cycle
_RUNTIME_LOOP_CYCLE_CODE = _RUNTIME_LOOP_CYCLE.__code__ if type(_RUNTIME_LOOP_CYCLE) is _FunctionType else None
_RUNTIME_LOOP_CALL = _RUNTIME_LOOP_TYPE.__call__
_RUNTIME_LOOP_CALL_CODE = _RUNTIME_LOOP_CALL.__code__ if type(_RUNTIME_LOOP_CALL) is _FunctionType else None
def _standard_callbacks() -> dict[int, tuple[object, object]]:
    defaults = Environment()
    return {id(value): (value, value.__code__ if type(value) is _FunctionType else None)
            for value in (*defaults.filters.values(), *defaults.tests.values())}


# The default environment's filters and tests: calling one runs no user code.
_STANDARD_CALLBACKS = _standard_callbacks()
_RUNTIME_LOOP_INIT = _RUNTIME_LOOP_TYPE.__init__
_RUNTIME_LOOP_INIT_CODE = _RUNTIME_LOOP_INIT.__code__ if type(_RUNTIME_LOOP_INIT) is _FunctionType else None
_MACRO_INIT = _MACRO_TYPE.__init__
_MACRO_INIT_CODE = _MACRO_INIT.__code__ if type(_MACRO_INIT) is _FunctionType else None
_MACRO_CALL = _MACRO_TYPE.__call__
_MACRO_CALL_CODE = _MACRO_CALL.__code__ if type(_MACRO_CALL) is _FunctionType else None
_MACRO_INVOKE = _MACRO_TYPE._invoke
_MACRO_INVOKE_CODE = _MACRO_INVOKE.__code__ if type(_MACRO_INVOKE) is _FunctionType else None
_MACRO_ASYNC_INVOKE = _MACRO_TYPE._async_invoke
_MACRO_ASYNC_INVOKE_CODE = _MACRO_ASYNC_INVOKE.__code__ if type(_MACRO_ASYNC_INVOKE) is _FunctionType else None
_CONTEXT_INIT = _Context.__init__
_CONTEXT_INIT_CODE = _CONTEXT_INIT.__code__ if type(_CONTEXT_INIT) is _FunctionType else None
_CONTEXT_GET_EXPORTED = _Context.get_exported
_CONTEXT_GET_EXPORTED_CODE = getattr(_CONTEXT_GET_EXPORTED, '__code__', None)
_EVAL_CONTEXT_TYPE = EvalContext
_EVAL_CONTEXT_INIT = EvalContext.__init__
_EVAL_CONTEXT_INIT_CODE = _EVAL_CONTEXT_INIT.__code__ if type(_EVAL_CONTEXT_INIT) is _FunctionType else None
_EVAL_CONTEXT_SAVE = EvalContext.save
_EVAL_CONTEXT_SAVE_CODE = _EVAL_CONTEXT_SAVE.__code__ if type(_EVAL_CONTEXT_SAVE) is _FunctionType else None
_EVAL_CONTEXT_REVERT = EvalContext.revert
_EVAL_CONTEXT_REVERT_CODE = _EVAL_CONTEXT_REVERT.__code__ if type(_EVAL_CONTEXT_REVERT) is _FunctionType else None
_ENV_GETATTR = Environment.getattr
_ENV_GETATTR_CODE = _ENV_GETATTR.__code__ if type(_ENV_GETATTR) is _FunctionType else None
_ENV_GETITEM = Environment.getitem
_ENV_GETITEM_CODE = _ENV_GETITEM.__code__ if type(_ENV_GETITEM) is _FunctionType else None
_CONTEXT_GET_ALL = _Context.get_all
_CONTEXT_GET_ALL_CODE = _CONTEXT_GET_ALL.__code__ if type(_CONTEXT_GET_ALL) is _FunctionType else None
_CONTEXT_RESOLVE = _Context.resolve_or_missing
_CONTEXT_RESOLVE_CODE = _CONTEXT_RESOLVE.__code__ if type(_CONTEXT_RESOLVE) is _FunctionType else None
_CONTEXT_DERIVED = _Context.derived
_CONTEXT_DERIVED_CODE = _CONTEXT_DERIVED.__code__ if type(_CONTEXT_DERIVED) is _FunctionType else None
_RUNTIME_NEW_CONTEXT = _RUNTIME_MODULE.new_context
_RUNTIME_NEW_CONTEXT_CODE = _RUNTIME_NEW_CONTEXT.__code__ if type(_RUNTIME_NEW_CONTEXT) is _FunctionType else None
_SANDBOX_CALL = _SANDBOX_MODULE.SandboxedEnvironment.call
_SANDBOX_CALL_CODE = _SANDBOX_CALL.__code__ if type(_SANDBOX_CALL) is _FunctionType else None
_SANDBOX_GETATTR = _SANDBOX_MODULE.SandboxedEnvironment.getattr
_SANDBOX_GETATTR_CODE = _SANDBOX_GETATTR.__code__ if type(_SANDBOX_GETATTR) is _FunctionType else None
_SANDBOX_GETITEM = _SANDBOX_MODULE.SandboxedEnvironment.getitem
_SANDBOX_GETITEM_CODE = _SANDBOX_GETITEM.__code__ if type(_SANDBOX_GETITEM) is _FunctionType else None
_SANDBOX_WRAP_STR_FORMAT = _SANDBOX_MODULE.SandboxedEnvironment.wrap_str_format
_SANDBOX_WRAP_STR_FORMAT_CODE = _SANDBOX_WRAP_STR_FORMAT.__code__ if type(_SANDBOX_WRAP_STR_FORMAT) is _FunctionType else None
_SANDBOX_IS_SAFE_ATTRIBUTE = _SANDBOX_MODULE.SandboxedEnvironment.is_safe_attribute
_SANDBOX_IS_SAFE_ATTRIBUTE_CODE = _SANDBOX_IS_SAFE_ATTRIBUTE.__code__ if type(_SANDBOX_IS_SAFE_ATTRIBUTE) is _FunctionType else None
_SANDBOX_IS_INTERNAL_ATTRIBUTE = _SANDBOX_MODULE.is_internal_attribute
_SANDBOX_IS_INTERNAL_ATTRIBUTE_CODE = _SANDBOX_IS_INTERNAL_ATTRIBUTE.__code__ if type(_SANDBOX_IS_INTERNAL_ATTRIBUTE) is _FunctionType else None
_runtime_sys = _RUNTIME_MODULE.__dict__.get('sys')
_runtime_modules = _runtime_sys.__dict__.get('modules') if type(_runtime_sys) is type(_RUNTIME_MODULE) else None
_SANDBOX_TYPES = _runtime_modules.get('types') if type(_runtime_modules) is dict else None
if _SANDBOX_TYPES is None:
    import types as _SANDBOX_TYPES
_SANDBOX_TYPE_NAMES = ('FunctionType', 'MethodType', 'BuiltinMethodType', 'CodeType', 'TracebackType', 'FrameType', 'GeneratorType', 'CoroutineType', 'AsyncGeneratorType')
_SANDBOX_TYPE_VALUES = tuple(_SANDBOX_TYPES.__dict__.get(name) for name in _SANDBOX_TYPE_NAMES) if type(_SANDBOX_TYPES) is type(_RUNTIME_MODULE) else ()
_BUILTIN_GETATTR = _BUILTINS.getattr
_BUILTIN_ISINSTANCE = _BUILTINS.isinstance
_BUILTIN_HASATTR = _BUILTINS.hasattr
_BUILTIN_TYPE = _BUILTINS.type
_BUILTIN_STR = _BUILTINS.str
_SANDBOX_IS_SAFE_CALLABLE = _SANDBOX_MODULE.SandboxedEnvironment.is_safe_callable
_SANDBOX_IS_SAFE_CALLABLE_CODE = _SANDBOX_IS_SAFE_CALLABLE.__code__ if type(_SANDBOX_IS_SAFE_CALLABLE) is _FunctionType else None
_SANDBOX_CALL_BINOP = _SANDBOX_MODULE.SandboxedEnvironment.call_binop
_SANDBOX_CALL_BINOP_CODE = _SANDBOX_CALL_BINOP.__code__ if type(_SANDBOX_CALL_BINOP) is _FunctionType else None
_SANDBOX_CALL_UNOP = _SANDBOX_MODULE.SandboxedEnvironment.call_unop
_SANDBOX_CALL_UNOP_CODE = _SANDBOX_CALL_UNOP.__code__ if type(_SANDBOX_CALL_UNOP) is _FunctionType else None
from markupsafe import escape

_ORIGINAL_TEMPLATE_MODULE = _ENVIRONMENT_MODULE.TemplateModule
_ORIGINAL_BLOCK_REFERENCE_CALL = _RUNTIME_MODULE.BlockReference.__call__


def _block_reference_call_is_standard() -> bool:
    """Whether ``BlockReference.__call__`` is still Jinja's own."""
    current = _RUNTIME_MODULE.BlockReference.__call__
    code = getattr(current, '__code__', None)
    return (current is _ORIGINAL_BLOCK_REFERENCE_CALL and code is not None
            and code.co_filename == _RUNTIME_MODULE.__file__ and code.co_name == '__call__')
_ORIGINAL_TEMPLATE_MODULE_STR = _ORIGINAL_TEMPLATE_MODULE.__str__
_ORIGINAL_TEMPLATE_MODULE_HTML = _ORIGINAL_TEMPLATE_MODULE.__html__
_ORIGINAL_ENVIRONMENT_CONCAT = _ENVIRONMENT_MODULE.concat


class _NativeModuleTemplate:
    """The imported template as TemplateModule.__init__ reads it."""

    __slots__ = ('name',)

    def __init__(self, name: object) -> None:
        self.name = name


class _NativeModuleContext:
    """The module context as TemplateModule.__init__ reads it."""

    __slots__ = ('_exported',)

    def __init__(self, exported: dict) -> None:
        self._exported = exported

    def get_exported(self) -> dict:
        return self._exported


def _native_template_module(name: object, exported: dict, body_stream: list) -> object:
    """Build the imported module with jinja2.environment's current TemplateModule."""
    module_class = _ENVIRONMENT_MODULE.TemplateModule
    # Stand-ins with the attributes TemplateModule reads.
    return module_class(_NativeModuleTemplate(name), _NativeModuleContext(exported), body_stream)  # ty: ignore[invalid-argument-type]


def _is_template_module_method(function: object, name: str) -> bool:
    """A TemplateModule method defined by jinja2.environment, even if the
    class was changed before this module was imported."""
    code = getattr(function, '__code__', None)
    return (type(function) is _FunctionType and code is not None
            and code.co_filename == _ENVIRONMENT_MODULE.__file__ and code.co_name == name
            and getattr(function, '__qualname__', None) == 'TemplateModule.' + name)


def _native_module_text_is_standard(module_class: type, instance: object = None) -> bool:
    """Whether str()/Markup of a module would run the original algorithm."""
    if instance is not None:
        attributes = getattr(instance, '__dict__', {})
        if '__str__' in attributes or '__html__' in attributes:
            return False
    concat = _ENVIRONMENT_MODULE.concat
    return (module_class is _ORIGINAL_TEMPLATE_MODULE
            and module_class.__str__ is _ORIGINAL_TEMPLATE_MODULE_STR
            and module_class.__html__ is _ORIGINAL_TEMPLATE_MODULE_HTML
            and _is_template_module_method(module_class.__str__, '__str__')
            and _is_template_module_method(module_class.__html__, '__html__')
            and concat is _ORIGINAL_ENVIRONMENT_CONCAT
            and getattr(concat, '__self__', None) == '' and getattr(concat, '__name__', None) == 'join')


# Contexts whose ``blocks`` the binding populated: their registry is authoritative.
_NATIVE_BLOCK_REGISTRIES = __import__('weakref').WeakSet()


def _native_block_function(name: str, runner: Callable[[object], object], asynchronous: bool) -> object:
    """A block function as Jinja compiles it, rendering the block natively."""
    if asynchronous:
        async def block(context):
            yield runner(context)
    else:
        def block(context):
            yield runner(context)
    block.__name__ = block.__qualname__ = 'block_' + name
    block._native_block = runner  # ty: ignore[invalid-assignment] - a function attribute
    return block


def _native_filter_generator(items, name):
    """A generator over a native filter iterator, named like Jinja's function."""
    def generator():
        # Jinja's filter generators loop: values passed to send() are ignored.
        for item in items:  # noqa: UP028
            yield item
    result = generator()
    result.__name__ = result.__qualname__ = name
    return result


def _native_async_variant(name, step):
    """The coroutine of an async_variant filter, named like Jinja's: `step`
    runs the body natively, or returns the reference coroutine to await."""
    async def variant():
        done, value = step()
        if done:
            return value
        return await value
    coroutine = variant()
    coroutine.__name__ = coroutine.__qualname__ = name
    return coroutine


def _native_async_select(name, filter_):
    """The coroutine of an async select/reject filter: it returns the async
    generator of accepted items, as Jinja's async_select_or_reject."""
    async def select():
        return _native_async_accepted(filter_)
    coroutine = select()
    coroutine.__name__ = coroutine.__qualname__ = name
    return coroutine


def _native_async_accepted(filter_):
    async def accepted():
        mode, source = filter_.start()
        if mode == 'sync':
            # auto_aiter's adapter: next() until StopIteration.
            while True:
                try:
                    item = next(source)
                except StopIteration:
                    break
                if filter_.accept(item):
                    yield item
        elif mode == 'async':
            async for item in source:
                if filter_.accept(item):
                    yield item
    generator = accepted()
    generator.__name__ = generator.__qualname__ = 'async_select_or_reject'
    return generator


def _native_async_map(filter_, globals_):
    """Jinja's async do_map generator; each result goes through the filters
    module's auto_await."""
    async def mapped():
        mode, source = filter_.start()
        if mode == 'sync':
            while True:
                try:
                    item = next(source)
                except StopIteration:
                    break
                yield await globals_['auto_await'](filter_.apply(item))
        elif mode == 'async':
            async for item in source:
                yield await globals_['auto_await'](filter_.apply(item))
    generator = mapped()
    generator.__name__ = generator.__qualname__ = 'do_map'
    return generator


def _native_filtered_items(items):
    """The accepted items of a filtered loop, as the generator Jinja compiles."""
    # A loop, as compiled: values passed to send() are ignored.
    for item in items:  # noqa: UP028
        yield item


async def _native_filtered_items_async(items):
    """The accepted items of a filtered async loop, as an async generator."""
    async for item in items:
        yield item


async def _await_native_render(render):
    """Expose a CPython coroutine to asyncio while Rust owns VM execution."""
    return await render


async def _await_native_value(value: object) -> object:
    """Expose CPython await delegation for the private native diagnostic."""
    return await value  # ty: ignore[invalid-await] -- checked by the Rust bridge


def _load_native_runtime(names: tuple[str, ...]) -> object:
    """Invoke Python's import protocol for the compiled-template boundary."""
    return __import__('jinja2.runtime', globals(), locals(), names, 0)


def _load_runtime_with_selected_method(
    loader: Callable[[tuple[str, ...]], object], names: tuple[str, ...], selected: object
) -> object:
    """Keep the selected method in this frame if the import raises."""
    return loader(names)


def _exported_comprehension_frame(error: BaseException, frame: list[object]) -> NoReturn:
    """Raise `error` from a frame that keeps the comprehension's iterator and key.

    Only `frame` stays in this frame's locals, so the traceback retains it
    without forming a cycle through the error.
    """
    try:
        raise error
    finally:
        del error


def _runtime_attribute_with_selected_method(
    resolve: Callable[[str], object], name: str, selected: object
) -> object:
    """Keep the selected method alive if resolving an import raises."""
    return resolve(name)


def _load_native_compiler_runtime(compiler_globals: dict[str, object], name: str) -> object:
    """Invoke the compiler's relative runtime import protocol."""
    return __import__('runtime', compiler_globals, None, (name,), 1)


def _indent_generator(iterator: Iterator[object]) -> Generator[object, None, None]:
    """Expose CPython's generator protocol over the Rust-owned indent iterator."""
    return (item for item in iterator)


def _traceback_probe_exec(code: object, globals_: dict[str, object], locals_: dict[str, object]) -> object:
    """Create an experimental traceback through a retained binding frame."""
    try:
        exec(code, globals_, locals_)  # noqa: S102  # ty: ignore[invalid-argument-type]
    except BaseException as error:
        if error is not globals_['__jinja_exception__']:
            raise
        traceback = BaseException.__traceback__.__get__(error)  # ty: ignore[missing-attribute]
        if traceback is None or traceback.tb_next is None:
            raise RuntimeError('traceback diagnostic did not create a template frame') from error
        frames = [traceback.tb_next]
        return frames[0]
    raise RuntimeError('traceback diagnostic unexpectedly returned')


class BridgeConfig:
    def __init__(self, environment, autoescape):
        self._native_loaded = {}
        # Set by DocxTemplate.render for its own special-value collection.
        self._native_internal_special_collection = False
        from . import UnsupportedFeature
        if environment is None:
            environment = Environment(autoescape=autoescape)
        elif autoescape:
            # This mutation is also part of docxtpl.render's public behavior.
            environment.autoescape = True
        custom_extensions = False
        if environment.extensions:
            from jinja2.ext import ExprStmtExtension, LoopControlExtension
            allowed={ExprStmtExtension:{'do'},LoopControlExtension:{'break','continue'}}
            for extension in environment.extensions.values():
                cls=type(extension)
                if (cls not in allowed or extension.tags!=allowed[cls]
                    or set(vars(extension))!={'environment'} or extension.environment is not environment
                    or cls.parse.__module__!='jinja2.ext'
                    or cls.parse.__qualname__!=cls.__name__+'.parse'):
                    custom_extensions = True
        baseline = Environment()
        for name in ['template_class','code_generator_class','concat','join_path',
                     'compile','parse','_parse','_generate','_tokenize','from_string','get_template','select_template','get_or_select_template']:
            actual=getattr(environment,name)
            expected=getattr(baseline,name)
            if getattr(actual,'__func__',actual) is not getattr(expected,'__func__',expected):
                raise UnsupportedFeature('custom Environment hook needs a native runtime adapter: '+name)
        if environment.bytecode_cache is not None:
            raise UnsupportedFeature('Jinja2 bytecode caches need a native compiler adapter')
        # MiniJinja keeps line statements' and line comments' whitespace
        # differently from Jinja's lexer: those prefixes stay unsupported.
        if any(getattr(environment, key) != getattr(baseline, key)
               for key in ("line_statement_prefix", "line_comment_prefix", "newline_sequence")):
            raise UnsupportedFeature("custom Jinja2 line prefixes/newlines need a native compiler adapter")
        # Custom delimiters and line prefixes go to the native syntax; the
        # native frontend's closed grammar only reads the default ones.
        self._default_syntax = all(getattr(environment, key) == getattr(baseline, key) for key in _SYNTAX_OPTIONS)
        asynchronous = bool(environment.is_async)
        self.environment = environment
        self._custom_extensions = custom_extensions
        self.is_async = asynchronous
        self.package_values_live = False
        self.filters = dict(environment.filters)
        self.tests = dict(environment.tests)
        self.globals = dict(environment.globals)
        self.trim_blocks = environment.trim_blocks
        self.lstrip_blocks = environment.lstrip_blocks
        self.keep_trailing_newline = environment.keep_trailing_newline
        self._compile_eval_contexts = {}
        self._compile_sources = {}
        self._compile_outputs = {}
        self._compile_finalizers = {}
        self._compile_trees = {}
        self._compile_ast_nodes = {}
        self._simple_names = None
        self._simple_objects = False
        self._simple_sources = set()
        self.native_frontend_sources = 0

    def enable_simple_sources(self, context: object, objects: bool = False) -> None:
        from .simple_frontend import (
            _STANDARD_CONTEXT_NAMES,
            _STANDARD_CONTEXT_NAMES_CODE,
            context_names,
        )
        if context_names is _STANDARD_CONTEXT_NAMES and context_names.__code__ is _STANDARD_CONTEXT_NAMES_CODE:
            self._simple_names = context_names(context, compare_metaclasses=False)
        else:
            self._simple_names = context_names(context)
        # With `objects`, a context of other Python values still names its
        # roots (read without running user code). Their attributes, methods
        # and protocols can run user code while a part renders, so only the
        # source validated before any rendering uses the native frontend
        # (`end_object_sources`).
        if (self._simple_names is None and objects and type(context) is dict
                and all(type(key) is str for key in context)):
            self._simple_names = frozenset(context)
            self._simple_objects = True

    def end_object_sources(self) -> None:
        """After the first rendered part, user code may have run."""
        if self._simple_objects:
            self._simple_names = None

    def autoescape(self, name):
        setting = self.environment.autoescape
        # Body/header/property sources are anonymous templates in docxtpl.
        return bool(setting(name) if callable(setting) else setting)

    def context(self, visible, autoescape):
        from . import _extension
        return _extension().python_context(self, visible, autoescape)

    def lookup(self, obj, key, attribute_first):
        from . import _extension
        return _extension().python_lookup(self.environment, obj, key, attribute_first)

    def _check_unique_blocks(self, name, tree, source):
        # Jinja's code generator rejects a block name used twice before it
        # compiles anything else (CodeGenerator.visit_Template).
        from jinja2 import nodes
        from jinja2.exceptions import TemplateAssertionError
        seen = set()
        for block in tree.find_all(nodes.Block):
            if block.name in seen:
                # docxtpl's parts are anonymous templates; loaded ones are named
                template_name = name if name in self._native_loaded else None
                error = TemplateAssertionError(f"block {block.name!r} defined twice", block.lineno, template_name,
                                               None)
                # as Environment.handle_exception leaves a compile error
                error.translated, error.source = True, source
                raise error
            seen.add(block.name)

    def validate_source(self, name, source):
        # Parse with the supplied source language, including disabled tags and
        # exact parser exceptions. The AST is not executed by Python.
        import re

        from jinja2 import TemplateError
        if (self._simple_names is not None and not (self._custom_extensions or self.is_async)
                and self._default_syntax and _uses_standard_parser(self.environment)):
            from .simple_frontend import accepts
            if accepts(source,self._simple_names):
                self._compile_finalizers[name]=(False,None,None)
                # The compiler's arithmetic and argument hooks read it, as for
                # a source the reference parser validated.
                self._compile_eval_contexts[name]=self._compile_eval_context()
                self._simple_sources.add(name)
                self.native_frontend_sources+=1
                return False,False
            # A complex source can call arbitrary code or mutate its environment.
            # Do not assume later parts still have the initial default contract.
            self._simple_names=None
        self._simple_sources.discard(name)
        try:
            tree=self.environment.parse(source)
            self._check_unique_blocks(name, tree, source)
            self._compile_eval_contexts[name]=self._compile_eval_context()
            self._compile_sources[name]=source.encode('utf-8')
            # Match output AST nodes to complete variable-token ranges once.
            # The parser does not provide expression columns. When a source
            # also uses print statements, retain the per-expression parse path.
            from jinja2 import nodes
            output_nodes=list(tree.find_all(nodes.Output))
            finalizer=self.environment.finalize if output_nodes else None
            present=bool(finalizer)
            finalizer_kind=getattr(getattr(finalizer,'jinja_pass_arg',None),'name',None) if present else None
            self._compile_finalizers[name]=(present,finalizer_kind,finalizer)
            self._compile_ast_nodes.pop(name,None)
            from . import _extension
            if (self._custom_extensions or not _uses_standard_parser(self.environment)
                    or _extension().requires_foreign_literals(source)):
                self._compile_trees[name]=tree
                return present,finalizer_kind=='context'
            expressions=[value for output in output_nodes for value in output.nodes
                         if not isinstance(value,nodes.TemplateData)]
            ranges=[];cursor=0;begin=None
            for line,kind,value in self.environment.lexer.tokeniter(source,None,None):
                position=source.find(value,cursor)
                if position<0:ranges=[];break
                cursor=position+len(value)
                if kind=='variable_begin':begin=(cursor,line)
                elif kind=='variable_end' and begin is not None:
                    ranges.append((begin[0],position,begin[1]));begin=None
            if len(ranges)!=len(expressions) or not all(item[2]==node.lineno for item,node in zip(ranges,expressions)):
                # parse('{{ ... }}') yields one Output node.
                expressions=[self.environment.parse(self._output_source(source[start:end])).body[0].nodes[0]  # ty: ignore[missing-union-attribute]
                             for start,end,_ in ranges]
            offsets=None
            if not source.isascii():
                from array import array
                offsets=array('I',[0]);total=0
                for character in source:total+=len(character.encode('utf-8'));offsets.append(total)
            byte=lambda offset:offset if offsets is None else offsets[offset]
            self._compile_outputs[name]=[(byte(start),byte(end),node) for (start,end,_),node in zip(ranges,expressions)]
            return present,finalizer_kind=='context'
        except TemplateError as error:
            # docxtpl's own context lines: a lazy map, read after hasattr.
            if name.startswith('word/') and getattr(error,'lineno',None) is not None:
                start=max(error.lineno-4,0)  # ty: ignore[missing-attribute]
                error.docx_context=map(lambda line:re.sub(r'<[^>]+>','',line),source.splitlines()[start:start+7])  # noqa: C417  # ty: ignore[missing-attribute]
            raise

    def _compile_eval_context(self) -> EvalContext:
        context=object.__new__(EvalContext)
        context.environment=self.environment
        context.volatile=False
        return context

    def _output_source(self, expression: str) -> str:
        """`expression` as one output tag in the environment's syntax."""
        environment = self.environment
        return environment.variable_start_string + ' ' + expression + ' ' + environment.variable_end_string

    def release_compilation(self, name: str) -> None:
        for field in ('_compile_eval_contexts', '_compile_sources', '_compile_outputs',
                      '_compile_finalizers', '_compile_trees', '_compile_ast_nodes'):
            getattr(self, field).pop(name, None)
        self._simple_sources.discard(name)

    def constant_output(self, name, start, end, autoescape):
        if name in self._simple_sources:return None
        if autoescape is None:return None
        present,kind,finalizer=self._compile_finalizers[name]
        if present and kind in ('context','eval_context'):return None
        context=self._compile_eval_contexts[name]
        if context.volatile:return None
        context.autoescape=autoescape
        try:
            from bisect import bisect_right
            if name in self._compile_ast_nodes:
                node=self._compile_ast_nodes[name][start]
            else:
                outputs=self._compile_outputs[name]
                index=bisect_right(outputs,start,key=lambda item:item[0])-1
                if index>=0 and outputs[index][0]<=start<=end<=outputs[index][1]:
                    node=outputs[index][2]
                else:
                    expression=self._compile_sources[name][start:end].decode('utf-8')
                    node=self.environment.parse(self._output_source(expression)).body[0].nodes[0]  # ty: ignore[missing-union-attribute]
            from . import _extension
            value=_extension().constant_value(node,context)
            if autoescape:value=escape(value)
            if present:
                value=finalizer(self.environment,value) if kind=='environment' else finalizer(value)
            return str(value)
        except Exception:  # noqa: BLE001
            # As Jinja's constant folding: a failed evaluation is retried at runtime.
            return None

    def invoke(self, callback, args, kwargs, visible, autoescape, trusted=False):
        from . import _extension
        return _extension().python_invoke(self, callback, args, kwargs, visible,
                                          autoescape, trusted)

    def format_value(self, value, visible, autoescape, name=None, prepared=None, has_prepared=False):
        from . import _extension
        return _extension().python_format_value(self, value, visible, autoescape,
                                                 name, prepared, has_prepared)

    def concat(self, values, autoescape):
        from . import _extension
        return _extension().python_concat(values, autoescape)

    def operate(self, op, left, right, autoescape):
        if op=='concat':
            return self.concat([left,right],autoescape)
        from . import _extension
        return _extension().python_operator(op,left,right)

    def missing(self, obj, name, root):
        if root:
            return self.environment.undefined(name=name)
        return self.environment.undefined(obj=obj, name=name)

    def load(self, name):
        from jinja2 import TemplateNotFound
        if self.environment.loader is None:
            raise TypeError("no loader for this environment specified")
        try:
            source, _filename, uptodate = self.environment.loader.get_source(self.environment, name)
        except TemplateNotFound:
            return None
        # What decides, as Environment._load_template does, whether the
        # loaded template may be reused: the cache it went into and uptodate.
        self._native_loaded[name] = (self.environment.cache, uptodate)
        return source

    def loaded_is_valid(self, name):
        """Whether Jinja would reuse the template loaded for ``name``."""
        recorded = self._native_loaded.get(name)
        if recorded is None:
            return False
        cache, uptodate = recorded
        current = self.environment.cache
        if current is None or current is not cache:
            return False
        if self.environment.auto_reload and uptodate is not None:
            return bool(uptodate())
        return True


def requires_live_model(context, environment):
    """Bind the exposed Python types to the native graph classifier."""
    from . import (
        Listing,
        RichText,
        RichTextParagraph,
        Subdoc,
        _extension,
        _OriginalSubdoc,
    )
    leaves=(int,float,bool,bytes,type(None),RichText,RichTextParagraph,Listing)
    return _extension().requires_live_model(context,environment,leaves,Subdoc,_OriginalSubdoc)


def collect_specials(context, config):
    """Bind the exposed types and converter to the native collector."""
    from . import (
        InlineImage,
        Subdoc,
        _extension,
        _OriginalInlineImage,
        _OriginalSubdoc,
        _value,
    )
    roots=config.globals
    internal = type(config) is BridgeConfig and getattr(config, '_native_internal_special_collection', False)
    args=(context,roots,(InlineImage,Subdoc),_value,_OriginalSubdoc,_OriginalInlineImage)
    return _extension().collect_specials(*args,True) if internal else _extension().collect_specials(*args)

# Binding-only adapters: CPython owns keyword expansion and generator identity.
def _call_with_mapping(function, argument, keywords):
    return function(argument, **keywords)


def _context_block_generator(items):
    yield from items
