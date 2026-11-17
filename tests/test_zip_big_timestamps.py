"""Arbitrary-size integer timestamps and ZIP packing failures."""

import gc
import io
import unittest
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
from docx import Document


class ZipBigTimestampTests(unittest.TestCase):
    def test_large_integer_fields_and_failure_timing(self) -> None:
        document = Document()
        document.add_paragraph('Hello {{ name }}')
        source = io.BytesIO()
        document.save(source)
        template_bytes = source.getvalue()

        def run(engine: Any, date: tuple[int, ...]) -> tuple[Any, bytes, int, int]:
            template = engine.DocxTemplate(io.BytesIO(template_bytes))
            template.render({'name': 'Ada'})
            output = io.BytesIO()
            calls: list[int] = []

            def clock(instant: int) -> tuple[int, ...]:
                calls.append(instant)
                return date

            try:
                with patch('zipfile.time.time', return_value=1704067200), patch('zipfile.time.localtime', clock):
                    template.save(output)
                result: Any = ('ok',)
            except Exception as error:  # noqa: BLE001 - compare actual ZIP failures
                result = ('error', type(error).__module__, type(error).__name__, str(error))
                error.__traceback__ = None
            gc.collect()
            return result, output.getvalue(), output.tell(), len(calls)

        big = 1 << 100
        cases = [
            ((-big, 1, 1, 0, 0, 0), 'ValueError'),
            ((big, 1, 1, 0, 0, 0), 'error'),
            ((2024, big, 1, 0, 0, 0), 'error'),
            ((2024, -big, 1, 0, 0, 0), 'error'),
            ((2024, 1, big, 0, 0, 0), 'error'),
            ((2024, 1, 1, big, 0, 0), 'error'),
            ((2024, 1, 1, 0, big, 0), 'error'),
            ((2024, 1, 1, 0, 0, -big), 'error'),
            ((2024, big, -1, 0, 0, 0), 'error'),
            ((1 << 60, 1, 1, 0, 0, 0), 'error'),
            ((2024, 1, 1, 0, 0, 1 << 64), 'error'),
            ((big, 1, 1, 0, 0, -1), 'error'),
            ((-big,), 'ValueError'),
            ((2024, big), 'IndexError'),
        ]
        for date, expected_error in cases:
            with self.subTest(date=date):
                reference = run(docxtpl, date)
                self.assertEqual(reference[0][0], 'error')
                self.assertEqual(reference[0][2], expected_error)
                self.assertEqual(run(docxtpl_native, date), reference)
