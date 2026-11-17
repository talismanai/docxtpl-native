"""Differential checks for immutable constants at Python callback boundaries."""

import unittest
from typing import Any

import docxtpl
import docxtpl_native
from jinja2 import DictLoader, Environment
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template
from test_native_filter_evaluation import render


class LiteralIdentityTests(unittest.TestCase):
    def test_same_named_loader_keeps_literal_constant_identity(self) -> None:
        def run(native: bool, asynchronous: bool, mode: str) -> tuple[str, list[list[bool]]]:
            from docxtpl_native.bridge import BridgeConfig

            seen: list[object] = []

            def capture(value: object) -> str:
                seen.append(value)
                return 'x'

            name = 'word/document.xml'
            environment = Environment(
                loader=DictLoader({name: '{{ capture(1.5) }}'}),
                auto_reload=False, enable_async=asynchronous,
            )
            source = "{{ capture(1.5) }}{% include 'word/document.xml' %}{{ capture(1.5) }}"
            if mode == 'block':
                source = '{% block body %}' + source + '{% endblock %}'
            elif mode == 'macro':
                source = ("{% macro item() %}{{ capture(1.5) }}{% endmacro %}"
                          "{{ item() }}{% include 'word/document.xml' %}{{ item() }}")
            context = {'capture': capture}
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)
                output = session.render(name, source) + session.render(name, source)
            else:
                output = environment.from_string(source).render(context) + environment.from_string(source).render(context)
            return output, [[left is right for right in seen] for left in seen]

        for asynchronous in (False, True):
            for mode in ('root', 'block', 'macro'):
                with self.subTest(asynchronous=asynchronous, mode=mode):
                    expected = run(False, asynchronous, mode)
                    self.assertEqual(expected[0], 'xxxxxx')
                    self.assertEqual([expected[1][left][right] for left, right in
                                      ((0, 2), (0, 1), (1, 4), (0, 3), (3, 5))],
                                     [True, False, True, False, True])
                    self.assertEqual(run(True, asynchronous, mode), expected)

    def test_complete_document_with_cross_site_identity(self) -> None:
        source = '{{ capture(1.0) }}{{ capture(1e400**0) }}'
        document = template(source)

        def run(engine: Any) -> bytes:
            seen: list[object] = []

            def capture(value: object) -> str:
                seen.append(value)
                return str(value is seen[0])

            return generate(engine, source, {'capture': capture}, source=document)

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_intercepted_sign_does_not_intern_callback_results(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[bool], list[bool]]:
            inputs: list[Any] = []
            outputs: list[Any] = []

            class Sandbox(SandboxedEnvironment):
                def call_unop(self, context: Any, operator: str, arg: Any) -> Any:
                    inputs.append(arg)
                    return super().call_unop(context, operator, arg)

            def capture(value: Any) -> str:
                outputs.append(value)
                return 'x'

            environment = Sandbox(enable_async=asynchronous)
            environment.intercepted_unops = frozenset({'-'})
            text = render('{% for item in items %}{{ capture(-1.5) }}{% endfor %}',
                          {'capture': capture, 'items': range(3)}, native, environment)
            return text, [inputs[0] is value for value in inputs], [outputs[0] is value for value in outputs]

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(run(True, asynchronous), run(False, asynchronous))

    def test_literal_identity_across_sites_loops_and_folded_values(self) -> None:
        def run(native: bool, source: str, asynchronous: bool, optimized: bool) -> tuple[str, list[tuple[str, Any]], list[list[bool]]]:
            seen: list[Any] = []

            def capture(value: Any) -> str:
                seen.append(value)
                return 'x'

            text = render(source, {'capture': capture, 'items': range(3)}, native,
                          Environment(enable_async=asynchronous, optimized=optimized))
            return text, [(type(value).__name__, repr(value)) for value in seen], [
                [left is right for right in seen] for left in seen
            ]

        sources = (
            '{{ capture(1.0) }}{{ capture(1e400**0) }}{{ capture(1.0) }}',
            '{% for item in items %}{{ capture(1000000) }}{{ capture(1000000.0) }}{% endfor %}',
            '{{ capture(0.0) }}{{ capture(-0.0) }}{{ capture(0.0) }}',
            '{{ capture(-0.0) }}{{ capture(-0.0) }}{{ capture(+1.0) }}{{ capture(1.0) }}',
            '{% for item in items %}{{ capture(-1000) }}{{ capture(-1.5) }}{% endfor %}',
            '{{ capture(true) }}{{ capture(1) }}{{ capture(1.0) }}',
            '{% macro a() %}{{ capture(1.0) }}{% endmacro %}{{ capture(1e400**0) }}{{ a() }}',
            '{{ capture("a literal containing spaces") }}{{ capture("a literal containing spaces") }}',
        )
        for source in sources:
            for asynchronous in (False, True):
                for optimized in ((True,) if '1e400' in source else (False, True)):
                    with self.subTest(source=source, asynchronous=asynchronous, optimized=optimized):
                        self.assertEqual(run(True, source, asynchronous, optimized),
                                         run(False, source, asynchronous, optimized))
