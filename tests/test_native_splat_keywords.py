"""Dynamic positional and keyword arguments remain distinct across the VM."""

import unittest
from collections.abc import Iterator

import jinja2
from test_native_filter_evaluation import render


class SplatKeywordTests(unittest.TestCase):
    def test_filter_test_and_call_preserve_arguments_and_order(self) -> None:
        def observe(native: bool, kind: str, asynchronous: bool) -> tuple[str, list[object]]:
            events: list[object] = []

            class Items:
                def __iter__(self) -> Iterator[int]:
                    events.append('iterate')
                    return iter([1, 2])

            def items() -> Items:
                events.append('items')
                return Items()

            def keywords() -> dict[str, int]:
                events.append('keywords')
                return {'label': 3}

            def callback(*args: object, **kwargs: object) -> str:
                events.append((args, kwargs))
                return 'ok'

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.filters['probe'] = callback
            environment.tests['probe'] = callback
            expressions = {
                'call': 'callback(*items(), **keywords())',
                'filter': 'value|probe(*items(), **keywords())',
                'test': 'value is probe(*items(), **keywords())',
            }
            result = render('{{ ' + expressions[kind] + ' }}',
                            {'value': 'V', 'items': items, 'keywords': keywords, 'callback': callback},
                            native, environment)
            return result, events

        for asynchronous in (False, True):
            for kind in ('call', 'filter', 'test'):
                with self.subTest(asynchronous=asynchronous, kind=kind):
                    self.assertEqual(observe(True, kind, asynchronous), observe(False, kind, asynchronous))

    def test_macro_dynamic_keywords(self) -> None:
        source = '{% macro m(a, b, label) %}{{ a }}:{{ b }}:{{ label }}{% endmacro %}{{ m(*items, **kwargs) }}'
        values = {'items': [1, 2], 'kwargs': {'label': 3}}
        self.assertEqual(render(source, values, True), render(source, values, False))

    def test_failed_expansion_does_not_evaluate_keywords(self) -> None:
        def observe(native: bool, fail_next: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []

            class Items:
                def __iter__(self) -> Iterator[int]:
                    events.append('iterate')
                    if not fail_next:
                        raise ValueError('bad iteration')
                    yield 1
                    events.append('next')
                    raise ValueError('bad iteration')

            def keywords() -> dict[str, int]:
                events.append('keywords')
                return {'label': 3}

            try:
                render('{{ callback(*items, **keywords()) }}',
                       {'items': Items(), 'keywords': keywords, 'callback': lambda: ''}, native)
            except ValueError as error:
                return type(error).__name__, str(error), events
            raise AssertionError('expected iteration failure')

        for fail_next in (False, True):
            with self.subTest(fail_next=fail_next):
                self.assertEqual(observe(True, fail_next), observe(False, fail_next))
