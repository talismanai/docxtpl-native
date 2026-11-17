"""Differential protocol checks for async filtered-loop iterator factories."""

import gc
import inspect
import sys
import unittest
import warnings
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
import jinja2.async_utils
import jinja2.runtime
from docxtpl_native.bridge import BridgeConfig
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


class AsyncFilteredFactoryTests(unittest.TestCase):
    def test_complete_document_with_replaced_factory(self) -> None:
        def run(native: bool) -> tuple[Any, list[str]]:
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

            environment = jinja2.Environment(enable_async=True)
            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                document = generate(docxtpl_native if native else docxtpl,
                                    '{% for x in values if x > 1 %}{{x}};{% endfor %}',
                                    {'values': [1, 2]}, environment)
            return document, events

        reference, expected_events = run(False)
        candidate, actual_events = run(True)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_two_factory_calls_are_lazy_and_preserve_identity(self) -> None:
        def run(native: bool, empty: bool) -> tuple[str, list[Any]]:
            source: list[int] = [] if empty else [1, 2]
            events: list[Any] = []
            intermediate: list[Any] = []

            def factory(value: Any) -> Any:
                events.append(('factory', value is source, bool(intermediate) and value is intermediate[0]))

                async def iterate() -> Any:
                    events.append(('start', value is source))
                    if hasattr(value, '__aiter__'):
                        async for item in value:
                            events.append(('yield', item))
                            yield item
                    else:
                        for item in value:
                            events.append(('yield', item))
                            yield item

                result = iterate()
                if not intermediate:
                    intermediate.append(result)
                return result

            def predicate(value: int) -> bool:
                events.append(('predicate', value))
                return value > 1

            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                result = render('{% for x in values if predicate(x) %}{{x}};{% endfor %}',
                                {'values': source, 'predicate': predicate}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        for empty in (False, True):
            with self.subTest(empty=empty):
                expected = run(False, empty)
                self.assertEqual(run(True, empty), expected)

    def test_second_factory_can_adapt_a_non_iterable_first_result(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            sentinel = object()

            class Items:
                def __aiter__(self) -> Any:
                    events.append('aiter')

                    async def iterate() -> Any:
                        events.append('yield')
                        yield 2

                    return iterate()

            def factory(value: Any) -> Any:
                if value is sentinel:
                    events.append('second')
                    return Items()
                events.append('first')
                return sentinel

            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                output = render('{% for x in values if x %}{{x}}{% endfor %}', {'values': [1]}, native,
                                jinja2.Environment(enable_async=True))
            return output, events

        expected = run(False)
        self.assertEqual(expected, ('2', ['first', 'second', 'aiter', 'yield']))
        self.assertEqual(run(True), expected)

    def test_intermediate_lifetime_and_reference_count(self) -> None:
        def run(native: bool) -> tuple[str, list[Any]]:
            events: list[Any] = []

            class Intermediate:
                def __aiter__(self) -> Any:
                    events.append(('aiter', sys.getrefcount(self)))

                    async def iterate() -> Any:
                        yield 1

                    return iterate()

                def __del__(self) -> None:
                    events.append(('destroy',))

            def factory(value: Any) -> Any:
                if isinstance(value, Intermediate):
                    events.append(('second', sys.getrefcount(value)))
                    return value
                events.append(('first',))
                return Intermediate()

            def after() -> str:
                events.append(('after',))
                return ''

            source = '{% for x in values if x %}{{x}}{% endfor %}{{after()}}'
            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                result = render(source, {'values': [1], 'after': after}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))

    def test_break_closes_both_factory_results_after_following_output(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def factory(value: Any) -> Any:
                events.append('factory')

                async def iterate() -> Any:
                    try:
                        if hasattr(value, '__aiter__'):
                            async for item in value:
                                yield item
                        else:
                            for item in value:
                                yield item
                    finally:
                        events.append('closed')

                return iterate()

            def after() -> str:
                events.append('after')
                return ''

            environment = jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols'])
            source = '{% for x in values if x %}{{x}}{% break %}{% endfor %}{{after()}}'
            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                result = render(source, {'values': [1, 2], 'after': after}, native, environment)
            return result, events

        expected = run(False)
        self.assertEqual(expected, ('1', ['factory', 'factory', 'after', 'closed', 'closed']))
        self.assertEqual(run(True), expected)

    def test_factory_rebinding_preserves_lifetime_after_render(self) -> None:
        def run(native: bool, filtered: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def replacement(value: Any) -> Any:
                events.append('replacement')

                async def iterate() -> Any:
                    for item in value:
                        yield item

                return iterate()

            class Factory:
                def __call__(self, value: Any) -> Any:
                    events.append('factory')
                    jinja2.runtime.__dict__['auto_aiter'] = replacement

                    async def iterate() -> Any:
                        if hasattr(value, '__aiter__'):
                            async for item in value:
                                yield item
                        else:
                            for item in value:
                                yield item

                    return iterate()

                def __del__(self) -> None:
                    events.append('destroy')

            def after() -> str:
                events.append('after')
                return ''

            previous = jinja2.runtime.auto_aiter
            try:
                jinja2.runtime.__dict__['auto_aiter'] = Factory()
                source = ('{% for x in values if x %}{{x}}{% endfor %}{{after()}}' if filtered
                          else '{% for x in values %}{{x}}{% endfor %}{{after()}}')
                result = render(source, {'values': [1], 'after': after}, native,
                                jinja2.Environment(enable_async=True))
                events.append('returned')
                gc.collect()
                events.append('collected')
            finally:
                jinja2.runtime.__dict__['auto_aiter'] = previous
            return result, events

        for filtered in (False, True):
            with self.subTest(filtered=filtered):
                expected = run(False, filtered)
                self.assertEqual(run(True, filtered), expected)

    def test_factory_can_reenter_same_session_without_locking_imports(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            environment = jinja2.Environment(enable_async=True)
            session: Any = None

            def factory(value: Any) -> Any:
                try:
                    if native:
                        session.render('inner', 'inner')
                    else:
                        environment.from_string('inner').render()
                except RuntimeError as error:
                    events.append(str(error))

                async def iterate() -> Any:
                    if hasattr(value, '__aiter__'):
                        async for item in value:
                            yield item
                    else:
                        for item in value:
                            yield item

                return iterate()

            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {'values': [1]})
            source = '{% for x in values if x %}{{x}}{% endfor %}'
            with warnings.catch_warnings(), patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                warnings.simplefilter('ignore', RuntimeWarning)
                result = (session.render('outer', source) if native
                          else environment.from_string(source).render(values=[1]))
            return result, events

        expected = run(False)
        self.assertEqual(len(expected[1]), 2)
        self.assertEqual(run(True), expected)

    def test_factory_reference_count_during_calls(self) -> None:
        def run(native: bool) -> list[int]:
            counts: list[int] = []

            def factory(value: Any) -> Any:
                counts.append(sys.getrefcount(factory))

                async def iterate() -> Any:
                    if hasattr(value, '__aiter__'):
                        async for item in value:
                            yield item
                    else:
                        for item in value:
                            yield item

                return iterate()

            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                render('{% for x in values if x %}{{x}}{% endfor %}', {'values': [1]}, native,
                       jinja2.Environment(enable_async=True))
            return counts

        self.assertEqual(run(True), run(False))

    def test_accumulated_filtered_loops_do_not_add_a_factory_call(self) -> None:
        def run(native: bool, metadata: bool) -> tuple[str, int]:
            calls: list[None] = []

            def factory(value: Any) -> Any:
                calls.append(None)

                async def iterate() -> Any:
                    if hasattr(value, '__aiter__'):
                        async for item in value:
                            yield item
                    else:
                        for item in value:
                            yield item

                return iterate()

            body = '{{loop.index}}' if metadata else '{{x}}'
            source = '{% if false %}{{loop}}{% endif %}{% for x in values if x %}' + body + '{% endfor %}'
            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                result = render(source, {'values': [1]}, native, jinja2.Environment(enable_async=True))
            return result, len(calls)

        for metadata in (False, True):
            with self.subTest(metadata=metadata):
                expected = run(False, metadata)
                self.assertEqual(expected, ('1', 2))
                self.assertEqual(run(True, metadata), expected)

    def test_accumulated_filter_factory_input_identity(self) -> None:
        def run(native: bool) -> tuple[str, list[tuple[bool, bool]]]:
            source = [1]
            calls: list[tuple[bool, bool]] = []

            def factory(value: Any) -> Any:
                calls.append((inspect.isasyncgen(value), value is source))

                async def iterate() -> Any:
                    if hasattr(value, '__aiter__'):
                        async for item in value:
                            yield item
                    else:
                        for item in value:
                            yield item

                return iterate()

            template = '{% if false %}{{loop}}{% endif %}{% for x in values if x %}{{loop.index}}{% endfor %}'
            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                result = render(template, {'values': source}, native, jinja2.Environment(enable_async=True))
            return result, calls

        self.assertEqual(run(True), run(False))

    def test_filtered_factory_can_transform_filtered_items(self) -> None:
        def run(native: bool) -> tuple[str, list[tuple[str, Any]]]:
            events: list[tuple[str, Any]] = []

            def factory(value: Any) -> Any:
                filtered = inspect.isasyncgen(value)
                events.append(('factory', filtered))

                async def iterate() -> Any:
                    if filtered:
                        async for item in value:
                            events.append(('outer', item))
                            yield item * 10
                    else:
                        for item in value:
                            events.append(('source', item))
                            yield item

                return iterate()

            source = ('{% if false %}{{loop}}{% endif %}'
                      '{% for x in values if x > 1 %}{{loop.index}}:{{x}};{% endfor %}')
            with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}):
                result = render(source, {'values': [1, 2, 3]}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        expected = run(False)
        self.assertEqual(expected, ('1:20;2:30;', [('factory', True), ('factory', False),
                                                   ('source', 1), ('source', 2), ('outer', 2),
                                                   ('source', 3), ('outer', 3)]))
        self.assertEqual(run(True), expected)

    def test_standard_filtered_factory_algorithm_stays_native(self) -> None:
        code = jinja2.async_utils.auto_aiter.__code__
        calls: list[str] = []
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: Any) -> None:
            if event == 'call' and frame.f_code is code:
                calls.append(frame.f_code.co_name)

        try:
            sys.setprofile(profile)
            for metadata in (False, True):
                body = '{{loop.index}}:{{x}};' if metadata else '{{x}};'
                source = '{% for x in values if x > 1 %}' + body + '{% endfor %}'
                render(source, {'values': [1, 2]}, True, jinja2.Environment(enable_async=True))
        finally:
            sys.setprofile(previous)
        self.assertEqual(calls, [])

    def test_second_factory_exception_keeps_identity(self) -> None:
        for native in (False, True):
            with self.subTest(native=native):
                marker = ValueError('second factory')
                events: list[int] = []

                def factory(value: Any, trace: list[int] = events, failure: ValueError = marker) -> Any:
                    trace.append(len(trace) + 1)
                    if len(trace) == 2:
                        raise failure

                    async def iterate() -> Any:
                        for item in value:
                            yield item

                    return iterate()

                with patch.dict(jinja2.runtime.__dict__, {'auto_aiter': factory}), self.assertRaises(ValueError) as caught:
                    render('{% for x in values if x %}{{x}}{% endfor %}', {'values': [1]}, native,
                           jinja2.Environment(enable_async=True))
                self.assertIs(caught.exception, marker)
                self.assertEqual(events, [1, 2])
