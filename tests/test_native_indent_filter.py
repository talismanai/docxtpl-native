"""Differential and execution-boundary coverage for Jinja's indent filter."""

import gc
import sys
import unittest
import weakref
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from types import CodeType, FrameType
from typing import Any, Self
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
from jinja2 import filters
from markupsafe import Markup
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_filter_evaluation import render


@contextmanager
def no_python_indent() -> Generator[None, None, None]:
    code = filters.do_indent.__code__
    codes = {code, *(value for value in code.co_consts if isinstance(value, CodeType))}
    previous = sys.getprofile()
    calls: list[str] = []

    def profile(frame: FrameType, event: str, argument: object) -> None:
        if event == 'call' and frame.f_code in codes:
            calls.append(frame.f_code.co_qualname)

    try:
        sys.setprofile(profile)
        yield
    finally:
        sys.setprofile(previous)
    if calls:
        raise AssertionError('standard indent body executed: ' + repr(calls))


class NativeIndentFilterTests(unittest.TestCase):
    def test_saved_generator_with_multiple_references_releases_lines(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[str], list[str], bool]:
            events: list[str] = []
            saved: list[Iterator[Any]] = []
            underlying: list[Any] = []

            class Lines:
                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Iterator[str]:
                    return iter(('last',))

                def __del__(self) -> None:
                    events.append('lines:drop')

            class CustomMarkup(Markup):
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    return Lines()

                def join(self, iterable: Iterable[Any]) -> Self:
                    iterator: Any = iter(iterable)
                    saved.extend([iterator, iterator])
                    underlying.append(iterator.gi_frame.f_locals.pop('.0'))
                    return type(self)('joined')

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('input')}, native, environment)
            before_consumption = list(events)
            same = saved[0] is saved[1]
            values = [str(item) for item in saved[0]]
            return output, before_consumption, values, same

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ('first\njoined', ['lines:drop'], ['  last'], True))
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_custom_join_callable_object(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Join:
                def __call__(self, iterable: Iterable[Any]) -> str:
                    events.append('call')
                    return ':'.join(str(item) for item in iterable)

            class CustomMarkup(Markup):
                @property
                def join(self) -> Join:  # ty: ignore[invalid-property-type-override]
                    events.append('lookup')
                    return Join()

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('first\nlast')}, native, environment)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ('first\n  last', ['lookup', 'call']))
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_custom_join_observes_generator_weakrefs(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[int]]:
            observed: list[int] = []

            class CustomMarkup(Markup):
                def join(self, iterable: Iterable[Any]) -> Self:
                    observed.append(len(weakref.getweakrefs(iterable)))
                    return type(self)(''.join(iterable))

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('first\nlast')}, native, environment)
            return output, observed

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_custom_join_observes_generator_refcount(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[tuple[int, int]]]:
            observed: list[tuple[int, int]] = []

            class CustomMarkup(Markup):
                def join(self, iterable: Iterable[Any]) -> Self:
                    observed.append((len(weakref.getweakrefs(iterable)), sys.getrefcount(iterable)))
                    return type(self)(''.join(iterable))

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('first\nlast')}, native, environment)
            return output, observed

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_custom_join_observes_generator_frame_refcount(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[int]]:
            observed: list[int] = []

            class CustomMarkup(Markup):
                def join(self, iterable: Iterable[Any]) -> Self:
                    generator: Any = iterable
                    observed.append(sys.getrefcount(generator.gi_frame))
                    return type(self)(''.join(iterable))

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('first\nlast')}, native, environment)
            return output, observed

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_thrown_generator_error_keeps_lines_alive_with_traceback(self) -> None:
        def run(native: bool, asynchronous: bool, started: bool,
                document: bool = False) -> tuple[Any, list[str], list[str]]:
            events: list[str] = []
            marker = ValueError('injected')

            class Lines:
                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Iterator[str]:
                    return iter(('last',))

                def __del__(self) -> None:
                    events.append('lines:drop')

            class CustomMarkup(Markup):
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    return Lines()

                def join(self, iterable: Iterable[Any]) -> Self:
                    generator: Any = iter(iterable)
                    if started:
                        events.append('next:' + str(next(generator)))
                    try:
                        generator.throw(marker)
                    except ValueError as error:
                        events.append('caught:' + str(error is marker))
                    return type(self)('joined')

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                source = '{{ value|indent(2,false,false) }}'
                values = {'value': CustomMarkup('input')}
                output = (generate(docxtpl_native if native else docxtpl, source, values, environment)
                          if document else render(source, values, native, environment))
            before_cleanup = list(events)
            marker.__traceback__ = None
            gc.collect()
            return output, before_cleanup, events

        for asynchronous in (False, True):
            for started in (False, True):
                with self.subTest(asynchronous=asynchronous, started=started):
                    expected = run(False, asynchronous, started)
                    first = ['next:  last'] if started else []
                    self.assertEqual(expected, ('first\njoined', first + ['caught:True'],
                                                first + ['caught:True', 'lines:drop']))
                    with no_python_indent():
                        self.assertEqual(run(True, asynchronous, started), expected)
                    reference, before_reference, after_reference = run(False, asynchronous, started, document=True)
                    with no_python_indent():
                        candidate, before_candidate, after_candidate = run(True, asynchronous, started, document=True)
                    self.assertEqual((before_candidate, after_candidate), (before_reference, after_reference))
                    self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_collect_during_line_callback_keeps_active_lines_alive(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[bool], bool]:
            reference: list[weakref.ReferenceType[Any]] = []
            observed: list[bool] = []

            class Line(str):
                def __bool__(self) -> bool:
                    gc.collect()
                    observed.append(reference[0]() is not None)
                    return True

            class Lines:
                owner: object | None = None

                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Iterator[Line]:
                    return iter((Line('last'),))

            class CustomMarkup(Markup):
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    lines = Lines()
                    reference.append(weakref.ref(lines))
                    return lines

                def join(self, iterable: Iterable[Any]) -> Self:
                    lines = reference[0]()
                    if lines is None:
                        raise AssertionError('lines disappeared during join')
                    lines.owner = iterable
                    return type(self)(''.join(iterable))

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('input')}, native, environment)
            gc.collect()
            return output, observed, reference[0]() is None

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ('first\n  last', [True], True))
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_caught_error_cycle_via_lines_is_collected(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, bool, bool]:
            references: list[weakref.ReferenceType[Any]] = []
            marker = ValueError('line failed')

            class Line(str):
                def __bool__(self) -> bool:
                    raise marker

            class Lines:
                owner: object | None = None

                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Iterator[Line]:
                    return iter((Line('last'),))

            class CustomMarkup(Markup):
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    lines = Lines()
                    references.append(weakref.ref(lines))
                    return lines

                def join(self, iterable: Iterable[Any]) -> Self:
                    lines = references[0]()
                    if lines is None:
                        raise AssertionError('lines disappeared during join')
                    lines.owner = iterable
                    references.append(weakref.ref(iterable))
                    try:
                        next(iter(iterable))
                    except ValueError as error:
                        if error is not marker:
                            raise AssertionError('exception identity changed') from error
                    return type(self)('joined')

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('input')}, native, environment)
            marker.__traceback__ = None
            gc.collect()
            return output, references[0]() is None, references[1]() is None

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ('first\njoined', True, True))
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_caught_error_observes_reference_count_of_lines(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[int]]:
            reference: list[weakref.ReferenceType[Any]] = []
            counts: list[int] = []
            marker = ValueError('line failed')

            class Line(str):
                def __bool__(self) -> bool:
                    raise marker

            class Lines:
                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Iterator[Line]:
                    return iter((Line('last'),))

            class CustomMarkup(Markup):
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    lines = Lines()
                    reference.append(weakref.ref(lines))
                    return lines

                def join(self, iterable: Iterable[Any]) -> Self:
                    try:
                        next(iter(iterable))
                    except ValueError as error:
                        self_error = error is marker
                        counts.append(sys.getrefcount(reference[0]()))
                        if not self_error:
                            raise AssertionError('exception identity changed') from error
                    return type(self)('joined')

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('input')}, native, environment)
            marker.__traceback__ = None
            return output, counts

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_join_error_retains_lines_until_traceback_release(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[list[str], list[str]]:
            events: list[str] = []
            marker = ValueError('join failed')

            class Lines:
                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Iterator[str]:
                    return iter(('last',))

                def __del__(self) -> None:
                    events.append('lines:drop')

            class CustomMarkup(Markup):
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    return Lines()

                def join(self, iterable: Iterable[Any]) -> Self:
                    events.append('join:error')
                    raise marker

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                try:
                    render('{{ value|indent(2,false,false) }}',
                           {'value': CustomMarkup('input')}, native, environment)
                except ValueError as error:
                    events.append('caught:' + str(error is marker))
                else:
                    raise AssertionError('expected join error')
            before_cleanup = list(events)
            marker.__traceback__ = None
            gc.collect()
            return before_cleanup, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, (['join:error', 'caught:True'],
                                            ['join:error', 'caught:True', 'lines:drop']))
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_saved_generator_error_after_render_does_not_retain_expired_lines(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            saved: list[Iterator[Any]] = []
            marker = ValueError('late failure')

            class Line(str):
                def __bool__(self) -> bool:
                    events.append('line:truth')
                    raise marker

            class Lines:
                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Iterator[Line]:
                    return iter((Line('last'),))

                def __del__(self) -> None:
                    events.append('lines:drop')

            class CustomMarkup(Markup):
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    return Lines()

                def join(self, iterable: Iterable[Any]) -> Self:
                    saved.append(iter(iterable))
                    return type(self)('joined')

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('input')}, native, environment)
            try:
                next(saved[0])
            except ValueError as error:
                events.append('caught:' + str(error is marker))
            else:
                raise AssertionError('expected late iterator error')
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ('first\njoined', ['lines:drop', 'line:truth', 'caught:True']))
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_join_observes_reference_count_of_lines(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[int]]:
            observed: list[int] = []
            reference: list[weakref.ReferenceType[Any]] = []

            class Lines:
                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Iterator[str]:
                    return iter(('last',))

            class CustomMarkup(Markup):
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    lines = Lines()
                    reference.append(weakref.ref(lines))
                    return lines

                def join(self, iterable: Iterable[Any]) -> Self:
                    observed.append(sys.getrefcount(reference[0]()))
                    return type(self)(''.join(iterable))

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('input')}, native, environment)
            return output, observed

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_saved_generator_does_not_retain_lines_without_error(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[str], list[str]]:
            events: list[str] = []
            saved: list[Iterator[Any]] = []

            class Lines:
                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Iterator[str]:
                    return iter(('last',))

                def __del__(self) -> None:
                    events.append('lines:drop')

            class CustomMarkup(Markup):
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    return Lines()

                def join(self, iterable: Iterable[Any]) -> Self:
                    saved.append(iter(iterable))
                    return type(self)('joined')

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('input')}, native, environment)
            before_consumption = list(events)
            values = [str(item) for item in saved[0]]
            return output, before_consumption, values

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ('first\njoined', ['lines:drop'], ['  last']))
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)

    def test_caught_iterator_error_keeps_lines_alive_with_traceback(self) -> None:
        def run(native: bool, asynchronous: bool, failure: str,
                document: bool = False) -> tuple[Any, list[str], list[str]]:
            events: list[str] = []
            marker = ValueError('iterator failed')

            class Line(str):
                def __bool__(self) -> bool:
                    if failure == 'truth':
                        events.append('line:error')
                        raise marker
                    return True

                def __del__(self) -> None:
                    events.append('line:drop')

            class Items:
                used = False

                def __iter__(self) -> Self:
                    return self

                def __next__(self) -> Line:
                    if self.used:
                        events.append('iterator:error')
                        raise marker
                    self.used = True
                    return Line('last')

                def __del__(self) -> None:
                    events.append('iterator:drop')

            class Lines:
                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Items:
                    return Items()

                def __del__(self) -> None:
                    events.append('lines:drop')

            class CustomMarkup(Markup):
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    return Lines()

                def join(self, iterable: Iterable[Any]) -> Self:
                    try:
                        for value in iterable:
                            events.append('yield:' + str(value))
                    except ValueError as error:
                        events.append('caught:' + str(error is marker))
                    events.append('join:end')
                    return type(self)('joined')

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                source = '{{ value|indent(2,false,false) }}'
                values = {'value': CustomMarkup('input')}
                output = (generate(docxtpl_native if native else docxtpl, source, values, environment)
                          if document else render(source, values, native, environment))
            before_cleanup = list(events)
            marker.__traceback__ = None
            gc.collect()
            return output, before_cleanup, events

        for asynchronous in (False, True):
            for failure in ('iterator', 'truth'):
                with self.subTest(asynchronous=asynchronous, failure=failure):
                    expected = run(False, asynchronous, failure)
                    first = (['yield:  last', 'iterator:error'] if failure == 'iterator' else ['line:error'])
                    self.assertEqual(expected[1], first + ['caught:True', 'join:end'])
                    self.assertCountEqual(expected[2][-3:], ['iterator:drop', 'line:drop', 'lines:drop'])
                    with no_python_indent():
                        self.assertEqual(run(True, asynchronous, failure), expected)
                    reference, before_reference, after_reference = run(False, asynchronous, failure, document=True)
                    with no_python_indent():
                        candidate, before_candidate, after_candidate = run(True, asynchronous, failure, document=True)
                    self.assertEqual((before_candidate, after_candidate), (before_reference, after_reference))
                    self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_exhausted_generator_releases_iterator_before_last_line(self) -> None:
        def run(native: bool, asynchronous: bool, document: bool = False) -> tuple[Any, list[str]]:
            events: list[str] = []

            class Line(str):
                def __del__(self) -> None:
                    events.append('line:drop')

            class Items:
                used = False

                def __iter__(self) -> Self:
                    return self

                def __next__(self) -> Line:
                    if self.used:
                        events.append('iterator:end')
                        raise StopIteration
                    self.used = True
                    return Line('last')

                def __del__(self) -> None:
                    events.append('iterator:drop')

            class Lines:
                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Items:
                    return Items()

                def __del__(self) -> None:
                    events.append('lines:drop')

            class CustomMarkup(Markup):
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    return Lines()

                def join(self, iterable: Iterable[Any]) -> Self:
                    for value in iterable:
                        events.append('yield:' + str(value))
                    events.append('join:end')
                    return type(self)('joined')

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                source = '{{ value|indent(2,false,false) }}'
                values = {'value': CustomMarkup('input')}
                output = (generate(docxtpl_native if native else docxtpl, source, values, environment)
                          if document else render(source, values, native, environment))
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ('first\njoined', [
                    'yield:  last', 'iterator:end', 'iterator:drop', 'line:drop', 'join:end', 'lines:drop',
                ]))
                with no_python_indent():
                    self.assertEqual(run(True, asynchronous), expected)
                reference, reference_events = run(False, asynchronous, document=True)
                with no_python_indent():
                    candidate, candidate_events = run(True, asynchronous, document=True)
                self.assertEqual(candidate_events, reference_events)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_line_callback_reentering_running_generator(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[tuple[str, str]]]:
            events: list[tuple[str, str]] = []
            state: dict[str, Any] = {}

            class Line(str):
                def __bool__(self) -> bool:
                    try:
                        state['generator'].send(None)
                    except ValueError as error:
                        events.append((type(error).__name__, str(error)))
                    return True

            class CustomMarkup(Markup):
                # Return a line with a user-defined truth protocol.
                def splitlines(self, keepends: bool = False) -> list[str]:  # ty: ignore[invalid-method-override]
                    return ['first', Line('last')]

                def join(self, iterable: Iterable[Any]) -> Self:
                    state['generator'] = iter(iterable)
                    return type(self)(''.join(state['generator']))

            environment = jinja2.Environment(enable_async=asynchronous)
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('input')}, native, environment)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected[1], [('ValueError', 'generator already executing')])
                with no_python_indent():
                    actual = run(True, asynchronous)
                self.assertEqual(actual, expected)

    def test_custom_join_drives_generator_send_and_throw(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            events: list[object] = []
            marker = ValueError('injected')

            class CustomMarkup(Markup):
                def join(self, iterable: Iterable[Any]) -> Self:
                    iterator: Any = iter(iterable)
                    events.append(('next', str(next(iterator))))
                    events.append(('send', str(iterator.send('resume'))))
                    try:
                        iterator.throw(marker)
                    except ValueError as error:
                        events.append(('throw', error is marker))
                    try:
                        next(iterator)
                    except StopIteration:
                        events.append('closed')
                    return type(self)('done')

            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('first\nA\nB')}, native)
            return output, events

        expected = run(False)
        with no_python_indent():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_suspended_generator_cycle_through_current_line_is_collected(self) -> None:
        def run(native: bool) -> tuple[str, bool, bool]:
            events: list[str] = []
            state: dict[str, Any] = {}
            references: list[weakref.ReferenceType[Any]] = []

            class Line(str):
                owner: object | None = None

                def __del__(self) -> None:
                    events.append('line:drop')

            class Items:
                def __iter__(self) -> Self:
                    return self

                def __next__(self) -> Line:
                    line = Line('last')
                    state['line'] = line
                    return line

            class Lines:
                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Items:
                    return Items()

            class CustomMarkup(Markup):
                # The test deliberately returns a foreign splitlines object.
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    return Lines()

                def join(self, iterable: Iterable[Any]) -> Self:
                    iterator = iter(iterable)
                    references.append(weakref.ref(iterator))
                    next(iterator)
                    state.pop('line').owner = iterator
                    return type(self)('saved')

            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('input')}, native)
            gc.collect()
            return output, references[0]() is None, 'line:drop' in events

        expected = run(False)
        self.assertEqual(expected[1:], (True, True))
        with no_python_indent():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_abandoned_generator_cycle_is_collected(self) -> None:
        def run(native: bool) -> tuple[str, bool, bool]:
            events: list[str] = []
            state: dict[str, Any] = {}
            references: list[weakref.ReferenceType[Any]] = []

            class Items:
                owner: object | None = None

                def __iter__(self) -> Self:
                    return self

                def __next__(self) -> str:
                    raise StopIteration

                def __del__(self) -> None:
                    events.append('iterator:drop')

            class Lines:
                def pop(self, index: int) -> str:
                    return 'first'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Items:
                    iterator = Items()
                    state['iterator'] = iterator
                    return iterator

            class CustomMarkup(Markup):
                # The test deliberately returns a foreign splitlines object.
                def splitlines(self, keepends: bool = False) -> Lines:  # ty: ignore[invalid-method-override]
                    return Lines()

                def join(self, iterable: Iterable[Any]) -> Self:
                    references.append(weakref.ref(iterable))
                    state.pop('iterator').owner = iterable
                    return type(self)('saved')

            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('input')}, native)
            gc.collect()
            return output, references[0]() is None, 'iterator:drop' in events

        expected = run(False)
        self.assertEqual(expected[1:], (True, True))
        with no_python_indent():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_custom_join_can_consume_generator_after_render(self) -> None:
        def run(native: bool) -> tuple[str, list[tuple[str, str]]]:
            saved: list[Iterator[Any]] = []

            class CustomMarkup(Markup):
                def join(self, iterable: Iterable[Any]) -> Self:
                    saved.append(iter(iterable))
                    return type(self)('saved')

            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('first\nlast')}, native)
            self.assertEqual(len(saved), 1)
            values = [(type(item).__name__, str(item)) for item in saved[0]]
            return output, values

        expected = run(False)
        with no_python_indent():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_custom_join_can_abandon_generator_without_evaluating_lines(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Line(str):
                def __bool__(self) -> bool:
                    events.append('line:bool')
                    return True

            class CustomMarkup(Markup):
                # Exercise the dynamic splitlines protocol with foreign values.
                def splitlines(self, keepends: bool = False) -> list[str]:  # ty: ignore[invalid-method-override]
                    events.append('splitlines')
                    return ['first', Line('last')]

                def join(self, iterable: Iterable[Any]) -> Self:
                    events.append('join:' + type(iterable).__name__)
                    return type(self)('ignored')

            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('input')}, native)
            return output, events

        expected = run(False)
        self.assertNotIn('line:bool', expected[1])
        with no_python_indent():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_generator_holds_previous_line_until_next_assignment(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Line(str):
                def __del__(self) -> None:
                    events.append('line:drop:' + str(self))

            class Lines:
                def pop(self, index: int) -> str:
                    return 'head'

                def __bool__(self) -> bool:
                    return True

                def __iter__(self) -> Iterator[Line]:
                    events.append('next:a')
                    yield Line('a')
                    events.append('next:b')
                    yield Line('b')
                    events.append('next:end')

            class Value:
                def __iadd__(self, other: object) -> Self:
                    return self

                def splitlines(self) -> Lines:
                    return Lines()

            result = render('{{ value|indent(2,false,false) }}', {'value': Value()}, native)
            return result, events

        expected = run(False)
        with no_python_indent():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_splitlines_result_lives_until_first_flag_is_evaluated(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Lines:
                def pop(self, index: int) -> str:
                    events.append('lines:pop')
                    return 'first'

                def __bool__(self) -> bool:
                    events.append('lines:bool')
                    return False

                def __del__(self) -> None:
                    events.append('lines:drop')

            class Value:
                def __iadd__(self, other: object) -> Self:
                    return self

                def splitlines(self) -> Lines:
                    return Lines()

            class Flag:
                def __bool__(self) -> bool:
                    events.append('first:bool')
                    return False

            result = render('{{ value|indent(2,first,false) }}',
                            {'value': Value(), 'first': Flag()}, native)
            return result, events

        expected = run(False)
        self.assertLess(expected[1].index('first:bool'), expected[1].index('lines:drop'))
        with no_python_indent():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_generator_wraps_stop_iteration_from_line_truth(self) -> None:
        def run(native: bool) -> tuple[str, str, bool]:
            marker = StopIteration('line failed')

            class Line(str):
                def __bool__(self) -> bool:
                    raise marker

            class Value:
                def __iadd__(self, other: object) -> Self:
                    return self

                def splitlines(self) -> list[str]:
                    return ['first', Line('last')]

            try:
                render('{{ value|indent }}', {'value': Value()}, native)
            except RuntimeError as error:
                return type(error).__name__, str(error), error.__cause__ is marker
            raise AssertionError('expected RuntimeError')

        expected = run(False)
        self.assertEqual(expected, ('RuntimeError', 'generator raised StopIteration', True))
        with no_python_indent():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_custom_join_observes_original_generator_code_identity(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            events: list[bool] = []
            original = next(value for value in filters.do_indent.__code__.co_consts
                            if isinstance(value, CodeType) and value.co_name == '<genexpr>')

            class CustomMarkup(Markup):
                def join(self, iterable: Iterable[Any]) -> Self:
                    events.append(getattr(iterable, 'gi_code', None) is original)
                    return type(self)(super().join(iterable))

            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                output = render('{{ value|indent(2,false,false) }}',
                                {'value': CustomMarkup('first\nlast')}, native)
            return output, events

        expected = run(False)
        self.assertEqual(expected[1], [True])
        with no_python_indent():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_values_constants_and_documents(self) -> None:
        cases = (
            ('', 4, False, False),
            ('a\n\nb\n', 2, True, False),
            ('a\r\nb\x85c\u2028', '--', False, True),
            ('a\n\nb', -2, True, True),
            (Markup('<a>\n\n&</a>'), 2, True, True),
        )
        for asynchronous in (False, True):
            for autoescape in (False, True):
                environment = jinja2.Environment(enable_async=asynchronous, autoescape=autoescape)
                for value, width, first, blank in cases:
                    context = {'value': value, 'width': width, 'first': first, 'blank': blank}
                    source = '{{ value|indent(width,first,blank) }}'
                    with self.subTest(asynchronous=asynchronous, autoescape=autoescape, context=context):
                        expected = render(source, context, False, environment)
                        with no_python_indent():
                            actual = render(source, context, True, environment)
                        self.assertEqual(actual, expected)
                source = "{{ 'a\\nb'|indent(2,true) }}"
                expected = render(source, {}, False, environment)
                with no_python_indent():
                    actual = render(source, {}, True, environment)
                self.assertEqual(actual, expected)

            source = '{{ value|indent(2,true,true) }}'
            context = {'value': 'a\nb'}
            environment = jinja2.Environment(enable_async=asynchronous)
            reference = generate(docxtpl, source, context, environment)
            with no_python_indent():
                candidate = generate(docxtpl_native, source, context, environment)
            self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_object_protocols_and_order(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Width:
                def __index__(self) -> int:
                    events.append('width:index')
                    return 2

            class Flag:
                def __init__(self, name: str, value: bool) -> None:
                    self.name, self.value = name, value

                def __bool__(self) -> bool:
                    events.append('flag:' + self.name)
                    return self.value

            class Line(str):
                def __bool__(self) -> bool:
                    events.append('line:bool:' + str(self))
                    return len(self) > 0

            class Head(str):
                def __iadd__(self, other: object) -> Self:
                    events.append('head:iadd')
                    return type(self)(str(self) + str(other))

            class Lines:
                def pop(self, index: int) -> Head:
                    events.append('lines:pop:' + str(index))
                    return Head('first')

                def __bool__(self) -> bool:
                    events.append('lines:bool')
                    return True

                def __iter__(self) -> Iterator[Line]:
                    events.append('lines:iter')
                    yield Line('')
                    yield Line('last')

            class Value:
                def __iadd__(self, other: object) -> Self:
                    events.append('value:iadd')
                    return self

                def splitlines(self) -> Lines:
                    events.append('value:splitlines')
                    return Lines()

            context = {'value': Value(), 'width': Width(), 'first': Flag('first', True),
                       'blank': Flag('blank', False)}
            return render('{{ value|indent(width,first,blank) }}', context, native), events

        expected = run(False)
        with no_python_indent():
            actual = run(True)
        self.assertEqual(actual, expected)

    def test_custom_join_observes_generator_protocol(self) -> None:
        def run(native: bool) -> tuple[str, list[tuple[str, str]]]:
            events: list[tuple[str, str]] = []

            class CustomMarkup(Markup):
                def join(self, iterable: Iterable[Any]) -> Self:
                    events.append((type(iterable).__name__, getattr(getattr(iterable, 'gi_code', None), 'co_name', '')))
                    return type(self)(super().join(iterable))

            value = CustomMarkup('first\nlast')
            with patch.dict(filters.__dict__, {'Markup': CustomMarkup}):
                result = render('{{ value|indent(2,false,false) }}', {'value': value}, native)
            return result, events

        expected = run(False)
        self.assertIn(('generator', '<genexpr>'), expected[1])
        with no_python_indent():
            actual = run(True)
        self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
