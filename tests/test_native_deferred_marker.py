"""Deferred image markers must not turn user text into package content."""

import asyncio
import base64
import builtins
import importlib
import io
import json
import random
import re
import sys
import types
import unittest
import zipfile
from typing import Any, cast
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu
from docxcompose import composer
from docxtpl_native.bridge import BridgeConfig
from markupsafe import Markup
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template
from PIL import Image


class DeferredMarkerTests(unittest.TestCase):
    @staticmethod
    def native_render(source: bytes, context: dict[str, Any], *, include_image: bool = False,
                      environment: jinja2.Environment | None = None) -> bytes:
        image_bytes = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_bytes, format='PNG')
        image_bytes.seek(0)
        image = docxtpl_native.InlineImage(None, image_bytes)
        context = dict(context)
        if include_image:
            context['image'] = image
        payload = json.dumps({
            'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        result, _ = docxtpl_native._extension().process_python_docx(
            source, context, BridgeConfig(environment or jinja2.Environment(), False), payload, False,
        )
        return bytes(result)

    def test_user_text_matching_marker_cannot_insert_image(self) -> None:
        source = template('{{ value }}')
        context = {'value': '\x01DTPLD0\x01'}
        reference = Document(io.BytesIO(generate(docxtpl, '', context, jinja2.Environment(), source)))
        self.assertEqual(reference.paragraphs[0].text, 'DTPLD0')
        self.assertEqual(len(reference.inline_shapes), 0)
        candidate = Document(io.BytesIO(self.native_render(source, context)))
        self.assertEqual(candidate.paragraphs[0].text, reference.paragraphs[0].text)
        self.assertEqual(len(candidate.inline_shapes), 0)

    def test_forged_text_and_real_image_remain_distinct_in_python_bridge(self) -> None:
        marker = '\x01DTPLD0\x01'
        expressions = (
            '{{ fake }}{{ image }}',
            '{{ image }}{{ fake }}',
            '{% set captured %}{{ fake }}{{ image }}{% endset %}{{ captured }}',
        )
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            for expression in expressions:
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    source = template(expression)
                    reference = docxtpl.DocxTemplate(io.BytesIO(source))
                    environment = jinja2.Environment(enable_async=asynchronous)
                    reference.render({'fake': marker, 'image': docxtpl.InlineImage(
                        reference, io.BytesIO(png.getvalue()))}, jinja_env=environment)
                    expected = io.BytesIO()
                    reference.save(expected)
                    reference_doc = Document(io.BytesIO(expected.getvalue()))
                    self.assertEqual(reference_doc.paragraphs[0].text, 'DTPLD0')
                    self.assertEqual(len(reference_doc.inline_shapes), 1)
                    actual = self.native_render(source, {'fake': marker}, include_image=True,
                                                environment=jinja2.Environment(enable_async=asynchronous))
                    candidate = Document(io.BytesIO(actual))
                    self.assertEqual(candidate.paragraphs[0].text, reference_doc.paragraphs[0].text)
                    self.assertEqual(len(candidate.inline_shapes), 1)
                    self.assertTrue(compare_docx(expected.getvalue(), actual)['equivalent'])

    def test_async_fragment_root_treats_user_marker_as_text(self) -> None:
        marker = '\x01DTPLD0\x01'
        expressions = (
            '{{ fake }}',
            '{{ fake }}{{ image }}',
            '{{ image }}{{ fake }}',
            '{{ first }}{{ second }}{{ image }}',
            '{% set captured %}{{ fake }}{{ image }}{% endset %}{{ captured }}',
        )
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for expression in expressions:
            with self.subTest(expression=expression):
                source = template(expression)
                context: dict[str, Any] = {'fake': marker, 'first': '\x01DTPL', 'second': 'D0\x01'}
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                reference.render({**context, 'image': docxtpl.InlineImage(reference, io.BytesIO(png.getvalue()))},
                                 jinja_env=jinja2.Environment(enable_async=True))
                expected = io.BytesIO()
                reference.save(expected)
                actual = self.native_render(source, context, include_image=True,
                                            environment=jinja2.Environment(enable_async=True))
                self.assertTrue(compare_docx(expected.getvalue(), actual)['equivalent'])
                self.assertEqual(Document(io.BytesIO(actual)).paragraphs[0].text, 'DTPLD0')
                self.assertEqual(len(Document(io.BytesIO(actual)).inline_shapes), int('{{ image }}' in expression))

    def test_sync_fragment_root_treats_user_marker_as_text(self) -> None:
        marker = '\x01DTPLD0\x01'
        expressions = (
            '{{ fake }}',
            '{{ fake }}{{ image }}',
            '{{ image }}{{ fake }}',
            '{{ first }}{{ second }}{{ image }}',
            '{% set captured %}{{ fake }}{{ image }}{% endset %}{{ captured }}',
        )
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for expression in expressions:
            with self.subTest(expression=expression):
                source = template(expression)
                context: dict[str, Any] = {'fake': marker, 'first': '\x01DTPL', 'second': 'D0\x01'}
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                reference.render({**context, 'image': docxtpl.InlineImage(reference, io.BytesIO(png.getvalue()))},
                                 jinja_env=jinja2.Environment())
                expected = io.BytesIO()
                reference.save(expected)
                actual = self.native_render(source, context, include_image=True, environment=jinja2.Environment())
                self.assertTrue(compare_docx(expected.getvalue(), actual)['equivalent'])
                candidate = Document(io.BytesIO(actual))
                self.assertEqual(candidate.paragraphs[0].text, 'DTPLD0')
                self.assertEqual(len(candidate.inline_shapes), int('{{ image }}' in expression))

    def test_fragment_root_preserves_other_control_character_recovery(self) -> None:
        source = template('{{ value }}')
        for asynchronous in (False, True):
            for value in ('a\x00b', 'a\x02b', 'a\x08b', 'a\x1fb'):
                with self.subTest(asynchronous=asynchronous, value=repr(value)):
                    context = {'value': value}
                    reference = generate(docxtpl, '', context, jinja2.Environment(enable_async=asynchronous), source)
                    candidate = self.native_render(source, context,
                                                   environment=jinja2.Environment(enable_async=asynchronous))
                    self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_async_fragment_root_keeps_constant_marker_guard(self) -> None:
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'deferred marker'):
            self.native_render(template("{{ '\\x01DTPLD0\\x01' }}"), {},
                               environment=jinja2.Environment(enable_async=True))

    def test_async_fragment_root_transport_like_text_cannot_forge_image(self) -> None:
        source = template('{{ fake }}{{ image }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for fake in ('\x010Z0\x02', '\x011Z0\x02', '\x010Z0\x02\x011Z0\x02'):
            with self.subTest(fake=repr(fake)):
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                reference.render({'fake': fake, 'image': docxtpl.InlineImage(reference, io.BytesIO(png.getvalue()))},
                                 jinja_env=jinja2.Environment(enable_async=True))
                expected = io.BytesIO()
                reference.save(expected)
                actual = self.native_render(source, {'fake': fake}, include_image=True,
                                            environment=jinja2.Environment(enable_async=True))
                self.assertTrue(compare_docx(expected.getvalue(), actual)['equivalent'])
                self.assertEqual(len(Document(io.BytesIO(actual)).inline_shapes), 1)

    def test_async_fragment_root_handles_callback_and_finalizer_text(self) -> None:
        marker = '\x01DTPLD0\x01'
        source = template('{{ callback() }}{{ value }}{{ image }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')

        def finalize(value: Any) -> Any:
            return marker if value == 'trigger' else value

        def callback() -> str:
            return marker

        context = {'callback': callback, 'value': 'trigger'}
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        reference.render({**context, 'image': docxtpl.InlineImage(reference, io.BytesIO(png.getvalue()))},
                         jinja_env=jinja2.Environment(enable_async=True, finalize=finalize))
        expected = io.BytesIO()
        reference.save(expected)
        actual = self.native_render(source, context, include_image=True,
                                    environment=jinja2.Environment(enable_async=True, finalize=finalize))
        self.assertTrue(compare_docx(expected.getvalue(), actual)['equivalent'])
        candidate = Document(io.BytesIO(actual))
        self.assertEqual(candidate.paragraphs[0].text, 'DTPLD0DTPLD0')
        self.assertEqual(len(candidate.inline_shapes), 1)

    def test_public_async_render_user_marker_and_image(self) -> None:
        source = template('{{ fake }}{{ image }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')

        def run(native: bool) -> bytes:
            module = docxtpl_native if native else docxtpl
            document = module.DocxTemplate(io.BytesIO(source))
            image = module.InlineImage(document, io.BytesIO(png.getvalue()))
            document.render({'fake': '\x01DTPLD0\x01', 'image': image},
                            jinja_env=jinja2.Environment(enable_async=True))
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        expected = run(False)
        actual = run(True)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])
        candidate = Document(io.BytesIO(actual))
        self.assertEqual(candidate.paragraphs[0].text, 'DTPLD0')
        self.assertEqual(len(candidate.inline_shapes), 1)

    def test_public_sync_render_user_marker_and_image(self) -> None:
        source = template('{{ fake }}{{ image }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')

        def run(native: bool) -> bytes:
            module = docxtpl_native if native else docxtpl
            document = module.DocxTemplate(io.BytesIO(source))
            image = module.InlineImage(document, io.BytesIO(png.getvalue()))
            document.render({'fake': '\x01DTPLD0\x01', 'image': image}, jinja_env=jinja2.Environment())
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        expected = run(False)
        actual = run(True)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])
        candidate = Document(io.BytesIO(actual))
        self.assertEqual(candidate.paragraphs[0].text, 'DTPLD0')
        self.assertEqual(len(candidate.inline_shapes), 1)

    def test_async_capture_callback_preserves_user_marker_and_real_image(self) -> None:
        source = template('{% set captured %}{{ fake }}{{ image }}{% endset %}{{ callback(captured) }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')

        def run(native: bool) -> tuple[bytes, list[tuple[bool, bool, bool]]]:
            events: list[tuple[bool, bool, bool]] = []

            def callback(value: str) -> str:
                events.append((type(value) is str, '\x01DTPLD0\x01' in value, '<w:drawing>' in value))
                return value

            module = docxtpl_native if native else docxtpl
            document = module.DocxTemplate(io.BytesIO(source))
            image = module.InlineImage(document, io.BytesIO(png.getvalue()))
            document.render({'fake': '\x01DTPLD0\x01', 'image': image, 'callback': callback},
                            jinja_env=jinja2.Environment(enable_async=True))
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        expected, reference_events = run(False)
        actual, candidate_events = run(True)
        self.assertEqual(reference_events, [(True, True, True)])
        self.assertEqual(candidate_events, reference_events)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])
        document = Document(io.BytesIO(actual))
        self.assertEqual(document.paragraphs[0].text, 'DTPLD0')
        self.assertEqual(len(document.inline_shapes), 1)

    def test_async_capture_does_not_reinterpret_raw_deferred_marker(self) -> None:
        source = template('{% set captured %}{{ fake }}{{ child }}{% endset %}{{ captured }}')
        child = object()
        payload = json.dumps({
            'specials': [{'identity': id(child), 'value': {
                'kind': 'subdoc', 'data': base64.b64encode(template('child')).decode('ascii'),
            }}],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'deferred marker'):
            docxtpl_native._extension().process_python_docx(
                source, {'fake': '\x01DTPLD0\x01', 'child': child},
                BridgeConfig(jinja2.Environment(enable_async=True), False), payload, False,
            )

    def test_sync_capture_does_not_reinterpret_raw_deferred_marker(self) -> None:
        source = template('{% set captured %}{{ fake }}{{ child }}{% endset %}{{ captured }}')
        child = object()
        payload = json.dumps({
            'specials': [{'identity': id(child), 'value': {
                'kind': 'subdoc', 'data': base64.b64encode(template('child')).decode('ascii'),
            }}],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'deferred marker'):
            docxtpl_native._extension().process_python_docx(
                source, {'fake': '\x01DTPLD0\x01', 'child': child},
                BridgeConfig(jinja2.Environment(), False), payload, False,
            )

    def test_callback_sees_image_xml_in_capture_not_internal_marker(self) -> None:
        source = template('{% set captured %}{{ image }}{% endset %}{{ inspect(captured) }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def callback(events: list[tuple[bool, bool]]) -> Any:
                    def inspect(value: str) -> str:
                        events.append(('<w:drawing>' in value, '\x01DTPLD' in value))
                        return 'ok'
                    return inspect

                reference_events: list[tuple[bool, bool]] = []
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                reference.render({'image': docxtpl.InlineImage(reference, io.BytesIO(png.getvalue())),
                                  'inspect': callback(reference_events)},
                                 jinja_env=jinja2.Environment(enable_async=asynchronous))
                expected = io.BytesIO()
                reference.save(expected)
                self.assertEqual(reference_events, [(True, False)])
                candidate_events: list[tuple[bool, bool]] = []
                actual = self.native_render(source, {'inspect': callback(candidate_events)},
                                            include_image=True,
                                            environment=jinja2.Environment(enable_async=asynchronous))
                self.assertEqual(candidate_events, reference_events)
                self.assertTrue(compare_docx(expected.getvalue(), actual)['equivalent'])

    def test_finalizer_image_is_materialized_before_capture_callback(self) -> None:
        source = template('{% set captured %}{{ value }}{% endset %}{{ inspect(captured) }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool, asynchronous: bool = asynchronous) -> tuple[bytes, list[tuple[bool, bool, bool]]]:
                    events: list[tuple[bool, bool, bool]] = []

                    def inspect(value: str) -> str:
                        events.append(('<w:drawing>' in value, '\x01DTPLD' in value, 'cx="30"' in value))
                        return 'ok'

                    if not native:
                        document = docxtpl.DocxTemplate(io.BytesIO(source))
                        image = docxtpl.InlineImage(document, io.BytesIO(png.getvalue()), width=Emu(10))
                        def finalize(value: Any) -> Any:
                            if value == 'input':
                                image.width = Emu(30)
                                return image
                            return value
                        environment = jinja2.Environment(enable_async=asynchronous, finalize=finalize)
                        document.render({'value': 'input', 'inspect': inspect}, jinja_env=environment)
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue(), events
                    image = docxtpl_native.InlineImage(None, io.BytesIO(png.getvalue()), width=Emu(10))
                    payload = json.dumps({
                        'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    def finalize(value: Any) -> Any:
                        if value == 'input':
                            image.width = Emu(30)
                            return image
                        return value
                    environment = jinja2.Environment(enable_async=asynchronous, finalize=finalize)
                    output, _ = docxtpl_native._extension().process_python_docx(
                        source, {'value': 'input', 'inspect': inspect}, BridgeConfig(environment, False),
                        payload, False,
                    )
                    return bytes(output), events

                expected, reference_events = run(False)
                self.assertEqual(reference_events, [(True, False, True)])
                actual, candidate_events = run(True)
                self.assertEqual(candidate_events, reference_events)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_file_callback_sees_captured_image_xml(self) -> None:
        source = template('{% set captured %}{{ image }}{% endset %}{{ inspect(captured) }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(engine: Any, asynchronous: bool = asynchronous) -> tuple[bytes, list[tuple[bool, bool]]]:
                    events: list[tuple[bool, bool]] = []
                    document = engine.DocxTemplate(io.BytesIO(source))
                    image = engine.InlineImage(document, io.BytesIO(png.getvalue()))
                    child = object.__new__(docxtpl_native.Subdoc)
                    child.tpl = None
                    child.source = io.BytesIO(template('unused'))

                    def inspect(value: str) -> str:
                        events.append(('<w:drawing>' in value, '\x01DTPLD' in value))
                        return 'ok'

                    document.render({'image': image, 'child': child, 'inspect': inspect},
                                    jinja_env=jinja2.Environment(enable_async=asynchronous))
                    output = io.BytesIO()
                    document.save(output)
                    return output.getvalue(), events

                expected, reference_events = run(docxtpl)
                self.assertEqual(reference_events, [(True, False)])
                actual, candidate_events = run(docxtpl_native)
                self.assertEqual(candidate_events, reference_events)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_repeated_captured_image_materializes_each_occurrence(self) -> None:
        source = template('{% set first %}{{ image }}{% endset %}'
                          '{% set second %}{{ image }}{% endset %}{{ first }}{{ second }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                image = docxtpl.InlineImage(reference, io.BytesIO(png.getvalue()))
                reference.render({'image': image}, jinja_env=jinja2.Environment(enable_async=asynchronous))
                expected = io.BytesIO()
                reference.save(expected)
                actual = self.native_render(source, {}, include_image=True,
                                            environment=jinja2.Environment(enable_async=asynchronous))
                self.assertEqual(len(Document(io.BytesIO(actual)).inline_shapes), 2)
                self.assertTrue(compare_docx(expected.getvalue(), actual)['equivalent'])

    def test_captured_image_text_keeps_identity_in_callback(self) -> None:
        source = template('{% set captured %}{{ image }}{% endset %}'
                          '{{ inspect(captured, captured) }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool, asynchronous: bool = asynchronous) -> tuple[bytes, list[bool]]:
                    seen: list[bool] = []

                    def inspect(first: object, second: object) -> str:
                        seen.append(first is second)
                        return 'ok'

                    if native:
                        return self.native_render(source, {'inspect': inspect}, include_image=True,
                                                  environment=jinja2.Environment(enable_async=asynchronous)), seen
                    document = docxtpl.DocxTemplate(io.BytesIO(source))
                    image = docxtpl.InlineImage(document, io.BytesIO(png.getvalue()))
                    document.render({'image': image, 'inspect': inspect},
                                    jinja_env=jinja2.Environment(enable_async=asynchronous))
                    output = io.BytesIO()
                    document.save(output)
                    return output.getvalue(), seen

                expected, expected_seen = run(False)
                self.assertEqual(expected_seen, [True])
                actual, actual_seen = run(True)
                self.assertEqual(actual_seen, expected_seen)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_captured_image_callback_error_preserves_identity(self) -> None:
        source = template('{% set captured %}{{ image }}{% endset %}{{ inspect(captured) }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    error = ValueError('callback failed')
                    events: list[bool] = []

                    def inspect(value: str, events: list[bool] = events, error: ValueError = error) -> str:
                        events.append('<w:drawing>' in value)
                        raise error

                    with self.assertRaises(ValueError) as raised:
                        if native:
                            self.native_render(source, {'inspect': inspect}, include_image=True,
                                               environment=jinja2.Environment(enable_async=asynchronous))
                        else:
                            document = docxtpl.DocxTemplate(io.BytesIO(source))
                            image = docxtpl.InlineImage(document, io.BytesIO(png.getvalue()))
                            document.render({'image': image, 'inspect': inspect},
                                            jinja_env=jinja2.Environment(enable_async=asynchronous))
                    self.assertIs(raised.exception, error)
                    self.assertEqual(events, [True])

    def test_captured_registered_image_uses_live_dimensions(self) -> None:
        source = template('{{ change() }}{% set captured %}{{ image }}{% endset %}{{ inspect(captured) }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool, asynchronous: bool = asynchronous) -> tuple[bytes, list[bool]]:
                    events: list[bool] = []
                    document = docxtpl.DocxTemplate(io.BytesIO(source)) if not native else None
                    image = (docxtpl_native.InlineImage(None, io.BytesIO(png.getvalue()), width=Emu(10))
                             if native else docxtpl.InlineImage(document, io.BytesIO(png.getvalue()), width=Emu(10)))

                    def change() -> str:
                        image.width = Emu(30)
                        return ''

                    def inspect(value: str) -> str:
                        events.append('cx="30"' in value)
                        return 'ok'

                    context = {'image': image, 'change': change, 'inspect': inspect}
                    environment = jinja2.Environment(enable_async=asynchronous)
                    if not native:
                        assert document is not None
                        document.render(context, jinja_env=environment)
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue(), events
                    payload = json.dumps({
                        'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    output, _ = docxtpl_native._extension().process_python_docx(
                        source, context, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(output), events

                expected, reference_events = run(False)
                self.assertEqual(reference_events, [True])
                actual, candidate_events = run(True)
                self.assertEqual(candidate_events, reference_events)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_captured_registered_image_uses_live_stream(self) -> None:
        source = template('{{ change() }}{% set captured %}{{ image }}{% endset %}{{ inspect(captured) }}')
        payloads: list[bytes] = []
        for color in ((21, 42, 84), (180, 40, 16)):
            stream = io.BytesIO()
            Image.new('RGB', (2, 2), color).save(stream, format='PNG')
            payloads.append(stream.getvalue())
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool, asynchronous: bool = asynchronous) -> tuple[bytes, list[bool]]:
                    events: list[bool] = []
                    document = docxtpl.DocxTemplate(io.BytesIO(source)) if not native else None
                    image = (docxtpl_native.InlineImage(None, io.BytesIO(payloads[0])) if native
                             else docxtpl.InlineImage(document, io.BytesIO(payloads[0])))

                    def change() -> str:
                        image.image_descriptor = io.BytesIO(payloads[1])
                        return ''

                    def inspect(value: str) -> str:
                        events.append('<w:drawing>' in value)
                        return 'ok'

                    context = {'image': image, 'change': change, 'inspect': inspect}
                    environment = jinja2.Environment(enable_async=asynchronous)
                    if not native:
                        assert document is not None
                        document.render(context, jinja_env=environment)
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue(), events
                    payload = json.dumps({
                        'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    output, _ = docxtpl_native._extension().process_python_docx(
                        source, context, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(output), events

                expected, reference_events = run(False)
                self.assertEqual(reference_events, [True])
                actual, candidate_events = run(True)
                self.assertEqual(candidate_events, reference_events)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_registered_image_mutated_after_capture_uses_latest_dimensions(self) -> None:
        source = template('{% set first %}{{ image }}{% endset %}{{ change() }}{{ image }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool, asynchronous: bool = asynchronous) -> bytes:
                    document = docxtpl.DocxTemplate(io.BytesIO(source)) if not native else None
                    image = (docxtpl_native.InlineImage(None, io.BytesIO(png.getvalue()), width=Emu(10))
                             if native else docxtpl.InlineImage(document, io.BytesIO(png.getvalue()), width=Emu(10)))

                    def change() -> str:
                        image.width = Emu(30)
                        return ''

                    context = {'image': image, 'change': change}
                    environment = jinja2.Environment(enable_async=asynchronous)
                    if not native:
                        assert document is not None
                        document.render(context, jinja_env=environment)
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue()
                    payload = json.dumps({
                        'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    output, _ = docxtpl_native._extension().process_python_docx(
                        source, context, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(output)

                expected = run(False)
                actual = run(True)
                self.assertEqual(Document(io.BytesIO(expected)).inline_shapes[0].width, Emu(30))
                self.assertEqual(Document(io.BytesIO(actual)).inline_shapes[0].width, Emu(30))
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_registered_image_metadata_cannot_forge_deferred_marker(self) -> None:
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            for source_text in ('{{ image }}', '{% set captured %}{{ image }}{% endset %}{{ captured }}'):
                with self.subTest(asynchronous=asynchronous, source=source_text):
                    image = docxtpl_native.InlineImage(None, io.BytesIO(png.getvalue()), title='\x01DTPLD0\x01')
                    payload = json.dumps({
                        'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'deferred marker'):
                        docxtpl_native._extension().process_python_docx(
                            template(source_text), {'image': image},
                            BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), payload, False,
                        )

    def test_async_image_metadata_capture_can_be_inspected_without_emission(self) -> None:
        source = template('{% set captured %}{{ image }}{% endset %}{{ inspect(captured) }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        image = docxtpl_native.InlineImage(None, io.BytesIO(png.getvalue()), title='\x01DTPLD0\x01')
        payload = json.dumps({
            'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        events: list[bool] = []

        def inspect(value: str) -> str:
            events.append('\x01DTPLD0\x01' in value)
            return 'ok'

        output, _ = docxtpl_native._extension().process_python_docx(
            source, {'image': image, 'inspect': inspect},
            BridgeConfig(jinja2.Environment(enable_async=True), False), payload, False,
        )
        self.assertEqual(events, [True])
        self.assertEqual(Document(io.BytesIO(bytes(output))).paragraphs[0].text, 'ok')

    def test_registered_image_capture_observes_replaced_insertion(self) -> None:
        source = template('{% set captured %}{{ image }}{% endset %}{{ inspect(captured) }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool, asynchronous: bool = asynchronous) -> tuple[bytes, list[str]]:
                    events: list[str] = []
                    document = docxtpl.DocxTemplate(io.BytesIO(source)) if not native else None
                    image = (docxtpl_native.InlineImage(None, io.BytesIO(png.getvalue())) if native
                             else docxtpl.InlineImage(document, io.BytesIO(png.getvalue())))

                    def insertion() -> str:
                        events.append('insert')
                        return 'custom image'

                    def inspect(value: str) -> str:
                        events.append(value)
                        return 'ok'

                    image._insert_image = insertion
                    context = {'image': image, 'inspect': inspect}
                    environment = jinja2.Environment(enable_async=asynchronous)
                    if not native:
                        assert document is not None
                        document.render(context, jinja_env=environment)
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue(), events
                    payload = json.dumps({
                        'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    output, _ = docxtpl_native._extension().process_python_docx(
                        source, context, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(output), events

                expected, reference_events = run(False)
                self.assertEqual(reference_events, ['insert', 'custom image'])
                actual, candidate_events = run(True)
                self.assertEqual(candidate_events, reference_events)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_autoescaped_registered_image_metadata_cannot_forge_marker(self) -> None:
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                image = docxtpl_native.InlineImage(None, io.BytesIO(png.getvalue()), title='\x01DTPLD0\x01')
                payload = json.dumps({
                    'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                    'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                })
                with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'deferred marker'):
                    docxtpl_native._extension().process_python_docx(
                        template('{{ image }}'), {'image': image},
                        BridgeConfig(jinja2.Environment(enable_async=asynchronous, autoescape=True), False),
                        payload, True,
                    )

    def test_registered_image_output_does_not_reimport_binding(self) -> None:
        source = template('{{ image }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        image = docxtpl_native.InlineImage(None, io.BytesIO(png.getvalue()))
        payload = json.dumps({
            'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        extension = docxtpl_native._extension()
        config = BridgeConfig(jinja2.Environment(), False)
        original = builtins.__import__
        imports: list[str] = []

        def observe(name: str, *args: Any, **kwargs: Any) -> Any:
            if name in ('docxtpl_native', 'builtins'):
                imports.append(name)
            return original(name, *args, **kwargs)

        with patch.object(builtins, '__import__', side_effect=observe):
            output, _ = extension.process_python_docx(source, {'image': image}, config, payload, False)
        self.assertEqual(len(Document(io.BytesIO(bytes(output))).inline_shapes), 1)
        self.assertEqual(imports, [])

    def test_package_class_replacement_is_observed_during_render(self) -> None:
        source = template('{{ change() }}{{ inspect(value) }}')
        events: list[str] = []
        value = object()
        original = docxtpl_native.InlineImage

        class Meta(type):
            def __instancecheck__(cls, instance: object) -> bool:
                if instance is value:
                    events.append('checked')
                return False

        class Replacement(metaclass=Meta):
            pass

        def change() -> str:
            docxtpl_native.InlineImage = cast(Any, Replacement)
            return ''

        def inspect(argument: object) -> str:
            events.append('callback:' + str(argument is value))
            return 'ok'

        payload = json.dumps({
            'specials': [],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        try:
            output, _ = docxtpl_native._extension().process_python_docx(
                source,
                {'change': change, 'inspect': inspect, 'value': value},
                BridgeConfig(jinja2.Environment(), False), payload, False,
            )
        finally:
            docxtpl_native.InlineImage = original
        self.assertEqual(Document(io.BytesIO(bytes(output))).paragraphs[0].text, 'ok')
        self.assertIn('checked', events)
        self.assertEqual(events[-1], 'callback:True')

    def test_package_class_replacement_after_await_is_observed(self) -> None:
        source = template('{{ change() }}{{ inspect(value) }}')
        events: list[str] = []
        value = object()
        original = docxtpl_native.InlineImage

        class Meta(type):
            def __instancecheck__(cls, instance: object) -> bool:
                if instance is value:
                    events.append('checked')
                return False

        class Replacement(metaclass=Meta):
            pass

        async def change() -> str:
            await asyncio.sleep(0)
            docxtpl_native.InlineImage = cast(Any, Replacement)
            return ''

        def inspect(argument: object) -> str:
            events.append('callback:' + str(argument is value))
            return 'ok'

        payload = json.dumps({
            'specials': [],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        try:
            output, _ = docxtpl_native._extension().process_python_docx(
                source, {'change': change, 'inspect': inspect, 'value': value},
                BridgeConfig(jinja2.Environment(enable_async=True), False), payload, False,
            )
        finally:
            docxtpl_native.InlineImage = original
        self.assertEqual(Document(io.BytesIO(bytes(output))).paragraphs[0].text, 'ok')
        self.assertIn('checked', events)
        self.assertEqual(events[-1], 'callback:True')

    def test_replaced_class_that_matches_an_argument_blocks_the_callback(self) -> None:
        # The value entered the engine before the replacement; the class that
        # replaces InlineImage during the render claims it, so it is an image
        # the engine cannot materialize and must not reach the callback.
        def run(asynchronous: bool) -> list[str]:
            source = template('{{ change() }}{{ inspect(value) }}')
            events: list[str] = []
            value = object()
            original = docxtpl_native.InlineImage

            class Meta(type):
                def __instancecheck__(cls, instance: object) -> bool:
                    events.append('checked')
                    return instance is value

            class Replacement(metaclass=Meta):
                pass

            def change() -> str:
                docxtpl_native.InlineImage = cast(Any, Replacement)
                return ''

            def inspect(argument: object) -> str:
                events.append('callback')
                return 'ok'

            payload = json.dumps({
                'specials': [],
                'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
            })
            try:
                with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
                    docxtpl_native._extension().process_python_docx(
                        source, {'change': change, 'inspect': inspect, 'value': value},
                        BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), payload, False,
                    )
            finally:
                docxtpl_native.InlineImage = original
            return events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                events = run(asynchronous)
                self.assertIn('checked', events)
                self.assertNotIn('callback', events)

    def test_callback_arguments_with_unchanged_package_do_not_import_it(self) -> None:
        source = template('{{ inspect(value) }}{{ inspect(value) }}')
        value = object()
        received: list[bool] = []

        def inspect(argument: object) -> str:
            received.append(argument is value)
            return 'ok'

        payload = json.dumps({
            'specials': [],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        package = sys.modules['docxtpl_native']
        original_import = builtins.__import__
        imports: list[str] = []

        def observe(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith('docxtpl_native'):
                imports.append(name)
            return original_import(name, *args, **kwargs)

        def render() -> bytes:
            output, _ = docxtpl_native._extension().process_python_docx(
                source, {'inspect': inspect, 'value': value},
                BridgeConfig(jinja2.Environment(), False), payload, False,
            )
            return bytes(output)

        # The first render of a process initializes the binding, which imports
        # the package once; this test is about each callback.
        render()
        received.clear()
        with patch.object(builtins, '__import__', side_effect=observe):
            output = render()
        self.assertIs(sys.modules['docxtpl_native'], package)
        self.assertEqual(Document(io.BytesIO(output)).paragraphs[0].text, 'okok')
        self.assertEqual(received, [True, True])
        self.assertEqual([name for name in imports if name == 'docxtpl_native'], [], imports)

    def test_none_package_entry_retains_import_failure(self) -> None:
        source = template('{{ change() }}{{ inspect(value) }}')
        original = sys.modules['docxtpl_native']
        events: list[str] = []

        def change() -> str:
            sys.modules['docxtpl_native'] = cast(Any, None)
            events.append('change')
            return ''

        def inspect(value: object) -> str:
            events.append('inspect')
            return 'ok'

        payload = json.dumps({
            'specials': [],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        try:
            with self.assertRaises(ModuleNotFoundError):
                docxtpl_native._extension().process_python_docx(
                    source, {'change': change, 'inspect': inspect, 'value': object()},
                    BridgeConfig(jinja2.Environment(), False), payload, False,
                )
        finally:
            sys.modules['docxtpl_native'] = original
        self.assertEqual(events, ['change'])

    def test_missing_package_entry_uses_import_hook(self) -> None:
        source = template('{{ change() }}{{ inspect(value) }}')
        package = sys.modules['docxtpl_native']
        original_import = builtins.__import__
        events: list[str] = []
        value = object()

        def change() -> str:
            sys.modules.pop('docxtpl_native', None)
            events.append('change')
            return ''

        def inspect(argument: object) -> str:
            events.append('callback:' + str(argument is value))
            return 'ok'

        def importer(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == 'docxtpl_native':
                events.append('import')
                sys.modules[name] = package
                return package
            return original_import(name, *args, **kwargs)

        payload = json.dumps({
            'specials': [],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        extension = docxtpl_native._extension()
        config = BridgeConfig(jinja2.Environment(), False)
        try:
            with patch.object(builtins, '__import__', side_effect=importer):
                output, _ = extension.process_python_docx(
                    source, {'change': change, 'inspect': inspect, 'value': value}, config, payload, False,
                )
        finally:
            sys.modules['docxtpl_native'] = package
        self.assertEqual(Document(io.BytesIO(bytes(output))).paragraphs[0].text, 'ok')
        self.assertEqual(events[0], 'change')
        self.assertIn('import', events)
        self.assertEqual(events[-1], 'callback:True')

    def test_autoescaped_registered_image_capture_observes_custom_html(self) -> None:
        source = template('{% set captured %}{{ image }}{% endset %}{{ inspect(captured) }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool, asynchronous: bool = asynchronous) -> tuple[bytes, list[str]]:
                    events: list[str] = []
                    document = docxtpl.DocxTemplate(io.BytesIO(source)) if not native else None
                    image = (docxtpl_native.InlineImage(None, io.BytesIO(png.getvalue())) if native
                             else docxtpl.InlineImage(document, io.BytesIO(png.getvalue())))

                    def html() -> str:
                        events.append('html')
                        return '<b>custom</b>'

                    def inspect(value: str) -> str:
                        events.append(value)
                        return 'ok'

                    image.__html__ = html
                    context = {'image': image, 'inspect': inspect}
                    environment = jinja2.Environment(enable_async=asynchronous, autoescape=True)
                    if not native:
                        assert document is not None
                        document.render(context, jinja_env=environment)
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue(), events
                    payload = json.dumps({
                        'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    output, _ = docxtpl_native._extension().process_python_docx(
                        source, context, BridgeConfig(environment, False), payload, True,
                    )
                    return bytes(output), events

                expected, reference_events = run(False)
                self.assertEqual(reference_events, ['html', '<b>custom</b>'])
                actual, candidate_events = run(True)
                self.assertEqual(candidate_events, reference_events)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])


    def test_captured_image_does_not_execute_reference_insertion(self) -> None:
        code = docxtpl.InlineImage._insert_image.__code__
        source = template('{% set captured %}{{ image }}{% endset %}{{ inspect(captured) }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for native in (False, True):
            with self.subTest(native=native):
                calls: list[str] = []

                def profile(frame: types.FrameType, event: str, argument: object,
                            calls: list[str] = calls) -> None:
                    if event == 'call' and frame.f_code is code:
                        calls.append(frame.f_code.co_name)

                previous = sys.getprofile()
                try:
                    sys.setprofile(profile)
                    if native:
                        self.native_render(source, {'inspect': lambda value: 'ok'}, include_image=True)
                    else:
                        document = docxtpl.DocxTemplate(io.BytesIO(source))
                        image = docxtpl.InlineImage(document, io.BytesIO(png.getvalue()))
                        document.render({'image': image, 'inspect': lambda value: 'ok'})
                finally:
                    sys.setprofile(previous)
                self.assertEqual(bool(calls), not native)

    def test_native_typed_user_text_cannot_insert_unused_image(self) -> None:
        source = template('{{ value }}')
        context = {'value': '\x01DTPLD0\x01'}
        reference = Document(io.BytesIO(generate(docxtpl, '', context, jinja2.Environment(), source)))
        image = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image, format='PNG')
        payload = {
            'context': {'kind': 'map', 'items': [
                ['value', {'kind': 'str', 'value': context['value']}],
                ['unused_image', {'kind': 'image', 'data': base64.b64encode(image.getvalue()).decode('ascii')}],
            ]},
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        }
        output = docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True)
        candidate = Document(io.BytesIO(bytes(output)))
        self.assertEqual(len(candidate.inline_shapes), 0)
        self.assertEqual(candidate.paragraphs[0].text, reference.paragraphs[0].text)

    def test_native_typed_xml_character_recovery(self) -> None:
        source = template('{{ value }}')
        values = ('a\x00b\x01c\x08d\x0be\x0cf\x0eg\x1fh',
                  'a\ufffeb\uffffc', 'a\tb\nc\rd')
        for value in values:
            with self.subTest(value=repr(value)):
                reference = generate(docxtpl, '', {'value': value}, jinja2.Environment(), source)
                payload = {'context': {'kind': 'map', 'items': [
                    ['value', {'kind': 'str', 'value': value}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
                self.assertTrue(compare_docx(reference, output)['equivalent'])

    def test_native_typed_forged_text_with_subdocument(self) -> None:
        child = Document()
        child.add_paragraph('child content')
        stream = io.BytesIO()
        child.save(stream)
        blob = stream.getvalue()
        for expression in ('{{ fake }}', '{{ fake }}{{ subdoc }}', '{{ subdoc }}{{ fake }}'):
            with self.subTest(expression=expression):
                source = template(expression)
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                context = {'fake': '\x01DTPLD0\x01', 'subdoc': reference.new_subdoc(io.BytesIO(blob))}
                reference.render(context)
                expected = io.BytesIO()
                reference.save(expected)
                payload = {'context': {'kind': 'map', 'items': [
                    ['fake', {'kind': 'str', 'value': context['fake']}],
                    ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(blob).decode('ascii')}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
                actual = Document(io.BytesIO(output))
                wanted = Document(io.BytesIO(expected.getvalue()))
                self.assertEqual([p.text for p in actual.paragraphs], [p.text for p in wanted.paragraphs])
                self.assertTrue(compare_docx(expected.getvalue(), output)['equivalent'])

    def test_native_typed_split_marker_is_text(self) -> None:
        source = template('{{ first }}{{ second }}')
        context = {'first': '\x01DT', 'second': 'PLD0\x01'}
        reference = generate(docxtpl, '', context, jinja2.Environment(), source)
        image = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image, format='PNG')
        payload = {'context': {'kind': 'map', 'items': [
            ['first', {'kind': 'str', 'value': context['first']}],
            ['second', {'kind': 'str', 'value': context['second']}],
            ['unused_image', {'kind': 'image', 'data': base64.b64encode(image.getvalue()).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
        self.assertTrue(compare_docx(reference, output)['equivalent'])

    def test_native_typed_invalid_marker_in_header_raises_reference_error(self) -> None:
        source_doc = Document()
        source_doc.sections[0].header.paragraphs[0].text = '{{ fake }}{{ image }}'
        source_doc.sections[0].footer.paragraphs[0].text = '{{ fake }}'
        source_stream = io.BytesIO()
        source_doc.save(source_stream)
        image = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image, format='PNG')
        blob = image.getvalue()
        reference = docxtpl.DocxTemplate(io.BytesIO(source_stream.getvalue()))
        xml_syntax_error = importlib.import_module('lxml.etree').XMLSyntaxError
        with self.assertRaises(xml_syntax_error) as reference_error:
            reference.render({'fake': '\x01DTPLD0\x01', 'image': docxtpl.InlineImage(reference, io.BytesIO(blob))})
        payload = {'context': {'kind': 'map', 'items': [
            ['fake', {'kind': 'str', 'value': '\x01DTPLD0\x01'}],
            ['image', {'kind': 'image', 'data': base64.b64encode(blob).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        with self.assertRaises(type(reference_error.exception)):
            docxtpl_native._extension().process_docx(source_stream.getvalue(), json.dumps(payload), False, True)

    def test_native_typed_invalid_header_stops_before_footer_numbering(self) -> None:
        source_doc = Document()
        source_doc.sections[0].header.paragraphs[0].text = '{{ invalid }}'
        source_doc.sections[0].footer.paragraphs[0].text = '{{ subdoc }}'
        source_stream = io.BytesIO()
        source_doc.save(source_stream)
        child = Document()
        paragraph = child.add_paragraph('numbered')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        child_stream = io.BytesIO()
        child.save(child_stream)
        blob = child_stream.getvalue()
        reference = docxtpl.DocxTemplate(io.BytesIO(source_stream.getvalue()))
        subdoc = reference.new_subdoc(io.BytesIO(blob))
        xml_syntax_error = importlib.import_module('lxml.etree').XMLSyntaxError
        with patch.object(composer.random, 'random', return_value=0.125) as reference_random:
            with self.assertRaises(xml_syntax_error):
                reference.render({'invalid': '\x01', 'subdoc': subdoc})
            reference_random.assert_not_called()
        payload = {'context': {'kind': 'map', 'items': [
            ['invalid', {'kind': 'str', 'value': '\x01'}],
            ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(blob).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        with patch.object(composer.random, 'random', return_value=0.125) as candidate_random:
            with self.assertRaises(xml_syntax_error):
                docxtpl_native._extension().process_docx(source_stream.getvalue(), json.dumps(payload), False, True)
            candidate_random.assert_not_called()

    def test_native_typed_invalid_header_error_details(self) -> None:
        source_doc = Document()
        source_doc.sections[0].header.paragraphs[0].text = '{{ invalid }}'
        source_stream = io.BytesIO()
        source_doc.save(source_stream)
        source = source_stream.getvalue()
        xml_syntax_error = importlib.import_module('lxml.etree').XMLSyntaxError
        for value in ('\x01', '\x00', 'x\x00y', 'é\x00fim', '\r\x00', 'bad & \x00',
                      '<not-closed>\x00', 'bad & value', '<not-closed>', '&#0;'):
            with self.subTest(value=repr(value)):
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                with self.assertRaises(xml_syntax_error) as expected:
                    reference.render({'invalid': value})
                payload = {'context': {'kind': 'map', 'items': [
                    ['invalid', {'kind': 'str', 'value': value}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                with self.assertRaises(xml_syntax_error) as actual:
                    docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True)
                for name in ('msg', 'code', 'position', 'lineno', 'offset', 'filename', 'args'):
                    self.assertEqual(getattr(actual.exception, name), getattr(expected.exception, name), name)
                self.assertEqual(str(actual.exception), str(expected.exception))

    def test_native_typed_invalid_header_error_log(self) -> None:
        source_doc = Document()
        source_doc.sections[0].header.paragraphs[0].text = '{{ invalid }}'
        stream = io.BytesIO()
        source_doc.save(stream)
        source = stream.getvalue()
        xml_syntax_error = importlib.import_module('lxml.etree').XMLSyntaxError
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        with self.assertRaises(xml_syntax_error) as expected:
            reference.render({'invalid': '\x01'})
        expected_log = [(entry.message, entry.domain, entry.type, entry.line, entry.column)
                        for entry in expected.exception.error_log]
        importlib.import_module('lxml.etree').clear_error_log()
        payload = {'context': {'kind': 'map', 'items': [
            ['invalid', {'kind': 'str', 'value': '\x01'}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        with self.assertRaises(xml_syntax_error) as actual:
            docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True)
        actual_log = [(entry.message, entry.domain, entry.type, entry.line, entry.column)
                      for entry in actual.exception.error_log]
        self.assertTrue(expected_log)
        self.assertEqual(actual_log[-1:], expected_log[-1:])

    def test_native_typed_invalid_footer_error_details(self) -> None:
        source_doc = Document()
        source_doc.sections[0].footer.paragraphs[0].text = '{{ invalid }}'
        stream = io.BytesIO()
        source_doc.save(stream)
        source = stream.getvalue()
        xml_syntax_error = importlib.import_module('lxml.etree').XMLSyntaxError
        for value in ('\x01', 'é\x00fim', 'bad & \x00'):
            with self.subTest(value=repr(value)):
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                with self.assertRaises(xml_syntax_error) as expected:
                    reference.render({'invalid': value})
                payload = {'context': {'kind': 'map', 'items': [
                    ['invalid', {'kind': 'str', 'value': value}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                with self.assertRaises(xml_syntax_error) as actual:
                    docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True)
                for name in ('msg', 'code', 'position', 'lineno', 'offset', 'filename', 'args'):
                    self.assertEqual(getattr(actual.exception, name), getattr(expected.exception, name), name)
                self.assertEqual(str(actual.exception), str(expected.exception))

    def test_native_typed_valid_header_and_footer_after_normalization(self) -> None:
        source_doc = Document()
        source_doc.sections[0].header.paragraphs[0].text = 'header: {{ value }}'
        source_doc.sections[0].footer.paragraphs[0].text = 'footer: {{ value }}'
        source_stream = io.BytesIO()
        source_doc.save(source_stream)
        source = source_stream.getvalue()
        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                context = {'value': 'plain'}
                reference = generate(docxtpl, '', context, jinja2.Environment(autoescape=autoescape), source)
                payload = {'context': {'kind': 'map', 'items': [
                    ['value', {'kind': 'str', 'value': context['value']}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), autoescape, True))
                self.assertTrue(compare_docx(reference, output)['equivalent'])

    def test_native_typed_marker_with_autoescape(self) -> None:
        source = template('{{ fake }}{{ image }}')
        image = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image, format='PNG')
        blob = image.getvalue()
        for value in ('\x01DTPLD0\x01 & <', 'a\x00b & <'):
            with self.subTest(value=repr(value)):
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                reference.render({'fake': value, 'image': docxtpl.InlineImage(reference, io.BytesIO(blob))},
                                 jinja_env=jinja2.Environment(autoescape=True))
                expected = io.BytesIO()
                reference.save(expected)
                payload = {'context': {'kind': 'map', 'items': [
                    ['fake', {'kind': 'str', 'value': value}],
                    ['image', {'kind': 'image', 'data': base64.b64encode(blob).decode('ascii')}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), True, True))
                # Intentional divergence: the forbidden character makes the
                # reference's recover mode drop the escaped `&` and `<` that
                # follow it; the native engine keeps them.
                self.assertIn('&amp; &lt;', self._text(output))
                self.assertNotIn('&amp; &lt;', self._text(expected.getvalue()))
                self.assertTrue(compare_docx(self._text_free(expected.getvalue()), self._text_free(output))['equivalent'])

    @staticmethod
    def _text(docx: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(docx)) as package:
            return ''.join(re.findall(r'<w:t(?:\s[^>]*)?>([^<]*)</w:t>', package.read('word/document.xml').decode()))

    @staticmethod
    def _text_free(docx: bytes) -> bytes:
        """The package with every text run emptied, to compare the rest."""
        source = zipfile.ZipFile(io.BytesIO(docx))
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == 'word/document.xml':
                    data = re.sub(rb'(<w:t(?:\s[^>]*)?>)[^<]*(</w:t>)', rb'\g<1>\g<2>', data)
                target.writestr(item, data)
        return stream.getvalue()

    def test_native_typed_image_and_forged_text_in_compositions(self) -> None:
        fake = '\x01DTPLD0\x01'
        expressions = (
            '{{ fake }}{{ image }}',
            '{% set x %}{{ fake }}{{ image }}{% endset %}{{ x }}',
            '{% macro item() %}{{ fake }}{{ image }}{% endmacro %}{{ item() }}',
            '{{ fake ~ image }}',
            '{{ image|string }}{{ fake }}',
            '{{ image|safe }}{{ fake }}',
            '{{ [fake, image]|join }}',
        )
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_stream, format='PNG')
        image_bytes = image_stream.getvalue()
        for expression in expressions:
            with self.subTest(expression=expression):
                source = template(expression)
                reference_template = docxtpl.DocxTemplate(io.BytesIO(source))
                reference_image = docxtpl.InlineImage(reference_template, io.BytesIO(image_bytes))
                reference_template.render({'fake': fake, 'image': reference_image})
                reference_buffer = io.BytesIO()
                reference_template.save(reference_buffer)
                payload = {'context': {'kind': 'map', 'items': [
                    ['fake', {'kind': 'str', 'value': fake}],
                    ['image', {'kind': 'image', 'data': base64.b64encode(image_bytes).decode('ascii')}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
                reference = Document(io.BytesIO(reference_buffer.getvalue()))
                candidate = Document(io.BytesIO(output))
                self.assertEqual(reference.paragraphs[0].text, 'DTPLD0')
                self.assertEqual(len(reference.inline_shapes), 1)
                self.assertEqual(candidate.paragraphs[0].text, reference.paragraphs[0].text)
                self.assertEqual(len(candidate.inline_shapes), len(reference.inline_shapes))
                self.assertTrue(compare_docx(reference_buffer.getvalue(), output)['equivalent'])

    def test_native_typed_image_compositions_without_forged_text(self) -> None:
        expressions = (
            '{{ image }}',
            '{% set x %}{{ image }}{% endset %}{{ x }}',
            '{% macro item() %}{{ image }}{% endmacro %}{{ item() }}',
            "{{ 'P' ~ image }}",
            '{{ image|string }}',
            '{{ image|safe }}',
        )
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_stream, format='PNG')
        image_bytes = image_stream.getvalue()
        for expression in expressions:
            with self.subTest(expression=expression):
                source = template(expression)
                reference_template = docxtpl.DocxTemplate(io.BytesIO(source))
                reference_template.render({'image': docxtpl.InlineImage(reference_template, io.BytesIO(image_bytes))})
                reference_buffer = io.BytesIO()
                reference_template.save(reference_buffer)
                payload = {'context': {'kind': 'map', 'items': [
                    ['image', {'kind': 'image', 'data': base64.b64encode(image_bytes).decode('ascii')}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
                reference = Document(io.BytesIO(reference_buffer.getvalue()))
                candidate = Document(io.BytesIO(output))
                self.assertEqual(candidate.paragraphs[0].text, reference.paragraphs[0].text)
                self.assertEqual(len(candidate.inline_shapes), len(reference.inline_shapes))
                self.assertTrue(compare_docx(reference_buffer.getvalue(), output)['equivalent'])

    def test_native_typed_images_in_join(self) -> None:
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_stream, format='PNG')
        image_bytes = image_stream.getvalue()
        for autoescape in (False, True):
            for expression in ("{{ [image, text]|join(' / ') }}", "{{ []|join(image) }}",
                               "{{ [text]|join(image) }}", "{{ [text, text]|join(image) }}",
                               "{{ [text, text, text]|join(image) }}"):
                with self.subTest(autoescape=autoescape, expression=expression):
                    source = template(expression)
                    reference_template = docxtpl.DocxTemplate(io.BytesIO(source))
                    image = docxtpl.InlineImage(reference_template, io.BytesIO(image_bytes))
                    reference_template.render({'image': image, 'text': 'plain'},
                                              jinja_env=jinja2.Environment(autoescape=autoescape))
                    reference_buffer = io.BytesIO()
                    reference_template.save(reference_buffer)
                    payload = {'context': {'kind': 'map', 'items': [
                        ['image', {'kind': 'image', 'data': base64.b64encode(image_bytes).decode('ascii')}],
                        ['text', {'kind': 'str', 'value': 'plain'}],
                    ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                    output = bytes(docxtpl_native._extension().process_docx(
                        source, json.dumps(payload), autoescape, True))
                    self.assertTrue(compare_docx(reference_buffer.getvalue(), output)['equivalent'])

    def test_native_typed_image_join_effect_order_and_conversions(self) -> None:
        streams = [io.BytesIO(), io.BytesIO()]
        Image.new('RGB', (2, 2), (21, 42, 84)).save(streams[0], format='PNG')
        Image.new('RGB', (2, 2), (84, 42, 21)).save(streams[1], format='PNG')
        blobs = [stream.getvalue() for stream in streams]
        expressions = (
            '{{ first }}{{ []|join(second) }}',
            '{{ []|join(second) }}{{ first }}',
            '{% set unused = []|join(second) %}{{ first }}',
            '{{ ["a", "b"]|join(first|safe) }}',
            '{{ ["a", "b"]|join(first|string) }}',
            '{{ first|string }}',
            '{{ first|safe }}',
            "{{ 'prefix' ~ first }}{{ []|join(second) }}",
            "{% set text = 'prefix' ~ first %}{{ []|join(second) }}{{ text }}",
        )
        for autoescape in (False, True):
            for expression in expressions:
                with self.subTest(autoescape=autoescape, expression=expression):
                    source = template(expression)
                    reference_template = docxtpl.DocxTemplate(io.BytesIO(source))
                    context = {name: docxtpl.InlineImage(reference_template, io.BytesIO(blob))
                               for name, blob in zip(('first', 'second'), blobs, strict=True)}
                    reference_template.render(context, jinja_env=jinja2.Environment(autoescape=autoescape))
                    reference_buffer = io.BytesIO()
                    reference_template.save(reference_buffer)
                    payload = {'context': {'kind': 'map', 'items': [
                        [name, {'kind': 'image', 'data': base64.b64encode(blob).decode('ascii')}]
                        for name, blob in zip(('first', 'second'), blobs, strict=True)
                    ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                    output = bytes(docxtpl_native._extension().process_docx(
                        source, json.dumps(payload), autoescape, True))
                    self.assertTrue(compare_docx(reference_buffer.getvalue(), output)['equivalent'])

    def test_native_typed_image_and_file_subdoc(self) -> None:
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_stream, format='PNG')
        image = image_stream.getvalue()
        child = Document()
        child.add_paragraph('child paragraph')
        subdoc_stream = io.BytesIO()
        child.save(subdoc_stream)
        subdoc = subdoc_stream.getvalue()
        for expression in ('{{ image }}{{ subdoc }}', '{{ subdoc }}{{ image }}',
                           '{{ []|join(image) }}{{ subdoc }}'):
            with self.subTest(expression=expression):
                source = template(expression)
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                reference.render({'image': docxtpl.InlineImage(reference, io.BytesIO(image)),
                                  'subdoc': reference.new_subdoc(io.BytesIO(subdoc))})
                reference_buffer = io.BytesIO()
                reference.save(reference_buffer)
                payload = {'context': {'kind': 'map', 'items': [
                    ['image', {'kind': 'image', 'data': base64.b64encode(image).decode('ascii')}],
                    ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(subdoc).decode('ascii')}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
                self.assertTrue(compare_docx(reference_buffer.getvalue(), output)['equivalent'])

    def test_typed_subdoc_preserves_picture_ids_at_construction_boundary(self) -> None:
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        image = png.getvalue()
        child = Document()
        child.add_picture(io.BytesIO(image))
        subdoc_stream = io.BytesIO()
        child.save(subdoc_stream)
        subdoc = subdoc_stream.getvalue()
        for expression in ('{{ image }}{{ subdoc }}', '{{ subdoc }}{{ image }}', '{{ image }}'):
            with self.subTest(expression=expression):
                master = Document(io.BytesIO(template(expression)))
                master.add_picture(io.BytesIO(image))
                master.sections[0].footer.paragraphs[0].add_run().add_picture(io.BytesIO(image))
                master.sections[0].header.paragraphs[0].add_run().add_picture(io.BytesIO(image))
                stream = io.BytesIO()
                master.save(stream)
                source = stream.getvalue()
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                value = reference.new_subdoc(io.BytesIO(subdoc))
                reference.render({'image': docxtpl.InlineImage(reference, io.BytesIO(image)), 'subdoc': value})
                result = io.BytesIO()
                reference.save(result)
                payload = {'context': {'kind': 'map', 'items': [
                    ['image', {'kind': 'image', 'data': base64.b64encode(image).decode('ascii')}],
                    ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(subdoc).decode('ascii')}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
                comparison = compare_docx(result.getvalue(), output)
                self.assertTrue(comparison['equivalent'], comparison)

    def test_typed_subdoc_picture_ids_use_namespace_not_prefix(self) -> None:
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        image = png.getvalue()
        master = Document(io.BytesIO(template('{{ subdoc }}')))
        master.add_picture(io.BytesIO(image))
        master.sections[0].header.paragraphs[0].add_run().add_picture(io.BytesIO(image))
        original = io.BytesIO()
        master.save(original)
        changed = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original.getvalue())) as source_zip, zipfile.ZipFile(changed, 'w') as target:
            for info in source_zip.infolist():
                content = source_zip.read(info.filename)
                if info.filename in ('word/document.xml', 'word/header1.xml'):
                    for old, new in ((b'xmlns:wp=', b'xmlns:draw='), (b'<wp:', b'<draw:'),
                                     (b'</wp:', b'</draw:'), (b'xmlns:pic=', b'xmlns:picture='),
                                     (b'<pic:', b'<picture:'), (b'</pic:', b'</picture:')):
                        content = content.replace(old, new)
                target.writestr(info, content)
        source = changed.getvalue()
        child = Document()
        child.add_paragraph('child')
        child_stream = io.BytesIO()
        child.save(child_stream)
        subdoc = child_stream.getvalue()
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        reference.render({'subdoc': reference.new_subdoc(io.BytesIO(subdoc))})
        result = io.BytesIO()
        reference.save(result)
        payload = {'context': {'kind': 'map', 'items': [
            ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(subdoc).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
        comparison = compare_docx(result.getvalue(), output)
        self.assertTrue(comparison['equivalent'], comparison)

    def test_native_typed_file_subdoc_numbering_and_styles(self) -> None:
        source = template('{{ subdoc }}')
        for style in (None, 'Heading 1', 'List Number', 'List Bullet'):
            with self.subTest(style=style):
                child = Document()
                child.add_paragraph('child paragraph', style=style)
                subdoc_stream = io.BytesIO()
                child.save(subdoc_stream)
                subdoc = subdoc_stream.getvalue()
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                reference.render({'subdoc': reference.new_subdoc(io.BytesIO(subdoc))})
                reference_buffer = io.BytesIO()
                reference.save(reference_buffer)
                payload = {'context': {'kind': 'map', 'items': [
                    ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(subdoc).decode('ascii')}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
                self.assertTrue(compare_docx(reference_buffer.getvalue(), output)['equivalent'])

    def test_native_typed_file_subdoc_direct_numbering(self) -> None:
        source = template('{{ subdoc }}')
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        subdoc_stream = io.BytesIO()
        child.save(subdoc_stream)
        subdoc = subdoc_stream.getvalue()
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        reference.render({'subdoc': reference.new_subdoc(io.BytesIO(subdoc))})
        reference_buffer = io.BytesIO()
        reference.save(reference_buffer)
        payload = {'context': {'kind': 'map', 'items': [
            ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(subdoc).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
        # Only the copied list's nsid differs (see the nsid tests below).
        self.assertTrue(compare_docx(self._without_nsids(reference_buffer.getvalue()),
                                     self._without_nsids(output))['equivalent'])

    def _numbered_subdoc(self) -> bytes:
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        stream = io.BytesIO()
        child.save(stream)
        return stream.getvalue()

    @staticmethod
    def _nsids(docx: bytes) -> list[str]:
        with zipfile.ZipFile(io.BytesIO(docx)) as package:
            return re.findall(r'<w:nsid w:val="([0-9A-Fa-f]{8})"', package.read('word/numbering.xml').decode())

    @staticmethod
    def _without_nsids(docx: bytes) -> bytes:
        source = zipfile.ZipFile(io.BytesIO(docx))
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == 'word/numbering.xml':
                    data = re.sub(rb'(<w:nsid w:val=")[0-9A-Fa-f]{8}(")', rb'\g<1>00000000\g<2>', data)
                target.writestr(item, data)
        return stream.getvalue()

    def test_native_typed_direct_numbering_nsid_is_reproducible(self) -> None:
        """Intentional divergence: docxcompose draws the copied list's nsid
        from Python's random; the native engine derives it from the list
        definition, so every render of the same input is identical."""
        source = template('{{ subdoc }}')
        subdoc = self._numbered_subdoc()
        payload = {'context': {'kind': 'map', 'items': [
            ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(subdoc).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        with patch.object(composer.random, 'random', return_value=0.125):
            reference.render({'subdoc': reference.new_subdoc(io.BytesIO(subdoc))})
        reference_buffer = io.BytesIO()
        reference.save(reference_buffer)
        state = random.getstate()
        with patch.object(composer.random, 'random', side_effect=AssertionError('random drawn')):
            outputs = [bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
                       for _ in range(2)]
        self.assertEqual(random.getstate(), state)
        self.assertEqual(outputs[0], outputs[1])
        nsids = self._nsids(outputs[0])
        self.assertEqual(len(set(nsids)), len(nsids))
        self.assertNotIn('00BEBC20', nsids)  # docxcompose: '%08X' % int(0.125 * 10**8)
        self.assertFalse(compare_docx(reference_buffer.getvalue(), outputs[0])['equivalent'])
        self.assertTrue(compare_docx(self._without_nsids(reference_buffer.getvalue()),
                                     self._without_nsids(outputs[0]))['equivalent'])

    def test_python_subdoc_numbering_nsid_is_reproducible(self) -> None:
        """The Python Subdoc composer derives the nsid the same way."""
        source = template('{{ subdoc }}')
        subdoc = self._numbered_subdoc()
        outputs = []
        with patch.object(composer.random, 'random', side_effect=AssertionError('random drawn')):
            for _ in range(2):
                document = docxtpl_native.DocxTemplate(io.BytesIO(source))
                document.render({'subdoc': document.new_subdoc(io.BytesIO(subdoc))})
                stream = io.BytesIO()
                document.save(stream)
                outputs.append(self._nsids(stream.getvalue()))
        self.assertEqual(outputs[0], outputs[1])
        self.assertTrue(outputs[0])

    def test_native_typed_direct_numbering_without_nsid(self) -> None:
        source = template('{{ subdoc }}')
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        original = io.BytesIO()
        child.save(original)
        rewritten = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original.getvalue())) as source_zip, zipfile.ZipFile(rewritten, 'w') as output_zip:
            for item in source_zip.infolist():
                data = source_zip.read(item.filename)
                if item.filename == 'word/numbering.xml':
                    data = re.sub(rb'<w:nsid\b[^>]*/>', b'', data)
                output_zip.writestr(item, data)
        subdoc = rewritten.getvalue()
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        with patch.object(composer.random, 'random', wraps=composer.random.random) as rng:
            reference.render({'subdoc': reference.new_subdoc(io.BytesIO(subdoc))})
        self.assertEqual(rng.call_count, 0)
        reference_buffer = io.BytesIO()
        reference.save(reference_buffer)
        payload = {'context': {'kind': 'map', 'items': [
            ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(subdoc).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
        self.assertTrue(compare_docx(reference_buffer.getvalue(), output)['equivalent'])

    def test_native_typed_direct_numbering_custom_int_lookup(self) -> None:
        source = template('{{ subdoc }}')
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        stream = io.BytesIO()
        child.save(stream)
        subdoc = stream.getvalue()
        payload = {'context': {'kind': 'map', 'items': [
            ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(subdoc).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        events: list[Any] = []

        def custom_int(value: Any) -> int:
            events.append(value)
            return builtins.int(value)

        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        with patch.object(composer, 'int', custom_int, create=True), patch.object(composer.random, 'random', return_value=0.125):
            reference.render({'subdoc': reference.new_subdoc(io.BytesIO(subdoc))})
        expected = events.copy()
        events.clear()
        with patch.object(composer, 'int', custom_int, create=True), patch.object(composer.random, 'random', return_value=0.125):
            docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True)
        # The reference also converts its random nsid sample (0.125 * 10**8)
        # with `int`; the native nsid is not drawn (intentional divergence).
        self.assertEqual(expected[-1], 12_500_000.0)
        self.assertEqual(events, expected[:-1])

    def test_native_typed_direct_numbering_custom_int_exception_identity(self) -> None:
        source = template('{{ subdoc }}')
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        stream = io.BytesIO()
        child.save(stream)
        payload = {'context': {'kind': 'map', 'items': [
            ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(stream.getvalue()).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        marker = RuntimeError('integer callback marker')
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        with patch.object(composer, 'int', side_effect=marker, create=True) as reference_callback, \
             patch.object(composer.random, 'random', return_value=0.125) as reference_rng, self.assertRaises(RuntimeError) as reference_error:
            reference.new_subdoc(io.BytesIO(stream.getvalue()))
        self.assertIs(reference_error.exception, marker)
        self.assertEqual(reference_callback.call_count, 1)
        self.assertEqual(reference_rng.call_count, 0)
        with patch.object(composer, 'int', side_effect=marker, create=True) as callback, \
             patch.object(composer.random, 'random', return_value=0.125) as rng, self.assertRaises(RuntimeError) as raised:
            docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True)
        self.assertIs(raised.exception, marker)
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(rng.call_count, 0)

    def test_native_typed_direct_numbering_invalid_abstract_id_error(self) -> None:
        source = template('{{ subdoc }}')
        rewritten = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(source)) as source_zip, zipfile.ZipFile(rewritten, 'w') as output_zip:
            for item in source_zip.infolist():
                data = source_zip.read(item.filename)
                if item.filename == 'word/numbering.xml':
                    data = data.replace(b'w:abstractNumId="0"', b'w:abstractNumId="invalid"', 1)
                output_zip.writestr(item, data)
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        stream = io.BytesIO()
        child.save(stream)
        subdoc = stream.getvalue()
        reference = docxtpl.DocxTemplate(io.BytesIO(rewritten.getvalue()))
        with self.assertRaises(ValueError) as expected:
            reference.new_subdoc(io.BytesIO(subdoc))
        payload = {'context': {'kind': 'map', 'items': [
            ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(subdoc).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        with self.assertRaises(ValueError) as actual:
            docxtpl_native._extension().process_docx(rewritten.getvalue(), json.dumps(payload), False, True)
        self.assertEqual(str(actual.exception), str(expected.exception))

    def test_native_typed_direct_numbering_does_not_call_standard_random(self) -> None:
        source = template('{{ subdoc }}')
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        stream = io.BytesIO()
        child.save(stream)
        subdoc = stream.getvalue()
        payload = {'context': {'kind': 'map', 'items': [
            ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(subdoc).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        calls: list[str] = []
        original_profile = sys.getprofile()
        standard_random = random.random

        def profile(_frame: types.FrameType, event: str, arg: Any) -> None:
            if event == 'c_call' and arg is standard_random:
                calls.append('random')

        try:
            sys.setprofile(profile)
            reference = docxtpl.DocxTemplate(io.BytesIO(source))
            reference.render({'subdoc': reference.new_subdoc(io.BytesIO(subdoc))})
            self.assertEqual(calls, ['random'])
            calls.clear()
            docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True)
            self.assertEqual(calls, [])
        finally:
            sys.setprofile(original_profile)

    def test_native_typed_direct_numbering_does_not_reimport_loaded_rng(self) -> None:
        source = template('{{ subdoc }}')
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        stream = io.BytesIO()
        child.save(stream)
        payload = {'context': {'kind': 'map', 'items': [
            ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(stream.getvalue()).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        original_import = builtins.__import__
        imports: list[str] = []

        def trace_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name in ('random', '_random', 'docxcompose.composer'):
                imports.append(name)
            return original_import(name, *args, **kwargs)

        extension = docxtpl_native._extension()
        with patch.object(builtins, '__import__', side_effect=trace_import):
            extension.process_docx(source, json.dumps(payload), False, True)
        self.assertEqual(imports, [])

    def test_native_typed_direct_numbering_missing_definition(self) -> None:
        source = template('{{ subdoc }}')
        child = Document()
        paragraph = child.add_paragraph('missing numbering definition')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '999')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        stream = io.BytesIO()
        child.save(stream)
        subdoc = stream.getvalue()
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        with patch.object(composer.random, 'random', wraps=composer.random.random) as rng:
            reference.render({'subdoc': reference.new_subdoc(io.BytesIO(subdoc))})
        self.assertEqual(rng.call_count, 0)
        reference_buffer = io.BytesIO()
        reference.save(reference_buffer)
        payload = {'context': {'kind': 'map', 'items': [
            ['subdoc', {'kind': 'subdoc', 'data': base64.b64encode(subdoc).decode('ascii')}],
        ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
        self.assertTrue(compare_docx(reference_buffer.getvalue(), output)['equivalent'])

    def test_native_typed_image_dimensions_hyperlink_and_header(self) -> None:
        source = template('{{ image }}{{ image }}', header='{{ image }}')
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 3), (21, 42, 84)).save(image_stream, format='PNG')
        image_bytes = image_stream.getvalue()
        for width, height in ((None, None), (123457, None), (None, 98765), (123457, 98765)):
            for anchor in (None, 'https://example.invalid/image'):
                with self.subTest(width=width, height=height, anchor=anchor):
                    reference_template = docxtpl.DocxTemplate(io.BytesIO(source))
                    image = docxtpl.InlineImage(reference_template, io.BytesIO(image_bytes),
                        width=Emu(width) if width is not None else None,
                        height=Emu(height) if height is not None else None, anchor=anchor)
                    reference_template.render({'image': image})
                    reference_buffer = io.BytesIO()
                    reference_template.save(reference_buffer)
                    payload = {'context': {'kind': 'map', 'items': [
                        ['image', {'kind': 'image', 'data': base64.b64encode(image_bytes).decode('ascii'),
                                   'width': width, 'height': height, 'anchor': anchor}],
                    ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                    output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
                    reference = reference_buffer.getvalue()
                    self.assertTrue(compare_docx(reference, output)['equivalent'])

    def test_native_typed_image_with_autoescape(self) -> None:
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 3), (21, 42, 84)).save(image_stream, format='PNG')
        image_bytes = image_stream.getvalue()
        for expression in ('{{ image }} {{ text }}', '{% set x %}{{ image }}{% endset %}{{ x }} {{ text }}'):
            with self.subTest(expression=expression):
                source = template(expression, header='{{ image }}')
                reference_template = docxtpl.DocxTemplate(io.BytesIO(source))
                image = docxtpl.InlineImage(reference_template, io.BytesIO(image_bytes))
                context = {'image': image, 'text': '<unsafe>&"'}
                reference_template.render(context, jinja_env=jinja2.Environment(autoescape=True))
                reference_buffer = io.BytesIO()
                reference_template.save(reference_buffer)
                payload = {'context': {'kind': 'map', 'items': [
                    ['image', {'kind': 'image', 'data': base64.b64encode(image_bytes).decode('ascii')}],
                    ['text', {'kind': 'str', 'value': context['text']}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), True, True))
                self.assertTrue(compare_docx(reference_buffer.getvalue(), output)['equivalent'])

    def test_native_typed_image_title_and_description(self) -> None:
        source = template('{{ image }}', header='{{ image }}')
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 3), (21, 42, 84)).save(image_stream, format='PNG')
        image_bytes = image_stream.getvalue()
        title, description = 'Title & "mark"', "Description <with> 'quotes'"
        for anchor in (None, 'https://example.invalid/image'):
            with self.subTest(anchor=anchor):
                reference_template = docxtpl.DocxTemplate(io.BytesIO(source))
                # The pinned reference supports these fields; the installed release may not.
                inline_image = cast(Any, docxtpl.InlineImage)
                image = inline_image(reference_template, io.BytesIO(image_bytes),
                    anchor=anchor, title=title, descr=description)
                reference_template.render({'image': image})
                reference_buffer = io.BytesIO()
                reference_template.save(reference_buffer)
                payload = {'context': {'kind': 'map', 'items': [
                    ['image', {'kind': 'image', 'data': base64.b64encode(image_bytes).decode('ascii'),
                               'anchor': anchor, 'title': title, 'descr': description}],
                ]}, 'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
                output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
                self.assertTrue(compare_docx(reference_buffer.getvalue(), output)['equivalent'])

    def test_native_typed_loads_blank_xml_nodes_before_render(self) -> None:
        source = template('{{ value }}', header='header')
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(buffer, 'w') as output:
            for item in original.infolist():
                content = original.read(item.filename)
                if item.filename == 'word/document.xml':
                    content = content.replace(b'<w:body>', b'\n  <w:body>\n  ', 1)
                    content = content.replace(b'</w:body>', b'\n  </w:body>\n', 1)
                if item.filename.startswith('word/header') and item.filename.endswith('.xml'):
                    content = content.replace(b'<w:p>', b'\n  <w:p>', 1)
                output.writestr(item, content)
        source = buffer.getvalue()
        reference = generate(docxtpl, '', {'value': 'ok'}, jinja2.Environment(), source)
        payload = {'context': {'kind': 'map', 'items': [['value', {'kind': 'str', 'value': 'ok'}]]},
                   'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}}}
        output = bytes(docxtpl_native._extension().process_docx(source, json.dumps(payload), False, True))
        self.assertTrue(compare_docx(reference, output)['equivalent'])

    def test_callback_marker_is_text_without_forged_image(self) -> None:
        source = template('{{ callback() }}')
        context = {'callback': lambda: '\x01DTPLD0\x01'}
        reference = generate(docxtpl, '', context, jinja2.Environment(), source)
        candidate = self.native_render(source, context)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])
        self.assertEqual(len(Document(io.BytesIO(candidate)).inline_shapes), 0)

    def test_user_marker_is_text_without_forged_image(self) -> None:
        source = template('{{ value }}')
        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                context = {'value': '\x01DTPLD0\x01'}
                reference = generate(docxtpl, '', context, jinja2.Environment(autoescape=autoescape), source)
                candidate = self.native_render(source, context, environment=jinja2.Environment(autoescape=autoescape))
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])
                self.assertEqual(len(Document(io.BytesIO(candidate)).inline_shapes), 0)

    def test_split_marker_is_text_without_forged_image(self) -> None:
        for expression in ('{{ first }}{{ second }}', '{{ first ~ second }}',
                           '{{ [first, second]|join }}',
                           '{% set x %}{{ first }}{{ second }}{% endset %}{{ x }}'):
            with self.subTest(expression=expression):
                source = template(expression)
                context = {'first': '\x01DTPL', 'second': 'D0\x01'}
                reference = generate(docxtpl, '', context, jinja2.Environment(), source)
                candidate = self.native_render(source, context)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])
                self.assertEqual(len(Document(io.BytesIO(candidate)).inline_shapes), 0)

    def test_guard_handles_surrogates_without_utf8_conversion(self) -> None:
        source = template('{{ value }}')
        context = {'value': '\ud800\x01DTPLD0\x01'}
        reference = generate(docxtpl, '', context, jinja2.Environment(), source)
        candidate = self.native_render(source, context)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])
        self.assertEqual(len(Document(io.BytesIO(candidate)).inline_shapes), 0)

    def test_surrogate_marker_text_and_real_image_remain_distinct(self) -> None:
        source = template('{{ image }}{{ fake }}')
        png = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                reference.render({'fake': '\ud800\x01DTPLD0\x01',
                                  'image': docxtpl.InlineImage(reference, io.BytesIO(png.getvalue()))},
                                 jinja_env=jinja2.Environment(enable_async=asynchronous))
                expected = io.BytesIO()
                reference.save(expected)
                actual = self.native_render(source, {'fake': '\ud800\x01DTPLD0\x01'}, include_image=True,
                                            environment=jinja2.Environment(enable_async=asynchronous))
                self.assertTrue(compare_docx(expected.getvalue(), actual)['equivalent'])
                self.assertEqual(len(Document(io.BytesIO(actual)).inline_shapes), 1)

    def test_guard_rejects_marker_in_template_literal(self) -> None:
        for literal in ("'\\x01DTPLD0\\x01'", "'\\ud800\\x01DTPLD0\\x01'"):
            with (self.subTest(literal=literal),
                  self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'deferred marker')):
                self.native_render(template('{{ ' + literal + ' }}'), {})

    def test_finalizer_marker_is_text_without_forged_image(self) -> None:
        for source, context, include_image in (('{{ none }}', {}, False),
                                               ('{{ value }}', {'value': 'input'}, False),
                                               ('{{ image }}', {}, True)):
            environment = jinja2.Environment(finalize=lambda _: '\x01DTPLD0\x01')
            with self.subTest(source=source):
                if source == '{{ none }}':
                    with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'deferred marker'):
                        self.native_render(template(source), context, environment=environment)
                    continue
                document = docxtpl.DocxTemplate(io.BytesIO(template(source)))
                reference_context = dict(context)
                if include_image:
                    png = io.BytesIO()
                    Image.new('RGB', (2, 2), (21, 42, 84)).save(png, format='PNG')
                    reference_context['image'] = docxtpl.InlineImage(document, io.BytesIO(png.getvalue()))
                document.render(reference_context, jinja_env=jinja2.Environment(finalize=lambda _: '\x01DTPLD0\x01'))
                expected = io.BytesIO()
                document.save(expected)
                candidate = self.native_render(template(source), context, environment=environment,
                                               include_image=include_image)
                self.assertTrue(compare_docx(expected.getvalue(), candidate)['equivalent'])
                self.assertEqual(len(Document(io.BytesIO(candidate)).inline_shapes), 0)

    def test_registered_image_still_materializes(self) -> None:
        output = self.native_render(template('{{ image }}'), {}, include_image=True)
        self.assertEqual(len(Document(io.BytesIO(output)).inline_shapes), 1)

    def test_standalone_session_does_not_reserve_control_character(self) -> None:
        value = '\x01DTPLD0\x01'
        session = docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(jinja2.Environment(), False), {'value': value},
        )
        self.assertEqual(session.render('<string>', '{{ value }}'), value)

    def test_unexecuted_constant_does_not_trigger_marker_guard(self) -> None:
        source = template("{% if enabled %}{{ '\\x01DTPLD0\\x01' }}{% endif %}ready")
        context = {'enabled': False}
        events: list[str] = []

        def finalize(value: object) -> object:
            events.append(repr(value))
            return value

        reference = Document(io.BytesIO(generate(
            docxtpl, '', context, jinja2.Environment(finalize=finalize), source,
        )))
        expected_events = events.copy()
        self.assertEqual(expected_events, [repr('\x01DTPLD0\x01')])
        events.clear()
        candidate = Document(io.BytesIO(self.native_render(
            source, context, environment=jinja2.Environment(finalize=finalize),
        )))
        self.assertEqual(candidate.paragraphs[0].text, reference.paragraphs[0].text)
        self.assertEqual(len(candidate.inline_shapes), 0)
        self.assertEqual(events, expected_events)

    def test_constant_guard_runs_after_preceding_callback(self) -> None:
        source = template("{{ callback() }}{{ '\\x01DTPLD0\\x01' }}")
        events: list[str] = []

        def callback() -> str:
            events.append('callback')
            return ''

        context = {'callback': callback}
        generate(docxtpl, '', context, jinja2.Environment(), source)
        expected_events = events.copy()
        events.clear()
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'deferred marker'):
            self.native_render(source, context)
        self.assertEqual(events, expected_events)

    def test_guard_rejects_raw_output_from_included_template(self) -> None:
        environment = jinja2.Environment(loader=jinja2.DictLoader({'injected': '\x01DTPLD0\x01'}))
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'deferred marker'):
            self.native_render(template("{% include 'injected' %}"), {}, environment=environment)

    def test_unexecuted_include_does_not_trigger_marker_guard(self) -> None:
        environment = jinja2.Environment(loader=jinja2.DictLoader({'injected': '\x01DTPLD0\x01'}))
        source = template("{% if enabled %}{% include 'injected' %}{% endif %}ready")
        context = {'enabled': False}
        reference = generate(docxtpl, '', context, environment, source)
        output = self.native_render(source, context, environment=environment)
        document = Document(io.BytesIO(output))
        self.assertEqual(document.paragraphs[0].text, 'ready')
        self.assertEqual(len(document.inline_shapes), 0)
        self.assertTrue(compare_docx(reference, output)['equivalent'])

    def test_callback_can_inspect_captured_reserved_text(self) -> None:
        source = template('{% set x %}{{ value }}{% endset %}{{ callback(x) }}')
        seen: list[str] = []

        def callback(value: str) -> str:
            seen.append(value)
            return 'observed'

        context = {'value': '\x01DTPLD0\x01', 'callback': callback}
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                environment = jinja2.Environment(enable_async=asynchronous)
                seen.clear()
                reference = generate(docxtpl, '', context, environment, source)
                expected = seen.copy()
                self.assertEqual(expected, ['\x01DTPLD0\x01'])
                seen.clear()
                candidate = self.native_render(source, context, environment=environment)
                self.assertEqual(seen, expected)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_async_callback_can_await_after_capture(self) -> None:
        source = template('{% set x %}{{ value }}{% endset %}{{ callback(x) }}')
        for value in ('plain', '\x01DTPLD0\x01'):
            with self.subTest(value=repr(value)):
                seen: list[tuple[str, str]] = []

                async def callback(argument: str, events: list[tuple[str, str]] = seen) -> str:
                    events.append(('before', argument))
                    await asyncio.sleep(0)
                    events.append(('after', argument))
                    return 'observed'

                context = {'value': value, 'callback': callback}
                reference = generate(docxtpl, '', context, jinja2.Environment(enable_async=True), source)
                expected = seen.copy()
                self.assertEqual(expected, [('before', value), ('after', value)])
                seen.clear()
                candidate = self.native_render(source, context, environment=jinja2.Environment(enable_async=True))
                self.assertEqual(seen, expected)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_nested_captured_text_preserves_callback_type_and_identity(self) -> None:
        source = template('{% set a %}{% set b %}{{ value }}{% endset %}{{ b }}{% endset %}'
                          '{{ callback(a, a) }}')
        seen: list[tuple[bool, bool, str]] = []

        def callback(first: object, second: object) -> str:
            seen.append((type(first) is str, first is second, str(first)))
            return 'observed'

        context = {'value': '\x01DTPLD0\x01', 'callback': callback}
        reference = generate(docxtpl, '', context, jinja2.Environment(), source)
        expected = seen.copy()
        self.assertEqual(expected, [(True, True, '\x01DTPLD0\x01')])
        seen.clear()
        candidate = self.native_render(source, context)
        self.assertEqual(seen, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_autoescaped_capture_preserves_markup_type(self) -> None:
        source = template('{% set x %}{{ value }}{% endset %}{{ callback(x) }}')
        seen: list[tuple[bool, str]] = []

        def callback(value: object) -> str:
            seen.append((type(value) is Markup, str(value)))
            return 'observed'

        context = {'value': '\x01DTPLD0\x01', 'callback': callback}
        reference = generate(docxtpl, '', context, jinja2.Environment(autoescape=True), source)
        expected = seen.copy()
        self.assertEqual(expected, [(True, '\x01DTPLD0\x01')])
        seen.clear()
        candidate = self.native_render(source, context, environment=jinja2.Environment(autoescape=True))
        self.assertEqual(seen, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_macro_capture_can_be_inspected_without_emitting_marker(self) -> None:
        source = template('{% macro item() %}{{ value }}{% endmacro %}{{ callback(item()) }}')
        seen: list[str] = []

        def callback(value: str) -> str:
            seen.append(value)
            return 'observed'

        context = {'value': '\x01DTPLD0\x01', 'callback': callback}
        reference = generate(docxtpl, '', context, jinja2.Environment(), source)
        expected = seen.copy()
        self.assertEqual(expected, ['\x01DTPLD0\x01'])
        seen.clear()
        candidate = self.native_render(source, context)
        self.assertEqual(seen, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_included_raw_capture_can_be_inspected_without_emitting_marker(self) -> None:
        source = template("{% set x %}{% include 'injected' %}{% endset %}{{ callback(x) }}")
        seen: list[str] = []

        def callback(value: str) -> str:
            seen.append(value)
            return 'observed'

        environment = jinja2.Environment(loader=jinja2.DictLoader({'injected': '\x01DTPLD0\x01'}))
        reference = generate(docxtpl, '', {'callback': callback}, environment, source)
        expected = seen.copy()
        self.assertEqual(expected, ['\x01DTPLD0\x01'])
        seen.clear()
        candidate = self.native_render(source, {'callback': callback}, environment=environment)
        self.assertEqual(seen, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_captured_constant_is_safe_to_inspect_and_emit(self) -> None:
        prefix = "{% set x %}{{ '\\x01DTPLD0\\x01' }}{% endset %}"
        seen: list[str] = []

        def callback(value: str) -> str:
            seen.append(value)
            return 'observed'

        source = template(prefix + '{{ callback(x) }}')
        reference = generate(docxtpl, '', {'callback': callback}, jinja2.Environment(), source)
        expected = seen.copy()
        seen.clear()
        candidate = self.native_render(source, {'callback': callback})
        self.assertEqual(seen, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])
        emitted = template(prefix + '{{ x }}')
        reference_emitted = generate(docxtpl, '', {}, jinja2.Environment(), emitted)
        candidate_emitted = self.native_render(emitted, {})
        self.assertTrue(compare_docx(reference_emitted, candidate_emitted)['equivalent'])
        self.assertEqual(len(Document(io.BytesIO(candidate_emitted)).inline_shapes), 0)

    def test_callback_returning_capture_does_not_forge_marker(self) -> None:
        source = template('{% set x %}{{ value }}{% endset %}{{ callback(x) }}')
        seen: list[str] = []

        def callback(value: str) -> str:
            seen.append(value)
            return value

        context = {'value': '\x01DTPLD0\x01', 'callback': callback}
        reference = generate(docxtpl, '', context, jinja2.Environment(), source)
        self.assertEqual(seen, ['\x01DTPLD0\x01'])
        seen.clear()
        candidate = self.native_render(source, context)
        self.assertEqual(seen, ['\x01DTPLD0\x01'])
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])
        self.assertEqual(len(Document(io.BytesIO(candidate)).inline_shapes), 0)

    def test_image_output_in_capture_still_materializes(self) -> None:
        source = template('{% set x %}{{ image }}{% endset %}{{ x }}')
        output = self.native_render(source, {}, include_image=True)
        self.assertEqual(len(Document(io.BytesIO(output)).inline_shapes), 1)


if __name__ == '__main__':
    unittest.main()
