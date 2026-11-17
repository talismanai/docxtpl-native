"""Sequence lengths and error timing for ZIP timestamps returned by a clock."""

import gc
import io
import unittest
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
from docx import Document


class ZipClockSequenceTests(unittest.TestCase):
    def test_timestamp_sequence_lengths(self) -> None:
        document = Document()
        document.add_paragraph('Hello {{ name }}')
        source = io.BytesIO()
        document.save(source)
        template_bytes = source.getvalue()

        def run(engine: Any, values: list[int], kind: str) -> tuple[Any, list[object], bytes, int]:
            template = engine.DocxTemplate(io.BytesIO(template_bytes))
            template.render({'name': 'Ada'})
            output = io.BytesIO()
            events: list[object] = []

            class ClockResult:
                def __getitem__(self, key: slice) -> tuple[int, ...] | list[int]:
                    events.append(('slice', key.start, key.stop, key.step))
                    return tuple(values) if kind == 'tuple' else list(values)

            def clock(instant: float) -> ClockResult:
                events.append(('localtime', instant))
                return ClockResult()

            try:
                with patch('zipfile.time.time', return_value=1704067200), patch('zipfile.time.localtime', clock):
                    template.save(output)
                result: Any = ('ok',)
            except Exception as error:  # noqa: BLE001 - compare exact failure types and messages
                result = ('error', type(error).__name__, str(error))
                error.__traceback__ = None
            gc.collect()
            return result, events, output.getvalue(), output.tell()

        date = [2024, 1, 2, 3, 4, 6]
        cases = [([], 'IndexError'), ([1979], 'ValueError')]
        cases.extend((date[:length], 'IndexError') for length in range(1, 6))
        cases.extend([(date, None), (date + [999], None)])
        for values, error_type in cases:
            for kind in ('list', 'tuple'):
                with self.subTest(values=values, kind=kind):
                    expected = run(docxtpl, values, kind)
                    self.assertEqual(expected[0][0], 'error' if error_type else 'ok')
                    if error_type:
                        self.assertEqual(expected[0][1], error_type)
                    self.assertEqual(run(docxtpl_native, values, kind), expected)
