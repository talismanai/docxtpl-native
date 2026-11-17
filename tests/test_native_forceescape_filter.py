"""Differential coverage and execution audit for the forceescape filter."""

import json
import subprocess
import sys
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType, SimpleNamespace
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
import markupsafe
from jinja2 import filters
from markupsafe import Markup
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


@contextmanager
def no_standard_forceescape() -> Generator[None, None, None]:
    codes = {filters.do_forceescape.__code__, markupsafe.escape.__code__}
    previous = sys.getprofile()
    calls: list[str] = []

    def profile(frame: FrameType, event: str, argument: object) -> None:
        if event == 'call' and frame.f_code in codes:
            calls.append(frame.f_code.co_name)

    try:
        sys.setprofile(profile)
        yield
    finally:
        sys.setprofile(previous)
    if calls:
        raise AssertionError('standard Python bodies executed: ' + repr(calls))


class NativeForceEscapeTests(unittest.TestCase):
    def test_replaced_compiler_escape_does_not_route_standard_body_through_python(self) -> None:
        script = '''
import sys
import types
import jinja2.compiler
import markupsafe

original = markupsafe.escape
clone = types.FunctionType(original.__code__, original.__globals__, original.__name__,
                           original.__defaults__, original.__closure__)
jinja2.compiler.escape = clone
import docxtpl_native
from test_native_filter_evaluation import render
docxtpl_native._extension()
calls = []
def profile(frame, event, arg):
    if event == 'call' and frame.f_code is original.__code__:
        calls.append(frame.f_code.co_name)
previous = sys.getprofile()
try:
    sys.setprofile(profile)
    assert render('{{ value|forceescape }}', {'value': '<&'}, True) == '&lt;&amp;'
finally:
    sys.setprofile(previous)
assert not calls, calls
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_temporary_inner_callback_is_released_before_markup(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original_markup = markupsafe.Markup

            class Inner:
                def __call__(self, value: str) -> str:
                    events.append('inner:call')
                    del markupsafe.__dict__['_escape_inner']
                    return value

                def __del__(self) -> None:
                    events.append('inner:drop')

            def markup(value: object) -> Markup:
                events.append('markup:call')
                return original_markup(value)

            with patch.dict(markupsafe.__dict__, {'Markup': markup}):
                markupsafe.__dict__['_escape_inner'] = Inner()
                output = render('{{ value|forceescape }}', {'value': 'input'}, native)
            return output, events

        expected = run(False)
        self.assertEqual(expected, ('input', ['inner:call', 'inner:drop', 'markup:call']))
        with no_standard_forceescape():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_temporary_converted_string_is_released_before_markup(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original_markup = markupsafe.Markup

            class Text(str):
                def __del__(self) -> None:
                    events.append('text:drop')

            class Value:
                def __str__(self) -> str:
                    events.append('value:str')
                    return Text('input')

            class Inner:
                def __call__(self, value: str) -> str:
                    events.append('inner:call')
                    del markupsafe.__dict__['_escape_inner']
                    return 'output'

                def __del__(self) -> None:
                    events.append('inner:drop')

            def markup(value: object) -> Markup:
                events.append('markup:call')
                return original_markup(value)

            with (patch.dict(filters.__dict__, {'str': lambda _: Value()}),
                  patch.dict(markupsafe.__dict__, {'Markup': markup})):
                markupsafe.__dict__['_escape_inner'] = Inner()
                output = render('{{ value|forceescape }}', {'value': 'input'}, native)
            return output, events

        expected = run(False)
        self.assertEqual(expected[0], 'output')
        self.assertLess(expected[1].index('text:drop'), expected[1].index('markup:call'))
        self.assertLess(expected[1].index('inner:drop'), expected[1].index('markup:call'))
        with no_standard_forceescape():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_inner_override_before_extension_load_is_not_retained(self) -> None:
        script = '''
import sys
from unittest.mock import patch
import markupsafe
from test_native_filter_evaluation import render

original_inner = markupsafe._escape_inner
original_markup = markupsafe.Markup
events: list[str] = []

class Inner:
    def __call__(self, value: str) -> str:
        events.append('inner:call')
        del markupsafe.__dict__['_escape_inner']
        return value
    def __del__(self) -> None:
        events.append('inner:drop')

