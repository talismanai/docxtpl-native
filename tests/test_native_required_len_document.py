"""Full package check for the required-block length callback."""

import builtins
import unittest
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


class RequiredLengthDocumentTests(unittest.TestCase):
    def test_custom_len_controls_required_block_in_docx(self) -> None:
        source = 'before{{ install() }}{% block body required %}{% endblock %}after'
        document = template(source)

        def observe(engine: Any, asynchronous: bool) -> tuple[bytes, list[str]]:
            selected: list[Any] = []
            events: list[str] = []
            original_len = builtins.len

            def custom_len(value: Any) -> int:
                if value is selected:
                    events.append('len')
                    return 2
                return original_len(value)

            def replacement(context: Any) -> Any:
                yield 'CUSTOM'

            async def async_replacement(context: Any) -> Any:
                yield 'CUSTOM'

            @jinja2.pass_context
            def install(context: Any) -> str:
                selected.append(async_replacement if asynchronous else replacement)
                context.blocks['body'] = selected
                builtins.len = custom_len  # ty: ignore[invalid-assignment]
                return ''

            with patch.object(builtins, 'len', original_len):
                output = generate(engine, source, {'install': install},
                                  jinja2.Environment(enable_async=asynchronous), source=document)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, reference_events = observe(docxtpl, asynchronous)
                native, native_events = observe(docxtpl_native, asynchronous)
                self.assertEqual(reference_events, ['len'])
                self.assertEqual(native_events, reference_events)
                self.assertTrue(compare_docx(reference, native)['equivalent'])
