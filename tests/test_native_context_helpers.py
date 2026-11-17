"""Native context derivation preserves shared values and lazy block copying."""
import gc
import sys
import types
import unittest
import weakref
from contextlib import contextmanager
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
import jinja2.runtime as runtime
from jinja2.runtime import Context
from docxtpl_native.bridge import BridgeConfig
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate


@contextmanager
def block_helpers():
    forbidden = {Context.derived.__code__, Context.get_all.__code__, runtime.new_context.__code__}
    previous = sys.getprofile()
    def profile(frame, event, argument):
        if event == 'call' and frame.f_code in forbidden:
            raise AssertionError('Python context helper executed')
    sys.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous)


def derive(native, context, locals):
    if not native:
        return context.derived(locals)
    with block_helpers():
        return docxtpl_native._extension().python_context_derived(context, locals)


def get_all(native, context):
    if not native:
        return context.get_all()
    with block_helpers():
        return docxtpl_native._extension().python_context_get_all(context)


class NativeContextHelperTests(unittest.TestCase):
    def context(self, parent=None):
        return Context(jinja2.Environment(), {} if parent is None else parent, 'name', {}, globals={})

    def test_get_all_fast_paths_preserve_mapping_identity(self):
        for parent, variables in (({}, {}), ({'x': 1}, {}), ({}, {'y': 2})):
            context = self.context(parent)
            context.vars = variables
            self.assertIs(get_all(True, context), get_all(False, context))

    def test_get_all_merge_uses_keyword_mapping_protocol(self):
        for duplicate, invalid in ((False, False), (True, False), (False, True)):
            def run(native):
                trace = []
                class Variables:
                    def keys(self):
                        trace.append('keys')
                        return [5] if invalid else ['key', 'key'] if duplicate else ['key']
                    def __getitem__(self, key):
                        trace.append(('item', key))
                        return 'value'
                context = self.context({'parent': 1})
                context.vars = Variables()
                try:
                    return get_all(native, context), trace
                except TypeError as error:
                    return type(error).__name__, str(error), trace
            with self.subTest(duplicate=duplicate, invalid=invalid):
                self.assertEqual(run(True), run(False))

    def test_get_all_custom_truth_and_attribute_order(self):
        for raises in (False, True):
            def run(native):
                trace = []
                class Mapping(dict):
                    def __bool__(self):
                        trace.append('bool')
                        if raises:
                            raise ValueError('truth failed')
                        return True
                context = self.context({'parent': 1})
                context.vars = Mapping(local=2)
                original = Context.__getattribute__
                def access(self, key):
                    if key in ('vars', 'parent'):
                        trace.append(key)
                    return original(self, key)
                with patch.object(Context, '__getattribute__', access):
                    try:
                        return get_all(native, context), trace
                    except ValueError as error:
                        return str(error), trace
            with self.subTest(raises=raises):
                self.assertEqual(run(True), run(False))

    def test_get_all_uses_current_dict_global(self):
        def run(native):
            context = self.context({'parent': 1})
            context.vars = {'local': 2}
            trace = []
            def mapping(parent, **keywords):
                trace.append([parent is context.parent, keywords])
                return 'custom'
            with patch.object(runtime, 'dict', mapping, create=True):
                return get_all(native, context), trace
        self.assertEqual(run(True), run(False))

    def test_shared_parent_overlays_eval_identity_and_block_copies(self):
        marker = object()
        for locals in (None, {}, {'local': marker, 'omitted': runtime.missing}):
            context = self.context({'parent': marker})
            context.vars = {'variable': marker}
            context.blocks = {'body': [marker]}
            for native in (False, True):
                with self.subTest(native=native, locals=locals):
                    result = derive(native, context, locals)
                    self.assertIs(type(result), Context)
                    self.assertIs(result.environment, context.environment)
                    self.assertIs(result.eval_ctx, context.eval_ctx)
                    self.assertEqual(result.name, context.name)
                    self.assertEqual(result.vars, {})
                    self.assertEqual(result.exported_vars, set())
                    self.assertEqual(result.globals_keys, set())
                    self.assertIs(result.parent['parent'], marker)
                    self.assertIs(result.parent['variable'], marker)
                    self.assertNotIn('omitted', result.parent)
                    self.assertIsNot(result.blocks['body'], context.blocks['body'])
                    self.assertIs(result.blocks['body'][0], marker)

    def test_empty_locals_reuse_parent_and_truthy_locals_copy_it(self):
        context = self.context({'parent': object()})
        for native in (False, True):
            self.assertIs(derive(native, context, None).parent, context.parent)
            self.assertIs(derive(native, context, {}).parent, context.parent)
            self.assertIsNot(derive(native, context, {'x': runtime.missing}).parent, context.parent)

    def test_locals_mapping_errors_and_missing_sentinel_mutation(self):
        for malformed in (False, True):
            def run(native):
                trace = []
                context = self.context({'parent': 1})
                marker = object()
                class Locals:
                    def __bool__(self):
                        trace.append('bool')
                        return True
                    def items(self):
                        trace.append('items')
                        if malformed:
                            return [('too', 'many', 'values')]
                        def pairs():
                            yield 'old_missing', runtime.missing
                            runtime.missing = marker
                            yield 'new_missing', marker
                            yield 'present', 2
                        return pairs()
                with patch.object(runtime, 'missing', runtime.missing):
                    try:
                        return derive(native, context, Locals()).parent, trace
                    except ValueError as error:
                        return str(error), trace
            with self.subTest(malformed=malformed):
                self.assertEqual(run(True), run(False))

    def test_derivation_keeps_autoescape_call_before_sharing_eval_context(self):
        def run(native):
            calls = []
            environment = jinja2.Environment(autoescape=lambda name: calls.append(name) or False)
            context = Context(environment, {}, 'named', {}, globals={})
            result = derive(native, context, {'x': 1})
            return calls, result.eval_ctx is context.eval_ctx
        self.assertEqual(run(True), run(False))

    def test_custom_methods_and_bound_receivers_remain_observable(self):
        marker = object()
        context = self.context({'scope': 'base'})
        donor = self.context({'scope': 'donor'})
        context.get_all = lambda: marker
        self.assertIs(get_all(True, context), marker)
        context.get_all = donor.get_all
        self.assertIs(get_all(True, context), donor.parent)
        context.derived = lambda locals: marker
        self.assertIs(derive(True, context, None), marker)
        context.derived = donor.derived
        self.assertEqual(derive(True, context, None).parent, donor.parent)

    def test_runtime_constructor_is_resolved_before_get_all_callback(self):
        def run(native):
            context = self.context({'x': 1})
            trace = []
            original = runtime.new_context
            def first(*args):
                trace.append('first')
                return original(*args)
            def second(*args):
                trace.append('unexpected second')
                return original(*args)
            def all_values():
                trace.append('get_all')
                runtime.new_context = second
                return context.parent
            context.get_all = all_values
            # The user constructor explicitly calls the original helper, so
            # this check observes behavior without blocking callback internals.
            with patch.object(runtime, 'new_context', first):
                if native:
                    result = docxtpl_native._extension().python_context_derived(context, None)
                else:
                    result = context.derived(None)
            return result.parent, trace
        self.assertEqual(run(True), run(False))

    def test_deferred_block_generator_preserves_type_order_and_list_global(self):
        def run(native):
            trace = []
            context = self.context({'x': 1})
            context.blocks = {'body': ['first', 'second']}
            retained = []
            original = Context.__init__
            class Blocks(dict):
                def update(self, values):
                    trace.append(('update', type(values) is types.GeneratorType))
                    retained.append(values)
            def initialize(self, *args, **kwargs):
                original(self, *args, **kwargs)
                self.blocks = Blocks()
            with patch.object(Context, '__init__', initialize):
                result = derive(native, context, None)
            self.assertEqual(result.blocks, {})
            def copy(values):
                trace.append(('list', tuple(values)))
                return ['copied']
            with patch.object(runtime, 'list', copy, create=True):
                value = next(retained[0])
                with self.assertRaises(StopIteration):
                    retained[0].send('ignored')
            return value, trace
        self.assertEqual(run(True), run(False))

    def test_block_generator_converts_inner_stop_iteration_and_closes(self):
        def run(native):
            context = self.context()
            failure = StopIteration('block conversion')
            retained = []
            original = Context.__init__
            class Blocks(dict):
                def update(self, values):
                    retained.append(values)
                    super().update(values)
            def initialize(self, *args, **kwargs):
                original(self, *args, **kwargs)
                self.blocks = Blocks()
            class Values:
                def __iter__(self):
                    raise failure
            context.blocks = {'body': Values()}
            try:
                with patch.object(Context, '__init__', initialize):
                    derive(native, context, None)
            except RuntimeError as error:
                observation = str(error), error.__cause__ is failure, error.__context__ is failure
            self.assertIsNone(retained[0].gi_frame)
            with self.assertRaises(StopIteration):
                next(retained[0])
            return observation
        self.assertEqual(run(True), run(False))

    def test_native_derived_context_cycles_are_collected(self):
        base = self.context()
        result = derive(True, base, {'x': 1})
        result.parent['cycle'] = result
        retained = weakref.ref(result)
        del result
        gc.collect()
        self.assertIsNone(retained())

    def test_context_call_honors_rebound_standard_receiver(self):
        def run(native):
            environment = jinja2.Environment()
            donor = Context(jinja2.Environment(), {'scope': 'bound'}, 'donor', {}, globals={})
            trace = []
            @jinja2.pass_context
            def callback(context):
                trace.append((context is donor, context.environment is donor.environment, context.name))
                return context.get('scope', 'fresh')
            with patch.object(Context, 'call', types.MethodType(Context.call, donor)):
                blob = generate(docxtpl_native if native else docxtpl, '{{ callback() }}', {'callback': callback}, environment)
            return blob, trace
        reference, expected = run(False)
        native, actual = run(True)
        self.assertEqual(actual, expected)
        difference = compare_docx(reference, native)
        self.assertTrue(difference['equivalent'], difference)

    def test_complete_documents_derive_loop_and_block_overlays_in_rust(self):
        for asynchronous in (False, True):
            def run(native):
                trace = []
                @jinja2.pass_context
                def callback(context):
                    trace.append([context['root'], context['local'], context['block']])
                    return '|'.join(trace[-1])
                environment = jinja2.Environment(enable_async=asynchronous)
                source = '{{ callback(_loop_vars=loop_values, _block_vars=block_values) }}'
                values = {'root': 'R', 'loop_values': {'local': 'L'}, 'block_values': {'block': 'B'}, 'callback': callback}
                if native:
                    with block_helpers():
                        blob = generate(docxtpl_native, source, values, environment)
                else:
                    blob = generate(docxtpl, source, values, environment)
                return blob, trace
            with self.subTest(asynchronous=asynchronous):
                reference, expected = run(False)
                native, actual = run(True)
                self.assertEqual(actual, expected)
                difference = compare_docx(reference, native)
                self.assertTrue(difference['equivalent'], difference)


if __name__ == '__main__':
    unittest.main()