def markup(value: object) -> object:
    events.append('markup:call')
    return original_markup(value)

with patch.dict(markupsafe.__dict__, {'Markup': markup}):
    markupsafe.__dict__['_escape_inner'] = Inner()
    assert render('{{ value|forceescape }}', {'value': 'input'}, True) == 'input'
    assert events == ['inner:call', 'inner:drop', 'markup:call'], events

calls: list[str] = []
def profile(frame: object, event: str, arg: object) -> None:
    if event == 'c_call' and arg is original_inner:
        calls.append(event)
previous = sys.getprofile()
try:
    sys.setprofile(profile)
    assert render('{{ value|forceescape }}', {'value': '<&'}, True) == '&lt;&amp;'
finally:
    sys.setprofile(previous)
assert not calls, calls
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_escape_provenance_recovers_when_module_returns(self) -> None:
        script = '''
import sys
from unittest.mock import patch
import docxtpl_native
import markupsafe
from test_native_filter_evaluation import render

original = markupsafe._escape_inner
module = sys.modules.pop('markupsafe._speedups')
try:
    with patch.dict(markupsafe.__dict__, {'_escape_inner': lambda value: value}):
        assert render('{{ value|forceescape }}', {'value': 'input'}, True) == 'input'
    assert 'markupsafe._speedups' not in sys.modules
    try:
        render('{{ value|forceescape }}', {'value': '<&'}, True)
    except docxtpl_native.UnsupportedFeature as error:
        assert 'provenance' in str(error), error
    else:
        raise AssertionError('unverified native algorithm must not execute through Python')
finally:
    sys.modules['markupsafe._speedups'] = module

calls: list[str] = []
def profile(frame: object, event: str, arg: object) -> None:
    if event == 'c_call' and arg is original:
        calls.append(event)
previous = sys.getprofile()
try:
    sys.setprofile(profile)
    assert render('{{ value|forceescape }}', {'value': '<&'}, True) == '&lt;&amp;'
finally:
    sys.setprofile(previous)
assert not calls, calls
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_inner_is_loaded_only_when_filter_executes(self) -> None:
        script = '''
import sys
import docxtpl_native
import markupsafe
from test_native_filter_evaluation import render

original = markupsafe.__dict__.pop('_escape_inner')
try:
    docxtpl_native._extension()
    assert render('{{ value }}', {'value': 'plain'}, True) == 'plain'
    errors: list[tuple[str, str, object]] = []
    for native in (False, True):
        try:
            render('{{ value|forceescape }}', {'value': 'input'}, native)
        except NameError as error:
            errors.append((type(error).__name__, str(error), error.name))
        else:
            raise AssertionError('expected missing global')
    assert errors[0] == errors[1], errors
finally:
    markupsafe.__dict__['_escape_inner'] = original

calls: list[str] = []
def profile(frame: object, event: str, arg: object) -> None:
    if event == 'c_call' and arg is original:
        calls.append(event)
previous = sys.getprofile()
try:
    sys.setprofile(profile)
    assert render('{{ value|forceescape }}', {'value': '<&'}, True) == '&lt;&amp;'
finally:
    sys.setprofile(previous)
assert not calls, calls
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_extension_load_does_not_invoke_inner_module_attribute_hook(self) -> None:
        script = '''
import types
import docxtpl_native
import markupsafe
from test_native_filter_evaluation import render

events: list[str] = []
class Module(types.ModuleType):
    def __getattribute__(self, name: str) -> object:
        if name == '_escape_inner':
            events.append(name)
        return super().__getattribute__(name)

original = markupsafe.__class__
try:
    markupsafe.__class__ = Module
    docxtpl_native._extension()
    assert render('{{ value|forceescape }}', {'value': '<&'}, True) == '&lt;&amp;'
    assert events == [], events
finally:
    markupsafe.__class__ = original
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inner_override_metadata_does_not_run_equality_on_load(self) -> None:
        script = '''
import docxtpl_native
import markupsafe
from test_native_filter_evaluation import render

events: list[str] = []
class ModuleTag:
    def __eq__(self, other: object) -> bool:
        events.append('equality')
        return False

