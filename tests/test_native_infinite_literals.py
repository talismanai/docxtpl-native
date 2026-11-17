"""Differential behavior of non-finite literals in generated Jinja code."""

import builtins
import io
import unittest
from typing import Any

import docxtpl
import docxtpl_native
from jinja2 import Environment
from paritylab.compare import compare_docx
from paritylab.protocol_base import template
from test_native_filter_evaluation import render


class InfiniteLiteralTests(unittest.TestCase):
    def test_complete_document_and_runtime_failure(self) -> None:
        def run(engine: Any, source: bytes, context: dict[str, object]) -> tuple[bytes | None, tuple[str, str, Any] | None]:
            document = engine.DocxTemplate(io.BytesIO(source))
            try:
                document.render(context)
            except NameError as error:
                return None, (type(error).__name__, str(error), error.name)
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), None

        source = template('{{ 1e400 }}')
        reference, reference_error = run(docxtpl, source, {})
        candidate, candidate_error = run(docxtpl_native, source, {})
        self.assertIsNone(reference_error)
        self.assertIsNone(candidate_error)
        assert reference is not None and candidate is not None
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])
        source = template('{{ capture(1e400) }}')
        self.assertEqual(run(docxtpl_native, source, {'capture': str}),
                         run(docxtpl, source, {'capture': str}))

    def test_runtime_name_lookup_and_timing(self) -> None:
        def run(native: bool, source: str, asynchronous: bool) -> tuple[Any, ...]:
            events: list[object] = []

            def capture(value: object) -> str:
                events.append(('capture', value))
                return str(value)

            try:
                value = render(source, {'capture': capture}, native, Environment(enable_async=asynchronous))
                return 'ok', value, events
            except NameError as error:
                return 'error', type(error).__name__, str(error), getattr(error, 'name', None), events

        sources = ('{{ 1e400 }}', '{{ -1e400 }}', '{{ capture(1e400) }}',
                   '{{ capture(-1e400) }}', '{{ capture(1e400|abs) }}',
                   '{% set value=1e400 %}{{ value }}',
                   '{% if false %}{{ capture(1e400) }}{% endif %}ok')
        for asynchronous in (False, True):
            for source in sources:
                with self.subTest(source=source, asynchronous=asynchronous):
                    self.assertEqual(run(True, source, asynchronous), run(False, source, asynchronous))

    def test_builtin_inf_is_resolved_at_runtime(self) -> None:
        original = getattr(builtins, 'inf', None)
        existed = hasattr(builtins, 'inf')
        try:
            vars(builtins)['inf'] = 42
            for source in ('{{ capture(1e400) }}', '{{ capture(-1e400) }}',
                           '{{ capture(1e400|abs) }}'):
                with self.subTest(source=source):
                    self.assertEqual(render(source, {'capture': str}, True),
                                     render(source, {'capture': str}, False))
        finally:
            if existed:
                vars(builtins)['inf'] = original
            else:
                del vars(builtins)['inf']

    def test_python_values_are_not_literal_lookups(self) -> None:
        for value in (float('inf'), float('-inf'), float('nan')):
            for asynchronous in (False, True):
                with self.subTest(value=repr(value), asynchronous=asynchronous):
                    context = {'capture': str, 'value': value}
                    self.assertEqual(render('{{ capture(value) }}', context, True, Environment(enable_async=asynchronous)),
                                     render('{{ capture(value) }}', context, False, Environment(enable_async=asynchronous)))

    def test_nonfinite_tuple_key_is_created_per_evaluation(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            keys: list[Any] = []

            class Value:
                def __getitem__(self, key: object) -> str:
                    keys.append(key)
                    return 'x'

            source = '{% for item in items %}{{ value[1e400, 2] }}{% endfor %}'
            output = render(source, {'value': Value(), 'items': [1, 2]}, native)
            return output, [keys[0] is keys[1], keys[0][0] is marker]

        marker: list[object] = []
        original = getattr(builtins, 'inf', None)
        existed = hasattr(builtins, 'inf')
        try:
            vars(builtins)['inf'] = marker
            self.assertEqual(run(False), ('xx', [False, True]))
            self.assertEqual(run(True), run(False))
        finally:
            if existed:
                vars(builtins)['inf'] = original
            else:
                del vars(builtins)['inf']

    def test_folded_nan_uses_runtime_builtin_lookup(self) -> None:
        def run(native: bool) -> tuple[object, ...]:
            environment = Environment()
            environment.filters['nonfinite'] = lambda value: [float('nan'), float('-inf')]
            try:
                result = render('{{ capture(0|nonfinite) }}', {'capture': str}, native, environment)
                return 'ok', result
            except NameError as error:
                return 'error', str(error), error.name

        original = {name: vars(builtins)[name] for name in ('nan', 'inf') if name in vars(builtins)}
        try:
            for name in ('nan', 'inf'):
                vars(builtins).pop(name, None)
            self.assertEqual(run(True), run(False))
            vars(builtins).update({'nan': 12, 'inf': 7})
            self.assertEqual(run(True), run(False))
        finally:
            for name in ('nan', 'inf'):
                vars(builtins).pop(name, None)
            vars(builtins).update(original)
