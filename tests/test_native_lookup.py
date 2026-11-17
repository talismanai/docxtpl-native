"""Foreign lookup protocols and exception handling remain observable in Rust."""
import builtins
import sys
import subprocess
import textwrap
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import docxtpl_native
import jinja2
import jinja2.environment as environment_module
from docxtpl_native.bridge import BridgeConfig
from native_audit import forbid
from paritylab.protocol_base import generate, observed


@contextmanager
def native_lookup_only():
    forbidden = {jinja2.Environment.getattr.__code__, jinja2.Environment.getitem.__code__}
    previous = sys.getprofile()
    def profile(frame, event, argument):
        if event == 'call' and frame.f_code in forbidden:
            raise AssertionError('Python environment lookup executed')
    sys.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous)


def lookup(native, environment, obj, key, attribute_first, gate=True):
    if native:
        with native_lookup_only() if gate else ExitStack():
            return docxtpl_native._extension().python_lookup(environment, obj, key, attribute_first)
    return (environment.getattr if attribute_first else environment.getitem)(obj, key)


def lookup_document(engine, asynchronous):
    """A complete document whose attribute and item lookups are traced."""
    trace = []
    class Object:
        def __getitem__(self, key):
            trace.append(('item', key))
            if key == 'missing':
                raise KeyError(key)
            return 'item'
        @property
        def attribute(self):
            trace.append('attribute')
            return 'attribute'
    source = "{{ obj.attribute }} / {{ obj['attribute'] }} / {{ obj.missing|default('empty') }}"
    return generate(engine, source, {'obj': Object()}, jinja2.Environment(enable_async=asynchronous)), trace