def inner(value: str) -> str:
    events.append('callback')
    return value

inner.__module__ = ModuleTag()
markupsafe._escape_inner = inner
docxtpl_native._extension()
assert render('{{ value|forceescape }}', {'value': 'input'}, True) == 'input'
assert events == ['callback'], events
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inner_override_does_not_import_native_module_on_load(self) -> None:
        script = '''
import builtins
import sys
import docxtpl_native
import markupsafe
from test_native_filter_evaluation import render

sys.modules.pop('markupsafe._native', None)
events: list[str] = []
def inner(value: str) -> str:
    events.append('callback')
    return value
inner.__module__ = 'markupsafe._native'
inner.__name__ = '_escape_inner'
markupsafe._escape_inner = inner
original_import = builtins.__import__
def recording_import(name: str, *args: object, **kwargs: object) -> object:
    if name == 'markupsafe._native':
        events.append('import')
    return original_import(name, *args, **kwargs)
builtins.__import__ = recording_import
try:
    docxtpl_native._extension()
    assert events == [], events
    assert render('{{ value|forceescape }}', {'value': 'input'}, True) == 'input'
    assert events == ['callback'], events
finally:
    builtins.__import__ = original_import
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_pure_inner_module_does_not_run_reference_algorithm(self) -> None:
        script = '''
import sys
import docxtpl_native
import markupsafe
import markupsafe._native as implementation
from test_native_filter_evaluation import render

original = implementation._escape_inner
markupsafe._escape_inner = original
sys.modules.pop('markupsafe._native')
docxtpl_native._extension()
calls: list[str] = []
def profile(frame: object, event: str, arg: object) -> None:
    if event == 'call' and frame.f_code is original.__code__:
        calls.append(event)
previous = sys.getprofile()
try:
    sys.setprofile(profile)
    try:
        render('{{ value|forceescape }}', {'value': '<&'}, True)
    except docxtpl_native.UnsupportedFeature as error:
        assert 'provenance' in str(error), error
    else:
        raise AssertionError('unverified reference algorithm must not execute')
finally:
    sys.setprofile(previous)
assert calls == [], calls
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mutated_escape_code_remains_user_callback(self) -> None:
        script = '''
import sys
import docxtpl_native
import markupsafe
from test_native_filter_evaluation import render

events: list[str] = []
markupsafe.__dict__['_callback_events'] = events
original = markupsafe.escape
original_code = original.__code__
namespace: dict[str, object] = {}
exec(compile("def escape(value):\\n    _callback_events.append('escape')\\n    return 'override'\\n", original_code.co_filename, 'exec'), namespace)
docxtpl_native._extension()
try:
    original.__code__ = namespace['escape'].__code__
    assert render('{{ value|forceescape }}', {'value': 'input'}, True) == 'override'
    assert events == ['escape'], events
finally:
    original.__code__ = original_code
calls: list[str] = []
def profile(frame: object, event: str, arg: object) -> None:
    if event == 'call' and frame.f_code is original_code:
        calls.append(event)
previous = sys.getprofile()
try:
    sys.setprofile(profile)
    assert render('{{ value|forceescape }}', {'value': '<&'}, True) == '&lt;&amp;'
finally:
    sys.setprofile(previous)
assert calls == [], calls
assert events == ['escape'], events
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mutated_pure_inner_code_remains_user_callback(self) -> None:
        script = '''
import sys
import docxtpl_native
import markupsafe
import markupsafe._native as implementation
from test_native_filter_evaluation import render

events: list[str] = []
implementation.__dict__['_callback_events'] = events
original = implementation._escape_inner
original_code = original.__code__
namespace: dict[str, object] = {}
exec(compile("def _escape_inner(value):\\n    _callback_events.append('inner')\\n    return 'override'\\n", original_code.co_filename, 'exec'), namespace)
markupsafe._escape_inner = original
docxtpl_native._extension()
try:
    original.__code__ = namespace['_escape_inner'].__code__
    assert render('{{ value|forceescape }}', {'value': 'input'}, True) == 'override'
    assert events == ['inner'], events
finally:
    original.__code__ = original_code
