"""Task and document parity for async callbacks in file composition."""

import asyncio
import builtins
import contextvars
import io
import json
import re
import sys
import threading
import unittest
import warnings
import zipfile
from collections.abc import AsyncIterator, Callable, Coroutine, Generator
from contextlib import contextmanager
from types import FrameType
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
import jinja2.async_utils
import jinja2.ext
from docx import Document
from docxcompose import composer
from docxtpl_native.bridge import BridgeConfig
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template
from paritylab.runtime_error_probes import exception_observation, make_case


def without_nsids(docx: bytes) -> bytes:
    """The package with copied-list nsid values blanked: docxcompose draws them
    from Python's random, docxtpl-native derives them from the list definition
    (intentional divergence, see test_native_deferred_marker)."""
    source = zipfile.ZipFile(io.BytesIO(docx))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == 'word/numbering.xml':
                data = re.sub(rb'(<w:nsid w:val=")[0-9A-Fa-f]{8}(")', rb'\g<1>--------\g<2>', data)
            target.writestr(item, data)
    return stream.getvalue()


def compare_numbered(reference: bytes, candidate: bytes) -> dict[str, Any]:
    return compare_docx(without_nsids(reference), without_nsids(candidate))


@contextmanager
def numbering_probe(events: list[Any], on_numbering: Callable[[], object]) -> Generator[None]:
    """Observe list-numbering composition, or fail it, in both engines.

    Both convert the abstract numbering ids with docxcompose's `int` while a
    subdocument's lists are copied; `on_numbering` runs once per contiguous
    composition (and may raise). docxcompose's random nsid draw is held
    constant: docxtpl-native does not draw one."""
    def integer(value: Any) -> int:
        if isinstance(value, str) and not (events and events[-1] == 'numbering'):
            on_numbering()
        return builtins.int(value)

    with patch.object(composer, 'int', integer, create=True), patch.object(composer.random, 'random', return_value=0.125):
        yield


