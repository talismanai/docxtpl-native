"""Undefined checks at their actual use in the live Python bridge."""

import asyncio
import builtins
import gc
import importlib
import io
import json
import sys
import unittest
from typing import Any, Self
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from markupsafe import Markup
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template
from PIL import Image
from PIL.TiffImagePlugin import IFDRational


class LazyUndefinedTests(unittest.TestCase):
    def test_dynamic_reference_image_does_not_reimport_loaded_modules(self) -> None:
        image_stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(image_stream, format='PNG')
        source = template('{{ make_image() }}')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                owner = docxtpl_native.DocxTemplate(io.BytesIO(source))
                extension = docxtpl_native._extension()
                config = BridgeConfig(jinja2.Environment(enable_async=asynchronous), False)
                payload = json.dumps({
                    'specials': [],
                    'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                })
                imports: list[str] = []
                original = builtins.__import__

                def observe(name: str, *args: Any, _imports: list[str] = imports,
                            _original: Any = original, **kwargs: Any) -> Any:
                    if name in ('docxtpl_native', 'builtins'):
                        _imports.append(name)
                    return _original(name, *args, **kwargs)

                def make_image(owner: Any = owner) -> object:
                    return docxtpl_native.InlineImage(owner, io.BytesIO(image_stream.getvalue()))

                with patch.object(builtins, '__import__', side_effect=observe):
                    output, _ = extension.process_python_docx(
                        source, {'make_image': make_image}, config, payload, False, owner,
                    )
                self.assertEqual(len(importlib.import_module('docx').Document(io.BytesIO(bytes(output))).inline_shapes), 1)
                self.assertEqual(imports, [])

    def test_simple_source_selection_preserves_replaced_callback_signature(self) -> None:
        from docxtpl_native import simple_frontend

        context = {'value': 'x'}
        events: list[bool] = []

        def custom(value: object) -> frozenset[str]:
            events.append(value is context)
            return frozenset({'value'})

        configuration = BridgeConfig(None, False)
        with patch.object(simple_frontend, 'context_names', custom):
            configuration.enable_simple_sources(context)
        self.assertEqual(events, [True])
        self.assertEqual(configuration._simple_names, frozenset({'value'}))

    def test_simple_source_selection_preserves_in_place_callback_change(self) -> None:
        from docxtpl_native import simple_frontend

        def changed(context: object, *, compare_metaclasses: bool = True) -> frozenset[str]:
            return frozenset({'default' if compare_metaclasses else 'explicit'})

        configuration = BridgeConfig(None, False)
        original = simple_frontend.context_names.__code__
        try:
            simple_frontend.context_names.__code__ = changed.__code__
            configuration.enable_simple_sources({'value': 'x'})
        finally:
            simple_frontend.context_names.__code__ = original
        self.assertEqual(configuration._simple_names, frozenset({'default'}))

    def test_simple_source_selection_does_not_compare_user_metaclass(self) -> None:
        class Meta(type):
            def __eq__(cls, other: object) -> bool:
                raise ValueError('unexpected class equality')

            __hash__ = type.__hash__

        class Value(metaclass=Meta):
            pass

        configuration = BridgeConfig(None, False)
        configuration.enable_simple_sources({'value': Value()})
        self.assertIsNone(configuration._simple_names)

    def test_file_subdoc_context_does_not_compare_user_metaclass(self) -> None:
        def run(engine: Any) -> tuple[bytes, list[str]]:
            events: list[str] = []

            class Meta(type):
                def __eq__(cls, other: object) -> bool:
                    events.append('class equality')
                    raise ValueError('unexpected class equality')

                __hash__ = type.__hash__

            class Value(metaclass=Meta):
                pass

            value = Value()
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('child'))

            def callback(argument: object) -> str:
                events.append('callback:' + str(argument is value))
                return 'ok'

            # The value is last so the native depth-first classifier reaches it
            # before the callback when checking whether the context is simple.
            context = {'child': child, 'callback': callback, 'value': value}
            return generate(engine, '{{ callback(value) }}', context), events

        reference, expected_events = run(docxtpl)
        self.assertEqual(expected_events, ['callback:True'])
        candidate, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_routing_does_not_compare_user_metaclass(self) -> None:
        def run(engine: Any) -> tuple[bytes, list[str]]:
            events: list[str] = []

            class Meta(type):
                def __eq__(cls, other: object) -> bool:
                    events.append('class equality')
                    raise ValueError('unexpected class equality')

                __hash__ = type.__hash__

            class Value(metaclass=Meta):
                pass

            value = Value()

            def callback(argument: object) -> str:
                events.append('callback:' + str(argument is value))
                return 'ok'

            output = generate(engine, '{{ callback(value) }}', {'value': value, 'callback': callback})
            return output, events

        reference, expected_events = run(docxtpl)
        self.assertEqual(expected_events, ['callback:True'])
        candidate, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_file_composition_does_not_probe_callback_argument_class(self) -> None:
        class Value:
            def __getattribute__(self, name: str) -> Any:
                if name == '__class__':
                    raise ValueError('unexpected class lookup')
                return object.__getattribute__(self, name)

        def run(native: bool) -> tuple[bytes, list[bool]]:
            value = Value()
            calls: list[bool] = []

            def callback(argument: object) -> str:
                calls.append(argument is value)
                return 'ok'

            context = {'value': value, 'callback': callback}
            environment = jinja2.Environment()
            source = template('{{ callback(value) }}')
            if not native:
                return generate(docxtpl, '', context, environment, source), calls
            payload = json.dumps({
                'specials': [],
                'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
            })
            output, _ = docxtpl_native._extension().process_python_docx(
                source, context, BridgeConfig(environment, False), payload, False,
            )
            return bytes(output), calls

        reference, expected_calls = run(False)
        self.assertEqual(expected_calls, [True])
        candidate, actual_calls = run(True)
        self.assertEqual(actual_calls, expected_calls)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_file_composition_passes_unmaterialized_image_to_callback(self) -> None:
        image = docxtpl_native.InlineImage(None, io.BytesIO(b'image'))

        def run(native: bool) -> tuple[bytes, list[bool]]:
            calls: list[bool] = []

            def callback(argument: object) -> str:
                calls.append(argument is image)
                return 'ok'

            context = {'image': image, 'callback': callback}
            environment = jinja2.Environment()
            source = template('{{ callback(image) }}')
            if not native:
                return generate(docxtpl, '', context, environment, source), calls
            payload = json.dumps({
                'specials': [],
                'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
            })
            output, _ = docxtpl_native._extension().process_python_docx(
                source, context, BridgeConfig(environment, False), payload, False,
            )
            return bytes(output), calls

        reference, expected_calls = run(False)
        self.assertEqual(expected_calls, [True])
        candidate, actual_calls = run(True)
        self.assertEqual(actual_calls, expected_calls)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_file_composition_unregistered_native_image_callback_protocols(self) -> None:
        expressions = ('{{ callback(image) }}', '{{ callback(item=image) }}',
                       '{{ callback([image]) }}', '{{ callback(image) is sameas(image) }}')
        for asynchronous in (False, True):
            for expression in expressions:
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    def run(native: bool) -> tuple[bytes, list[bool]]:
                        image = docxtpl_native.InlineImage(None, io.BytesIO(b'not read'))
                        calls: list[bool] = []

                        def callback(item: Any) -> object:
                            value = item[0] if isinstance(item, list) else item
                            calls.append(value is image)
                            return value if expression.endswith('is sameas(image) }}') else 'ok'

                        context = {'image': image, 'callback': callback}
                        source = template(expression)
                        environment = jinja2.Environment(enable_async=asynchronous)
                        if not native:
                            return generate(docxtpl, '', context, environment, source), calls
                        payload = json.dumps({
                            'specials': [],
                            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                        })
                        output, _ = docxtpl_native._extension().process_python_docx(
                            source, context, BridgeConfig(environment, False), payload, False,
                        )
                        return bytes(output), calls

                    expected, reference_calls = run(False)
                    self.assertEqual(reference_calls, [True])
                    actual, candidate_calls = run(True)
                    self.assertEqual(candidate_calls, reference_calls)
                    self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_file_composition_unregistered_image_survives_async_callback(self) -> None:
        def run(native: bool) -> tuple[bytes, list[bool]]:
            image = docxtpl_native.InlineImage(None, io.BytesIO(b'not read'))
            calls: list[bool] = []

            async def callback(value: object) -> str:
                calls.append(value is image)
                await asyncio.sleep(0)
                calls.append(value is image)
                return 'ok'

            context = {'image': image, 'callback': callback}
            source = template('{{ callback(image) }}')
            environment = jinja2.Environment(enable_async=True)
            if not native:
                return generate(docxtpl, '', context, environment, source), calls
            payload = json.dumps({
                'specials': [],
                'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
            })
            output, _ = docxtpl_native._extension().process_python_docx(
                source, context, BridgeConfig(environment, False), payload, False,
            )
            return bytes(output), calls

        expected, reference_calls = run(False)
        actual, candidate_calls = run(True)
        self.assertEqual(reference_calls, [True, True])
        self.assertEqual(candidate_calls, reference_calls)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_file_composition_does_not_materialize_image_instance_dictionary(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool) -> tuple[bytes, list[bool]]:
                    image = docxtpl_native.InlineImage(None, io.BytesIO(b'not read'))
                    observed: list[bool] = []

                    def callback(value: object) -> str:
                        observed.append(any(type(item) is dict for item in gc.get_referents(value)))
                        return 'ok'

                    context = {'image': image, 'callback': callback}
                    source = template('{{ callback(image) }}')
                    environment = jinja2.Environment(enable_async=asynchronous)
                    if not native:
                        return generate(docxtpl, '', context, environment, source), observed
                    payload = json.dumps({
                        'specials': [],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    output, _ = docxtpl_native._extension().process_python_docx(
                        source, context, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(output), observed

                expected, reference = run(False)
                actual, candidate = run(True)
                self.assertEqual(candidate, reference)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_file_composition_accepts_existing_plain_image_dictionary(self) -> None:
        image = docxtpl_native.InlineImage(None, io.BytesIO(b'not read'))
        dictionary = image.__dict__
        seen: list[bool] = []

        def callback(value: object) -> str:
            seen.append(value is image and image.__dict__ is dictionary)
            return 'ok'

        source = template('{{ callback(image) }}')
        context = {'image': image, 'callback': callback}
        expected = generate(docxtpl, '', context, jinja2.Environment(), source)
        payload = json.dumps({
            'specials': [],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        output, _ = docxtpl_native._extension().process_python_docx(
            source, context, BridgeConfig(jinja2.Environment(), False), payload, False,
        )
        self.assertEqual(seen, [True, True])
        self.assertTrue(compare_docx(expected, bytes(output))['equivalent'])

    def test_file_composition_guard_does_not_compare_user_dictionary_keys(self) -> None:
        events: list[str] = []

        class Key(str):
            __hash__ = str.__hash__

            def __eq__(self, other: object) -> bool:
                events.append('compare')
                return False

        image = docxtpl_native.InlineImage(None, io.BytesIO(b'not read'))
        image.__dict__[Key('_insert_image')] = object()
        events.clear()
        payload = json.dumps({
            'specials': [],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
            docxtpl_native._extension().process_python_docx(
                template('{{ callback(image) }}'), {'image': image, 'callback': lambda _: 'unused'},
                BridgeConfig(jinja2.Environment(), False), payload, False,
            )
        self.assertEqual(events, [])

    def test_file_composition_rejects_unregistered_image_when_callback_stringifies(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                image = docxtpl_native.InlineImage(None, io.BytesIO(b'not read'))
                events: list[bool] = []

                def callback(value: object) -> str:
                    events.append(value is image)
                    return str(value)

                payload = json.dumps({
                    'specials': [],
                    'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                })
                with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
                    docxtpl_native._extension().process_python_docx(
                        template('{{ callback(image) }}'), {'image': image, 'callback': callback},
                        BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), payload, False,
                    )
                self.assertEqual(events, [True])

    def test_file_composition_rejects_unregistered_native_image_on_output(self) -> None:
        for asynchronous in (False, True):
            for autoescape in (False, True):
                for expression in ('{{ image }}', '{{ [image]|join }}', '{{ image|safe }}'):
                    with self.subTest(asynchronous=asynchronous, autoescape=autoescape, expression=expression):
                        image = docxtpl_native.InlineImage(None, io.BytesIO(b'not read'))
                        payload = json.dumps({
                            'specials': [],
                            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                        })
                        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
                            docxtpl_native._extension().process_python_docx(
                                template(expression), {'image': image},
                                BridgeConfig(jinja2.Environment(enable_async=asynchronous,
                                                                 autoescape=autoescape), False),
                                payload, autoescape,
                            )

    def test_file_composition_keeps_guard_for_modified_native_image_protocol(self) -> None:
        for mode in ('class', 'instance', 'instance_html'):
            with self.subTest(mode=mode):
                image = docxtpl_native.InlineImage(None, io.BytesIO(b'not read'))
                events: list[bool] = []

                def callback(value: object) -> str:
                    events.append(value is image)
                    return 'unused'

                def replacement(*args: object) -> str:
                    raise AssertionError('modified insertion must not run')

                target = docxtpl_native.InlineImage if mode == 'class' else image
                name = '__html__' if mode == 'instance_html' else '_insert_image'
                payload = json.dumps({
                    'specials': [],
                    'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                })
                with patch.object(target, name, replacement):
                    with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
                        docxtpl_native._extension().process_python_docx(
                            template('{{ callback(image) }}'), {'image': image, 'callback': callback},
                            BridgeConfig(jinja2.Environment(), False), payload, False,
                        )
                self.assertEqual(events, [])

    def test_file_composition_registered_image_preserves_callback_identity(self) -> None:
        image = docxtpl_native.InlineImage(None, io.BytesIO(b'image'))

        def run(native: bool) -> tuple[bytes, list[bool]]:
            calls: list[bool] = []

            def callback(argument: object) -> str:
                calls.append(argument is image)
                return 'ok'

            context = {'image': image, 'callback': callback}
            environment = jinja2.Environment()
            source = template('{{ callback(image) }}')
            if not native:
                return generate(docxtpl, '', context, environment, source), calls
            payload = json.dumps({
                'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
            })
            output, _ = docxtpl_native._extension().process_python_docx(
                source, context, BridgeConfig(environment, False), payload, False,
            )
            return bytes(output), calls

        reference, expected_calls = run(False)
        self.assertEqual(expected_calls, [True])
        candidate, actual_calls = run(True)
        self.assertEqual(actual_calls, expected_calls)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_file_subdoc_preserves_image_callback_identity(self) -> None:
        def run(engine: Any) -> tuple[bytes, list[bool]]:
            image = docxtpl_native.InlineImage(None, io.BytesIO(b'image'))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('child'))
            calls: list[bool] = []

            def callback(argument: object) -> str:
                calls.append(argument is image)
                return 'same' if argument is image else 'different'

            output = generate(engine, '', {'child': child, 'image': image, 'callback': callback},
                              jinja2.Environment(), template('{{ callback(image) }}'))
            return output, calls

        reference, expected_calls = run(docxtpl)
        self.assertEqual(expected_calls, [True])
        candidate, actual_calls = run(docxtpl_native)
        self.assertEqual(actual_calls, expected_calls)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_registered_image_identity_in_callback_arguments(self) -> None:
        image = docxtpl_native.InlineImage(None, io.BytesIO(b'image'))
        expressions = ('{{ callback(image) }}', '{{ callback(item=image) }}',
                       '{{ callback([image, image]) }}', '{{ callback({"key": image}) }}',
                       '{{ callback(image) is sameas(image) }}')
        for expression in expressions:
            with self.subTest(expression=expression):
                def run(native: bool, expr: str = expression) -> tuple[bytes, list[bool]]:
                    calls: list[bool] = []

                    def callback(item: Any) -> object:
                        if isinstance(item, list):
                            calls.append(len(item) == 2 and all(value is image for value in item))
                        elif isinstance(item, dict):
                            calls.append(item['key'] is image)
                        else:
                            calls.append(item is image)
                        return item if expr.endswith('is sameas(image) }}') else 'ok'

                    context = {'image': image, 'callback': callback}
                    source = template(expr)
                    environment = jinja2.Environment()
                    if not native:
                        return generate(docxtpl, '', context, environment, source), calls
                    payload = json.dumps({
                        'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    output, _ = docxtpl_native._extension().process_python_docx(
                        source, context, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(output), calls

                reference, expected_calls = run(False)
                candidate, actual_calls = run(True)
                self.assertEqual(expected_calls, [True])
                self.assertEqual(actual_calls, expected_calls)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_registered_image_identity_in_async_callback(self) -> None:
        image = docxtpl_native.InlineImage(None, io.BytesIO(b'image'))

        def run(native: bool) -> tuple[bytes, list[bool]]:
            calls: list[bool] = []

            async def callback(item: object) -> str:
                calls.append(item is image)
                return 'ok'

            context = {'image': image, 'callback': callback}
            source = template('{{ callback(image) }}')
            environment = jinja2.Environment(enable_async=True)
            if not native:
                return generate(docxtpl, '', context, environment, source), calls
            payload = json.dumps({
                'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
            })
            output, _ = docxtpl_native._extension().process_python_docx(
                source, context, BridgeConfig(environment, False), payload, False,
            )
            return bytes(output), calls

        reference, expected_calls = run(False)
        candidate, actual_calls = run(True)
        self.assertEqual(expected_calls, [True])
        self.assertEqual(actual_calls, expected_calls)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_registered_subdocument_identity_in_callback(self) -> None:
        child = object.__new__(docxtpl_native.Subdoc)
        child.tpl = None
        child.source = io.BytesIO(template('child'))

        def run(native: bool) -> tuple[bytes, list[bool]]:
            calls: list[bool] = []

            def callback(item: object) -> str:
                calls.append(item is child)
                return 'ok'

            context = {'child': child, 'callback': callback}
            source = template('{{ callback(child) }}')
            environment = jinja2.Environment()
            if not native:
                return generate(docxtpl, '', context, environment, source), calls
            payload = json.dumps({
                'specials': [{'identity': id(child), 'value': docxtpl_native._value(child)}],
                'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
            })
            output, _ = docxtpl_native._extension().process_python_docx(
                source, context, BridgeConfig(environment, False), payload, False,
            )
            return bytes(output), calls

        reference, expected_calls = run(False)
        candidate, actual_calls = run(True)
        self.assertEqual(expected_calls, [True])
        self.assertEqual(actual_calls, expected_calls)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_callback_returning_registered_image_is_rendered(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        reference_image = docxtpl.InlineImage(reference, io.BytesIO(image_bytes))
        reference.render({'image': reference_image, 'callback': lambda item: item})
        expected = io.BytesIO()
        reference.save(expected)

        image = docxtpl_native.InlineImage(None, io.BytesIO(image_bytes))
        payload = json.dumps({
            'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        output, _ = docxtpl_native._extension().process_python_docx(
            source, {'image': image, 'callback': lambda item: item},
            BridgeConfig(jinja2.Environment(), False), payload, False,
        )
        self.assertTrue(compare_docx(expected.getvalue(), bytes(output))['equivalent'])

    def test_public_callback_can_stringify_registered_image(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> bytes:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, io.BytesIO(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            def callback(item: object) -> str:
                return str(item)

            document.render({'image': image, 'child': child, 'callback': callback})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_callback_stringify_observes_changed_width(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> bytes:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, io.BytesIO(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            def callback(item: Any) -> str:
                item.width = 123456
                return str(item)

            document.render({'image': image, 'child': child, 'callback': callback})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_callback_stringify_observes_changed_descriptor(self) -> None:
        streams = [io.BytesIO(), io.BytesIO()]
        Image.new('RGB', (2, 2), (21, 42, 84)).save(streams[0], format='PNG')
        Image.new('RGB', (2, 2), (84, 42, 21)).save(streams[1], format='PNG')
        images = [stream.getvalue() for stream in streams]
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> bytes:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, io.BytesIO(images[0]))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            def callback(item: Any) -> str:
                item.image_descriptor = io.BytesIO(images[1])
                return str(item)

            document.render({'image': image, 'child': child, 'callback': callback})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_callback_stringify_preserves_stream_position_effect(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> tuple[bytes, list[int]]:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, io.BytesIO(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            positions: list[int] = []

            def callback(item: Any) -> str:
                xml = str(item)
                positions.append(item.image_descriptor.tell())
                return xml

            document.render({'image': image, 'child': child, 'callback': callback})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), positions

        reference, expected_positions = run(docxtpl)
        candidate, actual_positions = run(docxtpl_native)
        self.assertEqual(actual_positions, expected_positions)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_callback_can_create_and_render_image(self) -> None:
        image_stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(image_stream, format='PNG')
        for as_text in (False, True):
            for asynchronous in (False, True):
                with self.subTest(as_text=as_text, asynchronous=asynchronous):
                    def run(engine: Any, as_text: bool = as_text,
                            asynchronous: bool = asynchronous) -> tuple[bytes, list[str]]:
                        document = engine.DocxTemplate(io.BytesIO(template('{{ make_image() }}')))
                        child = object.__new__(docxtpl_native.Subdoc)
                        child.tpl = None
                        child.source = io.BytesIO(template('unused'))
                        events: list[str] = []

                        class TracedStream(io.BytesIO):
                            def read(self, size: int | None = -1, /) -> bytes:
                                events.append('read')
                                return super().read(size)

                        def make_image() -> object:
                            image = engine.InlineImage(document, TracedStream(image_stream.getvalue()))
                            events.append('created')
                            return str(image) if as_text else image

                        async def make_async_image() -> object:
                            await asyncio.sleep(0)
                            return make_image()

                        callback = make_async_image if asynchronous else make_image
                        document.render({'make_image': callback, 'child': child},
                                        jinja2.Environment(enable_async=asynchronous))
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue(), events

                    expected, expected_events = run(docxtpl)
                    actual, actual_events = run(docxtpl_native)
                    self.assertEqual(actual_events, expected_events)
                    self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_callback_can_return_new_reference_image(self) -> None:
        image_stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(image_stream, format='PNG')

        def run(engine: Any) -> tuple[bytes, int]:
            calls = 0
            target = engine.InlineImage._insert_image.__code__

            def profile(frame: Any, event: str, argument: Any) -> None:
                nonlocal calls
                if event == 'call' and frame.f_code is target:
                    calls += 1

            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image() }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                document.render({'make_image': lambda: engine.InlineImage(
                    document, io.BytesIO(image_stream.getvalue())), 'child': child})
            finally:
                sys.setprofile(previous)
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), calls

        expected, expected_calls = run(docxtpl)
        actual, actual_calls = run(docxtpl_native)
        self.assertGreater(expected_calls, 0)
        self.assertEqual(actual_calls, 0)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_finalizer_can_create_image(self) -> None:
        image_stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(image_stream, format='PNG')

        def run(engine: Any) -> bytes:
            document = engine.DocxTemplate(io.BytesIO(template('{{ value }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            marker = object()

            def finalize(value: object) -> object:
                if value is marker:
                    return engine.InlineImage(document, io.BytesIO(image_stream.getvalue()))
                return value

            document.render({'value': marker, 'child': child}, jinja2.Environment(finalize=finalize))
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_callback_can_return_multiple_temporary_images(self) -> None:
        blobs: list[bytes] = []
        for color in (21, 42, 84, 126, 168, 210):
            stream = io.BytesIO()
            Image.new('RGB', (3, 2), (color, 42, 84)).save(stream, format='PNG')
            blobs.append(stream.getvalue())

        def run(engine: Any) -> bytes:
            source = template('{% for n in range(6) %}{{ make_image(n) }}{% endfor %}')
            document = engine.DocxTemplate(io.BytesIO(source))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            def make_image(index: int) -> object:
                return engine.InlineImage(document, io.BytesIO(blobs[index]))

            document.render({'make_image': make_image, 'child': child})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_dynamic_image_compositions(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(stream, format='PNG')
        for expression in ("{{ 'X' ~ make_image() }}", '{{ make_image()|string }}',
                           '{{ make_image()|safe }}', '{{ [make_image()]|join }}',
                           '{% set x %}{{ make_image() }}{% endset %}{{ x }}'):
            for subclass in (False, True):
                with self.subTest(expression=expression, subclass=subclass):
                    def run(engine: Any, expression: str = expression, subclass: bool = subclass) -> bytes:
                        document = engine.DocxTemplate(io.BytesIO(template(expression)))
                        child = object.__new__(docxtpl_native.Subdoc)
                        child.tpl = None
                        child.source = io.BytesIO(template('unused'))
                        image_type = type('DerivedImage', (engine.InlineImage,), {}) if subclass else engine.InlineImage
                        document.render({'make_image': lambda: image_type(
                            document, io.BytesIO(stream.getvalue())), 'child': child})
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue()

                    self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_join_dynamic_reference_image_preserves_string_conversion_order(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(stream, format='PNG')

        def run(engine: Any) -> tuple[bytes, list[str]]:
            events: list[str] = []
            document = engine.DocxTemplate(io.BytesIO(template('{{ [before(), make_image(), after()]|join }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            class Text:
                def __init__(self, label: str) -> None:
                    self.label = label

                def __str__(self) -> str:
                    events.append(f'str:{self.label}')
                    return self.label

            def item(label: str) -> Text:
                events.append(f'create:{label}')
                return Text(label)

            def make_image() -> Any:
                events.append('create:image')
                return engine.InlineImage(document, io.BytesIO(stream.getvalue()))

            document.render({'before': lambda: item('before'), 'make_image': make_image,
                             'after': lambda: item('after'), 'child': child})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        reference, expected = run(docxtpl)
        candidate, actual = run(docxtpl_native)
        self.assertEqual(expected, ['create:before', 'create:image', 'create:after', 'str:before', 'str:after'])
        self.assertEqual(actual, expected)
        comparison = compare_docx(reference, candidate)
        self.assertTrue(comparison['equivalent'], comparison)

    def test_owned_join_custom_map_receives_original_str(self) -> None:
        def run(engine: Any) -> tuple[bytes, list[bool]]:
            observed: list[bool] = []
            document = engine.DocxTemplate(io.BytesIO(template('{{ values|join }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            def custom_map(function: Any, values: Any) -> Any:
                observed.append(function is builtins.str)
                return builtins.map(function, values)

            with patch.dict(jinja2.filters.__dict__, {'map': custom_map}):
                document.render({'values': ['a', 'b'], 'child': child})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), observed

        reference, expected = run(docxtpl)
        candidate, actual = run(docxtpl_native)
        self.assertEqual(expected, [True])
        self.assertEqual(actual, expected)
        comparison = compare_docx(reference, candidate)
        self.assertTrue(comparison['equivalent'], comparison)

    def test_owned_join_default_map_retains_original_str(self) -> None:
        def run(engine: Any) -> tuple[bytes, list[bool]]:
            observed: list[bool] = []
            document = engine.DocxTemplate(io.BytesIO(template('{{ values|join }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            class Items:
                def __init__(self) -> None:
                    self.done = False

                def __iter__(self) -> Self:
                    return self

                def __next__(self) -> str:
                    if self.done:
                        raise StopIteration
                    self.done = True
                    maps = [value for value in gc.get_objects() if type(value) is map]
                    observed.append(any(value.__reduce__()[1][1] is self
                                        and value.__reduce__()[1][0] is builtins.str for value in maps))
                    return 'item'

            document.render({'values': Items(), 'child': child})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), observed

        reference, expected = run(docxtpl)
        candidate, actual = run(docxtpl_native)
        self.assertEqual(expected, [True])
        self.assertEqual(actual, expected)
        comparison = compare_docx(reference, candidate)
        self.assertTrue(comparison['equivalent'], comparison)

    def test_public_dynamic_reference_image_autoescape_compositions(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(stream, format='PNG')
        for expression in ("{{ 'X' ~ make_image() }}", '{{ make_image()|string }}'):
            with self.subTest(expression=expression):
                def run(engine: Any, expression: str = expression) -> bytes:
                    document = engine.DocxTemplate(io.BytesIO(template(expression)))
                    child = object.__new__(docxtpl_native.Subdoc)
                    child.tpl = None
                    child.source = io.BytesIO(template('unused'))
                    document.render({'make_image': lambda: engine.InlineImage(
                        document, io.BytesIO(stream.getvalue())), 'child': child},
                        jinja2.Environment(autoescape=True))
                    output = io.BytesIO()
                    document.save(output)
                    return output.getvalue()

                self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_dynamic_reference_image_string_filter_respects_str_override(self) -> None:
        import markupsafe

        def run(engine: Any) -> tuple[bytes, list[str]]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|string }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            events: list[str] = []

            class CustomStr(str):
                def __new__(cls, value: object) -> Self:
                    events.append(type(value).__name__)
                    return super().__new__(cls, 'custom')

            with patch.dict(markupsafe.__dict__, {'str': CustomStr}):
                document.render({'make_image': lambda: engine.InlineImage(
                    document, io.BytesIO(b'unused')), 'child': child})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        expected, expected_events = run(docxtpl)
        actual, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_dynamic_reference_image_safe_respects_markup_override(self) -> None:
        from jinja2 import filters

        def run(engine: Any) -> tuple[bytes, list[bool]]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            image = engine.InlineImage(document, io.BytesIO(b'unused'))
            events: list[bool] = []

            def custom_markup(value: object) -> str:
                events.append(value is image)
                return 'custom'

            with patch.dict(filters.__dict__, {'Markup': custom_markup}):
                document.render({'make_image': lambda: image, 'child': child})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        expected, expected_events = run(docxtpl)
        actual, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_dynamic_reference_image_safe_respects_new_descriptor(self) -> None:
        import markupsafe

        def run(engine: Any) -> tuple[bytes, list[str]]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            image = engine.InlineImage(document, io.BytesIO(b'unused'))
            events: list[str] = []

            class NewDescriptor:
                def __get__(self, instance: object, owner: type) -> Any:
                    events.append('get')

                    def construct(cls: type[str], value: object) -> object:
                        events.append('new-image' if value is image else 'new-other')
                        return str.__new__(cls, 'custom')

                    return construct

            with patch.object(markupsafe.Markup, '__new__', NewDescriptor()):
                document.render({'make_image': lambda: image, 'child': child})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        expected, expected_events = run(docxtpl)
        actual, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_dynamic_reference_image_safe_observes_late_markup_init(self) -> None:
        import markupsafe

        image_stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(image_stream, format='PNG')

        def run(engine: Any) -> tuple[bytes, list[bool]]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            events: list[bool] = []
            started = False
            image: object

            def custom_init(instance: object, value: object) -> None:
                events.append(value is image)

            change = patch.object(markupsafe.Markup, '__init__', custom_init, create=True)

            class ChangingStream(io.BytesIO):
                def read(self, size: int | None = -1, /) -> bytes:
                    nonlocal started
                    if not started:
                        started = True
                        change.start()
                    return super().read(size)

            image = engine.InlineImage(document, ChangingStream(image_stream.getvalue()))
            try:
                document.render({'make_image': lambda: image, 'child': child})
            finally:
                if started:
                    change.stop()
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        expected, expected_events = run(docxtpl)
        actual, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_dynamic_reference_image_safe_observes_existing_markup_init(self) -> None:
        import markupsafe

        image_stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(image_stream, format='PNG')

        def run(engine: Any) -> tuple[bytes, list[bool]]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            image = engine.InlineImage(document, io.BytesIO(image_stream.getvalue()))
            events: list[bool] = []

            def custom_init(instance: object, value: object) -> None:
                events.append(value is image)

            with patch.object(markupsafe.Markup, '__init__', custom_init, create=True):
                document.render({'make_image': lambda: image, 'child': child})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        expected, expected_events = run(docxtpl)
        actual, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_dynamic_reference_image_safe_observes_markup_globals(self) -> None:
        from types import SimpleNamespace

        import markupsafe

        image_stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(image_stream, format='PNG')

        def run(engine: Any) -> tuple[bytes, list[tuple[str, bool]]]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            image = engine.InlineImage(document, io.BytesIO(image_stream.getvalue()))
            events: list[tuple[str, bool]] = []

            def custom_hasattr(value: object, name: str) -> bool:
                if value is image:
                    events.append(('hasattr', name == '__html__'))
                return hasattr(value, name)

            def parent_new(cls: type[str], value: object) -> object:
                events.append(('new', cls is markupsafe.Markup))
                return str.__new__(cls, value)

            def custom_super() -> SimpleNamespace:
                events.append(('super', True))
                return SimpleNamespace(__new__=parent_new)

            with patch.dict(markupsafe.__dict__, {'hasattr': custom_hasattr, 'super': custom_super}):
                document.render({'make_image': lambda: image, 'child': child})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        expected, expected_events = run(docxtpl)
        actual, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_dynamic_reference_image_safe_observes_late_super(self) -> None:
        from types import SimpleNamespace

        import markupsafe

        stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(stream, format='PNG')

        def run(engine: Any) -> tuple[bytes, list[str]]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            events: list[str] = []
            started = False

            def parent_new(cls: type[str], value: object) -> str:
                events.append('new')
                return str.__new__(cls, value)

            def custom_super() -> SimpleNamespace:
                events.append('super')
                return SimpleNamespace(__new__=parent_new)

            change = patch.dict(markupsafe.__dict__, {'super': custom_super})

            class ChangingStream(io.BytesIO):
                def read(self, size: int | None = -1, /) -> bytes:
                    nonlocal started
                    if not started:
                        started = True
                        change.start()
                    return super().read(size)

            image = engine.InlineImage(document, ChangingStream(stream.getvalue()))
            try:
                document.render({'make_image': lambda: image, 'child': child})
            finally:
                if started:
                    change.stop()
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        expected, expected_events = run(docxtpl)
        actual, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_dynamic_reference_image_safe_observes_markup_defaults(self) -> None:
        import markupsafe

        stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(stream, format='PNG')

        def run(engine: Any) -> tuple[str, str, list[str]]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            events: list[str] = []

            class TracedStream(io.BytesIO):
                def read(self, size: int | None = -1, /) -> bytes:
                    events.append('read')
                    return super().read(size)

            image = engine.InlineImage(document, TracedStream(stream.getvalue()))
            with patch.object(markupsafe.Markup.__new__, '__defaults__', ('', 'utf-8', 'strict')):
                try:
                    document.render({'make_image': lambda: image, 'child': child})
                except TypeError as error:
                    return type(error).__name__, str(error), events
            raise AssertionError('expected a constructor error')

        expected = run(docxtpl)
        self.assertEqual(expected[0], 'TypeError')
        self.assertEqual(run(docxtpl_native), expected)

    def test_public_dynamic_reference_image_safe_observes_missing_markup_defaults(self) -> None:
        import markupsafe

        def run(engine: Any) -> tuple[str, str]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            image = engine.InlineImage(document, io.BytesIO(b'unused'))
            with patch.object(markupsafe.Markup.__new__, '__defaults__', ()):
                try:
                    document.render({'make_image': lambda: image, 'child': child})
                except TypeError as error:
                    return type(error).__name__, str(error)
            raise AssertionError('expected a constructor error')

        expected = run(docxtpl)
        self.assertEqual(expected[0], 'TypeError')
        self.assertEqual(run(docxtpl_native), expected)

    def test_public_dynamic_reference_image_safe_keeps_bound_markup_defaults(self) -> None:
        import markupsafe

        stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(stream, format='PNG')

        def run(engine: Any) -> bytes:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            change = patch.object(markupsafe.Markup.__new__, '__defaults__', ('', 'utf-8', 'strict'))
            started = False

            class ChangingStream(io.BytesIO):
                def read(self, size: int | None = -1, /) -> bytes:
                    nonlocal started
                    if not started:
                        started = True
                        change.start()
                    return super().read(size)

            image = engine.InlineImage(document, ChangingStream(stream.getvalue()))
            try:
                document.render({'make_image': lambda: image, 'child': child})
            finally:
                if started:
                    change.stop()
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_dynamic_reference_image_safe_observes_changed_class_cell(self) -> None:
        import markupsafe

        stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(stream, format='PNG')
        for empty in (False, True):
            with self.subTest(empty=empty):
                def run(engine: Any, empty: bool = empty) -> tuple[str, str, list[str]]:
                    document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
                    child = object.__new__(docxtpl_native.Subdoc)
                    child.tpl = None
                    child.source = io.BytesIO(template('unused'))
                    closure = markupsafe.Markup.__new__.__closure__
                    assert closure is not None
                    cell = closure[0]
                    original = cell.cell_contents
                    changed = False
                    events: list[str] = []

                    class ChangingStream(io.BytesIO):
                        def read(self, size: int | None = -1, /) -> bytes:
                            nonlocal changed
                            events.append('read')
                            if not changed:
                                changed = True
                                if empty:
                                    del cell.cell_contents
                                else:
                                    cell.cell_contents = 1
                            return super().read(size)

                    image = engine.InlineImage(document, ChangingStream(stream.getvalue()))
                    try:
                        try:
                            document.render({'make_image': lambda: image, 'child': child})
                        except (RuntimeError, NameError) as error:
                            return type(error).__name__, str(error), events
                    finally:
                        cell.cell_contents = original
                    raise AssertionError('expected a super error')

                expected = run(docxtpl)
                expected_type = 'NameError' if empty and sys.version_info >= (3, 12) else 'RuntimeError'
                self.assertEqual(expected[0], expected_type)
                self.assertEqual(run(docxtpl_native), expected)

    def test_public_dynamic_reference_image_safe_custom_super_with_empty_class_cell(self) -> None:
        from types import SimpleNamespace

        import markupsafe

        stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(stream, format='PNG')

        def run(engine: Any) -> tuple[str, str, list[str], bytes | None]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            closure = markupsafe.Markup.__new__.__closure__
            assert closure is not None
            cell = closure[0]
            original = cell.cell_contents
            changed = False
            events: list[str] = []

            class ChangingStream(io.BytesIO):
                def read(self, size: int | None = -1, /) -> bytes:
                    nonlocal changed
                    if not changed:
                        changed = True
                        del cell.cell_contents
                    return super().read(size)

            def parent_new(cls: type[str], value: object) -> str:
                events.append('new')
                return str.__new__(cls, value)

            def custom_super() -> SimpleNamespace:
                events.append('super')
                return SimpleNamespace(__new__=parent_new)

            image = engine.InlineImage(document, ChangingStream(stream.getvalue()))
            try:
                with patch.dict(markupsafe.__dict__, {'super': custom_super}):
                    try:
                        document.render({'make_image': lambda: image, 'child': child})
                    except (RuntimeError, NameError) as error:
                        return type(error).__name__, str(error), events, None
            finally:
                cell.cell_contents = original
            output = io.BytesIO()
            document.save(output)
            return 'ok', '', events, output.getvalue()

        expected_kind, expected_message, expected_events, expected = run(docxtpl)
        actual_kind, actual_message, actual_events, actual = run(docxtpl_native)
        self.assertEqual((actual_kind, actual_message, actual_events),
                         (expected_kind, expected_message, expected_events))
        if expected is not None and actual is not None:
            self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_dynamic_reference_image_safe_when_hasattr_returns_false(self) -> None:
        import markupsafe

        stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(stream, format='PNG')

        def run(engine: Any) -> tuple[bytes, list[str]]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            image = engine.InlineImage(document, io.BytesIO(stream.getvalue()))
            events: list[str] = []

            def custom_hasattr(value: object, name: str) -> bool:
                if value is image and name == '__html__':
                    events.append('image')
                    return False
                return hasattr(value, name)

            with patch.dict(markupsafe.__dict__, {'hasattr': custom_hasattr}):
                document.render({'make_image': lambda: image, 'child': child})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        expected, expected_events = run(docxtpl)
        actual, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_dynamic_reference_image_safe_avoids_markup_constructor(self) -> None:
        import markupsafe

        stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(stream, format='PNG')

        def run(engine: Any) -> tuple[bytes, int]:
            document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            image = engine.InlineImage(document, io.BytesIO(stream.getvalue()))
            target = markupsafe.Markup.__new__.__code__
            calls = 0

            def profile(frame: Any, event: str, argument: Any) -> None:
                nonlocal calls
                if event == 'call' and frame.f_code is target and frame.f_locals.get('object') is image:
                    calls += 1

            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                document.render({'make_image': lambda: image, 'child': child})
            finally:
                sys.setprofile(previous)
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), calls

        expected, expected_calls = run(docxtpl)
        actual, actual_calls = run(docxtpl_native)
        self.assertGreater(expected_calls, 0)
        self.assertEqual(actual_calls, 0)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_dynamic_reference_image_safe_async_and_autoescape(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (3, 2), (21, 42, 84)).save(stream, format='PNG')
        for asynchronous in (False, True):
            for autoescape in (False, True):
                with self.subTest(asynchronous=asynchronous, autoescape=autoescape):
                    def run(engine: Any, asynchronous: bool = asynchronous,
                            autoescape: bool = autoescape) -> bytes:
                        document = engine.DocxTemplate(io.BytesIO(template('{{ make_image()|safe }}')))
                        child = object.__new__(docxtpl_native.Subdoc)
                        child.tpl = None
                        child.source = io.BytesIO(template('unused'))

                        def make_image() -> object:
                            return engine.InlineImage(document, io.BytesIO(stream.getvalue()))

                        async def make_async_image() -> object:
                            await asyncio.sleep(0)
                            return make_image()

                        document.render({'make_image': make_async_image if asynchronous else make_image,
                                         'child': child}, jinja2.Environment(
                                             enable_async=asynchronous, autoescape=autoescape))
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue()

                    self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_callback_stringify_preserves_stream_protocol_trace(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> tuple[bytes, list[tuple[Any, ...]]]:
            events: list[tuple[Any, ...]] = []

            class TracedStream(io.BytesIO):
                def read(self, size: int | None = -1, /) -> bytes:
                    events.append(('read', size))
                    return super().read(size)

                def seek(self, offset: int, whence: int = 0) -> int:
                    events.append(('seek', offset, whence))
                    return super().seek(offset, whence)

            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, TracedStream(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            def callback(item: object) -> str:
                events.clear()
                return str(item)

            document.render({'image': image, 'child': child, 'callback': callback})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        reference, expected_events = run(docxtpl)
        candidate, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_callback_image_stream_protocol_formats(self) -> None:
        source = template('{{ callback(image) }}')
        exif = Image.Exif()
        exif[282] = IFDRational(300, 1)
        exif[283] = IFDRational(150, 1)
        exif[296] = 2
        cases = (('PNG', 'PNG', {}, False), ('PNG-DPI', 'PNG', {'dpi': (300, 150)}, False),
                 ('GIF', 'GIF', {}, False), ('BMP', 'BMP', {}, False),
                 ('JPEG', 'JPEG', {}, False), ('JPEG-EXIF', 'JPEG', {'exif': exif}, True),
                 ('TIFF', 'TIFF', {}, False))
        for name, fmt, options, strip_app0 in cases:
            with self.subTest(fmt=name):
                stream = io.BytesIO()
                Image.new('RGB', (7, 5), (21, 42, 84)).save(stream, format=fmt, **options)
                image_bytes = stream.getvalue()
                if strip_app0:
                    self.assertEqual(image_bytes[2:4], b'\xff\xe0')
                    length = int.from_bytes(image_bytes[4:6], 'big')
                    image_bytes = image_bytes[:2] + image_bytes[4 + length:]
                    self.assertEqual(image_bytes[6:10], b'Exif')

                def run(engine: Any, image_data: bytes = image_bytes) -> tuple[bytes, list[tuple[Any, ...]]]:
                    events: list[tuple[Any, ...]] = []

                    class TracedStream(io.BytesIO):
                        def read(self, size: int | None = -1, /) -> bytes:
                            events.append(('read', size))
                            return super().read(size)

                        def seek(self, offset: int, whence: int = 0) -> int:
                            events.append(('seek', offset, whence))
                            return super().seek(offset, whence)

                        def tell(self) -> int:
                            events.append(('tell',))
                            return super().tell()

                    document = engine.DocxTemplate(io.BytesIO(source))
                    image = engine.InlineImage(document, TracedStream(image_data))
                    child = object.__new__(docxtpl_native.Subdoc)
                    child.tpl = None
                    child.source = io.BytesIO(template('unused'))

                    def callback(item: Any) -> str:
                        events.clear()
                        xml = str(item)
                        events.append(('position', item.image_descriptor.tell()))
                        return xml

                    document.render({'image': image, 'child': child, 'callback': callback})
                    output = io.BytesIO()
                    document.save(output)
                    return output.getvalue(), events

                reference, expected_events = run(docxtpl)
                candidate, actual_events = run(docxtpl_native)
                self.assertEqual(actual_events, expected_events)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_callback_image_stream_not_read_before_callback(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> tuple[bytes, list[tuple[Any, ...]]]:
            events: list[tuple[Any, ...]] = []
            before_callback: list[tuple[Any, ...]] = []

            class TracedStream(io.BytesIO):
                def read(self, size: int | None = -1, /) -> bytes:
                    events.append(('read', size))
                    return super().read(size)

                def seek(self, offset: int, whence: int = 0) -> int:
                    events.append(('seek', offset, whence))
                    return super().seek(offset, whence)

                def tell(self) -> int:
                    events.append(('tell',))
                    return super().tell()

            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, TracedStream(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            def callback(item: object) -> str:
                before_callback.extend(events)
                return str(item)

            document.render({'image': image, 'child': child, 'callback': callback})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), before_callback

        reference, expected_events = run(docxtpl)
        candidate, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_unused_image_stream_is_not_accessed(self) -> None:
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> tuple[bytes, list[str]]:
            events: list[str] = []

            class UnusedStream(io.BytesIO):
                def read(self, size: int | None = -1, /) -> bytes:
                    events.append('read')
                    return super().read(size)

                def seek(self, offset: int, whence: int = 0) -> int:
                    events.append('seek')
                    return super().seek(offset, whence)

                def tell(self) -> int:
                    events.append('tell')
                    return super().tell()

            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, UnusedStream(b'unused'))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            document.render({'image': image, 'child': child, 'callback': lambda item: 'unused'})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        reference, expected_events = run(docxtpl)
        candidate, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_unused_image_descriptor_is_not_accessed(self) -> None:
        def run(engine: Any) -> bytes:
            class UnusedImage(engine.InlineImage):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'image_descriptor':
                        raise AssertionError('unused image descriptor was accessed')
                    return super().__getattribute__(name)

            document = engine.DocxTemplate(io.BytesIO(template('{{ callback(image) }}')))
            image = UnusedImage(document, io.BytesIO(b'unused'))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            document.render({'image': image, 'child': child, 'callback': lambda item: 'unused'})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_async_image_stream_not_read_before_callback(self) -> None:
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_stream, format='PNG')

        def run(engine: Any) -> tuple[bytes, list[str]]:
            events: list[str] = []
            before_callback: list[str] = []

            class TracedStream(io.BytesIO):
                def read(self, size: int | None = -1, /) -> bytes:
                    events.append('read')
                    return super().read(size)

                def seek(self, offset: int, whence: int = 0) -> int:
                    events.append('seek')
                    return super().seek(offset, whence)

                def tell(self) -> int:
                    events.append('tell')
                    return super().tell()

            document = engine.DocxTemplate(io.BytesIO(template('{{ callback(image) }}')))
            image = engine.InlineImage(document, TracedStream(image_stream.getvalue()))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            async def callback(item: object) -> str:
                before_callback.extend(events)
                await asyncio.sleep(0)
                return str(item)

            document.render({'image': image, 'child': child, 'callback': callback},
                            jinja2.Environment(enable_async=True))
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), before_callback

        expected, expected_events = run(docxtpl)
        actual, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_image_rendering_compositions(self) -> None:
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_stream, format='PNG')
        expressions = ('{{ image }}', '{% set x %}{{ image }}{% endset %}{{ x }}',
                       '{% macro item() %}{{ image }}{% endmacro %}{{ item() }}',
                       "{{ 'P' ~ image }}", '{{ image|string }}', '{{ image|safe }}',
                       '{{ [image]|join }}', '{{ callback(image) }}')
        for expression in expressions:
            for autoescape in (False, True):
                with self.subTest(expression=expression, autoescape=autoescape):
                    def run(engine: Any, expression: str = expression, autoescape: bool = autoescape) -> bytes:
                        document = engine.DocxTemplate(io.BytesIO(template(expression)))
                        image = engine.InlineImage(document, io.BytesIO(image_stream.getvalue()))
                        child = object.__new__(docxtpl_native.Subdoc)
                        child.tpl = None
                        child.source = io.BytesIO(template('unused'))
                        document.render({'image': image, 'child': child, 'callback': lambda item: item},
                                        jinja2.Environment(autoescape=autoescape))
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue()

                    self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_image_custom_string_protocols(self) -> None:
        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                def run(engine: Any, autoescape: bool = autoescape) -> tuple[bytes, list[str]]:
                    events: list[str] = []

                    class CustomImage(engine.InlineImage):
                        def __str__(self) -> str:
                            events.append('str')
                            return 'custom-str'

                        def __html__(self) -> str:
                            events.append('html')
                            return 'custom-html'

                    document = engine.DocxTemplate(io.BytesIO(template('{{ image }}')))
                    image = CustomImage(document, io.BytesIO(b'unused'))
                    child = object.__new__(docxtpl_native.Subdoc)
                    child.tpl = None
                    child.source = io.BytesIO(template('unused'))
                    document.render({'image': image, 'child': child}, jinja2.Environment(autoescape=autoescape))
                    output = io.BytesIO()
                    document.save(output)
                    return output.getvalue(), events

                expected, expected_events = run(docxtpl)
                actual, actual_events = run(docxtpl_native)
                self.assertEqual(actual_events, expected_events)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_reference_image_attribute_override(self) -> None:
        def run(engine: Any) -> tuple[bytes, list[str]]:
            events: list[str] = []

            class CustomReferenceImage(engine.InlineImage):
                def __getattribute__(self, name: str) -> Any:
                    if name == '_insert_image':
                        events.append('insert')
                        return lambda: 'custom-insert'
                    return super().__getattribute__(name)

            document = engine.DocxTemplate(io.BytesIO(template('{{ image }}')))
            image = CustomReferenceImage(document, io.BytesIO(b'unused'))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            document.render({'image': image, 'child': child})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        expected, expected_events = run(docxtpl)
        actual, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_reference_image_direct_output(self) -> None:
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_stream, format='PNG')
        for subclass in (False, True):
            for autoescape in (False, True):
                with self.subTest(autoescape=autoescape, subclass=subclass):
                    def run(engine: Any, autoescape: bool = autoescape, subclass: bool = subclass) -> bytes:
                        image_type = type('DerivedImage', (engine.InlineImage,), {}) if subclass else engine.InlineImage
                        document = engine.DocxTemplate(io.BytesIO(template('{{ image }}')))
                        image = image_type(document, io.BytesIO(image_stream.getvalue()))
                        child = object.__new__(docxtpl_native.Subdoc)
                        child.tpl = None
                        child.source = io.BytesIO(template('unused'))
                        document.render({'image': image, 'child': child}, jinja2.Environment(autoescape=autoescape))
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue()

                    self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_reference_image_default_insertion_is_native(self) -> None:
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_stream, format='PNG')

        def run(engine: Any) -> int:
            class DerivedImage(engine.InlineImage):
                pass

            calls = 0
            target = engine.InlineImage._insert_image.__code__

            def profile(frame: Any, event: str, argument: Any) -> None:
                nonlocal calls
                if event == 'call' and frame.f_code is target:
                    calls += 1

            document = engine.DocxTemplate(io.BytesIO(template('{{ image }}')))
            image = DerivedImage(document, io.BytesIO(image_stream.getvalue()))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                document.render({'image': image, 'child': child})
            finally:
                sys.setprofile(previous)
            return calls

        self.assertGreater(run(docxtpl), 0)
        self.assertEqual(run(docxtpl_native), 0)

    def test_public_callback_image_stream_header_is_read_live(self) -> None:
        streams = [io.BytesIO(), io.BytesIO()]
        Image.new('RGB', (2, 2), (21, 42, 84)).save(streams[0], format='PNG')
        Image.new('RGB', (7, 5), (21, 42, 84)).save(streams[1], format='PNG')
        blobs = [stream.getvalue() for stream in streams]
        source = template('{{ callback(image) }}')

        class ChangingStream(io.BytesIO):
            def read(self, size: int | None = -1, /) -> bytes:
                if size is None or size < 0:
                    return super().read(size)
                offset = self.tell()
                data = blobs[1][offset:offset + size]
                self.seek(offset + len(data))
                return data

        def run(engine: Any) -> bytes:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, ChangingStream(blobs[0]))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            document.render({'image': image, 'child': child, 'callback': lambda item: str(item)})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        reference = run(docxtpl)
        candidate = run(docxtpl_native)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_callback_image_stream_with_autoescape(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> bytes:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, io.BytesIO(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            document.render({'image': image, 'child': child, 'callback': lambda item: str(item)},
                            jinja_env=jinja2.Environment(autoescape=True))
            output = io.BytesIO()
            document.save(output)
            return output.getvalue()

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_public_callback_image_stream_error_identity(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')
        marker = RuntimeError('image stream read failed')

        class FailingStream(io.BytesIO):
            def read(self, size: int | None = -1, /) -> bytes:
                if size == 32:
                    raise marker
                return super().read(size)

        def run(engine: Any) -> None:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, FailingStream(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            document.render({'image': image, 'child': child, 'callback': lambda item: str(item)})

        for engine in (docxtpl, docxtpl_native):
            with self.subTest(engine=engine):
                with self.assertRaises(RuntimeError) as raised:
                    run(engine)
                self.assertIs(raised.exception, marker)

    def test_public_callback_image_stream_seek_error_identity(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')
        marker = RuntimeError('image stream seek failed')

        class FailingStream(io.BytesIO):
            def seek(self, offset: int, whence: int = 0) -> int:
                if offset == 8:
                    raise marker
                return super().seek(offset, whence)

        def run(engine: Any) -> None:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, FailingStream(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            document.render({'image': image, 'child': child, 'callback': lambda item: str(item)})

        for engine in (docxtpl, docxtpl_native):
            with self.subTest(engine=engine):
                with self.assertRaises(RuntimeError) as raised:
                    run(engine)
                self.assertIs(raised.exception, marker)

    def test_public_callback_image_stream_read_can_reenter_render(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> tuple[bytes, bytes]:
            nested: list[bytes] = []

            class ReentrantStream(io.BytesIO):
                def read(self, size: int | None = -1, /) -> bytes:
                    if size == 32 and not nested:
                        inner = engine.DocxTemplate(io.BytesIO(template('{{ value }}')))
                        inner.render({'value': 'nested'})
                        result = io.BytesIO()
                        inner.save(result)
                        nested.append(result.getvalue())
                    return super().read(size)

            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, ReentrantStream(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            document.render({'image': image, 'child': child, 'callback': lambda item: str(item)})
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), nested[0]

        reference = run(docxtpl)
        candidate = run(docxtpl_native)
        for expected, actual in zip(reference, candidate, strict=True):
            self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_callback_image_stream_does_not_call_python_header_parser(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> tuple[bytes, list[str]]:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, io.BytesIO(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            calls: list[str] = []

            def profile(frame: Any, event: str, arg: Any) -> None:
                if event == 'call' and '/docx/image/' in frame.f_code.co_filename:
                    calls.append(frame.f_code.co_name)

            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                document.render({'image': image, 'child': child, 'callback': lambda item: str(item)})
            finally:
                sys.setprofile(previous)
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), calls

        reference, reference_calls = run(docxtpl)
        candidate, candidate_calls = run(docxtpl_native)
        self.assertTrue(reference_calls)
        self.assertEqual(candidate_calls, [])
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_callback_truncated_png_stream_error(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()[:24]
        source = template('{{ callback(image) }}')
        error_type = importlib.import_module('docx.image.exceptions').UnexpectedEndOfFileError

        def run(engine: Any) -> None:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, io.BytesIO(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            document.render({'image': image, 'child': child, 'callback': lambda item: str(item)})

        for engine in (docxtpl, docxtpl_native):
            with self.subTest(engine=engine), self.assertRaises(error_type):
                run(engine)

    def test_public_callback_stringify_rejects_image_owned_by_another_template(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> None:
            owner = engine.DocxTemplate(io.BytesIO(template('owner')))
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(owner, io.BytesIO(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            def callback(item: object) -> str:
                return str(item)

            document.render({'image': image, 'child': child, 'callback': callback})

        with self.assertRaises(AttributeError):
            run(docxtpl)
        with self.assertRaises(AttributeError):
            run(docxtpl_native)

    def test_public_callback_stringify_restores_scope_after_nested_render(self) -> None:
        streams = [io.BytesIO(), io.BytesIO()]
        Image.new('RGB', (2, 2), (21, 42, 84)).save(streams[0], format='PNG')
        Image.new('RGB', (2, 2), (84, 42, 21)).save(streams[1], format='PNG')
        images = [stream.getvalue() for stream in streams]
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> tuple[bytes, bytes]:
            nested_result: list[bytes] = []

            def render(index: int) -> bytes:
                document = engine.DocxTemplate(io.BytesIO(source))
                image = engine.InlineImage(document, io.BytesIO(images[index]))
                child = object.__new__(docxtpl_native.Subdoc)
                child.tpl = None
                child.source = io.BytesIO(template('unused'))

                def callback(item: object) -> str:
                    if index == 0:
                        nested_result.append(render(1))
                    return str(item)

                document.render({'image': image, 'child': child, 'callback': callback})
                output = io.BytesIO()
                document.save(output)
                return output.getvalue()

            outer = render(0)
            return outer, nested_result[0]

        reference = run(docxtpl)
        candidate = run(docxtpl_native)
        for expected, actual in zip(reference, candidate, strict=True):
            self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_public_async_callback_can_stringify_registered_image(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> tuple[bytes, list[str]]:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, io.BytesIO(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))
            events: list[str] = []

            async def callback(item: object) -> str:
                events.append('before')
                await asyncio.sleep(0)
                events.append('after')
                return str(item)

            document.render({'image': image, 'child': child, 'callback': callback},
                            jinja_env=jinja2.Environment(enable_async=True))
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        reference, expected_events = run(docxtpl)
        candidate, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertEqual(actual_events, ['before', 'after'])
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_async_callback_image_stream_protocol_trace(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')

        def run(engine: Any) -> tuple[bytes, list[tuple[Any, ...]]]:
            events: list[tuple[Any, ...]] = []

            class TracedStream(io.BytesIO):
                def read(self, size: int | None = -1, /) -> bytes:
                    events.append(('read', size))
                    return super().read(size)

                def seek(self, offset: int, whence: int = 0) -> int:
                    events.append(('seek', offset, whence))
                    return super().seek(offset, whence)

            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, TracedStream(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            async def callback(item: object) -> str:
                await asyncio.sleep(0)
                events.clear()
                return str(item)

            document.render({'image': image, 'child': child, 'callback': callback},
                            jinja_env=jinja2.Environment(enable_async=True))
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        reference, expected_events = run(docxtpl)
        candidate, actual_events = run(docxtpl_native)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_callback_image_error_keeps_exception_identity(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ callback(image) }}')
        marker = RuntimeError('callback error after image conversion')

        def run(engine: Any) -> None:
            document = engine.DocxTemplate(io.BytesIO(source))
            image = engine.InlineImage(document, io.BytesIO(image_bytes))
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('unused'))

            def callback(item: object) -> str:
                str(item)
                raise marker

            document.render({'image': image, 'child': child, 'callback': callback})

        for engine in (docxtpl, docxtpl_native):
            with self.subTest(engine=engine):
                with self.assertRaises(RuntimeError) as raised:
                    run(engine)
                self.assertIs(raised.exception, marker)

    def test_finalizer_receives_and_returns_registered_image(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ image }}')
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        reference_image = docxtpl.InlineImage(reference, io.BytesIO(image_bytes))
        reference_seen: list[bool] = []

        def reference_finalize(item: object) -> object:
            reference_seen.append(item is reference_image)
            return item

        reference.render({'image': reference_image}, jinja_env=jinja2.Environment(finalize=reference_finalize))
        expected = io.BytesIO()
        reference.save(expected)
        image = docxtpl_native.InlineImage(None, io.BytesIO(image_bytes))
        candidate_seen: list[bool] = []

        def candidate_finalize(item: object) -> object:
            candidate_seen.append(item is image)
            return item

        payload = json.dumps({
            'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        output, _ = docxtpl_native._extension().process_python_docx(
            source, {'image': image}, BridgeConfig(jinja2.Environment(finalize=candidate_finalize), False), payload, False,
        )
        self.assertEqual(candidate_seen, reference_seen)
        self.assertEqual(candidate_seen, [True])
        self.assertTrue(compare_docx(expected.getvalue(), bytes(output))['equivalent'])

    def test_finalizer_can_return_different_registered_image(self) -> None:
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(stream, format='PNG')
        image_bytes = stream.getvalue()
        source = template('{{ value }}')
        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                reference_image = docxtpl.InlineImage(reference, io.BytesIO(image_bytes))
                reference.render({'value': 'input'}, jinja_env=jinja2.Environment(
                    finalize=lambda _, result=reference_image: result, autoescape=autoescape))
                expected = io.BytesIO()
                reference.save(expected)
                image = docxtpl_native.InlineImage(None, io.BytesIO(image_bytes))
                payload = json.dumps({
                    'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                    'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                })
                output, _ = docxtpl_native._extension().process_python_docx(
                    source, {'value': 'input'}, BridgeConfig(jinja2.Environment(
                        finalize=lambda _, result=image: result, autoescape=autoescape), False), payload, autoescape,
                )
                self.assertTrue(compare_docx(expected.getvalue(), bytes(output))['equivalent'])

    def test_file_composition_guards_image_stringified_by_callback(self) -> None:
        image = docxtpl_native.InlineImage(None, io.BytesIO(b'image'))

        def callback(value: object) -> str:
            return str(value)

        payload = json.dumps({
            'specials': [],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
            docxtpl_native._extension().process_python_docx(
                template('{{ callback(image) }}'), {'image': image, 'callback': callback},
                BridgeConfig(jinja2.Environment(), False), payload, False,
            )

    def test_public_file_subdoc_context_does_not_probe_other_callback_argument(self) -> None:
        class Value:
            def __getattribute__(self, name: str) -> Any:
                if name == '__class__':
                    raise ValueError('unexpected class lookup')
                return object.__getattribute__(self, name)

        def run(engine: Any) -> tuple[bytes, list[bool]]:
            value = Value()
            calls: list[bool] = []
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('child'))

            def callback(argument: object) -> str:
                calls.append(argument is value)
                return 'ok'

            source = template('{{ callback(value) }}')
            output = generate(engine, '', {'child': child, 'value': value, 'callback': callback},
                              jinja2.Environment(), source)
            return output, calls

        reference, expected_calls = run(docxtpl)
        self.assertEqual(expected_calls, [True])
        candidate, actual_calls = run(docxtpl_native)
        self.assertEqual(actual_calls, expected_calls)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_session_preserves_special_values_as_callback_arguments(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            image = docxtpl_native.InlineImage(None, io.BytesIO(b'image'))
            seen: list[bool] = []

            def callback(value: object) -> object:
                seen.append(value is image)
                return value

            context = {'image': image, 'callback': callback}
            source = '{{ callback(image) is sameas(image) }}'
            environment = jinja2.Environment()
            if native:
                session = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                )
                return session.render('<string>', source), seen
            return environment.from_string(source).render(context), seen

        expected = run(False)
        self.assertEqual(expected, ('True', [True]))
        self.assertEqual(run(True), expected)

    def test_file_composition_rejects_unmaterialized_lazy_image(self) -> None:
        image = docxtpl_native.InlineImage(None, io.BytesIO(b'image'))
        context = {'make_image': lambda: image}
        payload = json.dumps({
            'specials': [],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
            docxtpl_native._extension().process_python_docx(
                template('{{ make_image() }}'), context,
                BridgeConfig(jinja2.Environment(), False), payload, False,
            )

    def test_default_bridge_does_not_probe_callback_argument_class(self) -> None:
        class Value:
            def __getattribute__(self, name: str) -> Any:
                if name == '__class__':
                    raise ValueError('unexpected class lookup')
                return object.__getattribute__(self, name)

        def run(native: bool) -> tuple[str, list[bool]]:
            value = Value()
            calls: list[bool] = []

            def callback(argument: object) -> str:
                calls.append(argument is value)
                return 'ok'

            environment = jinja2.Environment()
            source = '{{ callback(value) }}'
            context = {'value': value, 'callback': callback}
            if native:
                session = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                )
                return session.render('<string>', source), calls
            return environment.from_string(source).render(context), calls

        expected = run(False)
        self.assertEqual(expected, ('ok', [True]))
        self.assertEqual(run(True), expected)

    def test_live_bridge_defers_undefined_probe_to_test(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            events: list[bool] = []
            state = {'undefined': False}

            class Value(Markup):
                def __getattribute__(self, name: str) -> Any:
                    if name == '__class__':
                        events.append(state['undefined'])
                        return jinja2.Undefined if state['undefined'] else Value
                    return super().__getattribute__(name)

            def flip() -> str:
                state['undefined'] = True
                return ''

            source = '{{ value is undefined }}|{{ flip() }}|{{ value is undefined }}'
            context = {'value': Value('a'), 'flip': flip}
            environment = jinja2.Environment()
            if not native:
                return environment.from_string(source).render(context), events
            configuration = BridgeConfig(environment, False)
            configuration.package_values_live = True
            session = docxtpl_native._extension().PythonRenderSession(configuration, context)
            return session.render('<string>', source), events

        self.assertEqual(run(True), run(False))

    def test_document_accepts_value_with_failing_class_lookup(self) -> None:
        class Value(Markup):
            def __getattribute__(self, name: str) -> Any:
                if name == '__class__':
                    raise ValueError('class probe')
                return super().__getattribute__(name)

        source = '{{ value }}'
        reference = generate(docxtpl, source, {'value': Value('ok')}, jinja2.Environment(autoescape=True))
        candidate = generate(docxtpl_native, source, {'value': Value('ok')}, jinja2.Environment(autoescape=True))
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])