calls: list[str] = []
def profile(frame: object, event: str, arg: object) -> None:
    if event == 'call' and frame.f_code is original_code:
        calls.append(event)
previous = sys.getprofile()
try:
    sys.setprofile(profile)
    assert render('{{ value|forceescape }}', {'value': '<&'}, True) == '&lt;&amp;'
finally:
    sys.setprofile(previous)
assert calls == [], calls
assert events == ['inner'], events
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_escape_override_before_load_keeps_filter_reference_native(self) -> None:
        script = '''
import sys
from unittest.mock import patch
import docxtpl_native
import markupsafe
from jinja2 import filters
from test_native_filter_evaluation import render

original = filters.escape
calls: list[str] = []
def profile(frame: object, event: str, arg: object) -> None:
    if event == 'call' and frame.f_code is original.__code__:
        calls.append(event)

with patch.object(markupsafe, 'escape', lambda value: 'override'):
    docxtpl_native._extension()
    previous = sys.getprofile()
    try:
        sys.setprofile(profile)
        assert render('{{ value|forceescape }}', {'value': '<&'}, True) == '&lt;&amp;'
    finally:
        sys.setprofile(previous)
assert not calls, calls
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unverified_escape_reference_is_rejected_without_python_fallback(self) -> None:
        script = '''
import sys
from unittest.mock import patch
import docxtpl_native
import markupsafe
import jinja2.runtime
from jinja2 import filters
from test_native_filter_evaluation import render

original = filters.escape
calls: list[str] = []
def profile(frame: object, event: str, arg: object) -> None:
    if event == 'call' and frame.f_code is original.__code__:
        calls.append(event)

with (patch.object(markupsafe, 'escape', lambda value: 'override'),
      patch.object(jinja2.runtime, 'escape', None)):
    docxtpl_native._extension()
    previous = sys.getprofile()
    try:
        sys.setprofile(profile)
        try:
            render('{{ value|forceescape }}', {'value': '<&'}, True)
        except docxtpl_native.UnsupportedFeature as error:
            assert 'provenance' in str(error), error
        else:
            raise AssertionError('unverified standard body must not execute')
    finally:
        sys.setprofile(previous)
assert not calls, calls
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_escape_override_before_load_is_not_retained_and_runs_as_callback(self) -> None:
        script = '''
import gc
import weakref
import docxtpl_native
import markupsafe
from jinja2 import filters
from test_native_filter_evaluation import render

events: list[str] = []
def custom(value: object) -> str:
    events.append('callback')
    return 'override'

reference = weakref.ref(custom)
old_markup, old_filter = markupsafe.escape, filters.escape
markupsafe.escape = filters.escape = custom
del custom
try:
    docxtpl_native._extension()
    assert render('{{ value|forceescape }}', {'value': 'input'}, True) == 'override'
    assert events == ['callback'], events
finally:
    markupsafe.escape, filters.escape = old_markup, old_filter
gc.collect()
assert reference() is None
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_metadata_escape_override_before_load_remains_callback(self) -> None:
        script = '''
import docxtpl_native
import markupsafe
from jinja2 import filters
from test_native_filter_evaluation import render

events: list[str] = []
markupsafe.__dict__['_callback_events'] = events
source = "def escape(value):\\n    _callback_events.append('callback')\\n    return 'override'\\n"
exec(compile(source, markupsafe.__file__, 'exec'), markupsafe.__dict__)
filters.escape = markupsafe.escape
docxtpl_native._extension()
assert render('{{ value|forceescape }}', {'value': 'input'}, True) == 'override'
assert events == ['callback'], events
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_metadata_escape_override_is_not_silently_skipped(self) -> None:
        script = '''
import docxtpl_native
import markupsafe
from jinja2 import filters
from test_native_filter_evaluation import render

events: list[str] = []
markupsafe.__dict__['_callback_events'] = events
source = "def escape(value):\\n    _callback_events.append('callback')\\n    return 'override'\\n"
exec(compile(source, markupsafe.__file__, 'exec'), markupsafe.__dict__)
filters.escape = markupsafe.escape
docxtpl_native._extension()
try:
    output = render('{{ value|forceescape }}', {'value': 'input'}, True)
except docxtpl_native.UnsupportedFeature as error:
    assert 'provenance' in str(error), error
else:
    assert output == 'override' and events == ['callback'], (output, events)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_metadata_escape_override_in_runtime_remains_callback(self) -> None:
        script = '''