class NativeLookupTests(unittest.TestCase):
    def test_session_does_not_import_binding_for_each_foreign_lookup(self):
        from paritylab.cases import LookupRecord
        session = docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(jinja2.Environment(), False),
            {'rows': [LookupRecord(i) for i in range(1000)]},
        )
        original = builtins.__import__
        imports = []
        def importing(name, *args, **kwargs):
            if name == 'docxtpl_native.bridge':
                imports.append(name)
            return original(name, *args, **kwargs)
        with patch.object(builtins, '__import__', importing), native_lookup_only():
            result = session.render('case', '{% for row in rows %}{{ row.label }}={{ row["value"] }};{% endfor %}')
        self.assertEqual(result, ''.join(f'row={i};' for i in range(1000)))
        self.assertEqual(len(imports), 0)

    def test_session_lookup_observes_method_replacement_during_render(self):
        def run(native):
            environment = jinja2.Environment()
            trace = []
            class Object:
                @property
                def value(self):
                    trace.append('attribute')
                    def item(obj, key):
                        trace.append(('item callback', obj is self, key))
                        environment.getattr = lambda obj, key: 'last'
                        return 'middle'
                    environment.getitem = item
                    return 'first'
            source = '{{ obj.value }}/{{ obj["value"] }}/{{ obj.value }}'
            values = {'obj': Object()}
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values)
                with native_lookup_only():
                    result = session.render('case', source)
            else:
                result = environment.from_string(source).render(values)
            return result, trace
        self.assertEqual(run(True), run(False))

    def test_session_lookup_observes_code_and_globals_changed_after_creation(self):
        environment = jinja2.Environment()
        session = docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(environment, False), {'obj': object()},
        )
        function = jinja2.Environment.getattr
        original_code = function.__code__
        def replacement(self, obj, key):
            return 'changed code'
        try:
            function.__code__ = replacement.__code__
            self.assertEqual(session.render('code', '{{ obj.value }}'), 'changed code')
        finally:
            function.__code__ = original_code
        with patch.object(environment_module, 'getattr', lambda obj, key: 'changed global', create=True), native_lookup_only():
            self.assertEqual(session.render('global', '{{ obj.value }}'), 'changed global')

    def test_precedence_fallback_and_identity(self):
        marker = object()
        for attribute_first in (False, True):
            for attribute_error in (None, AttributeError, KeyError, ValueError):
                for item_error in (None, AttributeError, TypeError, IndexError, KeyError, ValueError):
                    def run(native):
                        trace = []
                        class Object:
                            @property
                            def key(self):
                                trace.append('attribute')
                                if attribute_error:
                                    raise attribute_error('attribute failed')
                                return marker
                            def __getitem__(self, key):
                                trace.append(('item', key))
                                if item_error:
                                    raise item_error('item failed')
                                return marker
                        obj = Object()
                        try:
                            result = lookup(native, jinja2.Environment(), obj, 'key', attribute_first)
                            return (result is marker, isinstance(result, jinja2.Undefined), trace)
                        except Exception as error:
                            return type(error).__name__, str(error), trace
                    with self.subTest(attribute_first=attribute_first, attribute_error=attribute_error, item_error=item_error):
                        self.assertEqual(run(True), run(False))

    def test_undefined_retains_original_object_and_key(self):
        for key in ('missing', 2, ('tuple',), object()):
            for attribute_first in (False, True):
                if attribute_first and not isinstance(key, str):
                    continue
                obj = {}
                result = lookup(True, jinja2.Environment(), obj, key, attribute_first)
                self.assertIs(result._undefined_obj, obj)
                self.assertIs(result._undefined_name, key)

    def test_invalid_attribute_name_does_not_fall_back_to_items(self):
        for key in (None, 7, ('tuple',), object()):
            def run(native):
                trace = []
                class Object:
                    def __getitem__(self, key):
                        trace.append('unexpected item')
                        return 'item'
                try:
                    lookup(native, jinja2.Environment(), Object(), key, True)
                except TypeError as error:
                    return str(error), trace
            with self.subTest(key=key):
                self.assertEqual(run(True), run(False))

    def test_unhandled_exception_is_not_formatted_or_replaced(self):
        class Opaque(ValueError):
            def __str__(self):
                raise AssertionError('exception formatted')
        for error_type in (Opaque, StopIteration, KeyboardInterrupt):
            for attribute_first in (False, True):
                failure = error_type('original')
                class Object:
                    @property
                    def key(self):
                        raise failure
                    def __getitem__(self, key):
                        raise failure
                for native in (False, True):
                    with self.subTest(native=native, error_type=error_type, attribute_first=attribute_first):
                        with self.assertRaises(error_type) as raised:
                            lookup(native, jinja2.Environment(), Object(), 'key', attribute_first)
                        self.assertIs(raised.exception, failure)

    def test_string_conversion_and_handled_exception(self):
        for conversion_error in (None, ValueError, StopIteration, KeyboardInterrupt):
            def run(native):
                trace = []
                item_error = KeyError('item')
                class Key(str):
                    def __str__(self):
                        trace.append(('str', sys.exception() is item_error))
                        if conversion_error:
                            raise conversion_error('conversion')
                        return 'converted'
                class Object:
                    def __getitem__(self, key):
                        trace.append('item')
                        raise item_error
                    @property
                    def converted(self):
                        trace.append(('attribute', sys.exception() is item_error))
                        raise AttributeError('attribute')
                def undefined(**kwargs):
                    trace.append(('undefined', sys.exception() is item_error))
                    return 'missing'
                try:
                    value = lookup(native, jinja2.Environment(undefined=jinja2.Undefined), Object(), Key('key'), False)
                    result = 'undefined' if isinstance(value, jinja2.Undefined) else value
                    # Also observe the undefined callback inside the handler.
                    environment = jinja2.Environment()
                    environment.undefined = undefined
                    lookup(native, environment, Object(), Key('key'), False)
                    return result, trace, sys.exception() is None
                except BaseException as error:
                    return type(error).__name__, str(error), type(error.__context__).__name__, trace
            with self.subTest(conversion_error=conversion_error):
                self.assertEqual(run(True), run(False))

    def test_attribute_failure_is_cleared_before_item_lookup(self):
        def run(native):
            trace = []
            class Object:
                @property
                def key(self):
                    trace.append(('attribute', sys.exception() is None))
                    raise AttributeError('attribute')
                def __getitem__(self, key):
                    trace.append(('item', sys.exception() is None))
                    raise KeyError('item')
            def undefined(**kwargs):
                trace.append(('undefined', type(sys.exception()).__name__, str(sys.exception())))
                return 'missing'
            environment = jinja2.Environment()
            environment.undefined = undefined
            return lookup(native, environment, Object(), 'key', True), trace, sys.exception() is None
        self.assertEqual(run(True), run(False))

    def test_nested_exception_context_and_outer_handler_are_preserved(self):
        for attribute_first in (False, True):
            def run(native):
                original = RuntimeError('caller')
                item = KeyError('item')
                failure = ValueError('undefined')
                class Object:
                    def __getitem__(self, key):
                        raise item
                environment = jinja2.Environment()
                def undefined(**kwargs):
                    self.assertIs(sys.exception(), item)
                    raise failure
                environment.undefined = undefined
                try:
                    raise original
                except RuntimeError:
                    try:
                        lookup(native, environment, Object(), 'key', attribute_first)
                    except ValueError as error:
                        observation = (error is failure, error.__context__ is item, item.__context__ is original)
                    self.assertIs(sys.exception(), original)
                return observation
            with self.subTest(attribute_first=attribute_first):
                self.assertEqual(run(True), run(False))

    def test_exception_matching_uses_current_function_globals(self):
        for attribute_first in (False, True):
            for replacement in (RuntimeError, (KeyError, AttributeError), object()):
                def run(native):
                    class Object:
                        def __getitem__(self, key):
                            raise KeyError('missing item')
                    key = 'LookupError' if not attribute_first else 'AttributeError'
                    with patch.object(environment_module, key, replacement, create=True):
                        try:
                            value = lookup(native, jinja2.Environment(), Object(), 'key', attribute_first)
                            return isinstance(value, jinja2.Undefined)
                        except Exception as error:
                            return type(error).__name__, str(error), type(error.__context__).__name__
                with self.subTest(attribute_first=attribute_first, replacement=replacement):
                    self.assertEqual(run(True), run(False))

    def test_rebound_builtins_and_dynamic_lookup_order(self):
        for attribute_first in (False, True):
            def run(native):
                trace = []
                class Object:
                    def __getitem__(self, key):
                        trace.append('item')
                        raise KeyError('missing')
                def get(obj, key):
                    trace.append(('getattr', key))
                    return 'found'
                def instance(obj, cls):
                    trace.append(('isinstance', cls is str))
                    return True
                with patch.object(environment_module, 'getattr', get, create=True), patch.object(environment_module, 'isinstance', instance, create=True):
                    result = lookup(native, jinja2.Environment(), Object(), 'key', attribute_first)
                return result, trace
            with self.subTest(attribute_first=attribute_first):
                self.assertEqual(run(True), run(False))

    def test_user_overrides_and_rebound_method_receiver(self):
        marker = object()
        for method_name in ('getattr', 'getitem'):
            environment = jinja2.Environment()
            trace = []
            def callback(obj, key):
                trace.append((obj is marker, key))
                return marker
            setattr(environment, method_name, callback)
            self.assertIs(lookup(True, environment, marker, 'key', method_name == 'getattr'), marker)
            self.assertEqual(trace, [(True, 'key')])
            donor = jinja2.Environment()
            donor.undefined = lambda **kwargs: marker
            setattr(environment, method_name, getattr(donor, method_name))
            self.assertIs(lookup(True, environment, {}, 'missing', method_name == 'getattr'), marker)

    def test_changed_standard_method_code_remains_a_callback(self):
        for name in ('getattr', 'getitem'):
            function = getattr(jinja2.Environment, name)
            original = function.__code__
            def replacement(self, obj, key):
                return obj
            try:
                function.__code__ = replacement.__code__
                marker = object()
                self.assertIs(lookup(True, jinja2.Environment(), marker, 'key', name == 'getattr', gate=False), marker)
            finally:
                function.__code__ = original

    def test_override_installed_before_binding_import_remains_a_callback(self):
        code = textwrap.dedent('''
            import jinja2
            def getitem(self, obj, key):
                return obj
            jinja2.Environment.getitem = getitem
            import docxtpl_native
            from docxtpl_native.bridge import BridgeConfig
            marker = object()
            result = docxtpl_native._extension().python_lookup(jinja2.Environment(), marker, 'key', False)
            assert result is marker
        ''')
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)

    def test_module_removal_keeps_the_functions_original_globals(self):
        environment = jinja2.Environment()
        module = sys.modules.pop('jinja2.environment')
        try:
            self.assertEqual(lookup(True, environment, {'key': 'value'}, 'key', False), 'value')
            self.assertNotIn('jinja2.environment', sys.modules)
        finally:
            sys.modules['jinja2.environment'] = module

    def test_sandbox_policy_is_preserved(self):
        from jinja2.sandbox import SandboxedEnvironment
        environment = SandboxedEnvironment()
        for source in ("{{ obj.__class__ }}", "{{ obj['__class__'] }}", "{{ obj.value }}"):
            values = {'obj': type('Object', (), {'value': 'allowed'})()}
            expected = environment.from_string(source).render(values)
            result = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values).render('case', source)
            self.assertEqual(result, expected)

    def test_complete_document_uses_native_lookup_sync_and_async(self):
        # Document equivalence with docxtpl: test_audited_documents_reference.
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                with native_lookup_only(), forbid('docxtpl_native.bridge.BridgeConfig.lookup',
                                                  'jinja2.Template.render', message='Python lookup or renderer called'):
                    blob, trace = lookup_document(docxtpl_native, asynchronous)
                self.assertEqual(observed(blob).observation['text'], 'attribute / item / empty')
                self.assertEqual(trace, ['attribute', ('item', 'attribute'), ('item', 'missing')])

if __name__ == '__main__':
    unittest.main()
