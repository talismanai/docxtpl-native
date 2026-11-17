"""Differential checks for async loop iterator-conversion overrides."""

import json
import subprocess
import sys
import types
import unittest
import warnings
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
import jinja2.runtime
from docxtpl_native import bridge
from docxtpl_native.bridge import BridgeConfig
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


class AsyncLoopToIteratorTests(unittest.TestCase):
    def test_static_override_receives_original_object(self) -> None:
        def run(native: bool, late: bool) -> tuple[str, list[Any]]:
            events: list[Any] = []
            values = [1, 2]

            def factory(value: Any) -> Any:
                events.append(('factory', value is values))

                async def iterate() -> Any:
                    for item in value:
                        events.append(('yield', item))
                        yield item

                return iterate()

            def switch() -> list[int]:
                if late:
                    setattr(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(factory))  # noqa: B010
                return values

            original = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']
            try:
                if not late:
                    setattr(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(factory))  # noqa: B010
                source = '{% for x in switch() %}{{loop.index}}:{{x}};{% endfor %}'
                output = render(source, {'switch': switch}, native, jinja2.Environment(enable_async=True))
            finally:
                setattr(jinja2.runtime.AsyncLoopContext, '_to_iterator', original)  # noqa: B010
            return output, events

        for late in (False, True):
            with self.subTest(late=late):
                expected = run(False, late)
                self.assertEqual(expected, ('1:1;2:2;', [('factory', True), ('yield', 1), ('yield', 2)]))
                self.assertEqual(run(True, late), expected)

    def test_static_override_can_restore_class_before_loop_body(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            values = [1, 2]
            events: list[bool] = []
            original = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']

            def factory(value: Any) -> Any:
                events.append(value is values)
                setattr(jinja2.runtime.AsyncLoopContext, '_to_iterator', original)  # noqa: B010

                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(factory)):
                output = render('{% for x in values %}{{loop.index}}:{{x}};{% endfor %}', {'values': values}, native,
                                jinja2.Environment(enable_async=True))
            return output, events

        self.assertEqual(run(True), run(False))

    def test_override_with_standard_metadata_before_bridge_import(self) -> None:
        script = r"""
import json
import sys
import jinja2
import jinja2.runtime as runtime

events: list[str] = []
original = runtime.AsyncLoopContext.__dict__['_to_iterator'].__func__
runtime.__dict__['_probe_events'] = events
runtime.__dict__['_probe_original_to_iterator'] = original
source = "def _to_iterator(value: object) -> object:\n    _probe_events.append('factory')\n    return _probe_original_to_iterator(value)\n"
exec(compile(source, runtime.__file__, 'exec'), runtime.__dict__)
runtime.AsyncLoopContext._to_iterator = staticmethod(runtime.__dict__['_to_iterator'])
from test_native_filter_evaluation import render
output = render('{% for x in values %}{{loop.length}}:{{x}};{% endfor %}', {'values': iter([1, 2])},
                sys.argv[1] == 'native', jinja2.Environment(enable_async=True))
print(json.dumps([output, events]))
"""

        def run(native: bool) -> Any:
            result = subprocess.run([sys.executable, '-c', script, 'native' if native else 'reference'],
                                    capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

        expected = run(False)
        self.assertEqual(expected, ['2:1;2:2;', ['factory', 'factory']])
        self.assertEqual(run(True), expected)

    def test_exact_code_clone_after_snapshot_remains_callback(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            calls: list[str] = []
            original = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator'].__func__
            clone = types.FunctionType(original.__code__, original.__globals__, original.__name__,
                                       original.__defaults__, original.__closure__)
            clone.__qualname__ = original.__qualname__

            def profile(frame: Any, event: str, argument: Any) -> None:
                if event == 'call' and frame.f_code is original.__code__:
                    calls.append(frame.f_code.co_name)

            source = '{% for x in values %}{% if never %}{{loop.index}}{% endif %}{{x}}{% endfor %}'
            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(clone)):
                previous = sys.getprofile()
                try:
                    sys.setprofile(profile)
                    output = render(source, {'values': [1], 'never': False}, native,
                                    jinja2.Environment(enable_async=True))
                finally:
                    sys.setprofile(previous)
            return output, calls

        expected = run(False)
        self.assertEqual(expected, ('1', ['_to_iterator']))
        self.assertEqual(run(True), expected)

    def test_distinct_overrides_before_bridge_import(self) -> None:
        script = r"""
import json
import sys
from typing import Any
import jinja2
import jinja2.runtime as runtime

events: list[object] = []
async def iterate(value: Any) -> Any:
    for item in value:
        yield item

def static_factory(value: Any) -> Any:
    events.append('static')
    return iterate(value)

class Factory:
    def __call__(self, value: Any) -> Any:
        events.append('object')
        return iterate(value)

def class_factory(cls: type[Any], value: Any) -> Any:
    events.append(cls is runtime.AsyncLoopContext)
    return iterate(value)

class WrappedDescriptor:
    def __get__(self, instance: Any, owner: type[Any]) -> Any:
        events.append(instance is runtime.AsyncLoopContext and owner is runtime.AsyncLoopContext)
        def factory(value: Any) -> Any:
            events.append('wrapped')
            return iterate(value)
        return factory

kind = sys.argv[2]
if kind == 'static':
    runtime.AsyncLoopContext._to_iterator = staticmethod(static_factory)
elif kind == 'object':
    runtime.AsyncLoopContext._to_iterator = staticmethod(Factory())
elif kind == 'class':
    runtime.AsyncLoopContext._to_iterator = classmethod(class_factory)
else:
    runtime.AsyncLoopContext._to_iterator = classmethod(WrappedDescriptor())
from test_native_filter_evaluation import render
output = render('{% for x in values %}{{loop.index}}:{{x}};{% endfor %}', {'values': [1, 2]},
                sys.argv[1] == 'native', jinja2.Environment(enable_async=True))
print(json.dumps([output, events]))
"""

        def run(native: bool, kind: str) -> Any:
            result = subprocess.run([sys.executable, '-c', script, 'native' if native else 'reference', kind],
                                    capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

        for kind in ('static', 'object', 'class', 'wrapped'):
            with self.subTest(kind=kind):
                expected = run(False, kind)
                self.assertEqual(run(True, kind), expected)

    def test_exact_code_clone_before_bridge_import_remains_callback(self) -> None:
        script = r"""
import json
import sys
import types
from typing import Any
import jinja2
import jinja2.runtime as runtime

original = runtime.AsyncLoopContext.__dict__['_to_iterator'].__func__
clone = types.FunctionType(original.__code__, original.__globals__, original.__name__,
                           original.__defaults__, original.__closure__)
clone.__qualname__ = original.__qualname__
runtime.AsyncLoopContext._to_iterator = staticmethod(clone)
from test_native_filter_evaluation import render
calls: list[str] = []
def profile(frame: Any, event: str, argument: Any) -> None:
    if event == 'call' and frame.f_code is original.__code__:
        calls.append(frame.f_code.co_name)
previous = sys.getprofile()
try:
    sys.setprofile(profile)
    output = render('{% for x in values %}{{loop.length}}:{{x}};{% endfor %}', {'values': iter([1, 2])},
                    sys.argv[1] == 'native', jinja2.Environment(enable_async=True))
finally:
    sys.setprofile(previous)
print(json.dumps([output, calls]))
"""

        def run(native: bool) -> Any:
            result = subprocess.run([sys.executable, '-c', script, 'native' if native else 'reference'],
                                    capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

        expected = run(False)
        self.assertEqual(expected, ['2:1;2:2;', ['_to_iterator', '_to_iterator']])
        self.assertEqual(run(True), expected)

    def test_static_override_reenters_same_session_before_restoring_class(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            values = [1]
            environment = jinja2.Environment(enable_async=True)
            session: Any = None
            original = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']

            def factory(value: Any) -> Any:
                try:
                    if native:
                        session.render('inner', 'inner')
                    else:
                        environment.from_string('inner').render()
                except RuntimeError as error:
                    events.append(str(error))
                setattr(jinja2.runtime.AsyncLoopContext, '_to_iterator', original)  # noqa: B010

                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {'values': values})
            source = '{% for x in values %}{{loop.index}}{% endfor %}'
            with warnings.catch_warnings(), patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(factory)):
                warnings.simplefilter('ignore', RuntimeWarning)
                output = session.render('outer', source) if native else environment.from_string(source).render(values=values)
            return output, events

        expected = run(False)
        self.assertEqual(len(expected[1]), 1)
        self.assertEqual(run(True), expected)

    def test_static_override_document_with_class_restoration(self) -> None:
        def run(native: bool) -> tuple[Any, list[bool]]:
            values = [1, 2]
            events: list[bool] = []
            original = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']

            def factory(value: Any) -> Any:
                events.append(value is values)
                setattr(jinja2.runtime.AsyncLoopContext, '_to_iterator', original)  # noqa: B010

                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(factory)):
                document = generate(docxtpl_native if native else docxtpl,
                                    '{% for x in values %}{{loop.index}}:{{x}};{% endfor %}',
                                    {'values': values}, jinja2.Environment(enable_async=True))
            return document, events

        reference, expected_events = run(False)
        candidate, actual_events = run(True)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_class_override_can_restore_class_before_loop_body(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            events: list[bool] = []
            original = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']

            def factory(cls: type[Any], value: Any) -> Any:
                events.append(cls is jinja2.runtime.AsyncLoopContext)
                setattr(jinja2.runtime.AsyncLoopContext, '_to_iterator', original)  # noqa: B010

                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', classmethod(factory)):
                output = render('{% for x in values %}{{loop.index}}{% endfor %}', {'values': [1]}, native,
                                jinja2.Environment(enable_async=True))
            return output, events

        self.assertEqual(run(True), run(False))

    def test_static_callable_object_can_restore_class_before_loop_body(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            events: list[bool] = []
            values = [1]
            original = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']

            class Factory:
                def __call__(self, value: Any) -> Any:
                    events.append(value is values)
                    setattr(jinja2.runtime.AsyncLoopContext, '_to_iterator', original)  # noqa: B010

                    async def iterate() -> Any:
                        for item in value:
                            yield item

                    return iterate()

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(Factory())):
                output = render('{% for x in values %}{{loop.index}}{% endfor %}', {'values': values}, native,
                                jinja2.Environment(enable_async=True))
            return output, events

        self.assertEqual(run(True), run(False))

    def test_in_place_method_code_change_is_called(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            events: list[bool] = []
            values = [1]
            descriptor = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']
            function = descriptor.__func__
            original_code = function.__code__
            namespace = jinja2.runtime.__dict__
            injected = {'_to_iter_test_events': events, '_to_iter_test_values': values,
                        '_to_iter_test_function': function, '_to_iter_test_code': original_code}
            source = ("def callback(value: object) -> object:\n"
                      "    _to_iter_test_events.append(value is _to_iter_test_values)\n"
                      "    _to_iter_test_function.__code__ = _to_iter_test_code\n"
                      "    return auto_aiter(value)\n")
            with patch.dict(namespace, injected):
                exec(compile(source, '<custom-to-iterator>', 'exec'), namespace)  # noqa: S102
                function.__code__ = namespace['callback'].__code__
                try:
                    output = render('{% for x in values %}{{loop.index}}{% endfor %}', {'values': values}, native,
                                    jinja2.Environment(enable_async=True))
                finally:
                    function.__code__ = original_code
            return output, events

        self.assertEqual(run(True), run(False))

    def test_class_override_receives_real_loop_class(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            events: list[bool] = []

            def factory(cls: type[Any], value: Any) -> Any:
                events.append(cls is jinja2.runtime.AsyncLoopContext)

                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', classmethod(factory)):
                output = render('{% for x in values %}{{loop.index}}{% endfor %}', {'values': [1]}, native,
                                jinja2.Environment(enable_async=True))
            return output, events

        self.assertEqual(run(True), run(False))

    def test_override_result_does_not_require_aiter(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class NextOnly:
                def __init__(self) -> None:
                    self.values = iter([1, 2])

                def __aiter__(self) -> Any:
                    raise AssertionError('__aiter__ must not be called')

                async def __anext__(self) -> int:
                    events.append('next')
                    try:
                        return next(self.values)
                    except StopIteration as error:
                        raise StopAsyncIteration from error

            def factory(value: Any) -> NextOnly:
                events.append('factory')
                return NextOnly()

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(factory)):
                output = render('{% for x in values %}{{loop.index}}:{{x}};{% endfor %}', {'values': [1, 2]}, native,
                                jinja2.Environment(enable_async=True))
            return output, events

        self.assertEqual(run(True), run(False))

    def test_override_result_can_omit_aiter_after_class_restoration(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']

            class NextOnly:
                def __init__(self) -> None:
                    self.values = iter([1, 2])

                def __aiter__(self) -> Any:
                    raise AssertionError('__aiter__ must not be called')

                async def __anext__(self) -> int:
                    events.append('next')
                    try:
                        return next(self.values)
                    except StopIteration as error:
                        raise StopAsyncIteration from error

            def factory(value: Any) -> NextOnly:
                events.append('factory')
                setattr(jinja2.runtime.AsyncLoopContext, '_to_iterator', original)  # noqa: B010
                return NextOnly()

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(factory)):
                output = render('{% for x in values %}{{loop.index}}:{{x}};{% endfor %}', {'values': [1, 2]}, native,
                                jinja2.Environment(enable_async=True))
            return output, events

        self.assertEqual(run(True), run(False))

    def test_static_override_reference_count_during_call(self) -> None:
        def run(native: bool) -> list[int]:
            counts: list[int] = []
            original = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']

            def factory(value: Any) -> Any:
                counts.append(sys.getrefcount(factory))
                setattr(jinja2.runtime.AsyncLoopContext, '_to_iterator', original)  # noqa: B010

                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(factory)):
                render('{% for x in values %}{{loop.index}}{% endfor %}', {'values': [1]}, native,
                       jinja2.Environment(enable_async=True))
            return counts

        self.assertEqual(run(True), run(False))

    def test_static_override_is_reused_after_length_materialization(self) -> None:
        def run(native: bool) -> tuple[str, list[Any]]:
            events: list[Any] = []
            source = iter([1, 2])

            def factory(value: Any) -> Any:
                events.append(('factory', value is source, list(value) if isinstance(value, list) else None))

                async def iterate() -> Any:
                    for item in value:
                        events.append(('yield', item))
                        yield item

                return iterate()

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(factory)):
                output = render('{% for x in values %}{{loop.length}}:{{x}};{% endfor %}', {'values': source}, native,
                                jinja2.Environment(enable_async=True))
            return output, events

        expected = run(False)
        self.assertEqual(expected, ('2:1;2:2;', [('factory', True, None), ('yield', 1), ('yield', 2),
                                                ('factory', False, [2]), ('yield', 2)]))
        self.assertEqual(run(True), expected)

    def test_restoring_descriptor_can_return_original_method(self) -> None:
        def run(native: bool) -> str:
            original = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']

            class Descriptor:
                def __get__(self, instance: Any, owner: type[Any]) -> Any:
                    setattr(owner, '_to_iterator', original)  # noqa: B010
                    return original.__func__

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', Descriptor()):
                return render('{% for x in values %}{{loop.index}}{% endfor %}', {'values': [1]}, native,
                              jinja2.Environment(enable_async=True))

        self.assertEqual(run(True), run(False))

    def test_restoring_descriptor_result_is_bound_to_live_instance(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']

            class Descriptor:
                def __get__(self, instance: Any, owner: type[Any]) -> Any:
                    events.append('class' if instance is None else 'instance')
                    setattr(owner, '_to_iterator', original)  # noqa: B010

                    def callback(value: Any) -> Any:
                        events.append('callback')
                        return original.__func__(value)

                    return callback

            with patch.dict(globals(), {'auto_aiter': jinja2.runtime.auto_aiter}), patch.object(
                jinja2.runtime.AsyncLoopContext, '_to_iterator', Descriptor()
            ):
                output = render('{% for x in values %}{{loop.index}}{% endfor %}', {'values': [1]}, native,
                                jinja2.Environment(enable_async=True))
            return output, events

        expected = run(False)
        self.assertEqual(expected, ('1', ['instance', 'callback']))
        self.assertEqual(run(True), expected)

    def test_custom_descriptor_receives_live_loop_instance(self) -> None:
        def run(native: bool) -> tuple[str, list[tuple[bool, bool]]]:
            events: list[tuple[bool, bool]] = []

            class Descriptor:
                def __get__(self, instance: Any, owner: type[Any]) -> Any:
                    events.append((instance is None, isinstance(instance, owner)))

                    def factory(value: Any) -> Any:
                        async def iterate() -> Any:
                            for item in value:
                                yield item

                        return iterate()

                    return factory

            source = '{% for x in values %}{{loop.index}}{% endfor %}'
            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', Descriptor()):
                output = render(source, {'values': [1]}, native, jinja2.Environment(enable_async=True))
            return output, events

        expected = run(False)
        self.assertEqual(expected, ('1', [(False, True)]))
        self.assertEqual(run(True), expected)

    def test_custom_descriptor_is_not_called_with_no_instance(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            events: list[bool] = []

            class Descriptor:
                def __get__(self, instance: Any, owner: type[Any]) -> Any:
                    events.append(instance is None)
                    raise RuntimeError('descriptor requires a live loop')

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', Descriptor()):
                try:
                    render('{% for x in values %}{{loop.index}}{% endfor %}', {'values': [1]}, native,
                           jinja2.Environment(enable_async=True))
                except RuntimeError as error:
                    return str(error), events
            return 'rendered', events

        # The descriptor only ever receives the loop object.
        expected = run(False)
        self.assertEqual(expected, ('descriptor requires a live loop', [False]))
        self.assertEqual(run(True), expected)

    def test_modified_descriptor_runs_under_sandbox_as_in_reference(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def factory(value: Any) -> Any:
                events.append('factory')

                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(factory)):
                output = render('{% for x in values %}{{loop.index}}{% endfor %}', {'values': [1]}, native,
                                SandboxedEnvironment(enable_async=True))
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('1', ['factory']))
        self.assertEqual(observe(True), expected)

    def test_static_override_preserves_exception_identity(self) -> None:
        for native in (False, True):
            with self.subTest(native=native):
                marker = ValueError('conversion failed')
                values = [1]
                events: list[bool] = []

                def factory(value: Any, source: list[int] = values, failure: ValueError = marker,
                            trace: list[bool] = events) -> Any:
                    trace.append(value is source)
                    raise failure

                with patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', staticmethod(factory)), self.assertRaises(ValueError) as caught:
                    render('{% for x in values %}{{loop.index}}{% endfor %}', {'values': values}, native,
                           jinja2.Environment(enable_async=True))
                self.assertIs(caught.exception, marker)
                self.assertEqual(events, [True])

    def test_replaced_auto_aiter_is_reused_after_length_materialization(self) -> None:
        def run(native: bool) -> tuple[str, list[Any]]:
            source = iter([1, 2])
            events: list[Any] = []

            def factory(value: Any) -> Any:
                events.append(('factory', value is source, list(value) if isinstance(value, list) else None))

                async def iterate() -> Any:
                    for item in value:
                        events.append(('yield', item))
                        yield item

                return iterate()

            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                output = render('{% for x in values %}{{loop.length}}:{{x}};{% endfor %}', {'values': source}, native,
                                jinja2.Environment(enable_async=True))
            return output, events

        expected = run(False)
        self.assertEqual(expected, ('2:1;2:2;', [('factory', True, None), ('yield', 1), ('yield', 2),
                                                ('factory', False, [2]), ('yield', 2)]))
        self.assertEqual(run(True), expected)

    def test_default_method_and_adapter_bodies_stay_native_after_length(self) -> None:
        import jinja2.async_utils

        method_code = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator'].__func__.__code__
        adapter_code = jinja2.async_utils._IteratorToAsyncIterator.__anext__.__code__
        calls: list[str] = []
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: Any) -> None:
            if event == 'call' and frame.f_code in (method_code, adapter_code):
                calls.append(frame.f_code.co_name)

        try:
            sys.setprofile(profile)
            output = render('{% for x in values %}{{loop.length}}:{{x}};{% endfor %}',
                            {'values': iter([1, 2])}, True, jinja2.Environment(enable_async=True))
        finally:
            sys.setprofile(previous)
        self.assertEqual(output, '2:1;2:2;')
        self.assertEqual(calls, [])

    def test_snapshot_lookup_does_not_invoke_unrelated_key_equality(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            target = '_RUNTIME_ASYNC_LOOP_CLASS_ITEMS'

            class CollidingKey(str):
                def __hash__(self) -> int:
                    return hash(target)

                def __eq__(self, other: object) -> bool:
                    events.append('compare')
                    return False

            source = '{% for x in values %}{% if never %}{{loop.index}}{% endif %}{{x}}{% endfor %}'
            with patch.dict(bridge.__dict__):
                snapshot = bridge.__dict__.pop(target)
                bridge.__dict__[CollidingKey('other')] = object()
                bridge.__dict__[target] = snapshot
                events.clear()
                output = render(source, {'values': [1], 'never': False}, native,
                                jinja2.Environment(enable_async=True))
                observed = list(events)
            return output, observed

        expected = run(False)
        self.assertEqual(expected, ('1', []))
        self.assertEqual(run(True), expected)

    def test_changed_snapshot_does_not_execute_standard_method(self) -> None:
        code = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator'].__func__.__code__
        calls: list[str] = []
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: Any) -> None:
            if event == 'call' and frame.f_code is code:
                calls.append(frame.f_code.co_name)

        source = '{% for x in values %}{% if never %}{{loop.index}}{% endif %}{{x}}{% endfor %}'
        snapshots = tuple((key, object() if key == '_to_iterator' else value)
                          for key, value in bridge._RUNTIME_ASYNC_LOOP_CLASS_ITEMS)
        with patch.dict(bridge.__dict__, {'_RUNTIME_ASYNC_LOOP_CLASS_ITEMS': snapshots}):
            try:
                sys.setprofile(profile)
                output = render(source, {'values': [1], 'never': False}, True,
                                jinja2.Environment(enable_async=True))
            finally:
                sys.setprofile(previous)
        self.assertEqual(output, '1')
        self.assertEqual(calls, [])

    def test_unused_runtime_reference_does_not_execute_standard_method(self) -> None:
        code = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator'].__func__.__code__
        calls: list[str] = []
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: Any) -> None:
            if event == 'call' and frame.f_code is code:
                calls.append(frame.f_code.co_name)

        source = '{% for x in values %}{% if never %}{{loop.index}}{% endif %}{{x}}{% endfor %}'
        replacement = types.ModuleType('replacement')
        replacement.__dict__.update(jinja2.runtime.__dict__)
        with patch.dict(bridge.__dict__, {'_RUNTIME_MODULE': replacement}):
            try:
                sys.setprofile(profile)
                output = render(source, {'values': [1], 'never': False}, True,
                                jinja2.Environment(enable_async=True))
            finally:
                sys.setprofile(previous)
        self.assertEqual(output, '1')
        self.assertEqual(calls, [])

    def test_unused_snapshot_attributes_do_not_block_standard_loop(self) -> None:
        source = '{% for x in values %}{% if never %}{{loop.index}}{% endif %}{{x}}{% endfor %}'
        for name in ('_RUNTIME_ASYNC_LOOP_CLASS_ITEMS', '_RUNTIME_ASYNC_LOOP_CLASS_CODES'):
            with self.subTest(name=name), patch.dict(bridge.__dict__, {name: None}):
                expected = render(source, {'values': [1], 'never': False}, False,
                                  jinja2.Environment(enable_async=True))
                actual = render(source, {'values': [1], 'never': False}, True,
                                jinja2.Environment(enable_async=True))
                self.assertEqual(actual, expected)

    def test_default_static_method_body_stays_native(self) -> None:
        descriptor = jinja2.runtime.AsyncLoopContext.__dict__['_to_iterator']
        code = descriptor.__func__.__code__
        calls: list[str] = []
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: Any) -> None:
            if event == 'call' and frame.f_code is code:
                calls.append(frame.f_code.co_name)

        try:
            sys.setprofile(profile)
            output = render('{% for x in values %}{{loop.index}}{% endfor %}', {'values': [1]}, True,
                            jinja2.Environment(enable_async=True))
        finally:
            sys.setprofile(previous)
        self.assertEqual(output, '1')
        self.assertEqual(calls, [])