import docxtpl_native
import jinja2.runtime
import markupsafe
from jinja2 import filters
from test_native_filter_evaluation import render

events: list[str] = []
markupsafe.__dict__['_callback_events'] = events
source = "def escape(value):\\n    _callback_events.append('callback')\\n    return 'override'\\n"
exec(compile(source, markupsafe.__file__, 'exec'), markupsafe.__dict__)
filters.escape = jinja2.runtime.escape = markupsafe.escape
docxtpl_native._extension()
assert render('{{ value|forceescape }}', {'value': 'input'}, True) == 'override'
assert events == ['callback'], events
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_replaced_jinja_aliases_do_not_silently_hide_callback(self) -> None:
        script = '''
import docxtpl_native
import jinja2.runtime
import markupsafe
from jinja2 import filters
from test_native_filter_evaluation import render

events: list[str] = []
markupsafe.__dict__['_callback_events'] = events
source = "def escape(value):\\n    _callback_events.append('callback')\\n    return 'override'\\n"
exec(compile(source, markupsafe.__file__, 'exec'), markupsafe.__dict__)
filters.escape = jinja2.runtime.escape = markupsafe.escape
markupsafe.escape = lambda value: 'another override'
docxtpl_native._extension()
try:
    output = render('{{ value|forceescape }}', {'value': 'input'}, True)
except docxtpl_native.UnsupportedFeature:
    pass
else:
    assert output == 'override' and events == ['callback'], (output, events)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_metadata_python_inner_override_remains_callback(self) -> None:
        script = '''
import docxtpl_native
import markupsafe
import markupsafe._native as implementation
from test_native_filter_evaluation import render

