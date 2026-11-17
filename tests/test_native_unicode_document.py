"""Differential document checks for Python strings with surrogate code points."""

import base64
import io
import json
import unittest
import zipfile
from importlib import import_module
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
from docx import Document
from docxtpl_native.bridge import BridgeConfig
from paritylab.compare import compare_docx
from paritylab.protocol_base import template

etree: Any = import_module('lxml.etree')


class NativeUnicodeDocumentTests(unittest.TestCase):
    def test_noncharacter_on_live_model_is_not_counted_as_structural_parity(self) -> None:
        source = template('{{ value }}')
        for asynchronous in (False, True):
            for character in ('\ufffe', '\uffff'):
                with self.subTest(asynchronous=asynchronous, character=ascii(character)):
                    value = f'<x a="A{character}B">Z</x>'
                    outputs: list[bytes] = []
                    for module in (docxtpl, docxtpl_native):
                        document = module.DocxTemplate(io.BytesIO(source))
                        document.render({'value': value}, jinja_env=jinja2.Environment(enable_async=asynchronous))
                        output = io.BytesIO()
                        document.save(output)
                        outputs.append(output.getvalue())
                    comparison = compare_docx(*outputs)
                    self.assertFalse(comparison['equivalent'])
                    self.assertTrue(comparison['reference_errors'])
                    self.assertTrue(comparison['candidate_errors'])
                    with zipfile.ZipFile(io.BytesIO(outputs[0])) as expected, zipfile.ZipFile(io.BytesIO(outputs[1])) as actual:
                        self.assertEqual(expected.namelist(), actual.namelist())
                        for name in expected.namelist():
                            self.assertEqual(expected.read(name), actual.read(name), name)

    def test_noncharacters_in_rendered_attributes_preserve_file_output(self) -> None:
        source = template('{{ value }}{{ after() }}')
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        for asynchronous in (False, True):
            for character in ('\ufffe', '\uffff'):
                for prefix, suffix in (('', ''), ('&amp;', '&lt;'), ('A', 'B')):
                    value = f'<x a="{prefix}{character}{suffix}">Z</x>'
                    with self.subTest(asynchronous=asynchronous, value=ascii(value)):
                        events: list[str] = []

                        def after(record: list[str] = events) -> str:
                            record.append('after')
                            return 'tail'

                        environment = jinja2.Environment(enable_async=asynchronous)
                        document = docxtpl.DocxTemplate(io.BytesIO(source))
                        document.render({'value': value, 'after': after}, jinja_env=environment)
                        reference = io.BytesIO()
                        document.save(reference)
                        self.assertEqual(events, ['after'])
                        events.clear()
                        candidate, _ = docxtpl_native._extension().process_python_docx(
                            source, {'value': value, 'after': after},
                            BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), payload, False,
                        )
                        self.assertEqual(events, ['after'])
                        comparison = compare_docx(reference.getvalue(), bytes(candidate))
                        self.assertFalse(comparison['equivalent'])
                        self.assertTrue(comparison['reference_errors'])
                        self.assertTrue(comparison['candidate_errors'])
                        with zipfile.ZipFile(reference) as expected, zipfile.ZipFile(io.BytesIO(candidate)) as actual:
                            self.assertEqual(expected.namelist(), actual.namelist())
                            for name in expected.namelist():
                                self.assertEqual(expected.read(name), actual.read(name), name)

    def test_noncharacters_in_multiple_attribute_shapes_preserve_file_output(self) -> None:
        source = template('{{ value }}{{ after() }}')
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        shapes = (
            '<x a="one" b="B{char}C" c="three">Z</x>',
            '<x a="A"><y b="B{char}C"/>Z</x>',
            '<x xmlns:p="urn:test" p:a="B{char}C">Z</x>',
            "<x a='B{char}C'>Z</x>",
        )
        for asynchronous in (False, True):
            for character in ('\ufffe', '\uffff'):
                for shape in shapes:
                    value = shape.format(char=character)
                    with self.subTest(asynchronous=asynchronous, value=ascii(value)):
                        events: list[str] = []

                        def after(record: list[str] = events) -> str:
                            record.append('after')
                            return 'tail'

                        reference_document = docxtpl.DocxTemplate(io.BytesIO(source))
                        reference_document.render(
                            {'value': value, 'after': after},
                            jinja_env=jinja2.Environment(enable_async=asynchronous),
                        )
                        reference = io.BytesIO()
                        reference_document.save(reference)
                        self.assertEqual(events, ['after'])
                        events.clear()
                        candidate, _ = docxtpl_native._extension().process_python_docx(
                            source, {'value': value, 'after': after},
                            BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), payload, False,
                        )
                        self.assertEqual(events, ['after'])
                        comparison = compare_docx(reference.getvalue(), bytes(candidate))
                        self.assertFalse(comparison['equivalent'])
                        self.assertTrue(comparison['reference_errors'])
                        self.assertTrue(comparison['candidate_errors'])
                        with zipfile.ZipFile(reference) as expected, zipfile.ZipFile(io.BytesIO(candidate)) as actual:
                            self.assertEqual(expected.namelist(), actual.namelist())
                            for name in expected.namelist():
                                self.assertEqual(expected.read(name), actual.read(name), name)

    def test_noncharacter_file_output_with_table_and_valid_control(self) -> None:
        original = Document()
        original.add_paragraph('before')
        original.add_table(rows=1, cols=2).cell(0, 0).text = '{{ value }}'
        original.add_paragraph('after')
        image = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYPj/HwADAgH/5ncLrgAAAABJRU5ErkJggg=='
        )
        original.add_picture(io.BytesIO(image))
        source = io.BytesIO()
        original.save(source)
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        for asynchronous in (False, True):
            for value in ('<x a="A\ufffeB">Z</x>', '<x a="valid">Z</x>',
                          'A\x01B', 'A&amp;\x00B', '<x a="A\x00B">Z</x>',
                          '<x a="A\x01B">Z</x>'):
                with self.subTest(asynchronous=asynchronous, value=ascii(value)):
                    reference = docxtpl.DocxTemplate(io.BytesIO(source.getvalue()))
                    reference.render({'value': value}, jinja_env=jinja2.Environment(enable_async=asynchronous))
                    output = io.BytesIO()
                    reference.save(output)
                    candidate, _ = docxtpl_native._extension().process_python_docx(
                        source.getvalue(), {'value': value},
                        BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), payload, False,
                    )
                    comparison = compare_docx(output.getvalue(), bytes(candidate))
                    if '\ufffe' in value:
                        self.assertFalse(comparison['equivalent'])
                        self.assertTrue(comparison['reference_errors'])
                        with zipfile.ZipFile(output) as expected, zipfile.ZipFile(io.BytesIO(candidate)) as actual:
                            self.assertEqual(expected.namelist(), actual.namelist())
                            for name in expected.namelist():
                                self.assertEqual(expected.read(name), actual.read(name), name)
                    else:
                        self.assertTrue(comparison['equivalent'], comparison)

    def test_control_recovery_ignores_capi_mutation_by_callback(self) -> None:
        source = template('{{ value }}{{ alter() }}')
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        for asynchronous, value in ((mode, text) for mode in (False, True)
                                    for text in ('A\x01B', '<x a="A\ufffeB">Z</x>')):
            with self.subTest(asynchronous=asynchronous, value=ascii(value)):
                events: list[str] = []

                def alter(record: list[str] = events) -> str:
                    record.append('alter')
                    etree.__pyx_capi__['rootNodeOrRaise'] = None
                    return 'tail'

                with patch.dict(etree.__pyx_capi__):
                    reference = docxtpl.DocxTemplate(io.BytesIO(source))
                    reference.render({'value': value, 'alter': alter},
                                     jinja_env=jinja2.Environment(enable_async=asynchronous))
                    output = io.BytesIO()
                    reference.save(output)
                self.assertEqual(events, ['alter'])
                events.clear()
                with patch.dict(etree.__pyx_capi__):
                    candidate, _ = docxtpl_native._extension().process_python_docx(
                        source, {'value': value, 'alter': alter},
                        BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), payload, False,
                    )
                self.assertEqual(events, ['alter'])
                comparison = compare_docx(output.getvalue(), bytes(candidate))
                if '\ufffe' in value:
                    self.assertFalse(comparison['equivalent'])
                    self.assertTrue(comparison['reference_errors'])
                    with zipfile.ZipFile(output) as expected, zipfile.ZipFile(io.BytesIO(candidate)) as actual:
                        self.assertEqual(expected.namelist(), actual.namelist())
                        for name in expected.namelist():
                            self.assertEqual(expected.read(name), actual.read(name), name)
                else:
                    self.assertTrue(comparison['equivalent'], comparison)

    def test_noncharacter_recovery_survives_capi_change_before_render(self) -> None:
        source = template('{{ value }}')
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        extension = docxtpl_native._extension()
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), patch.dict(etree.__pyx_capi__):
                etree.__pyx_capi__['rootNodeOrRaise'] = None
                value = '<x a="A\ufffeB">Z</x>'
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                reference.render({'value': value}, jinja_env=jinja2.Environment(enable_async=asynchronous))
                output = io.BytesIO()
                reference.save(output)
                candidate, _ = extension.process_python_docx(
                    source, {'value': value}, BridgeConfig(jinja2.Environment(enable_async=asynchronous), False),
                    payload, False,
                )
                comparison = compare_docx(output.getvalue(), bytes(candidate))
                self.assertFalse(comparison['equivalent'])
                self.assertTrue(comparison['reference_errors'])
                with zipfile.ZipFile(output) as expected, zipfile.ZipFile(io.BytesIO(candidate)) as actual:
                    self.assertEqual(expected.namelist(), actual.namelist())
                    for name in expected.namelist():
                        self.assertEqual(expected.read(name), actual.read(name), name)

    def test_file_renderer_preserves_surrogate_output_and_callbacks(self) -> None:
        source = template('prefix{{ value }}{{ after() }}')
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        for asynchronous in (False, True):
            for value in ('A\ud800B', 'A\udfffB', 'A\ud800\udc00B', '😀\ud800B', '\ud800\x01DTPLD0\x01'):
                with self.subTest(asynchronous=asynchronous, value=ascii(value)):
                    events: list[str] = []

                    def after(record: list[str] = events) -> str:
                        record.append('after')
                        return 'suffix'

                    document = docxtpl.DocxTemplate(io.BytesIO(source))
                    document.render({'value': value, 'after': after},
                                    jinja_env=jinja2.Environment(enable_async=asynchronous))
                    expected = io.BytesIO()
                    document.save(expected)
                    self.assertEqual(events, ['after'])
                    events.clear()
                    actual, _ = docxtpl_native._extension().process_python_docx(
                        source, {'value': value, 'after': after},
                        BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), payload, False,
                    )
                    self.assertEqual(events, ['after'])
                    self.assertTrue(compare_docx(expected.getvalue(), bytes(actual))['equivalent'])

    def test_surrogate_output_keeps_render_callbacks_and_document(self) -> None:
        source = template('prefix{{ value }}{{ after() }}')
        values = ('A\ud800B', 'A\udfffB', 'A\ud800\udc00B', '😀\ud800B', '\ud800\x01DTPLD0\x01')
        for asynchronous in (False, True):
            for value in values:
                with self.subTest(asynchronous=asynchronous, value=ascii(value)):
                    def run(native: bool, current_value: str = value,
                            enable_async: bool = asynchronous) -> tuple[bytes | None, list[str], Exception | None]:
                        events: list[str] = []

                        def after() -> str:
                            events.append('after')
                            return 'suffix'

                        module: Any = docxtpl_native if native else docxtpl
                        document = module.DocxTemplate(io.BytesIO(source))
                        try:
                            document.render({'value': current_value, 'after': after},
                                            jinja_env=jinja2.Environment(enable_async=enable_async))
                            output = io.BytesIO()
                            document.save(output)
                            return output.getvalue(), events, None
                        except Exception as error:  # noqa: BLE001 - preserve the failed render and callback trace
                            return None, events, error

                    expected, reference_events, reference_error = run(False)
                    actual, candidate_events, candidate_error = run(True)
                    self.assertEqual(reference_events, ['after'])
                    self.assertEqual(candidate_events, reference_events)
                    self.assertIsNone(reference_error)
                    self.assertIsNone(candidate_error)
                    assert expected is not None and actual is not None
                    self.assertTrue(compare_docx(expected, actual)['equivalent'])
                    self.assertEqual(Document(io.BytesIO(actual)).paragraphs[0].text,
                                     Document(io.BytesIO(expected)).paragraphs[0].text)

    def test_header_surrogate_preserves_error_and_callback(self) -> None:
        source = template('body', header='{{ value }}{{ after() }}')
        for asynchronous in (False, True):
            for value in ('\ud800', '\udfff', '\ud800\udc00'):
                with self.subTest(asynchronous=asynchronous, value=ascii(value)):
                    def run(native: bool, current_value: str = value,
                            enable_async: bool = asynchronous) -> tuple[str, str, str, list[str]]:
                        events: list[str] = []

                        def after() -> str:
                            events.append('after')
                            return 'suffix'

                        module: Any = docxtpl_native if native else docxtpl
                        document = module.DocxTemplate(io.BytesIO(source))
                        try:
                            document.render({'value': current_value, 'after': after},
                                            jinja_env=jinja2.Environment(enable_async=enable_async))
                        except UnicodeEncodeError as error:
                            return type(error).__name__, error.encoding, error.reason, events
                        raise AssertionError('expected header encoding error')

                    self.assertEqual(run(True), run(False))

    def test_header_surrogate_encoding_error_preserves_object_and_positions(self) -> None:
        source = template('body', header='before {{ value }}{{ after() }} end')
        for asynchronous in (False, True):
            for value in ('\ud800', '\udfff', '\ud800\udc00', '😀\ud800x',
                          '\ud800x\udfff', '\udfff\ud800', 'é\udfff', '\ud800😀\udfff'):
                with self.subTest(asynchronous=asynchronous, value=ascii(value)):
                    def run(native: bool, current_value: str = value,
                            enable_async: bool = asynchronous) -> tuple[Any, ...]:
                        events: list[str] = []

                        def after() -> str:
                            events.append('after')
                            return 'suffix'

                        module: Any = docxtpl_native if native else docxtpl
                        document = module.DocxTemplate(io.BytesIO(source))
                        try:
                            document.render({'value': current_value, 'after': after},
                                            jinja_env=jinja2.Environment(enable_async=enable_async))
                        except UnicodeEncodeError as error:
                            return (type(error).__name__, error.encoding, error.object,
                                    error.start, error.end, error.reason, events)
                        raise AssertionError('expected header encoding error')

                    expected = run(False)
                    self.assertEqual(expected[-1], ['after'])
                    self.assertEqual(run(True), expected)


if __name__ == '__main__':
    unittest.main()
