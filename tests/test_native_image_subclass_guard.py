"""Deferred image guards must preserve safe subclass callback arguments."""

import asyncio
import gc
import io
import json
import unittest
from itertools import product
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


class NativeImageSubclassGuardTests(unittest.TestCase):
    def test_inherited_image_protocol_passes_callback_and_preserves_identity(self) -> None:
        class ImageSubclass(docxtpl_native.InlineImage):
            pass

        def run(native: bool, asynchronous: bool) -> tuple[bytes, list[tuple[bool, bool]]]:
            image = ImageSubclass(None, io.BytesIO(b'not read'))
            observed: list[tuple[bool, bool]] = []

            def callback(value: object) -> str:
                observed.append((value is image and type(value) is ImageSubclass,
                                 any(type(item) is dict for item in gc.get_referents(value))))
                return 'ok'

            context = {'image': image, 'callback': callback}
            source = template('{{ callback(image) }}')
            env = jinja2.Environment(enable_async=asynchronous)
            if not native:
                return generate(docxtpl, '', context, env, source), observed
            payload = json.dumps({'specials': [], 'replacements': {
                'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
            }})
            output, _ = docxtpl_native._extension().process_python_docx(
                source, context, BridgeConfig(env, False), payload, False,
            )
            return bytes(output), observed

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected, reference = run(False, asynchronous)
                actual, candidate = run(True, asynchronous)
                self.assertEqual(len(reference), 1)
                self.assertTrue(reference[0][0])
                self.assertEqual(candidate, reference)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_image_subclass_identity_survives_suspension_in_callback(self) -> None:
        class ImageSubclass(docxtpl_native.InlineImage):
            pass

        def run(native: bool) -> tuple[bytes, list[bool]]:
            image = ImageSubclass(None, io.BytesIO(b'not read'))
            observed: list[bool] = []

            async def callback(value: object) -> str:
                observed.append(value is image)
                await asyncio.sleep(0)
                observed.append(value is image)
                return 'ok'

            source = template('{{ callback(image) }}')
            context = {'image': image, 'callback': callback}
            env = jinja2.Environment(enable_async=True)
            if not native:
                return generate(docxtpl, '', context, env, source), observed
            payload = json.dumps({'specials': [], 'replacements': {
                'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
            }})
            output, _ = docxtpl_native._extension().process_python_docx(
                source, context, BridgeConfig(env, False), payload, False,
            )
            return bytes(output), observed

        expected, reference = run(False)
        actual, candidate = run(True)
        self.assertEqual(reference, [True, True])
        self.assertEqual(candidate, reference)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_inherited_image_protocol_rejects_unregistered_insertion(self) -> None:
        class ImageSubclass(docxtpl_native.InlineImage):
            pass

        for asynchronous, autoescape in product((False, True), repeat=2):
            for operation in ('{{ image }}', '{{ callback(image) }}', '{{ [image]|join }}', '{{ image|safe }}'):
                with self.subTest(asynchronous=asynchronous, autoescape=autoescape, operation=operation):
                    image = ImageSubclass(None, io.BytesIO(b'not read'))
                    observed: list[bool] = []

                    def callback(value: Any) -> str:
                        observed.append(value is image)
                        return str(value)

                    payload = json.dumps({'specials': [], 'replacements': {
                        'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
                    }})
                    with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
                        docxtpl_native._extension().process_python_docx(
                            template(operation), {'image': image, 'callback': callback},
                            BridgeConfig(jinja2.Environment(enable_async=asynchronous, autoescape=autoescape),
                                         autoescape), payload, autoescape,
                        )
                    self.assertEqual(observed, [True] if 'callback' in operation else [])

    def test_overridden_protocol_stays_guarded(self) -> None:
        class ModifiedImage(docxtpl_native.InlineImage):
            def _insert_image(self) -> str:
                raise AssertionError('must not run')

        observed: list[object] = []
        image = ModifiedImage(None, io.BytesIO(b'not read'))
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
            docxtpl_native._extension().process_python_docx(
                template('{{ callback(image) }}'), {'image': image, 'callback': observed.append},
                BridgeConfig(jinja2.Environment(), False), payload, False,
            )
        self.assertEqual(observed, [])

    def test_subclass_descriptor_is_not_executed_by_guard(self) -> None:
        events: list[str] = []

        class Descriptor:
            def __get__(self, instance: object, owner: type[object]) -> Any:
                events.append('descriptor')
                raise AssertionError('guard executed subclass descriptor')

        class ModifiedImage(docxtpl_native.InlineImage):
            _insert_image = Descriptor()

        image = ModifiedImage(None, io.BytesIO(b'not read'))
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
            docxtpl_native._extension().process_python_docx(
                template('{{ callback(image) }}'), {'image': image, 'callback': lambda _: 'unused'},
                BridgeConfig(jinja2.Environment(), False), payload, False,
            )
        self.assertEqual(events, [])

    def test_subclass_dictionary_keys_are_not_compared_by_guard(self) -> None:
        events: list[str] = []

        class Key(str):
            __hash__ = str.__hash__

            def __eq__(self, other: object) -> bool:
                events.append('compare')
                return str.__eq__(self, other)

        metaclass = type(docxtpl_native.InlineImage)
        subclass = metaclass('ImageSubclass', (docxtpl_native.InlineImage,), {Key('_insert_image'): object()})
        image = subclass(None, io.BytesIO(b'not read'))
        events.clear()
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
            docxtpl_native._extension().process_python_docx(
                template('{{ callback(image) }}'), {'image': image, 'callback': lambda _: 'unused'},
                BridgeConfig(jinja2.Environment(), False), payload, False,
            )
        self.assertEqual(events, [])

    def test_native_class_descriptor_is_not_executed_by_guard(self) -> None:
        events: list[str] = []

        class Descriptor:
            def __get__(self, instance: object, owner: type[object]) -> Any:
                events.append('descriptor')
                raise AssertionError('guard executed native class descriptor')

        image = docxtpl_native.InlineImage(None, io.BytesIO(b'not read'))
        original = docxtpl_native.InlineImage.__dict__['_insert_image']
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        try:
            setattr(docxtpl_native.InlineImage, '_insert_image', Descriptor())  # noqa: B010
            with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
                docxtpl_native._extension().process_python_docx(
                    template('{{ callback(image) }}'), {'image': image, 'callback': lambda _: 'unused'},
                    BridgeConfig(jinja2.Environment(), False), payload, False,
                )
            self.assertEqual(events, [])
        finally:
            setattr(docxtpl_native.InlineImage, '_insert_image', original)  # noqa: B010

    def test_parent_getattribute_descriptor_is_not_executed_by_guard(self) -> None:
        events: list[str] = []

        class Descriptor:
            def __get__(self, instance: object, owner: type[object]) -> Any:
                events.append('descriptor')
                raise AssertionError('guard executed parent descriptor')

        class Parent(docxtpl_native.InlineImage):
            pass

        class Image(Parent):
            pass

        image = Image(None, io.BytesIO(b'not read'))
        parent = Parent
        self.assertNotIn('__getattribute__', parent.__dict__)
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        try:
            setattr(parent, '__getattribute__', Descriptor())  # noqa: B010
            with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
                docxtpl_native._extension().process_python_docx(
                    template('{{ callback(image) }}'), {'image': image, 'callback': lambda _: 'unused'},
                    BridgeConfig(jinja2.Environment(), False), payload, False,
                )
            self.assertEqual(events, [])
        finally:
            delattr(parent, '__getattribute__')  # noqa: B010

    def test_removed_native_method_stays_guarded(self) -> None:
        image = docxtpl_native.InlineImage(None, io.BytesIO(b'not read'))
        original = docxtpl_native.InlineImage.__dict__['_insert_image']
        payload = json.dumps({'specials': [], 'replacements': {
            'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
        }})
        try:
            delattr(docxtpl_native.InlineImage, '_insert_image')  # noqa: B010
            with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'materialization hook'):
                docxtpl_native._extension().process_python_docx(
                    template('{{ callback(image) }}'), {'image': image, 'callback': lambda _: 'unused'},
                    BridgeConfig(jinja2.Environment(), False), payload, False,
                )
        finally:
            setattr(docxtpl_native.InlineImage, '_insert_image', original)  # noqa: B010
