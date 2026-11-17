"""A ZIP clock callback may change a caller-owned stream while saving."""

import gc
import hashlib
import io
import unittest
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
from docx import Document
from paritylab.compare import compare_docx


class MutatingClockTests(unittest.TestCase):
    def test_stream_mutations_match_reference(self) -> None:
        document = Document()
        document.add_paragraph('Hello {{ name }}')
        source = io.BytesIO()
        document.save(source)
        template_bytes = source.getvalue()

        def run(engine: Any, action: str, when: int) -> tuple[Any, list[object], bytes, int]:
            template = engine.DocxTemplate(io.BytesIO(template_bytes))
            template.render({'name': 'Ada'})
            output = io.BytesIO()
            trace: list[object] = []
            failure = RuntimeError('clock callback failed')

            def clock() -> int:
                data = output.getvalue()
                trace.append((output.tell(), len(data), hashlib.sha256(data).hexdigest()))
                if action == 'seek':
                    output.seek(1)
                elif action == 'overwrite' and len(trace) == when:
                    output.seek(0)
                    output.write(b'!')
                elif action == 'append' and len(trace) == when:
                    output.seek(0, 2)
                    output.write(b'padding')
                elif action == 'truncate' and len(trace) == when:
                    output.truncate(3)
                elif action == 'raise' and len(trace) == when:
                    output.seek(1)
                    raise failure
                return 1704067200 + len(trace) * 2

            try:
                with patch('zipfile.time.time', clock):
                    template.save(output)
                result: Any = ('ok',)
            except Exception as error:  # noqa: BLE001 - compare unexpected reference failures too
                result = ('error', type(error).__name__, str(error), error is failure)
            failure.__traceback__ = None
            gc.collect()
            return result, trace, output.getvalue(), output.tell()

        for action in ('seek', 'overwrite', 'append', 'truncate', 'raise'):
            for when in (1, 3, 10, 17):
                with self.subTest(action=action, when=when):
                    expected = run(docxtpl, action, when)
                    actual = run(docxtpl_native, action, when)
                    if action == 'raise':
                        self.assertEqual(expected[0], ('error', 'RuntimeError', 'clock callback failed', True))
                        self.assertEqual(len(expected[1]), when)
                    else:
                        self.assertEqual(expected[0], ('ok',))
                        self.assertEqual(len(expected[1]), 17)
                    self.assertEqual(actual, expected)
                    if action == 'seek':
                        self.assertTrue(compare_docx(expected[2], actual[2])['equivalent'])
