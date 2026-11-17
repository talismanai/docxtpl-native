"""Foreign object builders execute in Rust while keeping original Python types."""
import sys
import gc
import subprocess
import textwrap
import unittest
import weakref
from contextlib import contextmanager
from unittest.mock import patch
import docxtpl_native
import jinja2
from jinja2.runtime import Context
from jinja2.nodes import EvalContext
from docxtpl_native.bridge import BridgeConfig
from native_audit import forbid
from paritylab.protocol_base import generate, observed


@contextmanager
def block_builders():
    forbidden = {Context.__init__.__code__, EvalContext.__init__.__code__}
    previous = sys.getprofile()
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code in forbidden:
            raise AssertionError('Python context constructor called')
    sys.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous)


class NativeContextBuilderTests(unittest.TestCase):
    def test_eval_context_observes_changed_default_arguments(self):
        original = EvalContext.__init__.__defaults__
        try:
            for defaults in (('named-default',), None, ()):
                EvalContext.__init__.__defaults__ = defaults
                def run(native):
                    trace = []
                    environment = jinja2.Environment(autoescape=lambda name: trace.append(name) or False)
                    try:
                        if native:
                            with block_builders():
                                context = docxtpl_native._extension().python_eval_context(environment)
                        else:
                            context = EvalContext(environment)
                        return 'ok', context.autoescape, trace
                    except TypeError as error:
                        return type(error).__name__, str(error), trace
                with self.subTest(defaults=defaults):
                    self.assertEqual(run(True), run(False))
        finally:
            EvalContext.__init__.__defaults__ = original

    def test_context_preserves_block_mapping_protocol_and_errors(self):
        for raises in (False, True):
            def run(native):
                trace = []
                failure = ValueError('blocks failed')
                class Blocks(dict):
                    def items(self):
                        trace.append('items')
                        if raises:
                            raise failure
                        return [('override', 'callback')]
                blocks = Blocks(ignored='original')
                environment = jinja2.Environment()
                try:
                    if native:
                        with block_builders():
                            context = docxtpl_native._extension().python_new_context(environment, {}, None, blocks, {})
                    else:
                        context = Context(environment, {}, None, blocks, globals={})
                    return 'ok', context.blocks, trace
                except ValueError as error:
                    return 'error', error is failure, trace
            with self.subTest(raises=raises):
                self.assertEqual(run(True), run(False))

    def test_context_reads_its_stored_environment_before_eval_construction(self):
        original = Context.__getattribute__
        environment = jinja2.Environment()
        replacement = jinja2.Environment(autoescape=True)
        trace = []
        def getattribute(context, name):
            trace.append(name)
            if name == 'environment':
                return replacement
            return original(context, name)
        with patch.object(Context, '__getattribute__', getattribute):
            reference = Context(environment, {}, None, {}, globals={})
            expected = trace[:]
            trace.clear()
            with block_builders():
                native = docxtpl_native._extension().python_new_context(environment, {}, None, {}, {})
            self.assertEqual(trace, expected)
        self.assertIs(native.eval_ctx.environment, reference.eval_ctx.environment)
        self.assertIs(native.eval_ctx.environment, replacement)

    def test_native_instances_participate_in_python_cycle_collection(self):
        environment = jinja2.Environment()
        parent = {}
        with block_builders():
            context = docxtpl_native._extension().python_new_context(environment, parent, None, {}, {})
        retained = weakref.ref(context)
        evaluation = weakref.ref(context.eval_ctx)
        parent['context'] = context
        del context, parent
        gc.collect()
        self.assertIsNone(retained())
        self.assertIsNone(evaluation())

    def test_context_resolves_rebound_set_from_its_function_globals(self):
        import jinja2.runtime as runtime
        environment = jinja2.Environment()
        trace = []
        def make_set(*args):
            trace.append(args)
            return set(*args)
        with patch.object(runtime, 'set', make_set, create=True):
            Context(environment, {}, None, {}, globals={'key': 1})
            expected = trace[:]
            trace.clear()
            with block_builders():
                context = docxtpl_native._extension().python_new_context(environment, {}, None, {}, {'key': 1})
            self.assertEqual(trace, expected)
            self.assertEqual(context.globals_keys, {'key'})

    def test_eval_context_resolves_rebound_callable(self):
        import jinja2.nodes as nodes
        marker = object()
        class Environment:
            autoescape = marker
        environment = Environment()
        trace = []
        def callable_override(value):
            trace.append(value is marker)
            return False
        with patch.object(nodes, 'callable', callable_override, create=True):
            reference = EvalContext(environment)
            expected = trace[:]
            trace.clear()
            with block_builders():
                native = docxtpl_native._extension().python_eval_context(environment)
            self.assertEqual(trace, expected)
            self.assertIs(native.autoescape, reference.autoescape)

    def test_context_uses_retained_function_globals_when_module_is_removed(self):
        environment = jinja2.Environment()
        runtime = sys.modules.pop('jinja2.runtime')
        try:
            with block_builders():
                context = docxtpl_native._extension().python_new_context(environment, {}, None, {}, {})
            self.assertIs(type(context), Context)
            self.assertIs(type(context.eval_ctx), EvalContext)
            self.assertNotIn('jinja2.runtime', sys.modules)
        finally:
            sys.modules['jinja2.runtime'] = runtime

    def test_standard_context_fields_types_and_identity(self):
        for globals in (None, {}, {'first': 1, 'second': 2}):
            environment = jinja2.Environment()
            parent = {'value': object()}
            block = lambda context: iter(())
            blocks = {'body': block}
            name = 'template'
            reference = Context(environment, parent, name, blocks, globals=globals)
            with block_builders():
                native = docxtpl_native._extension().python_new_context(
                    environment, parent, name, blocks, globals)
            with self.subTest(globals=globals):
                self.assertIs(type(native), type(reference))
                self.assertIs(type(native.eval_ctx), type(reference.eval_ctx))
                self.assertEqual(list(vars(native)), list(vars(reference)))
                self.assertIs(native.parent, parent)
                self.assertIs(native.environment, environment)
                self.assertIs(native.eval_ctx.environment, environment)
                self.assertEqual(vars(native.eval_ctx), vars(reference.eval_ctx))
                self.assertEqual(native.vars, {})
                self.assertIs(type(native.vars), dict)
                self.assertEqual(native.exported_vars, set())
                self.assertEqual(native.globals_keys, reference.globals_keys)
                self.assertIs(native.name, name)
                self.assertEqual(native.blocks, reference.blocks)
                self.assertIs(native.blocks['body'][0], block)
                self.assertIsNot(native.blocks['body'], reference.blocks['body'])

    def test_eval_context_preserves_attribute_order_raw_result_and_errors(self):
        for raises in (False, True):
            def run(native):
                trace = []
                marker = object()
                failure = ValueError('autoescape failed')
                class Environment:
                    @property
                    def autoescape(self):
                        trace.append('get')
                        def setting(name):
                            trace.append(('call', name))
                            if raises:
                                raise failure
                            return marker
                        return setting
                environment = Environment()
                try:
                    if native:
                        with block_builders():
                            context = docxtpl_native._extension().python_eval_context(environment)
                    else:
                        context = EvalContext(environment)
                    return ('ok', trace, type(context) is EvalContext,
                            context.environment is environment, context.autoescape is marker,
                            context.volatile, list(vars(context)))
                except BaseException as error:
                    return ('error', trace, type(error).__name__, error is failure)
            with self.subTest(raises=raises):
                expected = run(False)
                self.assertEqual(expected[1], ['get', 'get', ('call', None)])
                self.assertEqual(run(True), expected)

    def test_custom_initializer_remains_a_callback(self):
        original_init = Context.__init__
        trace = []
        def initialize(self, *args, **kwargs):
            trace.append(('init', len(args), list(kwargs)))
            original_init(self, *args, **kwargs)
        environment = jinja2.Environment()
        with patch.object(Context, '__init__', initialize):
            reference = Context(environment, {}, None, {}, globals={})
            expected = trace[:]
            trace.clear()
            native = docxtpl_native._extension().python_new_context(environment, {}, None, {}, {})
            self.assertEqual(trace, expected)
            self.assertIs(type(native), type(reference))

    def test_custom_allocator_remains_a_callback(self):
        # Replacing/deleting __new__ can leave a different CPython slot even
        # after restoring its Python attribute. Isolate that global mutation.
        code = '''
            import jinja2, docxtpl_native
            import docxtpl_native.bridge
            from jinja2.runtime import Context
            trace = []
            def allocate(cls, *args, **kwargs):
                trace.append((len(args), list(kwargs)))
                return object.__new__(cls)
            Context.__new__ = staticmethod(allocate)
            environment = jinja2.Environment()
            reference = Context(environment, {}, None, {}, globals={})
            expected = trace[:]
            trace.clear()
            native = docxtpl_native._extension().python_new_context(environment, {}, None, {}, {})
            assert trace == expected == [(4, ['globals'])], trace
            assert type(native) is type(reference)
        '''
        result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_initializer_override_before_binding_import(self):
        code = '''
            import jinja2
            from jinja2.runtime import Context
            original = Context.__init__
            trace = []
            def initialize(self, *args, **kwargs):
                trace.append((len(args), list(kwargs)))
                original(self, *args, **kwargs)
            Context.__init__ = initialize
            import docxtpl_native
            import docxtpl_native.bridge
            environment = jinja2.Environment()
            reference = Context(environment, {}, None, {}, globals={})
            expected = trace[:]
            trace.clear()
            native = docxtpl_native._extension().python_new_context(environment, {}, None, {}, {})
            assert trace == expected == [(4, ['globals'])], trace
            assert type(native) is type(reference)
        '''
        result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_initializer_code_mutation_is_observed(self):
        original = Context.__init__.__code__
        try:
            Context.__init__.__code__ = original.replace(co_names=tuple(
                'custom_globals_keys' if name == 'globals_keys' else name for name in original.co_names))
            environment = jinja2.Environment()
            reference = Context(environment, {}, None, {}, globals={'visible': 1})
            native = docxtpl_native._extension().python_new_context(environment, {}, None, {}, {'visible': 1})
            self.assertEqual(native.custom_globals_keys, reference.custom_globals_keys)
            self.assertFalse(hasattr(native, 'globals_keys'))
        finally:
            Context.__init__.__code__ = original

    def test_runtime_and_nodes_constructor_aliases_keep_their_call_shapes(self):
        import jinja2.runtime as runtime
        import jinja2.nodes as nodes
        environment = jinja2.Environment()
        trace = []
        marker = object()
        def constructor(*args):
            trace.append(args)
            return marker
        with patch.object(runtime, 'EvalContext', constructor):
            reference = Context(environment, {}, 'named', {}, globals={})
            expected = trace[:]
            trace.clear()
            native = docxtpl_native._extension().python_new_context(environment, {}, 'named', {}, {})
            self.assertEqual(trace, expected)
            self.assertEqual(len(trace[0]), 2)
            self.assertIs(native.eval_ctx, reference.eval_ctx)
        trace.clear()
        with patch.object(nodes, 'EvalContext', constructor):
            native = docxtpl_native._extension().python_eval_context(environment)
            self.assertIs(native, marker)
            self.assertEqual(trace, [(environment,)])

    def test_callback_filter_and_finalizer_build_without_python_helpers(self):
        def run(native):
            trace = []
            environment = jinja2.Environment()
            @jinja2.pass_context
            def callback(context):
                trace.append(('callback', type(context) is Context, context['value']))
                return context['value']
            @jinja2.pass_eval_context
            def filter(context, value):
                trace.append(('filter', type(context) is EvalContext, context.environment is environment))
                return value
            @jinja2.pass_context
            def finalize(context, value):
                trace.append(('finalize', type(context) is Context, context['value']))
                return value
            environment.filters['custom'] = filter
            environment.finalize = finalize
            source = '{{ callback()|custom }}'
            values = {'value': 'X', 'callback': callback}
            if native:
                with block_builders(), forbid('docxtpl_native.bridge.BridgeConfig.context', message='Python context helper called'):
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), values).render('<string>', source)
            else:
                output = environment.from_string(source).render(values)
            return output, trace
        self.assertEqual(run(True), run(False))

    def test_complete_document_builders(self):
        # Document equivalence with docxtpl: test_audited_documents_reference.
        with block_builders(), forbid('jinja2.Template.render', 'docxtpl_native.bridge.BridgeConfig.context',
                                      message='Python algorithm called'):
            native = context_document(docxtpl_native)
        self.assertEqual(observed(native).observation['text'], 'X')


def context_document(engine):
    """A complete document whose pass_context callback reads the render context."""
    @jinja2.pass_context
    def callback(context):
        return context['value']
    return generate(engine, '{{ callback() }}', {'callback': callback, 'value': 'X'})