events: list[str] = []
implementation.__dict__['_callback_events'] = events
source = "def _escape_inner(value):\\n    _callback_events.append('callback')\\n    return 'override'\\n"
exec(compile(source, implementation.__file__, 'exec'), implementation.__dict__)
markupsafe._escape_inner = implementation._escape_inner
docxtpl_native._extension()
assert render('{{ value|forceescape }}', {'value': 'input'}, True) == 'override'
assert events == ['callback'], events
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exact_code_clone_installed_before_load_remains_callback(self) -> None:
        script = '''
import sys
import types
import jinja2.runtime
import markupsafe
from jinja2 import filters

if sys.argv[1] == 'after-package':
    import docxtpl_native

original = markupsafe.escape
clone = types.FunctionType(original.__code__, original.__globals__, original.__name__, original.__defaults__, original.__closure__)
clone.__annotations__ = original.__annotations__.copy()
clone.__qualname__ = original.__qualname__
assert clone is not original and clone.__code__ is original.__code__
markupsafe.escape = jinja2.runtime.escape = filters.escape = clone
import docxtpl_native
from test_native_filter_evaluation import render
docxtpl_native._extension()

def observe(native: bool) -> tuple[str, int]:
    calls = 0
    def profile(frame: types.FrameType, event: str, argument: object) -> None:
        nonlocal calls
        if event == 'call' and frame.f_code is clone.__code__:
            calls += 1
    old = sys.getprofile()
    try:
        sys.setprofile(profile)
        result = render('{{ value|forceescape }}', {'value': '<&'}, native)
    finally:
        sys.setprofile(old)
    return result, calls

expected = observe(False)
actual = observe(True)
assert expected[0] == '&lt;&amp;' and expected[1] > 0, expected
assert actual == expected, (expected, actual)
'''
        for timing in ('before-package', 'after-package'):
            with self.subTest(timing=timing):
                result = subprocess.run([sys.executable, '-c', script, timing], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_escape_specializes_during_repeated_render(self) -> None:
        script = '''
import json
import sys
from unittest.mock import patch
import markupsafe
from jinja2 import filters
from test_native_filter_evaluation import render

def invalid_str(value: object) -> int:
    return 123

def identity(value: object) -> object:
    return value

messages: list[str] = []
with patch.dict(filters.__dict__, {"str": invalid_str}), patch.dict(markupsafe.__dict__, {"str": identity}):
    for _ in range(40):
        try:
            render("{{ value|forceescape }}", {"value": "input"}, sys.argv[1] == "native")
        except SystemError as error:
            messages.append(str(error))
        else:
            raise AssertionError("expected SystemError")
print(json.dumps(messages))
'''
        outcomes: list[list[str]] = []
        for implementation in ('reference', 'native'):
            result = subprocess.run(
                [sys.executable, '-c', script, implementation],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outcomes.append(json.loads(result.stdout))
        self.assertEqual(len(outcomes[0]), 40)
        self.assertGreater(len(set(outcomes[0])), 1, 'reference must exercise specialization')
        self.assertEqual(outcomes[1], outcomes[0])

    @unittest.skipUnless(sys.implementation.name == 'cpython' and sys.version_info[:2] in ((3, 11), (3, 12)),
                         'adaptive bytecode probe is specific to CPython 3.11 and 3.12')
    def test_invalid_escape_updates_reference_adaptive_call_site(self) -> None:
        script = '''
import dis
import json
import sys
from unittest.mock import patch
import markupsafe
from jinja2 import filters
from test_native_filter_evaluation import render

def invalid_str(value: object) -> int:
    return 123

def identity(value: object) -> object:
    return value

with patch.dict(filters.__dict__, {'str': invalid_str}), patch.dict(markupsafe.__dict__, {'str': identity}):
    for _ in range(40):
        try:
            render('{{ value|forceescape }}', {'value': 'input'}, sys.argv[1] == 'native')
        except SystemError:
            pass
        else:
            raise AssertionError('expected SystemError')
    instructions = list(dis.get_instructions(markupsafe.escape, adaptive=True))
    starts = [index for index, item in enumerate(instructions)
              if item.opname.startswith('LOAD_GLOBAL') and item.argval == '_escape_inner']
    assert len(starts) == 2, starts
    specialized = any(item.opname in ('PRECALL_NO_KW_BUILTIN_O', 'CALL_NO_KW_BUILTIN_O')
                      for item in instructions[starts[-1]:])
print(json.dumps(specialized))
'''
        outcomes: list[bool] = []
        for implementation in ('reference', 'native'):
            result = subprocess.run(
                [sys.executable, '-c', script, implementation],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outcomes.append(json.loads(result.stdout))
        self.assertTrue(outcomes[0], 'reference must exercise specialization')
        self.assertEqual(outcomes[1], outcomes[0])

    def test_invalid_escape_after_profiling_is_disabled(self) -> None:
        script = '''
import json
import sys
from unittest.mock import patch
import markupsafe
from jinja2 import filters
from test_native_filter_evaluation import render

def invalid_str(value: object) -> int:
    return 123

def identity(value: object) -> object:
    return value

def probe() -> str:
    try:
        return render("{{ value|forceescape }}", {"value": "input"}, sys.argv[1] == "native")
    except SystemError as error:
        return str(error)

with patch.dict(filters.__dict__, {"str": invalid_str}), patch.dict(markupsafe.__dict__, {"str": identity}):
    for _ in range(40):
        try:
            markupsafe.escape(123)
        except SystemError:
            pass
    sys.setprofile(lambda frame, event, argument: None)
    profiled = probe()
    sys.setprofile(None)
    messages = [profiled] + [probe() for _ in range(4)]
print(json.dumps(messages))
'''
        outcomes: list[list[str]] = []
        for implementation in ('reference', 'native'):
            result = subprocess.run(
                [sys.executable, '-c', script, implementation],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outcomes.append(json.loads(result.stdout))
        self.assertEqual(len(outcomes[0]), 5)
        self.assertGreater(len(set(outcomes[0])), 1, 'reference must exercise profiling transition')
        self.assertEqual(outcomes[1], outcomes[0])

    def test_c_escape_preserves_surrogate_subclass_identity_without_escaping(self) -> None:
        class Text(str):
            pass

        def run(native: bool, source: str) -> tuple[str, list[tuple[str, bool, str]]]:
            value = Text(source)
            original_markup = markupsafe.Markup
            events: list[tuple[str, bool, str]] = []

            def markup(item: object) -> Markup:
                events.append((type(item).__name__, item is value, str(item)))
                return original_markup(item)

            with (patch.dict(filters.__dict__, {'str': lambda _: value}),
                  patch.dict(markupsafe.__dict__, {'str': lambda item: item, 'Markup': markup})):
                output = render('{{ value|forceescape }}', {'value': 'ignored'}, native)
            return output, events

        for source in ('\ud800', '\ud800&', "'\ud800", '\ud800<>\"'):
            with self.subTest(source=repr(source)):
                expected = run(False, source)
                with no_standard_forceescape():
                    actual = run(True, source)
                self.assertEqual(actual, expected)

    def test_invalid_escape_error_with_matching_instrumentation(self) -> None:
        script = '''
import json
import sys
from types import FrameType
from unittest.mock import patch
import markupsafe
from jinja2 import filters
from test_native_filter_evaluation import render

native = sys.argv[1] == "native"
profiled = sys.argv[2] == "profiled"
warmed = sys.argv[3] == "warmed"
calls: list[str] = []
codes = {filters.do_forceescape.__code__, markupsafe.escape.__code__}

def trace(frame: FrameType, event: str, argument: object) -> None:
    if event == "call" and frame.f_code in codes:
        calls.append(frame.f_code.co_name)

def invalid_str(value: object) -> int:
    return 123

def identity(value: object) -> object:
    return value

with patch.dict(filters.__dict__, {"str": invalid_str}), patch.dict(markupsafe.__dict__, {"str": identity}):
    if warmed:
        for _ in range(64):
            try:
                markupsafe.escape(123)
            except SystemError:
                pass
    if profiled:
        sys.setprofile(trace)
    try:
        render("{{ value|forceescape }}", {"value": "input"}, native)
    except SystemError as error:
        result = [type(error).__name__, str(error)]
    else:
        raise AssertionError("expected SystemError")
    finally:
        sys.setprofile(None)
if native and calls:
    raise AssertionError("standard Python body executed: " + repr(calls))
print(json.dumps(result))
'''
        for profile in ('unprofiled', 'profiled'):
            for temperature in ('cold', 'warmed'):
                with self.subTest(profile=profile, temperature=temperature):
                    outcomes: list[object] = []
                    for implementation in ('reference', 'native'):
                        result = subprocess.run(
                            [sys.executable, '-c', script, implementation, profile, temperature],
                            capture_output=True, text=True, check=False,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        outcomes.append(json.loads(result.stdout))
                    self.assertEqual(outcomes[0], outcomes[1])

    def test_pure_python_markup_escape_is_migrated_in_isolated_process(self) -> None:
        script = '''
import sys
import jinja2
import markupsafe
import markupsafe._native

markupsafe._escape_inner = markupsafe._native._escape_inner
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig

source = "{{ value|forceescape }}"
environment = jinja2.Environment()
context = {"value": "<&"}
expected = environment.from_string(source).render(context)
session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
codes = {jinja2.filters.do_forceescape.__code__, markupsafe.escape.__code__, markupsafe._native._escape_inner.__code__}
calls: list[str] = []
def profile(frame: object, event: str, arg: object) -> None:
    if event == "call" and frame.f_code in codes:
        calls.append(frame.f_code.co_name)
sys.setprofile(profile)
try:
    actual = session.render("case", source)
finally:
    sys.setprofile(None)
assert actual == expected, (actual, expected)
assert calls == [], calls

events: list[str] = []
class TraceString(str):
    def replace(self, old: str, new: str, count: int = -1) -> "TraceString":
        events.append(old)
        return TraceString(super().replace(old, new, count))

jinja2.filters.str = lambda value: TraceString(value)
markupsafe.str = lambda value: value
expected = environment.from_string(source).render(context)
expected_events = events.copy()
events.clear()
calls.clear()
sys.setprofile(profile)
try:
    actual = session.render("case", source)
finally:
    sys.setprofile(None)
assert (actual, events) == (expected, expected_events), (actual, events, expected, expected_events)
assert calls == [], calls
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_constant_and_surrogate(self) -> None:
        for source, context in (
            ("{{ '<&'|forceescape }}", {}),
            ('{{ value|forceescape|wordcount }}', {'value': 'a\ud800&b'}),
        ):
            with self.subTest(source=source):
                expected = render(source, context, False)
                with no_standard_forceescape():
                    actual = render(source, context, True)
                self.assertEqual(actual, expected)

    def test_runtime_values_and_document(self) -> None:
        for asynchronous in (False, True):
            for autoescape in (False, True):
                for value in ('<&\'"', Markup('<strong>&</strong>'), 123, None):
                    with self.subTest(asynchronous=asynchronous, autoescape=autoescape, value=value):
                        environment = jinja2.Environment(enable_async=asynchronous, autoescape=autoescape)
                        expected = render('{{ value|forceescape }}', {'value': value}, False, environment)
                        with no_standard_forceescape():
                            actual = render('{{ value|forceescape }}', {'value': value}, True, environment)
                        self.assertEqual(actual, expected)
            environment = jinja2.Environment(enable_async=asynchronous)
            reference = generate(docxtpl, '{{ value|forceescape }}', {'value': '<&'}, environment)
            with no_standard_forceescape():
                candidate = generate(docxtpl_native, '{{ value|forceescape }}', {'value': '<&'}, environment)
            self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_html_protocol_and_replaced_globals(self) -> None:
        def run(native: bool, mode: str) -> tuple[object, list[str]]:
            events: list[str] = []

            class Value:
                @property
                def __html__(self) -> object:
                    events.append('html lookup')

                    def html() -> str:
                        events.append('html call')
                        if mode == 'failure':
                            raise ValueError('html error')
                        return '<b>&</b>'

                    return html

            def replaced_escape(value: object) -> str:
                events.append('escape:' + str(value))
                return 'override'

            overrides = {'escape': replaced_escape} if mode == 'override' else {}
            with patch.dict(filters.__dict__, overrides):
                try:
                    result: object = render('{{ value|forceescape }}', {'value': Value()}, native)
                except ValueError as error:
                    result = (type(error).__name__, str(error))
            return result, events

        for mode in ('normal', 'failure', 'override'):
            with self.subTest(mode=mode):
                expected = run(False, mode)
                with no_standard_forceescape():
                    actual = run(True, mode)
                self.assertEqual(actual, expected)

    def test_live_typing_cast_and_escape_inner_overrides(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Value:
                def __html__(self) -> str:
                    events.append('html')
                    return '<x>'

            def cast(type_name: object, value: object) -> object:
                events.append('cast:' + str(type_name))
                return value

            def inner(value: str) -> str:
                events.append('escape:' + value)
                return 'CUSTOM'

            with (patch.dict(filters.__dict__, {'t': SimpleNamespace(cast=cast)}),
                  patch.dict(markupsafe.__dict__, {'_escape_inner': inner})):
                result = render('{{ value|forceescape }}', {'value': Value()}, native)
            return result, events

        expected = run(False)
        with no_standard_forceescape():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_replaced_str_with_invalid_result_preserves_exception(self) -> None:
        def run(native: bool) -> tuple[str, str]:
            with (patch.dict(filters.__dict__, {'str': lambda value: 123}),
                  patch.dict(markupsafe.__dict__, {'str': lambda value: value})):
                try:
                    render('{{ value|forceescape }}', {'value': 'input'}, native)
                except SystemError as error:
                    return type(error).__name__, str(error)
            raise AssertionError('expected SystemError')

        expected = run(False)
        with no_standard_forceescape():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_warmed_escape_invalid_input_preserves_exception(self) -> None:
        def run(native: bool) -> tuple[str, str]:
            try:
                render('{{ value|forceescape }}', {'value': 'input'}, native)
            except SystemError as error:
                return type(error).__name__, str(error)
            raise AssertionError('expected SystemError')

        def invalid_str(value: object) -> int:
            return 123

        def identity(value: object) -> object:
            return value

        with (patch.dict(filters.__dict__, {'str': invalid_str}),
              patch.dict(markupsafe.__dict__, {'str': identity})):
            for _ in range(32):
                try:
                    markupsafe.escape(123)
                except SystemError:
                    pass
            expected = run(False)
            with no_standard_forceescape():
                actual = run(True)
        self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