class NativeFileAsyncTests(unittest.TestCase):
    @staticmethod
    def render_native(source: bytes, context: dict[str, Any], environment: jinja2.Environment) -> bytes:
        payload = json.dumps({
            'specials': [],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        output, _ = docxtpl_native._extension().process_python_docx(
            source, context, BridgeConfig(environment, False), payload, False,
        )
        return bytes(output)

    def test_awaited_callbacks_in_body_and_header_use_separate_tasks(self) -> None:
        source = template("{{ callback('body') }}", header="{{ callback('header') }}")

        def run(native: bool) -> tuple[bytes, list[tuple[str, str, str, bool]], str]:
            variable: contextvars.ContextVar[str] = contextvars.ContextVar('file_async', default='outside')
            events: list[tuple[str, str, str, bool]] = []
            tasks: dict[str, asyncio.Task[Any]] = {}
            owner = threading.get_ident()

            async def callback(label: str) -> str:
                task = asyncio.current_task()
                assert task is not None
                tasks[label] = task
                events.append(('before', label, variable.get(), threading.get_ident() == owner))
                variable.set(label)
                await asyncio.sleep(0)
                events.append(('after', label, variable.get(), asyncio.current_task() is task
                               and threading.get_ident() == owner))
                return label

            context = {'callback': callback}
            environment = jinja2.Environment(enable_async=True)
            data = (self.render_native(source, context, environment) if native
                    else generate(docxtpl, '', context, environment, source))
            events.append(('separate', '', '', tasks['body'] is not tasks['header']))
            return data, events, variable.get()

        reference, expected, expected_outer = run(False)
        self.assertEqual(expected, [
            ('before', 'body', 'outside', True), ('after', 'body', 'body', True),
            ('before', 'header', 'outside', True), ('after', 'header', 'header', True),
            ('separate', '', '', True),
        ])
        candidate, actual, actual_outer = run(True)
        self.assertEqual(actual, expected)
        self.assertEqual(actual_outer, expected_outer)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_awaited_callback_exception_keeps_identity_and_stops_output(self) -> None:
        source = template('{{ fail() }}{{ after() }}')
        marker = ValueError('async callback failure')

        def run(native: bool) -> tuple[bool, list[str]]:
            events: list[str] = []

            async def fail() -> str:
                events.append('before')
                await asyncio.sleep(0)
                events.append('raise')
                raise marker

            def after() -> str:
                events.append('after')
                return 'unexpected'

            context = {'fail': fail, 'after': after}
            try:
                if native:
                    self.render_native(source, context, jinja2.Environment(enable_async=True))
                else:
                    generate(docxtpl, '', context, jinja2.Environment(enable_async=True), source)
            except ValueError as error:
                return error is marker, events
            raise AssertionError('expected callback exception')

        expected = run(False)
        self.assertEqual(expected, (True, ['before', 'raise']))
        self.assertEqual(run(True), expected)

    def test_awaited_callback_in_core_property(self) -> None:
        document = Document(io.BytesIO(template('body')))
        document.core_properties.title = '{{ callback() }}'
        buffer = io.BytesIO()
        document.save(buffer)
        source = buffer.getvalue()

        def run(native: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            async def callback() -> str:
                events.append('before')
                await asyncio.sleep(0)
                events.append('after')
                return 'title from async'

            context = {'callback': callback}
            environment = jinja2.Environment(enable_async=True)
            result = (self.render_native(source, context, environment) if native
                      else generate(docxtpl, '', context, environment, source))
            return result, events

        reference, expected = run(False)
        self.assertEqual(expected, ['before', 'after'])
        candidate, actual = run(True)
        self.assertEqual(actual, expected)
        self.assertEqual(Document(io.BytesIO(candidate)).core_properties.title, 'title from async')
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_async_property_exception_keeps_original_identity(self) -> None:
        document = Document(io.BytesIO(template('body')))
        document.core_properties.title = '{{ fail() }}'
        buffer = io.BytesIO()
        document.save(buffer)
        source = buffer.getvalue()
        marker = ValueError('property failure')

        def run(native: bool) -> tuple[bool, list[str]]:
            events: list[str] = []

            async def fail() -> str:
                events.append('before')
                await asyncio.sleep(0)
                events.append('raise')
                raise marker

            environment = jinja2.Environment(enable_async=True)
            try:
                if native:
                    self.render_native(source, {'fail': fail}, environment)
                else:
                    generate(docxtpl, '', {'fail': fail}, environment, source)
            except ValueError as error:
                return error is marker, events
            raise AssertionError('expected property callback exception')

        expected = run(False)
        self.assertEqual(expected, (True, ['before', 'raise']))
        self.assertEqual(run(True), expected)

    def test_context_initialization_occurs_inside_render_task(self) -> None:
        source = template('{{ value }}')

        def run(native: bool) -> tuple[bytes, list[bool]]:
            events: list[bool] = []

            class Context(jinja2.runtime.Context):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(asyncio.current_task() is not None)
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(enable_async=True)
            environment.context_class = Context
            context = {'value': 'body'}
            output = (self.render_native(source, context, environment) if native
                      else generate(docxtpl, '', context, environment, source))
            return output, events

        reference, expected = run(False)
        self.assertEqual(expected, [True] * 7)
        candidate, actual = run(True)
        self.assertEqual(actual, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_file_subdoc_route_awaits_callback(self) -> None:
        source = template('{{ callback() }}')

        def run(engine: Any) -> tuple[bytes, list[str]]:
            child = object.__new__(docxtpl_native.Subdoc)
            child.tpl = None
            child.source = io.BytesIO(template('child'))
            events: list[str] = []

            async def callback() -> str:
                events.append('before')
                await asyncio.sleep(0)
                events.append('after')
                return 'observed'

            context = {'child': child, 'callback': callback}
            output = generate(engine, '', context, jinja2.Environment(enable_async=True), source)
            return output, events

        reference, expected = run(docxtpl)
        self.assertEqual(expected, ['before', 'after'])
        candidate, actual = run(docxtpl_native)
        self.assertEqual(actual, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_render_inside_running_loop_raises_before_callback(self) -> None:
        source = template('{{ callback() }}')

        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def callback() -> str:
                events.append('callback')
                return 'unexpected'

            async def outer() -> str:
                environment = jinja2.Environment(enable_async=True)
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', RuntimeWarning)
                    try:
                        if native:
                            self.render_native(source, {'callback': callback}, environment)
                        else:
                            generate(docxtpl, '', {'callback': callback}, environment, source)
                    except RuntimeError as error:
                        return str(error)
                raise AssertionError('expected running event loop error')

            return asyncio.run(outer()), events

        expected = run(False)
        self.assertEqual(expected, ('asyncio.run() cannot be called from a running event loop', []))
        self.assertEqual(run(True), expected)

    def test_async_generator_loop_preserves_lookahead_and_cleanup(self) -> None:
        source = template('{% for item in values %}{{ emit(item) }}'
                          '{% if not loop.last %},{% endif %}{% endfor %}')

        def run(native: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            async def values() -> AsyncIterator[str]:
                try:
                    for item in ('A', 'B'):
                        events.append('yield:' + item)
                        await asyncio.sleep(0)
                        yield item
                finally:
                    events.append('closed')

            def emit(item: str) -> str:
                events.append('emit:' + item)
                return item

            context = {'values': values(), 'emit': emit}
            environment = jinja2.Environment(enable_async=True)
            output = (self.render_native(source, context, environment) if native
                      else generate(docxtpl, '', context, environment, source))
            return output, events

        reference, expected = run(False)
        self.assertEqual(expected, ['yield:A', 'emit:A', 'yield:B', 'emit:B', 'closed'])
        candidate, actual = run(True)
        self.assertEqual(actual, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_context_mutation_after_await_is_visible_in_header(self) -> None:
        source = template('{{ value }}{{ change() }}', header='{{ value }}')

        def run(native: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []
            context: dict[str, Any] = {'value': 'before'}

            async def change() -> str:
                events.append('before await')
                await asyncio.sleep(0)
                context['value'] = 'after'
                events.append('after await')
                return ''

            context['change'] = change
            environment = jinja2.Environment(enable_async=True)
            output = (self.render_native(source, context, environment) if native
                      else generate(docxtpl, '', context, environment, source))
            return output, events

        reference, expected = run(False)
        self.assertEqual(expected, ['before await', 'after await'])
        candidate, actual = run(True)
        self.assertEqual(actual, expected)
        document = Document(io.BytesIO(candidate))
        self.assertEqual(document.paragraphs[0].text, 'before')
        self.assertEqual(document.sections[0].header.paragraphs[0].text, 'after')
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_context_constructor_stop_iteration_crosses_coroutine_boundary(self) -> None:
        source = template('{{ value }}')
        marker = StopIteration('context stopped')

        def run(native: bool) -> tuple[str, str, bool, bool]:
            class Context(jinja2.runtime.Context):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    raise marker

            environment = jinja2.Environment(enable_async=True)
            environment.context_class = Context
            try:
                if native:
                    self.render_native(source, {'value': 'body'}, environment)
                else:
                    generate(docxtpl, '', {'value': 'body'}, environment, source)
            except RuntimeError as error:
                return type(error).__name__, str(error), error.__cause__ is marker, error.__context__ is marker
            raise AssertionError('expected coroutine boundary error')

        expected = run(False)
        self.assertEqual(expected, ('RuntimeError', 'coroutine raised StopIteration', True, True))
        self.assertEqual(run(True), expected)

    def test_iteration_exceptions_in_async_file_render(self) -> None:
        for origin in ('filter', 'callback', 'str'):
            for exception_type in (StopIteration, StopAsyncIteration):
                with self.subTest(origin=origin, exception_type=exception_type.__name__):
                    environment, expression, values, marker = make_case(True, origin, exception_type)
                    source = template(expression)

                    def run(
                        native: bool, source: bytes = source, values: dict[str, Any] = values,
                        environment: jinja2.Environment = environment, marker: BaseException = marker,
                    ) -> tuple[dict[str, Any], bytes | None]:
                        try:
                            output = (self.render_native(source, values, environment) if native
                                      else generate(docxtpl, '', values, environment, source))
                        except BaseException as error:  # noqa: BLE001 - compare error boundary
                            return exception_observation(error, marker), None
                        text = Document(io.BytesIO(output)).paragraphs[0].text
                        return {'status': 'ok', 'text': text}, output

                    expected, reference = run(False)
                    actual, candidate = run(True)
                    self.assertEqual(actual, expected)
                    if reference is not None:
                        self.assertIsNotNone(candidate)
                        assert candidate is not None
                        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_task_cancellation_stops_file_render(self) -> None:
        source = template('{{ cancel() }}{{ after() }}')

        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def cancel() -> str:
                events.append('start')
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                try:
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    events.append('cancelled')
                    raise
                return 'unexpected'

            def after() -> str:
                events.append('after')
                return 'unexpected'

            context = {'cancel': cancel, 'after': after}
            try:
                if native:
                    self.render_native(source, context, jinja2.Environment(enable_async=True))
                else:
                    generate(docxtpl, '', context, jinja2.Environment(enable_async=True), source)
            except asyncio.CancelledError as error:
                return type(error).__name__, events
            raise AssertionError('expected cancellation')

        expected = run(False)
        self.assertEqual(expected, ('CancelledError', ['start', 'cancelled']))
        self.assertEqual(run(True), expected)

    def test_asyncio_run_receives_one_coroutine_per_rendered_part(self) -> None:
        source = template('{{ value }}')
        original = asyncio.run

        def run(native: bool) -> tuple[bytes, list[tuple[str, str, str]]]:
            events: list[tuple[str, str, str]] = []

            def recorder(coroutine: Coroutine[Any, Any, Any], **kwargs: Any) -> Any:
                events.append((type(coroutine).__name__, coroutine.__name__, coroutine.__qualname__))
                return original(coroutine, **kwargs)

            environment = jinja2.Environment(enable_async=True)
            with patch.object(asyncio, 'run', recorder):
                output = (self.render_native(source, {'value': 'body'}, environment) if native
                          else generate(docxtpl, '', {'value': 'body'}, environment, source))
            return output, events

        reference, expected = run(False)
        self.assertEqual(expected, [('coroutine', 'render_async', 'Template.render_async')] * 7)
        candidate, actual = run(True)
        self.assertEqual(actual, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_replaced_asyncio_run_can_close_coroutine_without_starting_context(self) -> None:
        source = template('{{ value }}')
        body = ('<w:body xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:p><w:r><w:t>stub</w:t></w:r></w:p><w:sectPr/></w:body>')

        def run(native: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    raise AssertionError('context must not start')

            def replacement(coroutine: Coroutine[Any, Any, Any]) -> str:
                events.append(coroutine.__name__)
                coroutine.close()
                return body if len(events) == 1 else ''

            environment = jinja2.Environment(enable_async=True)
            environment.context_class = Context
            with patch.object(asyncio, 'run', replacement):
                output = (self.render_native(source, {'value': 'unused'}, environment) if native
                          else generate(docxtpl, '', {'value': 'unused'}, environment, source))
            return output, events

        reference, expected = run(False)
        self.assertEqual(expected, ['render_async'] * 7)
        candidate, actual = run(True)
        self.assertEqual(actual, expected)
        self.assertEqual(Document(io.BytesIO(candidate)).paragraphs[0].text, 'stub')
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_asyncio_run_observes_and_can_replace_rendered_string(self) -> None:
        source = template('{{ value }}')
        original = asyncio.run

        def run(native: bool, replace: bool) -> tuple[bytes, list[tuple[bool, bool]]]:
            events: list[tuple[bool, bool]] = []

            def wrapper(coroutine: Coroutine[Any, Any, Any], **kwargs: Any) -> Any:
                result = original(coroutine, **kwargs)
                events.append((type(result) is str, 'original' in result if isinstance(result, str) else False))
                if replace and isinstance(result, str):
                    return result.replace('original', 'replaced')
                return result

            environment = jinja2.Environment(enable_async=True)
            with patch.object(asyncio, 'run', wrapper):
                output = (self.render_native(source, {'value': 'original'}, environment) if native
                          else generate(docxtpl, '', {'value': 'original'}, environment, source))
            return output, events

        for replace in (False, True):
            with self.subTest(replace=replace):
                reference, expected = run(False, replace)
                candidate, actual = run(True, replace)
                self.assertEqual(actual, expected)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_asyncio_run_observes_rendered_string_reference_count(self) -> None:
        source = template('{{ value }}')
        original = asyncio.run

        def run(native: bool) -> list[int]:
            observed: list[int] = []

            def wrapper(coroutine: Coroutine[Any, Any, Any], **kwargs: Any) -> Any:
                result = original(coroutine, **kwargs)
                if isinstance(result, str) and 'reference-count-value' in result:
                    observed.append(sys.getrefcount(result))
                return result

            environment = jinja2.Environment(enable_async=True)
            with patch.object(asyncio, 'run', wrapper):
                if native:
                    self.render_native(source, {'value': 'reference-count-value'}, environment)
                else:
                    generate(docxtpl, '', {'value': 'reference-count-value'}, environment, source)
            return observed

        expected = run(False)
        self.assertEqual(len(expected), 1)
        self.assertEqual(run(True), expected)

    def test_runner_observes_materialized_registered_image(self) -> None:
        import base64
        from PIL import Image

        source = template('{{ value }}')
        image_bytes = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_bytes, format='PNG')
        original = asyncio.run

        def run(native: bool) -> tuple[bytes, list[tuple[bool, bool]]]:
            events: list[tuple[bool, bool]] = []
            def wrapper(coroutine: Coroutine[Any, Any, Any], **kwargs: Any) -> str:
                result: str = original(coroutine, **kwargs)
                if '<w:body' in result:
                    events.append(('<w:drawing>' in result, '\x01DTPLD' in result))
                return result

            environment = jinja2.Environment(enable_async=True)
            with patch.object(asyncio, 'run', wrapper):
                if native:
                    value = object()
                    payload = json.dumps({
                        'specials': [{'identity': id(value), 'value': {
                            'kind': 'image', 'data': base64.b64encode(image_bytes.getvalue()).decode('ascii'),
                        }}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    result, _ = docxtpl_native._extension().process_python_docx(
                        source, {'value': value}, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(result), events
                document = docxtpl.DocxTemplate(io.BytesIO(source))
                value = docxtpl.InlineImage(document, io.BytesIO(image_bytes.getvalue()))
                document.render({'value': value}, jinja_env=environment)
                result = io.BytesIO()
                document.save(result)
                return result.getvalue(), events

        reference, expected = run(False)
        candidate, actual = run(True)
        self.assertEqual(expected, [(True, False)])
        self.assertEqual(actual, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_runner_observes_materialized_registered_subdocument(self) -> None:
        import base64

        source = template('{{ value }}')
        child = Document()
        child.add_paragraph('child paragraph')
        buffer = io.BytesIO()
        child.save(buffer)
        child_bytes = buffer.getvalue()
        original = asyncio.run

        def run(native: bool) -> tuple[bytes, list[tuple[bool, bool]]]:
            events: list[tuple[bool, bool]] = []
            def wrapper(coroutine: Coroutine[Any, Any, Any], **kwargs: Any) -> str:
                result: str = original(coroutine, **kwargs)
                if '<w:body' in result:
                    events.append(('child paragraph' in result, '\x01DTPLD' in result))
                return result

            environment = jinja2.Environment(enable_async=True)
            with patch.object(asyncio, 'run', wrapper):
                if native:
                    value = object()
                    payload = json.dumps({
                        'specials': [{'identity': id(value), 'value': {
                            'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                        }}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    result, _ = docxtpl_native._extension().process_python_docx(
                        source, {'value': value}, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(result), events
                document = docxtpl.DocxTemplate(io.BytesIO(source))
                value = document.new_subdoc(io.BytesIO(child_bytes))
                document.render({'value': value}, jinja_env=environment)
                result = io.BytesIO()
                document.save(result)
                return result.getvalue(), events

        reference, expected = run(False)
        candidate, actual = run(True)
        self.assertEqual(expected, [(True, False)])
        self.assertEqual(actual, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_public_subdocuments_attach_in_construction_order(self) -> None:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        source = template('{{ before() }}{{ first }}{{ second }}')

        def child_bytes(text: str) -> bytes:
            child = Document()
            paragraph = child.add_paragraph(text)
            numbering = OxmlElement('w:numPr')
            number_id = OxmlElement('w:numId')
            number_id.set(qn('w:val'), '5')
            numbering.append(number_id)
            paragraph._p.get_or_add_pPr().append(numbering)
            output = io.BytesIO()
            child.save(output)
            return output.getvalue()

        first = child_bytes('first')
        second = child_bytes('second')

        def run(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []
            draws = iter((0.125, 0.25))

            def random_value() -> float:
                events.append('numbering')
                return next(draws)

            def before() -> str:
                events.append('before')
                return ''

            module = docxtpl_native if native else docxtpl
            environment = jinja2.Environment(enable_async=asynchronous)
            with numbering_probe(events, random_value):
                document = module.DocxTemplate(io.BytesIO(source))
                second_subdoc = document.new_subdoc(io.BytesIO(second))
                first_subdoc = document.new_subdoc(io.BytesIO(first))
                document.render({'first': first_subdoc, 'second': second_subdoc, 'before': before},
                                jinja_env=environment)
                output = io.BytesIO()
                document.save(output)
            return output.getvalue(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, expected = run(False, asynchronous)
                candidate, actual = run(True, asynchronous)
                # Both subdocuments compose before rendering (one contiguous block).
                self.assertEqual(expected, ['numbering', 'before'])
                self.assertEqual(actual, expected)
                comparison = compare_numbered(reference, candidate)
                self.assertTrue(comparison['equivalent'], comparison)

    def test_public_unused_subdocument_changes_package_during_construction(self) -> None:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        source = template('{{ before() }}')
        child = Document()
        paragraph = child.add_paragraph('unused')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        child_buffer = io.BytesIO()
        child.save(child_buffer)
        child_data = child_buffer.getvalue()

        def run(native: bool, asynchronous: bool) -> tuple[bytes, bytes, list[str]]:
            events: list[str] = []

            def random_value() -> float:
                events.append('numbering')
                return 0.125

            def before() -> str:
                events.append('before')
                return ''

            module = docxtpl_native if native else docxtpl
            with numbering_probe(events, random_value):
                document = module.DocxTemplate(io.BytesIO(source))
                document.new_subdoc(io.BytesIO(child_data))
                constructed = io.BytesIO()
                document.get_docx().save(constructed)
                document.render({'before': before}, jinja_env=jinja2.Environment(enable_async=asynchronous))
                rendered = io.BytesIO()
                document.save(rendered)
            return constructed.getvalue(), rendered.getvalue(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                before_ref, after_ref, expected = run(False, asynchronous)
                before_native, after_native, actual = run(True, asynchronous)
                self.assertEqual(expected, ['numbering', 'before'])
                self.assertEqual(actual, expected)
                comparison = compare_numbered(before_ref, before_native)
                self.assertTrue(comparison['equivalent'], comparison)
                comparison = compare_numbered(after_ref, after_native)
                self.assertTrue(comparison['equivalent'], comparison)

    def test_public_subdocument_numbering_error_occurs_during_construction(self) -> None:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        source = template('unused')
        child = Document()
        paragraph = child.add_paragraph('numbered')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        child_buffer = io.BytesIO()
        child.save(child_buffer)
        failure = ValueError('numbering callback failed')

        def fail() -> float:
            raise failure

        for module in (docxtpl, docxtpl_native):
            with self.subTest(module=module.__name__), numbering_probe([], fail):
                document = module.DocxTemplate(io.BytesIO(source))
                with self.assertRaises(ValueError) as caught:
                    document.new_subdoc(io.BytesIO(child_buffer.getvalue()))
                self.assertIs(caught.exception, failure)

    def test_registered_subdocument_direct_numbering_matches_document(self) -> None:
        import base64
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        source = template('{{ value }}')
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        buffer = io.BytesIO()
        child.save(buffer)
        child_bytes = buffer.getvalue()

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool) -> tuple[bytes, list[str]]:
                    events: list[str] = []
                    def random_value() -> float:
                        events.append('numbering')
                        return 0.12345678

                    environment = jinja2.Environment(enable_async=asynchronous)
                    with numbering_probe(events, random_value):
                        if native:
                            value = object()
                            payload = json.dumps({
                                'specials': [{'identity': id(value), 'value': {
                                    'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                                }}],
                                'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                            })
                            output, _ = docxtpl_native._extension().process_python_docx(
                                source, {'value': value}, BridgeConfig(environment, False), payload, False,
                            )
                            return bytes(output), events
                        document = docxtpl.DocxTemplate(io.BytesIO(source))
                        value = document.new_subdoc(io.BytesIO(child_bytes))
                        document.render({'value': value}, jinja_env=environment)
                        output = io.BytesIO()
                        document.save(output)
                        return output.getvalue(), events

                reference, expected_events = run(False)
                candidate, actual_events = run(True)
                self.assertEqual(expected_events, ['numbering'])
                self.assertEqual(actual_events, expected_events)
                comparison = compare_numbered(reference, candidate)
                self.assertTrue(comparison['equivalent'], comparison)

    def test_registered_subdocument_numbering_draw_precedes_render_callback(self) -> None:
        import base64
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        source = template('{{ before() }}{{ value }}')
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        buffer = io.BytesIO()
        child.save(buffer)
        child_bytes = buffer.getvalue()

        def run(native: bool, asynchronous: bool) -> list[str]:
            events: list[str] = []
            def before() -> str:
                events.append('before')
                return ''
            def random_value() -> float:
                events.append('numbering')
                return 0.12345678

            environment = jinja2.Environment(enable_async=asynchronous)
            with numbering_probe(events, random_value):
                if native:
                    value = object()
                    payload = json.dumps({
                        'specials': [{'identity': id(value), 'value': {
                            'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                        }}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    docxtpl_native._extension().process_python_docx(
                        source, {'value': value, 'before': before}, BridgeConfig(environment, False), payload, False,
                    )
                else:
                    document = docxtpl.DocxTemplate(io.BytesIO(source))
                    value = document.new_subdoc(io.BytesIO(child_bytes))
                    document.render({'value': value, 'before': before}, jinja_env=environment)
            return events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ['numbering', 'before'])
                self.assertEqual(run(True, asynchronous), expected)

    def test_unused_registered_subdocument_attaches_before_compilation(self) -> None:
        import base64

        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        buffer = io.BytesIO()
        child.save(buffer)
        child_bytes = buffer.getvalue()
        source = template('plain document')

        def run(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            def random_value() -> float:
                events.append('numbering')
                return 0.12345678

            def autoescape(name: str | None) -> bool:
                events.append('autoescape')
                return False

            environment = jinja2.Environment(enable_async=asynchronous, autoescape=autoescape)
            with numbering_probe(events, random_value):
                if native:
                    value = object()
                    payload = json.dumps({
                        'specials': [{'identity': id(value), 'value': {
                            'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                        }}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    output, _ = docxtpl_native._extension().process_python_docx(
                        source, {'value': value}, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(output), events
                document = docxtpl.DocxTemplate(io.BytesIO(source))
                value = document.new_subdoc(io.BytesIO(child_bytes))
                document.render({'value': value}, jinja_env=environment)
                output = io.BytesIO()
                document.save(output)
                return output.getvalue(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, expected = run(False, asynchronous)
                candidate, actual = run(True, asynchronous)
                self.assertEqual(expected[0], 'numbering')
                self.assertEqual(actual, expected)
                comparison = compare_numbered(reference, candidate)
                self.assertTrue(comparison['equivalent'], comparison)

    def test_registered_subdocuments_attach_in_registration_order(self) -> None:
        import base64

        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        children: list[bytes] = []
        for text in ('first', 'second'):
            child = Document()
            paragraph = child.add_paragraph(text)
            numbering = OxmlElement('w:numPr')
            number_id = OxmlElement('w:numId')
            number_id.set(qn('w:val'), '5')
            numbering.append(number_id)
            paragraph._p.get_or_add_pPr().append(numbering)
            output = io.BytesIO()
            child.save(output)
            children.append(output.getvalue())
        source = template('{{ second }}{{ first }}')

        def run(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            def random_value() -> float:
                events.append('numbering')
                return 0.125

            environment = jinja2.Environment(enable_async=asynchronous)
            with numbering_probe(events, random_value):
                if native:
                    first, second = object(), object()
                    specials = [
                        {'identity': id(value), 'value': {
                            'kind': 'subdoc', 'data': base64.b64encode(data).decode('ascii'),
                        }}
                        for value, data in zip((first, second), children, strict=True)
                    ]
                    payload = json.dumps({
                        'specials': specials,
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    output, _ = docxtpl_native._extension().process_python_docx(
                        source, {'first': first, 'second': second}, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(output), events
                document = docxtpl.DocxTemplate(io.BytesIO(source))
                first = document.new_subdoc(io.BytesIO(children[0]))
                second = document.new_subdoc(io.BytesIO(children[1]))
                document.render({'first': first, 'second': second}, jinja_env=environment)
                output = io.BytesIO()
                document.save(output)
                return output.getvalue(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, expected = run(False, asynchronous)
                candidate, actual = run(True, asynchronous)
                self.assertEqual(expected, ['numbering'])
                self.assertEqual(actual, expected)
                # Registration order shows in which copied list each subdocument's
                # paragraphs refer to.
                comparison = compare_numbered(reference, candidate)
                self.assertTrue(comparison['equivalent'], comparison)

    def test_unused_registered_subdocument_error_precedes_compilation(self) -> None:
        import base64

        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        buffer = io.BytesIO()
        child.save(buffer)
        child_bytes = buffer.getvalue()
        source = template('plain document')

        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    events: list[str] = []
                    failure = ValueError('numbering failed')

                    def random_value(_events: list[str] = events, error: ValueError = failure) -> float:
                        _events.append('numbering')
                        raise error

                    def autoescape(name: str | None, _events: list[str] = events) -> bool:
                        _events.append('autoescape')
                        return False

                    environment = jinja2.Environment(enable_async=asynchronous, autoescape=autoescape)
                    with numbering_probe(events, random_value), self.assertRaises(ValueError) as caught:
                        if native:
                            value = object()
                            payload = json.dumps({
                                'specials': [{'identity': id(value), 'value': {
                                    'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                                }}],
                                'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                            })
                            docxtpl_native._extension().process_python_docx(
                                source, {'value': value}, BridgeConfig(environment, False), payload, False,
                            )
                        else:
                            document = docxtpl.DocxTemplate(io.BytesIO(source))
                            value = document.new_subdoc(io.BytesIO(child_bytes))
                            document.render({'value': value}, jinja_env=environment)
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(events, ['numbering'])

    def test_registered_subdocument_in_body_and_header_attaches_once(self) -> None:
        import base64

        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        master = Document()
        master.add_paragraph('{{ value }}')
        master.sections[0].header.paragraphs[0].text = '{{ value }}'
        output = io.BytesIO()
        master.save(output)
        source = output.getvalue()
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        output = io.BytesIO()
        child.save(output)
        child_bytes = output.getvalue()

        def run(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            def random_value() -> float:
                events.append('numbering')
                return 0.12345678

            environment = jinja2.Environment(enable_async=asynchronous)
            with numbering_probe(events, random_value):
                if native:
                    value = object()
                    payload = json.dumps({
                        'specials': [{'identity': id(value), 'value': {
                            'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                        }}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    result, _ = docxtpl_native._extension().process_python_docx(
                        source, {'value': value}, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(result), events
                document = docxtpl.DocxTemplate(io.BytesIO(source))
                value = document.new_subdoc(io.BytesIO(child_bytes))
                document.render({'value': value}, jinja_env=environment)
                result = io.BytesIO()
                document.save(result)
                return result.getvalue(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, expected = run(False, asynchronous)
                candidate, actual = run(True, asynchronous)
                self.assertEqual(expected, ['numbering'])
                self.assertEqual(actual, expected)
                comparison = compare_numbered(reference, candidate)
                self.assertTrue(comparison['equivalent'], comparison)

    def test_registered_image_and_subdocument_preserve_both_values(self) -> None:
        import base64

        from PIL import Image

        source = template('{{ image }}{{ child }}')
        child_document = Document()
        child_document.add_paragraph('child paragraph')
        child_stream = io.BytesIO()
        child_document.save(child_stream)
        child_bytes = child_stream.getvalue()
        image_stream = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_stream, format='PNG')
        image_bytes = image_stream.getvalue()

        def run(native: bool, asynchronous: bool) -> bytes:
            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                image, child = object(), object()
                payload = json.dumps({
                    'specials': [
                        {'identity': id(image), 'value': {
                            'kind': 'image', 'data': base64.b64encode(image_bytes).decode('ascii'),
                        }},
                        {'identity': id(child), 'value': {
                            'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                        }},
                    ],
                    'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                })
                result, _ = docxtpl_native._extension().process_python_docx(
                    source, {'image': image, 'child': child}, BridgeConfig(environment, False), payload, False,
                )
                return bytes(result)
            document = docxtpl.DocxTemplate(io.BytesIO(source))
            child = document.new_subdoc(io.BytesIO(child_bytes))
            image = docxtpl.InlineImage(document, io.BytesIO(image_bytes))
            document.render({'image': image, 'child': child}, jinja_env=environment)
            result = io.BytesIO()
            document.save(result)
            return result.getvalue()

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                comparison = compare_docx(run(False, asynchronous), run(True, asynchronous))
                self.assertTrue(comparison['equivalent'], comparison)

    def test_registered_subdocument_numbering_uses_live_integer_callback(self) -> None:
        import base64
        import builtins
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docxcompose import composer

        source = template('{{ value }}')
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        buffer = io.BytesIO()
        child.save(buffer)
        child_bytes = buffer.getvalue()

        def run(native: bool, asynchronous: bool) -> tuple[bytes, list[Any]]:
            events: list[Any] = []
            def integer(value: Any) -> int:
                events.append(value)
                return builtins.int(value)

            environment = jinja2.Environment(enable_async=asynchronous)
            with (patch.object(composer, 'int', integer, create=True),
                  patch.object(composer.random, 'random', return_value=0.125)):
                if native:
                    value = object()
                    payload = json.dumps({
                        'specials': [{'identity': id(value), 'value': {
                            'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                        }}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    output, _ = docxtpl_native._extension().process_python_docx(
                        source, {'value': value}, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(output), events
                document = docxtpl.DocxTemplate(io.BytesIO(source))
                value = document.new_subdoc(io.BytesIO(child_bytes))
                document.render({'value': value}, jinja_env=environment)
                output = io.BytesIO()
                document.save(output)
                return output.getvalue(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, expected = run(False, asynchronous)
                candidate, actual = run(True, asynchronous)
                # The reference also converts its random nsid sample
                # (0.125 * 10**8); the native nsid is not drawn.
                self.assertEqual(expected[-1], 12_500_000.0)
                self.assertTrue(expected[:-1])
                self.assertEqual(actual, expected[:-1])
                self.assertTrue(compare_numbered(reference, candidate)['equivalent'])

    def test_registered_subdocument_numbering_callback_error_keeps_identity(self) -> None:
        import base64
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        source = template('{{ value }}')
        child = Document()
        paragraph = child.add_paragraph('numbered paragraph')
        numbering = OxmlElement('w:numPr')
        number_id = OxmlElement('w:numId')
        number_id.set(qn('w:val'), '5')
        numbering.append(number_id)
        paragraph._p.get_or_add_pPr().append(numbering)
        buffer = io.BytesIO()
        child.save(buffer)
        child_bytes = buffer.getvalue()

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                failure = ValueError('numbering callback failed')
                def fail() -> float:
                    raise failure

                environment = jinja2.Environment(enable_async=asynchronous)
                with numbering_probe([], fail):
                    try:
                        docxtpl.DocxTemplate(io.BytesIO(source)).new_subdoc(io.BytesIO(child_bytes))
                    except ValueError as error:
                        self.assertIs(error, failure)
                    else:
                        self.fail('expected reference numbering callback failure')
                with numbering_probe([], fail):
                    value = object()
                    payload = json.dumps({
                        'specials': [{'identity': id(value), 'value': {
                            'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                        }}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    try:
                        docxtpl_native._extension().process_python_docx(
                            source, {'value': value}, BridgeConfig(environment, False), payload, False,
                        )
                    except ValueError as error:
                        self.assertIs(error, failure)
                    else:
                        self.fail('expected numbering callback failure')

    def test_registered_subdocument_with_existing_styles_matches_document(self) -> None:
        import base64

        for style in ('Heading 1', 'List Number', 'List Bullet'):
            with self.subTest(style=style):
                source = template('{{ value }}')
                child = Document()
                child.add_paragraph('child paragraph', style=style)
                buffer = io.BytesIO()
                child.save(buffer)
                child_bytes = buffer.getvalue()
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                value = reference.new_subdoc(io.BytesIO(child_bytes))
                reference.render({'value': value}, jinja_env=jinja2.Environment(enable_async=True))
                expected = io.BytesIO()
                reference.save(expected)

                value = object()
                payload = json.dumps({
                    'specials': [{'identity': id(value), 'value': {
                        'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                    }}],
                    'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                })
                output, _ = docxtpl_native._extension().process_python_docx(
                    source, {'value': value}, BridgeConfig(jinja2.Environment(enable_async=True), False), payload, False,
                )
                comparison = compare_docx(expected.getvalue(), bytes(output))
                self.assertTrue(comparison['equivalent'], comparison)

    def test_registered_subdocument_with_existing_styles_sync_matches_document(self) -> None:
        import base64

        for style in ('Heading 1', 'List Number', 'List Bullet'):
            with self.subTest(style=style):
                source = template('{{ value }}')
                child = Document()
                child.add_paragraph('child paragraph', style=style)
                buffer = io.BytesIO()
                child.save(buffer)
                child_bytes = buffer.getvalue()
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                value = reference.new_subdoc(io.BytesIO(child_bytes))
                reference.render({'value': value}, jinja_env=jinja2.Environment(enable_async=False))
                expected = io.BytesIO()
                reference.save(expected)

                value = object()
                payload = json.dumps({
                    'specials': [{'identity': id(value), 'value': {
                        'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                    }}],
                    'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                })
                output, _ = docxtpl_native._extension().process_python_docx(
                    source, {'value': value}, BridgeConfig(jinja2.Environment(enable_async=False), False), payload, False,
                )
                comparison = compare_docx(expected.getvalue(), bytes(output))
                self.assertTrue(comparison['equivalent'], comparison)

    def test_registered_subdocument_restarts_numbering_once_per_style(self) -> None:
        import base64

        source = template('{{ value }}')
        child = Document()
        child.add_paragraph('first item', style='List Number')
        child.add_paragraph('second item', style='List Number')
        buffer = io.BytesIO()
        child.save(buffer)
        child_bytes = buffer.getvalue()
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        value = reference.new_subdoc(io.BytesIO(child_bytes))
        reference.render({'value': value}, jinja_env=jinja2.Environment(enable_async=True))
        expected = io.BytesIO()
        reference.save(expected)
        value = object()
        payload = json.dumps({
            'specials': [{'identity': id(value), 'value': {
                'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
            }}],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        output, _ = docxtpl_native._extension().process_python_docx(
            source, {'value': value}, BridgeConfig(jinja2.Environment(enable_async=True), False), payload, False,
        )
        comparison = compare_docx(expected.getvalue(), bytes(output))
        self.assertTrue(comparison['equivalent'], comparison)

    def test_repeated_styled_subdocument_matches_complete_document(self) -> None:
        import base64

        for style in ('Heading 1', 'List Number', 'List Bullet'):
            with self.subTest(style=style):
                source = template('{{ value }}{{ value }}')
                child = Document()
                child.add_paragraph('child paragraph', style=style)
                buffer = io.BytesIO()
                child.save(buffer)
                child_bytes = buffer.getvalue()
                reference = docxtpl.DocxTemplate(io.BytesIO(source))
                value = reference.new_subdoc(io.BytesIO(child_bytes))
                reference.render({'value': value}, jinja_env=jinja2.Environment(enable_async=True))
                expected = io.BytesIO()
                reference.save(expected)
                value = object()
                payload = json.dumps({
                    'specials': [{'identity': id(value), 'value': {
                        'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                    }}],
                    'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                })
                output, _ = docxtpl_native._extension().process_python_docx(
                    source, {'value': value}, BridgeConfig(jinja2.Environment(enable_async=True), False), payload, False,
                )
                comparison = compare_docx(expected.getvalue(), bytes(output))
                self.assertTrue(comparison['equivalent'], comparison)

    def test_repeated_registered_subdocument_merges_numbering_once(self) -> None:
        import base64
        import xml.etree.ElementTree as ET

        child = Document()
        child.add_paragraph('child paragraph', style='Heading 1')
        buffer = io.BytesIO()
        child.save(buffer)
        child_bytes = buffer.getvalue()

        def numbering_count(native: bool, repeated: bool) -> int:
            expression = '{{ value }}' * (2 if repeated else 1)
            source = template(expression)
            environment = jinja2.Environment(enable_async=True)
            if native:
                value = object()
                payload = json.dumps({
                    'specials': [{'identity': id(value), 'value': {
                        'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
                    }}],
                    'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                })
                result, _ = docxtpl_native._extension().process_python_docx(
                    source, {'value': value}, BridgeConfig(environment, False), payload, False,
                )
                output = bytes(result)
            else:
                document = docxtpl.DocxTemplate(io.BytesIO(source))
                value = document.new_subdoc(io.BytesIO(child_bytes))
                document.render({'value': value}, jinja_env=environment)
                result = io.BytesIO()
                document.save(result)
                output = result.getvalue()
            with zipfile.ZipFile(io.BytesIO(output)) as archive:
                root = ET.fromstring(archive.read('word/numbering.xml'))
            return len(root.findall('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}num'))

        reference_single = numbering_count(False, False)
        reference_repeat = numbering_count(False, True)
        self.assertEqual(reference_repeat, reference_single)
        self.assertEqual(numbering_count(True, True), numbering_count(True, False))

    def test_repeated_registered_image_keeps_each_insertion(self) -> None:
        import base64
        from PIL import Image

        source = template('{{ value }}{{ value }}')
        image_bytes = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_bytes, format='PNG')
        document = docxtpl.DocxTemplate(io.BytesIO(source))
        value = docxtpl.InlineImage(document, io.BytesIO(image_bytes.getvalue()))
        document.render({'value': value}, jinja_env=jinja2.Environment(enable_async=True))
        reference = io.BytesIO()
        document.save(reference)

        value = object()
        payload = json.dumps({
            'specials': [{'identity': id(value), 'value': {
                'kind': 'image', 'data': base64.b64encode(image_bytes.getvalue()).decode('ascii'),
            }}],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        output, _ = docxtpl_native._extension().process_python_docx(
            source, {'value': value}, BridgeConfig(jinja2.Environment(enable_async=True), False), payload, False,
        )
        candidate = bytes(output)
        self.assertEqual(len(Document(io.BytesIO(candidate)).inline_shapes), 2)
        self.assertTrue(compare_docx(reference.getvalue(), candidate)['equivalent'])

    def test_repeated_registered_subdocument_matches_complete_document(self) -> None:
        import base64

        source = template('{{ value }}{{ value }}')
        child = Document()
        child.add_paragraph('child paragraph')
        buffer = io.BytesIO()
        child.save(buffer)
        child_bytes = buffer.getvalue()
        reference = docxtpl.DocxTemplate(io.BytesIO(source))
        value = reference.new_subdoc(io.BytesIO(child_bytes))
        reference.render({'value': value}, jinja_env=jinja2.Environment(enable_async=True))
        expected = io.BytesIO()
        reference.save(expected)

        value = object()
        payload = json.dumps({
            'specials': [{'identity': id(value), 'value': {
                'kind': 'subdoc', 'data': base64.b64encode(child_bytes).decode('ascii'),
            }}],
            'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
        })
        output, _ = docxtpl_native._extension().process_python_docx(
            source, {'value': value}, BridgeConfig(jinja2.Environment(enable_async=True), False), payload, False,
        )
        self.assertTrue(compare_docx(expected.getvalue(), bytes(output))['equivalent'])

    def test_invalid_registered_image_stops_before_later_callback(self) -> None:
        import base64

        source = template('{{ value }}{{ after() }}')
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            def after() -> str:
                events.append('after')
                return 'unexpected'
            environment = jinja2.Environment(enable_async=True)
            try:
                if native:
                    value = object()
                    payload = json.dumps({
                        'specials': [{'identity': id(value), 'value': {
                            'kind': 'image', 'data': base64.b64encode(b'invalid').decode('ascii'),
                        }}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    docxtpl_native._extension().process_python_docx(
                        source, {'value': value, 'after': after}, BridgeConfig(environment, False), payload, False,
                    )
                else:
                    document = docxtpl.DocxTemplate(io.BytesIO(source))
                    value = docxtpl.InlineImage(document, io.BytesIO(b'invalid'))
                    document.render({'value': value, 'after': after}, jinja_env=environment)
            except Exception as error:  # noqa: BLE001 - compare callback timing separately from exception parity
                return type(error).__name__, events
            raise AssertionError('expected invalid image failure')

        expected_error, expected_events = run(False)
        actual_error, actual_events = run(True)
        self.assertTrue(expected_error)
        self.assertTrue(actual_error)
        self.assertEqual(expected_events, [])
        self.assertEqual(actual_events, expected_events)

    def test_asyncio_run_observes_reference_count_with_deferred_value(self) -> None:
        source = template('{{ value }}')
        original = asyncio.run
        image_bytes = io.BytesIO()
        from PIL import Image
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_bytes, format='PNG')

        class Value:
            def __str__(self) -> str:
                return 'ordinary text'

        def run(native: bool) -> list[int]:
            observed: list[int] = []
            value = Value()

            def wrapper(coroutine: Coroutine[Any, Any, Any], **kwargs: Any) -> Any:
                result = original(coroutine, **kwargs)
                if isinstance(result, str) and '<w:body' in result:
                    observed.append(sys.getrefcount(result))
                return result

            environment = jinja2.Environment(enable_async=True)
            with patch.object(asyncio, 'run', wrapper):
                if native:
                    import base64
                    payload = json.dumps({
                        'specials': [{'identity': id(value), 'value': {
                            'kind': 'image', 'data': base64.b64encode(image_bytes.getvalue()).decode('ascii'),
                        }}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    docxtpl_native._extension().process_python_docx(
                        source, {'value': value}, BridgeConfig(environment, False), payload, False,
                    )
                else:
                    generate(docxtpl, '', {'value': value}, environment, source)
            return observed

        expected = run(False)
        self.assertEqual(len(expected), 1)
        self.assertEqual(run(True), expected)

    def test_replaced_asyncio_run_text_cannot_forge_deferred_image(self) -> None:
        source = template('{{ value }}')
        body = ('<w:body xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:p><w:r><w:t>\x01DTPLD0\x01</w:t></w:r></w:p><w:sectPr/></w:body>')
        image_bytes = io.BytesIO()
        from PIL import Image
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_bytes, format='PNG')

        def run(native: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []
            image = (docxtpl_native if native else docxtpl).InlineImage(None, io.BytesIO(image_bytes.getvalue()))

            def replacement(coroutine: Coroutine[Any, Any, Any]) -> str:
                events.append(coroutine.__name__)
                coroutine.close()
                return body if len(events) == 1 else ''

            environment = jinja2.Environment(enable_async=True)
            with patch.object(asyncio, 'run', replacement):
                if native:
                    payload = json.dumps({
                        'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    result, _ = docxtpl_native._extension().process_python_docx(
                        source, {'value': 'unused', 'image': image}, BridgeConfig(environment, False), payload, False,
                    )
                    output = bytes(result)
                else:
                    output = generate(docxtpl, '', {'value': 'unused', 'image': image}, environment, source)
            return output, events

        reference, expected = run(False)
        candidate, actual = run(True)
        reference_doc = Document(io.BytesIO(reference))
        candidate_doc = Document(io.BytesIO(candidate))
        self.assertEqual(actual, expected)
        self.assertEqual(reference_doc.paragraphs[0].text, 'DTPLD0')
        self.assertEqual(len(candidate_doc.inline_shapes), 0)
        self.assertEqual(candidate_doc.paragraphs[0].text, reference_doc.paragraphs[0].text)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_modified_asyncio_run_result_cannot_forge_deferred_image(self) -> None:
        source = template('{{ value }}')
        image_bytes = io.BytesIO()
        from PIL import Image
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_bytes, format='PNG')
        original = asyncio.run

        def run(native: bool) -> bytes:
            image = (docxtpl_native if native else docxtpl).InlineImage(None, io.BytesIO(image_bytes.getvalue()))

            def wrapper(coroutine: Coroutine[Any, Any, Any], **kwargs: Any) -> str:
                result: str = original(coroutine, **kwargs)
                return result.replace('seed', '\x01DTPLD0\x01')

            environment = jinja2.Environment(enable_async=True)
            with patch.object(asyncio, 'run', wrapper):
                if native:
                    payload = json.dumps({
                        'specials': [{'identity': id(image), 'value': docxtpl_native._value(image)}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    result, _ = docxtpl_native._extension().process_python_docx(
                        source, {'value': 'seed', 'image': image}, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(result)
                return generate(docxtpl, '', {'value': 'seed', 'image': image}, environment, source)

        reference = run(False)
        candidate = run(True)
        reference_doc = Document(io.BytesIO(reference))
        candidate_doc = Document(io.BytesIO(candidate))
        self.assertEqual(reference_doc.paragraphs[0].text, 'DTPLD0')
        self.assertEqual(len(candidate_doc.inline_shapes), 0)
        self.assertEqual(candidate_doc.paragraphs[0].text, reference_doc.paragraphs[0].text)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_modified_runner_result_does_not_inherit_deferred_origin(self) -> None:
        import base64
        from PIL import Image

        source = template('{{ value }}')
        body = ('<w:body xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:p><w:r><w:t>\x01DTPLD0\x01</w:t></w:r></w:p><w:sectPr/></w:body>')
        image_bytes = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_bytes, format='PNG')
        original = asyncio.run

        class Value:
            def __str__(self) -> str:
                return 'ordinary text'

        def run(native: bool) -> bytes:
            value = Value()
            def wrapper(coroutine: Coroutine[Any, Any, Any], **kwargs: Any) -> str:
                result: str = original(coroutine, **kwargs)
                return body if '<w:body' in result else result

            environment = jinja2.Environment(enable_async=True)
            with patch.object(asyncio, 'run', wrapper):
                if native:
                    payload = json.dumps({
                        'specials': [{'identity': id(value), 'value': {
                            'kind': 'image', 'data': base64.b64encode(image_bytes.getvalue()).decode('ascii'),
                        }}],
                        'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
                    })
                    result, _ = docxtpl_native._extension().process_python_docx(
                        source, {'value': value}, BridgeConfig(environment, False), payload, False,
                    )
                    return bytes(result)
                document = docxtpl.DocxTemplate(io.BytesIO(source))
                image = docxtpl.InlineImage(document, io.BytesIO(image_bytes.getvalue()))
                document.render({'value': image}, jinja_env=environment)
                output = io.BytesIO()
                document.save(output)
                return output.getvalue()

        reference = run(False)
        candidate = run(True)
        candidate_doc = Document(io.BytesIO(candidate))
        self.assertEqual(candidate_doc.paragraphs[0].text, 'DTPLD0')
        self.assertEqual(len(candidate_doc.inline_shapes), 0)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_copied_unchanged_runner_result_preserves_deferred_value(self) -> None:
        import base64
        from PIL import Image

        source = template('{{ value }}')
        image_bytes = io.BytesIO()
        Image.new('RGB', (2, 2), (21, 42, 84)).save(image_bytes, format='PNG')
        original = asyncio.run

        def run(copy: bool) -> bytes:
            value = object()
            payload = json.dumps({
                'specials': [{'identity': id(value), 'value': {
                    'kind': 'image', 'data': base64.b64encode(image_bytes.getvalue()).decode('ascii'),
                }}],
                'replacements': {'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {}},
            })
            def wrapper(coroutine: Coroutine[Any, Any, Any], **kwargs: Any) -> str:
                result: str = original(coroutine, **kwargs)
                if copy and '<w:body' in result:
                    duplicate = (result + ' ')[:-1]
                    self.assertEqual(duplicate, result)
                    self.assertIsNot(duplicate, result)
                    return duplicate
                return result

            environment = jinja2.Environment(enable_async=True)
            with patch.object(asyncio, 'run', wrapper):
                output, _ = docxtpl_native._extension().process_python_docx(
                    source, {'value': value}, BridgeConfig(environment, False), payload, False,
                )
            return bytes(output)

        expected = run(False)
        candidate = run(True)
        self.assertEqual(len(Document(io.BytesIO(expected)).inline_shapes), 1)
        self.assertTrue(compare_docx(expected, candidate)['equivalent'])

    def test_modified_runner_result_after_materialization_is_safe(self) -> None:
        self.test_modified_runner_result_does_not_inherit_deferred_origin()

    def test_replaced_asyncio_run_error_precedes_context_and_keeps_identity(self) -> None:
        source = template('{{ value }}')
        marker = ValueError('runner failure')

        def run(native: bool) -> tuple[bool, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('context')
                    super().__init__(*args, **kwargs)

            def replacement(coroutine: Coroutine[Any, Any, Any]) -> str:
                events.append('run')
                coroutine.close()
                raise marker

            environment = jinja2.Environment(enable_async=True)
            environment.context_class = Context
            try:
                with patch.object(asyncio, 'run', replacement):
                    if native:
                        self.render_native(source, {'value': 'unused'}, environment)
                    else:
                        generate(docxtpl, '', {'value': 'unused'}, environment, source)
            except ValueError as error:
                return error is marker, events
            raise AssertionError('expected runner exception')

        expected = run(False)
        self.assertEqual(expected, (True, ['run']))
        self.assertEqual(run(True), expected)

    def test_extension_preprocess_observes_body_source(self) -> None:
        source = template('{{ value }}')

        def run(native: bool, asynchronous: bool) -> list[tuple[bool, bool]]:
            observations: list[tuple[bool, bool]] = []

            class ObserveSource(jinja2.ext.Extension):
                def preprocess(
                    self, source: str, name: str | None, filename: str | None = None,
                ) -> str:
                    if '<w:body' in source:
                        observations.append(('<w:document' in source, '<w:body' in source))
                    return source

            environment = jinja2.Environment(enable_async=asynchronous, extensions=[ObserveSource])
            if native:
                self.render_native(source, {'value': 'body'}, environment)
            else:
                generate(docxtpl, '', {'value': 'body'}, environment, source)
            return observations

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, [(False, True)])
                self.assertEqual(run(True, asynchronous), expected)

    def test_markup_outside_body_is_not_rendered(self) -> None:
        source = template('{{ value }}')
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(buffer, 'w') as output:
            for item in original.infolist():
                content = original.read(item.filename)
                if item.filename == 'word/document.xml':
                    content = content.replace(b'<w:body>', b'<w:background w:color="{{ callback() }}"/><w:body>', 1)
                output.writestr(item, content)
        source = buffer.getvalue()

        def run(native: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            def callback() -> str:
                events.append('outside')
                return 'FFFFFF'

            environment = jinja2.Environment(enable_async=True)
            context = {'value': 'body', 'callback': callback}
            document = (self.render_native(source, context, environment) if native
                        else generate(docxtpl, '', context, environment, source))
            return document, events

        reference, expected = run(False)
        self.assertEqual(expected, [])
        candidate, actual = run(True)
        self.assertEqual(actual, expected)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_async_filter_autoescape_avoids_original_runtime_bodies(self) -> None:
        source = template('{{ value|decorate }}')

        def run(native: bool) -> tuple[bytes, list[str], list[str]]:
            events: list[str] = []
            calls: list[str] = []

            async def decorate(value: str) -> str:
                events.append('before:' + value)
                await asyncio.sleep(0)
                events.append('after:' + value)
                return '<&'

            environment = jinja2.Environment(enable_async=True, autoescape=True)
            environment.filters['decorate'] = decorate
            codes = {jinja2.Template.render_async.__code__, jinja2.async_utils.auto_await.__code__,
                     jinja2.async_utils.auto_aiter.__code__}

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code in codes:
                    calls.append(frame.f_code.co_name)

            previous = sys.getprofile()
            if native:
                sys.setprofile(profile)
            try:
                output = (self.render_native(source, {'value': 'x'}, environment) if native
                          else generate(docxtpl, '', {'value': 'x'}, environment, source))
            finally:
                if native:
                    sys.setprofile(previous)
            return output, events, calls

        reference, expected, _ = run(False)
        self.assertEqual(expected, ['before:x', 'after:x'])
        candidate, actual, calls = run(True)
        self.assertEqual(actual, expected)
        self.assertEqual(calls, [])
        self.assertEqual(Document(io.BytesIO(candidate)).paragraphs[0].text, '<&')
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])


if __name__ == '__main__':
    unittest.main()
