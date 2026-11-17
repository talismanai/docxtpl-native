"""Differential checks for a user-supplied async loop iterator factory."""

import gc
import sys
import unittest
import weakref
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
import jinja2.async_utils
import jinja2.runtime
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


class AsyncLoopAutoAiterTests(unittest.TestCase):
    def test_replaced_auto_aiter_receives_original_iterable(self) -> None:
        def run(native: bool, metadata: bool, sandboxed: bool, document: bool) -> tuple[Any, list[object]]:
            events: list[object] = []
            values = [1, 2]

            def factory(value: Any) -> Any:
                events.append(('factory', value is values))

                async def iterate() -> Any:
                    for item in value:
                        events.append(('yield', item))
                        yield item

                return iterate()

            environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=True)
            source = ('{% for x in values %}{{ loop.index }}:{{ x }};{% endfor %}' if metadata
                      else '{% for x in values %}{{ x }};{% endfor %}')
            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                if document:
                    output = generate(docxtpl_native if native else docxtpl, source, {'values': values}, environment)
                else:
                    output = render(source, {'values': values}, native, environment)
            return output, events

        for metadata in (False, True):
            for sandboxed in (False, True):
                with self.subTest(metadata=metadata, sandboxed=sandboxed):
                    expected, expected_events = run(False, metadata, sandboxed, False)
                    self.assertEqual(expected_events, [('factory', True), ('yield', 1), ('yield', 2)])
                    actual, actual_events = run(True, metadata, sandboxed, False)
                    self.assertEqual((actual, actual_events), (expected, expected_events))
                    reference, reference_events = run(False, metadata, sandboxed, True)
                    candidate, candidate_events = run(True, metadata, sandboxed, True)
                    self.assertEqual(candidate_events, reference_events)
                    self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_factory_selection_during_iterable_evaluation(self) -> None:
        def run(native: bool, metadata: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def factory(value: Any) -> Any:
                events.append('factory')

                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            def switch() -> list[int]:
                jinja2.runtime.__dict__['auto_aiter'] = factory
                return [1, 2]

            source = ('{% for x in switch() %}{{loop.index}}{% endfor %}' if metadata
                      else '{% for x in switch() %}{{x}}{% endfor %}')
            with patch.object(jinja2.runtime, 'auto_aiter', jinja2.runtime.auto_aiter):
                return render(source, {'switch': switch}, native, jinja2.Environment(enable_async=True)), events

        for metadata in (False, True):
            with self.subTest(metadata=metadata):
                self.assertEqual(run(True, metadata), run(False, metadata))

    def test_simple_loop_does_not_access_metadata_iterator(self) -> None:
        class UnexpectedAccess:
            def __get__(self, instance: Any, owner: Any) -> Any:
                raise ValueError('unexpected early access')

        class Items:
            def __aiter__(self) -> Any:
                async def iterate() -> Any:
                    yield 1

                return iterate()

        for native in (False, True):
            with self.subTest(native=native), patch.object(jinja2.runtime.AsyncLoopContext, '_to_iterator', UnexpectedAccess()):
                self.assertEqual(render('{% for x in values %}{{x}}{% endfor %}', {'values': Items()},
                                        native, jinja2.Environment(enable_async=True)), '1')

    def test_replaced_factory_in_filtered_loop(self) -> None:
        def run(native: bool, metadata: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def factory(value: Any) -> Any:
                events.append('factory')

                async def iterate() -> Any:
                    if hasattr(value, '__aiter__'):
                        async for item in value:
                            yield item
                    else:
                        for item in value:
                            yield item

                return iterate()

            source = ('{% for x in values if x > 1 %}{{loop.index}}:{{x}};{% endfor %}' if metadata
                      else '{% for x in values if x > 1 %}{{x}};{% endfor %}')
            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                return render(source, {'values': [1, 2]}, native, jinja2.Environment(enable_async=True)), events

        for metadata in (False, True):
            with self.subTest(metadata=metadata):
                expected = run(False, metadata)
                self.assertEqual(run(True, metadata), expected)

    def test_standard_factory_algorithm_stays_native(self) -> None:
        calls: list[str] = []
        code = jinja2.async_utils.auto_aiter.__code__
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: Any) -> None:
            if event == 'call' and frame.f_code is code:
                calls.append(frame.f_code.co_name)

        try:
            sys.setprofile(profile)
            for metadata in (False, True):
                source = ('{% for x in values %}{{loop.index}}:{{x}};{% endfor %}' if metadata
                          else '{% for x in values %}{{x}};{% endfor %}')
                render(source, {'values': [1, 2]}, True, jinja2.Environment(enable_async=True))
        finally:
            sys.setprofile(previous)
        self.assertEqual(calls, [])

    def test_factory_reference_count_in_unfiltered_loop(self) -> None:
        def run(native: bool, metadata: bool) -> list[int]:
            counts: list[int] = []

            def factory(value: Any) -> Any:
                counts.append(sys.getrefcount(factory))

                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            source = ('{% for x in values %}{{loop.index}}{% endfor %}' if metadata
                      else '{% for x in values %}{{x}}{% endfor %}')
            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                render(source, {'values': [1]}, native, jinja2.Environment(enable_async=True))
            return counts

        for metadata in (False, True):
            with self.subTest(metadata=metadata):
                self.assertEqual(run(True, metadata), run(False, metadata))

    def test_live_factory_can_remove_itself_without_early_destruction(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            initial = jinja2.runtime.auto_aiter

            class Factory:
                def __call__(self, value: Any) -> Any:
                    events.append('call')
                    jinja2.runtime.__dict__['auto_aiter'] = initial
                    gc.collect()
                    events.append('after-collect')

                    async def iterate() -> Any:
                        events.append('start')
                        for item in value:
                            yield item

                    return iterate()

                def __del__(self) -> None:
                    events.append('destroy')

            def switch() -> list[int]:
                jinja2.runtime.__dict__['auto_aiter'] = Factory()
                return [1]

            def after() -> str:
                events.append('after')
                return ''

            source = '{% for x in switch() %}{{loop.index}}{% endfor %}{{after()}}'
            with patch.object(jinja2.runtime, 'auto_aiter', initial):
                result = render(source, {'switch': switch, 'after': after}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        expected = run(False)
        self.assertEqual(expected, ('1', ['call', 'after-collect', 'destroy', 'start', 'after']))
        self.assertEqual(run(True), expected)

    def test_live_function_can_remove_itself_while_running(self) -> None:
        def run(native: bool) -> tuple[str, list[Any]]:
            events: list[Any] = []
            references: list[weakref.ReferenceType[Any]] = []
            initial = jinja2.runtime.auto_aiter

            def switch() -> list[int]:
                def factory(value: Any) -> Any:
                    events.append('call')
                    jinja2.runtime.__dict__['auto_aiter'] = initial
                    gc.collect()
                    events.append(('alive', references[0]() is not None))

                    async def iterate() -> Any:
                        events.append('start')
                        for item in value:
                            yield item

                    return iterate()

                references.append(weakref.ref(factory, lambda _: events.append('destroy')))
                jinja2.runtime.__dict__['auto_aiter'] = factory
                return [1]

            def after() -> str:
                events.append('after')
                return ''

            source = '{% for x in switch() %}{{loop.index}}{% endfor %}{{after()}}'
            with patch.object(jinja2.runtime, 'auto_aiter', initial):
                result = render(source, {'switch': switch, 'after': after}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        expected = run(False)
        self.assertEqual(expected, ('1', ['call', ('alive', True), 'destroy', 'start', 'after']))
        self.assertEqual(run(True), expected)

    def test_live_callable_object_reference_count_after_rebinding(self) -> None:
        def run(native: bool) -> tuple[str, list[Any]]:
            events: list[Any] = []
            initial = jinja2.runtime.auto_aiter

            class Factory:
                def __call__(self, value: Any) -> Any:
                    events.append(('before', sys.getrefcount(self)))
                    jinja2.runtime.__dict__['auto_aiter'] = initial
                    events.append(('after', sys.getrefcount(self)))

                    async def iterate() -> Any:
                        for item in value:
                            yield item

                    return iterate()

                def __del__(self) -> None:
                    events.append(('destroy',))

            def switch() -> list[int]:
                jinja2.runtime.__dict__['auto_aiter'] = Factory()
                return [1]

            with patch.object(jinja2.runtime, 'auto_aiter', initial):
                result = render('{% for x in switch() %}{{loop.index}}{% endfor %}', {'switch': switch},
                                native, jinja2.Environment(enable_async=True))
            return result, events

        expected = run(False)
        self.assertEqual(expected[0], '1')
        self.assertEqual(expected[1][-1], ('destroy',))
        self.assertEqual(run(True), expected)

    def test_live_factory_reference_count_after_rebinding(self) -> None:
        def run(native: bool) -> list[int]:
            counts: list[int] = []

            def initial(value: Any) -> Any:
                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            def replacement(value: Any) -> Any:
                counts.append(sys.getrefcount(replacement))

                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            def switch() -> list[int]:
                jinja2.runtime.__dict__['auto_aiter'] = replacement
                return [1]

            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': initial}):
                render('{% for x in switch() %}{{loop.index}}{% endfor %}', {'switch': switch}, native,
                       jinja2.Environment(enable_async=True))
            return counts

        self.assertEqual(run(True), run(False))

    def test_replaced_auto_aiter_exception_identity(self) -> None:
        for metadata in (False, True):
            for native in (False, True):
                with self.subTest(metadata=metadata, native=native):
                    marker = ValueError('factory failed')
                    values = [1]
                    events: list[bool] = []

                    def factory(value: Any, trace: list[bool] = events,
                                source: list[int] = values, failure: ValueError = marker) -> Any:
                        trace.append(value is source)
                        raise failure

                    environment = jinja2.Environment(enable_async=True)
                    source = ('{% for x in values %}{{loop.index}}{% endfor %}' if metadata
                              else '{% for x in values %}{{x}}{% endfor %}')
                    with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}), self.assertRaises(ValueError) as caught:
                        render(source, {'values': values}, native, environment)
                    self.assertIs(caught.exception, marker)
                    self.assertEqual(events, [True])
