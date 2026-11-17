"""Filtered-loop comparisons must not advance the source ahead of the body."""

import io
import unittest
from collections.abc import AsyncIterator, Iterator
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from paritylab.compare import compare_docx
from paritylab.protocol_base import template


class FilteredComparisonTests(unittest.TestCase):
    def test_comparison_filter_preserves_complete_document(self) -> None:
        source = template('{% for x in values if x > 0 %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}')

        def observe(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            def values() -> Iterator[int]:
                for value in range(3):
                    events.append(f'yield:{value}')
                    yield value

            def tick() -> str:
                events.append('body')
                return ''

            module: Any = docxtpl_native if native else docxtpl
            document = module.DocxTemplate(io.BytesIO(source))
            document.render({'values': values(), 'tick': tick},
                            jinja_env=jinja2.Environment(enable_async=asynchronous))
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected, expected_events = observe(False, asynchronous)
                actual, actual_events = observe(True, asynchronous)
                self.assertEqual(actual_events, expected_events)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_comparison_filter_preserves_source_and_body_order(self) -> None:
        def observe(native: bool, asynchronous: bool, expression: str) -> tuple[str, list[str]]:
            events: list[str] = []

            def values() -> Iterator[int]:
                for value in range(3):
                    events.append(f'yield:{value}')
                    yield value

            def tick() -> str:
                events.append('body')
                return ''

            source = '{% for x in values if ' + expression + ' %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            environment = jinja2.Environment(enable_async=asynchronous)
            context = {'values': values(), 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for expression in ('x > 0', 'x >= 1', 'x < 2', 'x <= 1', 'x == 1', 'x != 1',
                               '0 < x', '1 <= x', '2 > x', '1 >= x', '1 == x', '1 != x'):
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    expected = observe(False, asynchronous, expression)
                    if expression == 'x > 0':
                        self.assertEqual(expected, ('1:1;2:2;', ['yield:0', 'yield:1', 'body', 'yield:2', 'body']))
                    self.assertEqual(observe(True, asynchronous, expression), expected)

    def test_comparison_callback_failure_occurs_after_previous_body(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Item:
                def __init__(self, value: int) -> None:
                    self.value = value

                def __gt__(self, other: object) -> bool:
                    events.append(f'compare:{self.value}')
                    if self.value == 2:
                        raise RuntimeError('comparison failed')
                    return True

            def tick() -> str:
                events.append('body')
                return ''

            context = {'values': [Item(1), Item(2)], 'tick': tick}
            source = '{% for x in values if x > 0 %}{{loop.index}}{{tick()}}{% endfor %}'
            environment = jinja2.Environment(enable_async=asynchronous)
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('comparison', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                return str(error), events
            raise AssertionError('expected comparison failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('comparison failed', ['compare:1', 'body', 'compare:2']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_comparison_truthiness_is_evaluated_per_item(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Result:
                def __init__(self, value: int) -> None:
                    self.value = value

                def __bool__(self) -> bool:
                    events.append(f'bool:{self.value}')
                    return self.value > 0

            class Item:
                def __init__(self, value: int) -> None:
                    self.value = value

                def __gt__(self, other: object) -> Any:
                    events.append(f'compare:{self.value}')
                    return Result(self.value)

                def __str__(self) -> str:
                    return str(self.value)

            def tick() -> str:
                events.append('body')
                return ''

            context = {'values': [Item(0), Item(1), Item(2)], 'tick': tick}
            source = '{% for x in values if x > 0 %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_comparison_filter_preserves_metadata_and_lookahead_order(self) -> None:
        def observe(native: bool, asynchronous: bool, field: str) -> tuple[str, list[str]]:
            events: list[str] = []

            def values() -> Iterator[int]:
                for value in range(4):
                    events.append(f'yield:{value}')
                    yield value

            def tick() -> str:
                events.append('body')
                return ''

            source = '{% for x in values if x > 0 %}{{loop.' + field + '}}:{{x}}{{tick()}};{% endfor %}'
            environment = jinja2.Environment(enable_async=asynchronous)
            context = {'values': values(), 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for field in ('index', 'last', 'nextitem', 'length', 'revindex'):
                with self.subTest(asynchronous=asynchronous, field=field):
                    self.assertEqual(observe(True, asynchronous, field), observe(False, asynchronous, field))

    def test_comparison_filter_preserves_async_iterator_order(self) -> None:
        def observe(native: bool, field: str) -> tuple[str, list[str]]:
            events: list[str] = []

            async def values() -> AsyncIterator[int]:
                for value in range(4):
                    events.append(f'yield:{value}')
                    yield value

            def tick() -> str:
                events.append('body')
                return ''

            source = '{% for x in values if x > 0 %}{{loop.' + field + '}}:{{x}}{{tick()}};{% endfor %}'
            environment = jinja2.Environment(enable_async=True)
            context = {'values': values(), 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for field in ('index', 'last', 'length'):
            with self.subTest(field=field):
                self.assertEqual(observe(True, field), observe(False, field))


if __name__ == '__main__':
    unittest.main()
