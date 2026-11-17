"""Differential protocols and execution-boundary checks for Jinja join."""

import asyncio
import gc
import inspect
import subprocess
import sys
import unittest
from collections.abc import AsyncIterator, Iterator
from types import FrameType
from types import CodeType
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from jinja2 import filters
from jinja2 import async_utils
from markupsafe import Markup
from test_native_filter_evaluation import render


def observe(source: str, context: dict[str, Any], native: bool,
            environment: jinja2.Environment) -> tuple[str, str, str]:
    try:
        return 'ok', render(source, context, native, environment), ''
    except Exception as error:  # noqa: BLE001 - exception details are the observable result
        return 'error', type(error).__name__, str(error)


class NativeJoinFilterTests(unittest.TestCase):
    def test_join_snapshot_recovers_after_async_module_is_temporarily_replaced(self) -> None:
        script = '''
import sys
import types
import docxtpl_native
import jinja2
import jinja2.async_utils
from jinja2 import filters
from test_native_filter_evaluation import render

name = "jinja2.async_utils"
original = sys.modules.pop(name)
replacements = {"none": None, "object": object(), "module": types.ModuleType(name)}
mode = sys.argv[1]
replacement = replacements.get(mode)
if mode != "missing":
    sys.modules[name] = replacement
try:
    docxtpl_native._extension()
    if mode == "missing":
        assert name not in sys.modules
    else:
        assert sys.modules[name] is replacement
finally:
    sys.modules[name] = original

wrapper = filters.do_join
codes = {wrapper.__code__, filters.sync_do_join.__code__}
for cell in wrapper.__closure__ or ():
    candidate = cell.cell_contents
    if getattr(candidate, "__name__", None) == "do_join" and hasattr(candidate, "__code__"):
        codes.add(candidate.__code__)
calls: list[str] = []
def profile(frame: types.FrameType, event: str, argument: object) -> None:
    if event == "call" and frame.f_code in codes:
        calls.append(frame.f_code.co_name)
for asynchronous in (False, True):
    sys.setprofile(profile)
    try:
        output = render("{{ values|join('|') }}", {"values": ["a", "b"]}, True,
                        jinja2.Environment(enable_async=asynchronous))
    finally:
        sys.setprofile(None)
    assert output == "a|b", output
assert calls == [], calls
'''
        for mode in ('missing', 'none', 'object', 'module'):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, '-c', script, mode],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_sync_adapter_anext_body_is_native(self) -> None:
        code = async_utils._IteratorToAsyncIterator.__anext__.__code__

        def run(native: bool) -> tuple[str, list[str]]:
            calls: list[str] = []

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code is code:
                    calls.append(frame.f_code.co_name)

            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                result = render('{{ values|join }}', {'values': iter(['a', 'b'])}, native,
                                jinja2.Environment(enable_async=True))
            finally:
                sys.setprofile(previous)
            return result, calls

        expected, reference_calls = run(False)
        actual, candidate_calls = run(True)
        self.assertEqual(actual, expected)
        self.assertTrue(reference_calls)
        self.assertEqual(candidate_calls, [])

    def test_custom_factory_standard_adapter_anext_body_is_native(self) -> None:
        code = async_utils._IteratorToAsyncIterator.__anext__.__code__
        adapter = async_utils._IteratorToAsyncIterator

        def run(native: bool) -> tuple[str, list[str], list[str]]:
            calls: list[str] = []
            events: list[str] = []

            def factory(iterator: Iterator[str]) -> Any:
                events.append('factory')
                return adapter(iterator)

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code is code:
                    calls.append(frame.f_code.co_name)

            previous = sys.getprofile()
            try:
                with patch.dict(async_utils.__dict__, {'_IteratorToAsyncIterator': factory}):
                    sys.setprofile(profile)
                    output = render('{{ values|join }}', {'values': iter(['a', 'b'])}, native,
                                    jinja2.Environment(enable_async=True))
            finally:
                sys.setprofile(previous)
            return output, events, calls

        expected, reference_events, reference_calls = run(False)
        actual, actual_events, actual_calls = run(True)
        self.assertEqual(actual, expected)
        self.assertEqual(actual_events, reference_events)
        self.assertTrue(reference_calls)
        self.assertEqual(actual_calls, [])

    def test_custom_factory_adapter_error_preserves_traceback_frame(self) -> None:
        adapter = async_utils._IteratorToAsyncIterator
        code = adapter.__anext__.__code__

        def run(native: bool) -> tuple[bool, list[str]]:
            failure = ValueError('next failed')

            class Values:
                def __iter__(self) -> 'Values':
                    return self

                def __next__(self) -> str:
                    raise failure

            def factory(iterator: Iterator[str]) -> Any:
                return adapter(iterator)

            try:
                with patch.dict(async_utils.__dict__, {'_IteratorToAsyncIterator': factory}):
                    render('{{ values|join }}', {'values': Values()}, native,
                           jinja2.Environment(enable_async=True))
            except ValueError as error:
                frames: list[str] = []
                traceback = error.__traceback__
                while traceback is not None:
                    if traceback.tb_frame.f_code is code:
                        frames.append(traceback.tb_frame.f_code.co_name)
                    traceback = traceback.tb_next
                return error is failure, frames
            raise AssertionError('expected iterator failure')

        expected = run(False)
        self.assertEqual(expected, (True, ['__anext__']))
        self.assertEqual(run(True), expected)

    def test_custom_factory_adapter_traceback_local_identity(self) -> None:
        adapter = async_utils._IteratorToAsyncIterator
        code = adapter.__anext__.__code__

        def run(native: bool) -> list[tuple[int, int, bool]]:
            instances: list[Any] = []

            class Values:
                def __iter__(self) -> 'Values':
                    return self

                def __next__(self) -> str:
                    raise ValueError('next failed')

            def factory(iterator: Iterator[str]) -> Any:
                result = adapter(iterator)
                instances.append(result)
                return result

            try:
                with patch.dict(async_utils.__dict__, {'_IteratorToAsyncIterator': factory}):
                    render('{{ values|join }}', {'values': Values()}, native,
                           jinja2.Environment(enable_async=True))
            except ValueError as error:
                frames: list[tuple[int, int, bool]] = []
                traceback = error.__traceback__
                while traceback is not None:
                    if traceback.tb_frame.f_code is code:
                        frames.append((traceback.tb_lineno, traceback.tb_lasti,
                                       traceback.tb_frame.f_locals.get('self') is instances[0]))
                    traceback = traceback.tb_next
                return frames
            raise AssertionError('expected iterator failure')

        expected = run(False)
        self.assertEqual(len(expected), 1)
        self.assertTrue(expected[0][-1])
        self.assertEqual(run(True), expected)

    def test_sync_adapter_constructor_body_is_native(self) -> None:
        code = async_utils._IteratorToAsyncIterator.__init__.__code__

        def run(native: bool) -> tuple[str, list[str]]:
            calls: list[str] = []

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code is code:
                    calls.append(frame.f_code.co_name)

            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                result = render('{{ values|join }}', {'values': iter(['a', 'b'])}, native,
                                jinja2.Environment(enable_async=True))
            finally:
                sys.setprofile(previous)
            return result, calls

        expected, reference_calls = run(False)
        actual, candidate_calls = run(True)
        self.assertEqual(actual, expected)
        self.assertTrue(reference_calls)
        self.assertEqual(candidate_calls, [])

    def test_sync_adapter_custom_constructor_remains_callback(self) -> None:
        adapter = async_utils._IteratorToAsyncIterator

        def run(native: bool) -> tuple[str, list[bool]]:
            events: list[bool] = []
            original = iter(['input'])

            def initialize(self: Any, iterator: Iterator[str]) -> None:
                events.append(iterator is original)
                self._iterator = iter(['custom'])

            with patch.object(adapter, '__init__', initialize):
                result = render('{{ values|join }}', {'values': original}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(False), ('custom', [True]))
        self.assertEqual(run(True), ('custom', [True]))

    def test_sync_adapter_distinct_inner_iterator_keeps_python_protocol(self) -> None:
        observe('{{ values|join }}', {'values': ['a']}, True,
                jinja2.Environment(enable_async=True))
        adapter = async_utils._IteratorToAsyncIterator
        code = adapter.__anext__.__code__

        def run(native: bool) -> tuple[str, list[str], list[str]]:
            events: list[str] = []
            calls: list[str] = []

            def factory(iterator: Iterator[str]) -> Any:
                events.append('factory')
                return adapter(iter(['other']))

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code is code:
                    calls.append(frame.f_code.co_name)

            previous = sys.getprofile()
            try:
                with patch.dict(async_utils.__dict__, {'_IteratorToAsyncIterator': factory}):
                    sys.setprofile(profile)
                    result = render('{{ values|join }}', {'values': iter(['input'])}, native,
                                    jinja2.Environment(enable_async=True))
            finally:
                sys.setprofile(previous)
            return result, events, calls

        expected, reference_events, reference_calls = run(False)
        actual, candidate_events, candidate_calls = run(True)
        self.assertEqual((actual, candidate_events), (expected, reference_events))
        self.assertTrue(reference_calls)
        self.assertTrue(candidate_calls)

    def test_sync_adapter_custom_getattribute_keeps_protocol(self) -> None:
        observe('{{ values|join }}', {'values': ['a']}, True,
                jinja2.Environment(enable_async=True))
        adapter = async_utils._IteratorToAsyncIterator

        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def getattribute(self: object, name: str) -> Any:
                if name == '_iterator':
                    events.append('lookup')
                return object.__getattribute__(self, name)

            with patch.object(adapter, '__getattribute__', getattribute):
                result = render('{{ values|join }}', {'values': iter(['a'])}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_custom_getattr_is_not_probed_early(self) -> None:
        observe('{{ values|join }}', {'values': ['a']}, True,
                jinja2.Environment(enable_async=True))
        adapter = async_utils._IteratorToAsyncIterator

        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            inner = iter(['a'])

            def missing(self: object, name: str) -> object:
                events.append(name)
                if name == '_iterator':
                    return inner
                raise AttributeError(name)

            def factory(iterator: Iterator[str]) -> Any:
                instance = adapter(iterator)
                del instance._iterator
                return instance

            with patch.object(adapter, '__getattr__', missing, create=True):
                with patch.dict(async_utils.__dict__, {'_IteratorToAsyncIterator': factory}):
                    result = render('{{ values|join }}', {'values': inner}, native,
                                    jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_descriptor_is_not_probed_early(self) -> None:
        observe('{{ values|join }}', {'values': ['a']}, True,
                jinja2.Environment(enable_async=True))
        adapter = async_utils._IteratorToAsyncIterator

        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def get_iterator(instance: Any) -> Iterator[str]:
                events.append('get')
                return instance.__dict__['_saved_iterator']

            def set_iterator(instance: Any, value: Iterator[str]) -> None:
                events.append('set')
                instance.__dict__['_saved_iterator'] = value

            with patch.object(adapter, '_iterator', property(get_iterator, set_iterator), create=True):
                result = render('{{ values|join }}', {'values': iter(['a'])}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_captures_next_before_inner_key_comparison(self) -> None:
        observe('{{ values|join }}', {'values': ['a']}, True,
                jinja2.Environment(enable_async=True))
        adapter = async_utils._IteratorToAsyncIterator

        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def newer(iterator: Iterator[str]) -> str:
                events.append('new')
                return next(iterator)

            def original(iterator: Iterator[str]) -> str:
                events.append('original')
                return next(iterator)

            class Key(str):
                __hash__ = str.__hash__

                def __eq__(self, other: object) -> bool:
                    events.append('compare')
                    async_utils.__dict__['next'] = newer
                    return super().__eq__(other)

            def factory(iterator: Iterator[str]) -> Any:
                instance = adapter(iterator)
                value = instance.__dict__.pop('_iterator')
                instance.__dict__[Key('_iterator')] = value
                return instance

            with patch.dict(async_utils.__dict__, {'_IteratorToAsyncIterator': factory, 'next': original}):
                result = render('{{ values|join }}', {'values': iter(['a'])}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_distinct_inner_key_compared_once_per_step(self) -> None:
        observe('{{ values|join }}', {'values': ['a']}, True,
                jinja2.Environment(enable_async=True))
        adapter = async_utils._IteratorToAsyncIterator

        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Key(str):
                __hash__ = str.__hash__

                def __eq__(self, other: object) -> bool:
                    events.append('compare')
                    return super().__eq__(other)

            def factory(iterator: Iterator[str]) -> Any:
                instance = adapter(iter(['other']))
                value = instance.__dict__.pop('_iterator')
                instance.__dict__[Key('_iterator')] = value
                return instance

            with patch.dict(async_utils.__dict__, {'_IteratorToAsyncIterator': factory}):
                result = render('{{ values|join }}', {'values': iter(['input'])}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_does_not_materialize_instance_dictionary(self) -> None:
        observe('{{ values|join }}', {'values': ['a']}, True,
                jinja2.Environment(enable_async=True))
        adapter = async_utils._IteratorToAsyncIterator

        def run(native: bool) -> tuple[str, list[bool]]:
            instances: list[Any] = []
            observed: list[bool] = []

            def factory(iterator: Iterator[str]) -> Any:
                instance = adapter(iterator)
                instances.append(instance)
                return instance

            def next_value(iterator: Iterator[str]) -> str:
                observed.append(any(isinstance(value, dict) for value in gc.get_referents(instances[0])))
                return next(iterator)

            with patch.dict(async_utils.__dict__, {'_IteratorToAsyncIterator': factory, 'next': next_value}):
                result = render('{{ values|join }}', {'values': iter(['a'])}, native,
                                jinja2.Environment(enable_async=True))
            return result, observed

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_observes_next_and_exception_callbacks(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            events: list[object] = []
            marker = StopIteration('finished')

            def next_value(iterator: Iterator[str]) -> str:
                events.append('next')
                try:
                    return next(iterator)
                except StopIteration:
                    raise marker from None

            def stop(value: object) -> StopAsyncIteration:
                events.append(('stop', value, sys.exception() is marker))
                return StopAsyncIteration(value)

            with patch.dict(async_utils.__dict__, {'next': next_value, 'StopAsyncIteration': stop}):
                result = render('{{ values|join }}', {'values': iter(['a'])}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_observes_replaced_anext_during_iteration(self) -> None:
        adapter = async_utils._IteratorToAsyncIterator
        original = adapter.__anext__

        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(self: object) -> str:
                events.append('replacement')
                raise StopAsyncIteration

            def next_value(iterator: Iterator[str]) -> str:
                events.append('next')
                setattr(adapter, '__anext__', replacement)
                return next(iterator)

            try:
                with patch.dict(async_utils.__dict__, {'next': next_value}):
                    result = render('{{ values|join }}', {'values': iter(['a', 'b'])}, native,
                                    jinja2.Environment(enable_async=True))
                return result, events
            finally:
                setattr(adapter, '__anext__', original)

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_observes_replaced_exception_type(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            events: list[object] = []

            class AlternateStop(Exception):
                value = 'alternate'

            marker = AlternateStop()

            def next_value(iterator: Iterator[str]) -> str:
                events.append('next')
                raise marker

            def stop(value: object) -> StopAsyncIteration:
                events.append((value, sys.exception() is marker))
                return StopAsyncIteration(value)

            with patch.dict(async_utils.__dict__, {'next': next_value, 'StopIteration': AlternateStop,
                                                   'StopAsyncIteration': stop}):
                result = render('{{ values|join }}', {'values': iter(['a'])}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_unmatched_stop_iteration_uses_coroutine_error(self) -> None:
        def run(native: bool) -> tuple[str, str, bool, bool]:
            marker = StopIteration('unmatched')

            class Values:
                def __iter__(self) -> 'Values':
                    return self

                def __next__(self) -> str:
                    raise marker

            try:
                with patch.dict(async_utils.__dict__, {'StopIteration': ValueError}):
                    render('{{ values|join }}', {'values': Values()}, native,
                           jinja2.Environment(enable_async=True))
            except Exception as error:  # noqa: BLE001 - compare the boundary exception
                return type(error).__name__, str(error), error.__cause__ is marker, error.__context__ is marker
            raise AssertionError('expected an exception')

        expected = run(False)
        self.assertEqual(expected, ('RuntimeError', 'coroutine raised StopIteration', True, True))
        self.assertEqual(run(True), expected)

    def test_sync_adapter_preserves_stop_constructor_exception_context(self) -> None:
        def run(native: bool) -> tuple[bool, bool, str]:
            exhausted = StopIteration('end')
            failure = ValueError('stop constructor')

            def next_value(iterator: Iterator[str]) -> str:
                raise exhausted

            def stop(value: object) -> StopAsyncIteration:
                raise failure

            try:
                with patch.dict(async_utils.__dict__, {'next': next_value, 'StopAsyncIteration': stop}):
                    render('{{ values|join }}', {'values': iter([])}, native,
                           jinja2.Environment(enable_async=True))
            except ValueError as error:
                return error is failure, error.__context__ is exhausted, str(error)
            raise AssertionError('expected stop constructor failure')

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_reference_lifetime_during_next_callback(self) -> None:
        observe('{{ values|join }}', {'values': ['a']}, True,
                jinja2.Environment(enable_async=True))
        adapter = async_utils._IteratorToAsyncIterator

        def run(native: bool) -> tuple[str, list[int]]:
            instances: list[Any] = []
            observed: list[int] = []

            def factory(iterator: Iterator[str]) -> Any:
                instance = adapter(iterator)
                instances.append(instance)
                return instance

            def next_value(iterator: Iterator[str]) -> str:
                observed.append(sys.getrefcount(instances[0]))
                return next(iterator)

            with patch.dict(async_utils.__dict__, {'next': next_value, '_IteratorToAsyncIterator': factory}):
                result = render('{{ values|join }}', {'values': iter(['a'])}, native,
                                jinja2.Environment(enable_async=True))
            return result, observed

        self.assertEqual(run(True), run(False))

    def test_async_collection_iterator_reference_lifetime(self) -> None:
        def run(native: bool) -> tuple[str, list[int]]:
            observed: list[int] = []

            class Values:
                def __init__(self) -> None:
                    self.done = False

                def __aiter__(self) -> 'Values':
                    return self

                async def __anext__(self) -> str:
                    observed.append(sys.getrefcount(self))
                    if self.done:
                        raise StopAsyncIteration
                    self.done = True
                    return 'a'

            result = render('{{ values|join }}', {'values': Values()}, native,
                            jinja2.Environment(enable_async=True))
            return result, observed

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_inner_iterator_reference_lifetime(self) -> None:
        def run(native: bool) -> tuple[str, list[int]]:
            observed: list[int] = []

            def next_value(iterator: Iterator[str]) -> str:
                observed.append(sys.getrefcount(iterator))
                return next(iterator)

            with patch.dict(async_utils.__dict__, {'next': next_value}):
                result = render('{{ values|join }}', {'values': iter(['a'])}, native,
                                jinja2.Environment(enable_async=True))
            return result, observed

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_python_next_method_reference_lifetime(self) -> None:
        def run(native: bool) -> tuple[str, list[int]]:
            observed: list[int] = []

            class Values:
                def __init__(self) -> None:
                    self.done = False

                def __iter__(self) -> 'Values':
                    return self

                def __next__(self) -> str:
                    observed.append(sys.getrefcount(self))
                    if self.done:
                        raise StopIteration
                    self.done = True
                    return 'a'

            result = render('{{ values|join }}', {'values': Values()}, native,
                            jinja2.Environment(enable_async=True))
            return result, observed

        self.assertEqual(run(True), run(False))

    def test_sync_adapter_python_iterator_can_replace_anext(self) -> None:
        adapter = async_utils._IteratorToAsyncIterator
        original = adapter.__anext__

        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def replacement(self: Any) -> str:
                events.append('replacement')
                raise StopAsyncIteration

            class Values:
                def __iter__(self) -> 'Values':
                    return self

                def __next__(self) -> str:
                    events.append('next')
                    setattr(adapter, '__anext__', replacement)  # noqa: B010 - test a live method override
                    return 'a'

            try:
                result = render('{{ values|join }}', {'values': Values()}, native,
                                jinja2.Environment(enable_async=True))
                return result, events
            finally:
                setattr(adapter, '__anext__', original)  # noqa: B010 - restore the original method

        self.assertEqual(run(False), ('a', ['next', 'replacement']))
        self.assertEqual(run(True), ('a', ['next', 'replacement']))

    def test_async_collection_source_lifetime_with_distinct_iterator(self) -> None:
        def run(native: bool) -> tuple[str, list[int]]:
            observed: list[int] = []
            source = object()

            async def values() -> AsyncIterator[str]:
                observed.append(sys.getrefcount(source))
                yield 'a'

            def make_iterator(value: object) -> AsyncIterator[str]:
                return values()

            with patch.dict(async_utils.__dict__, {'auto_aiter': make_iterator}):
                result = render('{{ values|join }}', {'values': source}, native,
                                jinja2.Environment(enable_async=True))
            return result, observed

        self.assertEqual(run(True), run(False))

    def test_async_collection_source_reference_in_custom_auto_aiter(self) -> None:
        def run(native: bool) -> tuple[str, list[int]]:
            observed: list[int] = []

            async def values() -> AsyncIterator[str]:
                yield 'a'

            def make_iterator(value: object) -> AsyncIterator[str]:
                observed.append(sys.getrefcount(value))
                return values()

            with patch.dict(async_utils.__dict__, {'auto_aiter': make_iterator}):
                result = render('{{ values|join }}', {'values': object()}, native,
                                jinja2.Environment(enable_async=True))
            return result, observed

        self.assertEqual(run(True), run(False))

    def test_auto_aiter_source_reference_during_hasattr_callback(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool) -> tuple[str, list[int]]:
                    observed: list[int] = []

                    def has_async(value: object, name: str) -> bool:
                        observed.append(sys.getrefcount(value))
                        return hasattr(value, name)

                    async def values() -> AsyncIterator[str]:
                        yield 'a'

                    source = values() if asynchronous else iter(['a'])
                    with patch.dict(async_utils.__dict__, {'hasattr': has_async}):
                        result = render('{{ values|join }}', {'values': source}, native,
                                        jinja2.Environment(enable_async=True))
                    return result, observed

                self.assertEqual(run(True), run(False))

    def test_auto_aiter_source_reference_during_iter_callback(self) -> None:
        def run(native: bool) -> tuple[str, list[int]]:
            observed: list[int] = []

            def make_iterator(value: object) -> Iterator[str]:
                observed.append(sys.getrefcount(value))
                return iter(['a'])

            with patch.dict(async_utils.__dict__, {'iter': make_iterator}):
                result = render('{{ values|join }}', {'values': object()}, native,
                                jinja2.Environment(enable_async=True))
            return result, observed

        self.assertEqual(run(True), run(False))

    def test_auto_aiter_source_reference_during_method(self) -> None:
        def run(native: bool) -> tuple[str, list[int]]:
            observed: list[int] = []

            class Values:
                def __aiter__(self) -> AsyncIterator[str]:
                    observed.append(sys.getrefcount(self))

                    async def values() -> AsyncIterator[str]:
                        yield 'a'

                    return values()

            result = render('{{ values|join }}', {'values': Values()}, native,
                            jinja2.Environment(enable_async=True))
            return result, observed

        self.assertEqual(run(True), run(False))

    def test_join_keyword_arguments_use_native_standard_body(self) -> None:
        wrapper = filters.do_join
        codes = {wrapper.__code__, filters.sync_do_join.__code__}
        codes.update(candidate.__code__ for cell in wrapper.__closure__ or ()
                     if hasattr(candidate := cell.cell_contents, '__code__')
                     and getattr(candidate, '__name__', None) == 'do_join')
        for asynchronous in (False, True):
            for autoescape in (False, True):
                with self.subTest(asynchronous=asynchronous, autoescape=autoescape):
                    def run(native: bool) -> tuple[tuple[str, str, str], list[str]]:
                        calls: list[str] = []

                        def profile(frame: FrameType, event: str, argument: object) -> None:
                            if event == 'call' and frame.f_code in codes:
                                calls.append(frame.f_code.co_name)

                        previous = sys.getprofile()
                        try:
                            sys.setprofile(profile)
                            result = observe('{{ values|join(d=sep, attribute=none) }}',
                                             {'values': ['a', 'b'], 'sep': '|'}, native,
                                             jinja2.Environment(enable_async=asynchronous,
                                                                autoescape=autoescape))
                        finally:
                            sys.setprofile(previous)
                        return result, calls

                    expected, reference_calls = run(False)
                    actual, candidate_calls = run(True)
                    self.assertEqual(actual, expected)
                    self.assertTrue(reference_calls)
                    self.assertEqual(candidate_calls, [])

    def test_join_keyword_defaults_and_invalid_calls(self) -> None:
        for asynchronous in (False, True):
            function = next(cell.cell_contents for name, cell in zip(
                filters.do_join.__code__.co_freevars, filters.do_join.__closure__ or (), strict=True)
                if name == ('async_func' if asynchronous else 'normal_func'))
            cases = ('{{ values|join(attribute=none) }}',
                     '{{ values|join(unknown=1) }}',
                     '{{ values|join(eval_ctx=1) }}',
                     '{{ values|join(value=1) }}',
                     '{{ values|join("|", d="x") }}',
                     '{{ values|join("|", none, attribute="x") }}',
                     '{{ values|join("|", none, "extra", d="x") }}')
            with patch.object(function, '__defaults__', (';', None)):
                for source in cases:
                    with self.subTest(asynchronous=asynchronous, source=source):
                        environment = jinja2.Environment(enable_async=asynchronous)
                        self.assertEqual(observe(source, {'values': ['a', 'b']}, True, environment),
                                         observe(source, {'values': ['a', 'b']}, False, environment))

    def test_join_keyword_separator_autoescape_and_callback_order(self) -> None:
        for asynchronous in (False, True):
            for separator in ('<&>', Markup('<i>|</i>')):
                with self.subTest(asynchronous=asynchronous, separator=separator):
                    def run(native: bool) -> tuple[tuple[str, str, str], list[str]]:
                        events: list[str] = []

                        def get_separator() -> object:
                            events.append('separator')
                            return separator

                        def get_attribute() -> None:
                            events.append('attribute')
                            return None

                        result = observe('{{ values|join(d=separator(), attribute=attribute()) }}',
                                         {'values': ['a&', Markup('<b>')],
                                          'separator': get_separator, 'attribute': get_attribute}, native,
                                         jinja2.Environment(enable_async=asynchronous, autoescape=True))
                        return result, events

                    self.assertEqual(run(True), run(False))

    def test_async_join_auto_aiter_body_is_native(self) -> None:
        code = async_utils.auto_aiter.__code__
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool) -> tuple[str, list[str]]:
                    calls: list[str] = []

                    async def values() -> AsyncIterator[str]:
                        yield 'a'

                    def profile(frame: FrameType, event: str, argument: object) -> None:
                        if event == 'call' and frame.f_code is code:
                            calls.append(frame.f_code.co_name)

                    previous = sys.getprofile()
                    try:
                        sys.setprofile(profile)
                        result = render('{{ values|join }}',
                                        {'values': values() if asynchronous else ['a']}, native,
                                        jinja2.Environment(enable_async=True))
                    finally:
                        sys.setprofile(previous)
                    return result, calls

                expected, reference_calls = run(False)
                actual, candidate_calls = run(True)
                self.assertEqual(actual, expected)
                self.assertTrue(reference_calls)
                self.assertEqual(candidate_calls, [])

    def test_auto_aiter_observes_live_global_callbacks(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            events: list[object] = []
            source = ['a']

            def has_async(value: object, name: str) -> bool:
                events.append(('hasattr', value is source, name))
                return False

            def make_iterator(value: object) -> Iterator[str]:
                events.append(('iter', value is source))
                return iter(source)

            def make_adapter(iterator: Iterator[str]) -> AsyncIterator[str]:
                events.append('adapter')

                async def values() -> AsyncIterator[str]:
                    for value in iterator:
                        events.append('next')
                        yield value

                return values()

            with patch.dict(async_utils.__dict__, {'hasattr': has_async, 'iter': make_iterator,
                                                   '_IteratorToAsyncIterator': make_adapter}):
                result = render('{{ values|join }}', {'values': source}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))

    def test_auto_aiter_looks_up_instance_method_twice(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Values:
                def __getattribute__(self, name: str) -> Any:
                    if name == '__aiter__':
                        events.append('lookup')
                    return object.__getattribute__(self, name)

                def __aiter__(self) -> AsyncIterator[str]:
                    events.append('aiter')

                    async def values() -> AsyncIterator[str]:
                        yield 'a'

                    return values()

            result = render('{{ values|join }}', {'values': Values()}, native,
                            jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))

    def test_auto_aiter_preserves_second_lookup_exception(self) -> None:
        def run(native: bool) -> tuple[bool, str, list[str]]:
            events: list[str] = []
            marker = ValueError('second lookup')

            class Values:
                def __getattribute__(self, name: str) -> Any:
                    if name == '__aiter__':
                        events.append('lookup')
                        if len(events) == 2:
                            raise marker
                    return object.__getattribute__(self, name)

                def __aiter__(self) -> AsyncIterator[str]:
                    raise AssertionError('method should not run')

            try:
                render('{{ values|join }}', {'values': Values()}, native,
                       jinja2.Environment(enable_async=True))
            except ValueError as error:
                return error is marker, str(error), events
            raise AssertionError('expected lookup error')

        self.assertEqual(run(True), run(False))

    def test_auto_aiter_callback_reference_released_before_iteration(self) -> None:
        def run(native: bool) -> tuple[str, list[int]]:
            observed: list[int] = []

            async def values() -> AsyncIterator[str]:
                observed.append(sys.getrefcount(async_utils.auto_aiter))
                yield 'a'

            result = render('{{ values|join }}', {'values': values()}, native,
                            jinja2.Environment(enable_async=True))
            return result, observed

        # Both executions observe the same initialized standard snapshots.
        observe('{{ values|join }}', {'values': ['a']}, True, jinja2.Environment())
        self.assertEqual(run(True), run(False))

    def test_async_join_collection_body_is_native(self) -> None:
        codes = {async_utils.auto_to_list.__code__}
        codes.update(value for value in async_utils.auto_to_list.__code__.co_consts
                     if isinstance(value, CodeType))
        for asynchronous_iterable in (False, True):
            with self.subTest(asynchronous_iterable=asynchronous_iterable):
                def run(native: bool) -> tuple[str, list[str]]:
                    calls: list[str] = []

                    async def values() -> AsyncIterator[str]:
                        yield 'a'
                        await asyncio.sleep(0)
                        yield 'b'

                    def profile(frame: FrameType, event: str, argument: object) -> None:
                        if event == 'call' and frame.f_code in codes:
                            calls.append(frame.f_code.co_name)

                    previous = sys.getprofile()
                    try:
                        sys.setprofile(profile)
                        result = render('{{ values|join("|") }}',
                                        {'values': values() if asynchronous_iterable else ['a', 'b']},
                                        native, jinja2.Environment(enable_async=True))
                    finally:
                        sys.setprofile(previous)
                    return result, calls

                expected, reference_calls = run(False)
                actual, candidate_calls = run(True)
                self.assertEqual(actual, expected)
                self.assertTrue(reference_calls)
                self.assertEqual(candidate_calls, [])

    def test_async_collection_calls_live_auto_aiter(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            events: list[object] = []
            source = object()

            async def iterator() -> AsyncIterator[str]:
                events.append('iterate')
                await asyncio.sleep(0)
                yield 'a'

            def replacement(value: object) -> AsyncIterator[str]:
                events.append(value is source)
                return iterator()

            with patch.dict(async_utils.__dict__, {'auto_aiter': replacement}):
                output = render('{{ values|join }}', {'values': source}, native,
                                jinja2.Environment(enable_async=True))
            return output, events

        self.assertEqual(run(True), run(False))

    def test_async_collection_invalid_anext_preserves_cause(self) -> None:
        def run(native: bool) -> tuple[str, str, str, str]:
            class Values:
                def __aiter__(self) -> 'Values':
                    return self

                def __anext__(self) -> int:
                    return 42

            try:
                render('{{ values|join }}', {'values': Values()}, native,
                       jinja2.Environment(enable_async=True))
            except Exception as error:  # noqa: BLE001 - preserve observable exception chain
                cause = error.__cause__
                return type(error).__name__, str(error), type(cause).__name__, str(cause)
            raise AssertionError('expected invalid awaitable')

        self.assertEqual(run(True), run(False))

    def test_async_collection_await_setup_errors_and_slot_order(self) -> None:
        for failure in ('value', 'stop', 'bad-iterator'):
            with self.subTest(failure=failure):
                def run(native: bool) -> tuple[object, ...]:
                    events: list[str] = []

                    class Awaitable:
                        def __await__(self) -> Any:
                            events.append('await')
                            if failure == 'value':
                                raise ValueError('await setup')
                            if failure == 'stop':
                                raise StopAsyncIteration('await setup')
                            return 42

                    class Values:
                        def __aiter__(self) -> 'Values':
                            events.append('aiter')
                            return self

                        def __anext__(self) -> Awaitable:
                            events.append('anext')
                            return Awaitable()

                    try:
                        output = render('{{ values|join }}', {'values': Values()}, native,
                                        jinja2.Environment(enable_async=True))
                    except Exception as error:  # noqa: BLE001 - compare exception chain
                        cause = error.__cause__
                        return ('error', type(error).__name__, str(error),
                                type(cause).__name__, str(cause), events)
                    return ('ok', output, events)

                self.assertEqual(run(True), run(False))

    def test_async_collection_retains_previous_item_until_next_step(self) -> None:
        def run(native: bool) -> tuple[str, list[int]]:
            observed: list[int] = []

            class Value:
                def __str__(self) -> str:
                    return 'a'

            async def values() -> AsyncIterator[Value]:
                first = Value()
                yield first
                observed.append(sys.getrefcount(first))
                yield Value()

            result = render('{{ values|join }}', {'values': values()}, native,
                            jinja2.Environment(enable_async=True))
            return result, observed

        self.assertEqual(run(True), run(False))

    def test_async_collection_stop_iteration_chain(self) -> None:
        for origin in ('anext', 'await'):
            with self.subTest(origin=origin):
                def run(native: bool) -> tuple[object, ...]:
                    marker = StopIteration('stopped')

                    class Awaitable:
                        def __await__(self) -> Any:
                            raise marker

                    class Values:
                        def __aiter__(self) -> 'Values':
                            return self

                        def __anext__(self) -> Awaitable:
                            if origin == 'anext':
                                raise marker
                            return Awaitable()

                    try:
                        render('{{ values|join }}', {'values': Values()}, native,
                               jinja2.Environment(enable_async=True))
                    except Exception as error:  # noqa: BLE001 - compare chaining and identity
                        cause = error.__cause__
                        return type(error).__name__, str(error), type(cause).__name__, str(cause), cause is marker
                    raise AssertionError('expected an iteration error')

                self.assertEqual(run(True), run(False))

    def test_async_decision_can_replace_the_selected_callback(self) -> None:
        # Initialize the native snapshots before changing the live closure.
        observe('{{ values|join }}', {'values': ['a']}, True, jinja2.Environment())
        wrapper = filters.do_join
        cells = dict(zip(wrapper.__code__.co_freevars, wrapper.__closure__ or (), strict=True))
        original_decision = cells['is_async'].cell_contents
        original_normal = cells['normal_func'].cell_contents

        def run(native: bool) -> tuple[tuple[str, str, str], list[str]]:
            events: list[str] = []

            def replacement(*args: Any, **kwargs: Any) -> str:
                events.append('replacement')
                return 'replaced'

            def decision(args: tuple[Any, ...]) -> bool:
                events.append('decision')
                cells['normal_func'].cell_contents = replacement
                return False

            cells['is_async'].cell_contents = decision
            cells['normal_func'].cell_contents = original_normal
            return observe('{{ values|join }}', {'values': ['a']}, native, jinja2.Environment()), events

        try:
            expected = run(False)
            actual = run(True)
        finally:
            cells['is_async'].cell_contents = original_decision
            cells['normal_func'].cell_contents = original_normal
        self.assertEqual(expected, (('ok', 'replaced', ''), ['decision', 'replacement']))
        self.assertEqual(actual, expected)

    def test_standard_body_is_native_in_sync_and_async_modes(self) -> None:
        wrapper = filters.do_join
        codes = {wrapper.__code__, filters.sync_do_join.__code__}
        for cell in wrapper.__closure__ or ():
            candidate = cell.cell_contents
            if getattr(candidate, '__name__', None) == 'do_join' and hasattr(candidate, '__code__'):
                codes.add(candidate.__code__)
        for asynchronous in (False, True):
            for autoescape in (False, True):
                with self.subTest(asynchronous=asynchronous, autoescape=autoescape):
                    def run(native: bool, asynchronous: bool = asynchronous,
                            autoescape: bool = autoescape) -> tuple[tuple[str, str, str], list[str]]:
                        calls: list[str] = []
                        previous = sys.getprofile()

                        def profile(frame: FrameType, event: str, argument: object) -> None:
                            if event == 'call' and frame.f_code in codes:
                                calls.append(frame.f_code.co_name)

                        try:
                            sys.setprofile(profile)
                            result = observe('{{ values|join("|") }}', {'values': ['a', 'b']},
                                             native, jinja2.Environment(enable_async=asynchronous,
                                                                        autoescape=autoescape))
                        finally:
                            sys.setprofile(previous)
                        return result, calls

                    expected, reference_calls = run(False)
                    actual, candidate_calls = run(True)
                    self.assertEqual(actual, expected)
                    self.assertTrue(reference_calls, 'reference execution must be observed')
                    self.assertEqual(candidate_calls, [])

    def test_autoescape_separator_and_attribute_combinations(self) -> None:
        cases = [
            ('{{ values|join }}', ['a', 2, None]),
            ('{{ values|join(sep) }}', ['a&', Markup('<b>'), '<c>']),
            ('{{ values|join(sep, attribute="a.0") }}', [{'a': ['<x>']}, {'a': ['&y']}]),
        ]
        for autoescape in (False, True):
            for separator in ('|', Markup('<i>|</i>')):
                for source, values in cases:
                    with self.subTest(autoescape=autoescape, separator=separator, source=source):
                        context = {'values': values, 'sep': separator}
                        environment = jinja2.Environment(autoescape=autoescape)
                        self.assertEqual(observe(source, context, True, environment),
                                         observe(source, context, False, environment))

    def test_iterator_conversion_order_and_original_exception(self) -> None:
        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                def run(native: bool, autoescape: bool = autoescape) -> tuple[tuple[str, str, str], list[str], bool]:
                    events: list[str] = []
                    marker = ValueError('string failure')
                    caught: list[BaseException] = []

                    class Value:
                        def __init__(self, index: int) -> None:
                            self.index = index

                        def __str__(self) -> str:
                            events.append(f'str:{self.index}')
                            if self.index == 1:
                                raise marker
                            return f'v{self.index}'

                    def values() -> Iterator[Value]:
                        for index in range(3):
                            events.append(f'yield:{index}')
                            yield Value(index)

                    try:
                        result = render('{{ values|join("|") }}', {'values': values()}, native,
                                        jinja2.Environment(autoescape=autoescape))
                        outcome = ('ok', result, '')
                    except ValueError as error:
                        caught.append(error)
                        outcome = ('error', type(error).__name__, str(error))
                    return outcome, events, bool(caught and caught[0] is marker)

                self.assertEqual(run(True), run(False))

    def test_custom_separator_join_receives_original_map_function(self) -> None:
        def run(native: bool) -> tuple[str, list[bool]]:
            seen: list[bool] = []

            class Separator(str):
                def __str__(self) -> 'Separator':
                    return self

                def join(self, values: Any, /) -> str:  # ty: ignore[invalid-method-override]
                    reduced = values.__reduce__()
                    seen.append(reduced[0] is map and reduced[1][0] is str)
                    return '|'.join(values)

            output = render('{{ values|join(sep) }}', {'values': ['a', 'b'], 'sep': Separator('|')}, native)
            return output, seen

        expected = run(False)
        self.assertEqual(expected, ('a|b', [True]))
        self.assertEqual(run(True), expected)

    def test_replaced_map_and_attrgetter_remain_callbacks(self) -> None:
        def run(native: bool) -> tuple[str, list[tuple[str, Any]]]:
            events: list[tuple[str, Any]] = []

            def custom_map(function: Any, values: Any) -> Any:
                events.append(('map', function is str))
                return map(function, values)

            def custom_getter(environment: object, attribute: object) -> Any:
                events.append(('getter', attribute))
                return lambda item: item['custom']

            with patch.dict(filters.__dict__, {'map': custom_map, 'make_attrgetter': custom_getter}):
                output = render('{{ values|join("|", attribute="ignored") }}',
                                {'values': [{'custom': 'a'}, {'custom': 'b'}]}, native)
            return output, events

        self.assertEqual(run(True), run(False))

    def test_async_iterator_order_and_task_identity(self) -> None:
        def run(native: bool) -> tuple[str, list[tuple[str, bool]]]:
            events: list[tuple[str, bool]] = []
            task: list[asyncio.Task[Any]] = []

            async def values() -> AsyncIterator[str]:
                current = asyncio.current_task()
                assert current is not None
                task.append(current)
                for value in ('a', 'b'):
                    events.append((value, asyncio.current_task() is task[0]))
                    await asyncio.sleep(0)
                    yield value

            result = render('{{ values|join("|") }}', {'values': values()}, native,
                            jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))

    def test_async_join_resolves_sync_callback_before_iteration(self) -> None:
        original = filters.sync_do_join

        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def replacement(*args: Any, **kwargs: Any) -> str:
                events.append('replacement')
                return 'replacement'

            async def values() -> AsyncIterator[str]:
                events.append('start')
                filters.__dict__['sync_do_join'] = replacement
                await asyncio.sleep(0)
                events.append('resume')
                yield 'a'
                yield 'b'

            try:
                result = render('{{ values|join("|") }}', {'values': values()}, native,
                                jinja2.Environment(enable_async=True))
                return result, events
            finally:
                filters.sync_do_join = original

        expected = run(False)
        self.assertEqual(expected, ('a|b', ['start', 'resume']))
        self.assertEqual(run(True), expected)

    def test_async_join_preserves_iterator_exception_identity(self) -> None:
        def run(native: bool) -> tuple[bool, str, list[str]]:
            error = ValueError('iterator failed')
            events: list[str] = []

            async def values() -> AsyncIterator[str]:
                events.append('start')
                await asyncio.sleep(0)
                events.append('raise')
                raise error
                yield ''  # pragma: no cover

            try:
                render('{{ values|join }}', {'values': values()}, native,
                       jinja2.Environment(enable_async=True))
            except ValueError as caught:
                return caught is error, str(caught), events
            raise AssertionError('expected iterator exception')

        self.assertEqual(run(True), run(False))

    def test_async_join_preserves_cancellation_and_cleanup(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def values() -> AsyncIterator[str]:
                try:
                    events.append('start')
                    await asyncio.sleep(0)
                    events.append('cancel')
                    task = asyncio.current_task()
                    assert task is not None
                    task.cancel('cancel join')
                    await asyncio.sleep(0)
                    yield ''  # pragma: no cover
                finally:
                    events.append('cleanup')

            try:
                render('{{ values|join }}', {'values': values()}, native,
                       jinja2.Environment(enable_async=True))
            except asyncio.CancelledError as error:
                return str(error), events
            raise AssertionError('expected cancellation')

        self.assertEqual(run(True), run(False))

    def test_async_join_observes_replaced_collection_callback(self) -> None:
        def run(native: bool) -> tuple[tuple[str, str, str], list[object]]:
            events: list[object] = []

            async def collect(values: object) -> list[str]:
                events.append(values)
                await asyncio.sleep(0)
                return ['custom', 'items']

            values = object()
            with patch.dict(filters.__dict__, {'auto_to_list': collect}):
                result = observe('{{ values|join("|") }}', {'values': values}, native,
                                 jinja2.Environment(enable_async=True))
            return result, [item is values for item in events]

        self.assertEqual(run(True), run(False))

    def test_async_join_calls_replaced_sync_callback_after_collection(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            events: list[object] = []

            async def values() -> AsyncIterator[str]:
                events.append('start')
                await asyncio.sleep(0)
                yield 'a'

            def replacement(eval_context: object, items: object, delimiter: object,
                            attribute: object) -> str:
                events.append((list(items), delimiter, attribute))  # type: ignore[arg-type]
                return 'custom'

            with patch.dict(filters.__dict__, {'sync_do_join': replacement}):
                result = render('{{ values|join("|") }}', {'values': values()}, native,
                                jinja2.Environment(enable_async=True))
            return result, events

        self.assertEqual(run(True), run(False))


class NativeJoinAttributeGetterTests(unittest.TestCase):
    def test_getter_preserves_function_protocol(self) -> None:
        environment = jinja2.Environment()
        reference = filters.make_attrgetter(environment, 'x')
        candidate = docxtpl_native._extension()._join_attribute_getter(environment, 'x')
        expected = (inspect.isfunction(reference), type(reference), getattr(reference, '__name__', None),
                    getattr(reference, '__code__', None))
        actual = (inspect.isfunction(candidate), type(candidate), getattr(candidate, '__name__', None),
                  getattr(candidate, '__code__', None))
        self.assertEqual(actual, expected)

    def test_lookup_protocols_defaults_and_postprocess(self) -> None:
        def run(native: bool) -> tuple[list[object], list[tuple[str, object]]]:
            events: list[tuple[str, object]] = []

            class Environment(jinja2.Environment):
                def getitem(self, obj: Any, argument: Any) -> Any:
                    events.append(('getitem', argument))
                    return super().getitem(obj, argument)

            def postprocess(value: object) -> object:
                events.append(('postprocess', value))
                return value

            environment = Environment()
            factory = docxtpl_native._extension()._join_attribute_getter if native else filters.make_attrgetter
            getter = factory(environment, 'a.0', postprocess=postprocess, default='missing')
            result = [getter({'a': ['found']}), getter({'a': []})]
            return result, events

        self.assertEqual(run(True), run(False))

    def test_standard_algorithms_do_not_run_in_python(self) -> None:
        import jinja2.environment

        codes = {filters.make_attrgetter.__code__, filters._prepare_attribute_parts.__code__,
                 jinja2.environment.Environment.getitem.__code__}
        inner = next(item for item in filters.make_attrgetter.__code__.co_consts
                     if getattr(item, 'co_name', None) == 'attrgetter')
        codes.add(inner)

        def run(native: bool) -> tuple[object, list[str]]:
            calls: list[str] = []
            previous = sys.getprofile()

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code in codes:
                    calls.append(frame.f_code.co_name)

            try:
                sys.setprofile(profile)
                environment = jinja2.Environment()
                factory = docxtpl_native._extension()._join_attribute_getter if native else filters.make_attrgetter
                result = factory(environment, 'a.0')({'a': ['found']})
            finally:
                sys.setprofile(previous)
            return result, calls

        expected, reference_calls = run(False)
        actual, candidate_calls = run(True)
        self.assertEqual(actual, expected)
        self.assertTrue(reference_calls)
        self.assertEqual(candidate_calls, [])

    def test_custom_parts_factory_and_factory_signature(self) -> None:
        def run(native: bool) -> tuple[object, list[tuple[str, object]]]:
            events: list[tuple[str, object]] = []

            def custom_parts(attribute: object) -> list[str]:
                events.append(('parts', attribute))
                return ['custom']

            environment = jinja2.Environment()
            factory = docxtpl_native._extension()._join_attribute_getter if native else filters.make_attrgetter
            with patch.dict(filters.__dict__, {'_prepare_attribute_parts': custom_parts}):
                getter = factory(environment, 'ignored')
            return getter({'custom': 'found'}), events

        self.assertEqual(run(True), run(False))

        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def custom_factory(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return 'custom'

        environment = jinja2.Environment()
        with patch.dict(filters.__dict__, {'make_attrgetter': custom_factory}):
            self.assertEqual(docxtpl_native._extension()._join_attribute_getter(
                environment, 'x', default=None), 'custom')
        self.assertEqual(calls, [((environment, 'x'), {'default': None})])

    def test_replaced_parts_iterable_is_consumed_lazily(self) -> None:
        def run(native: bool) -> tuple[list[object], list[str]]:
            events: list[str] = []

            def custom_parts(attribute: object) -> Iterator[str]:
                events.append('factory')

                def parts() -> Iterator[str]:
                    events.append('iterate')
                    yield 'x'

                return parts()

            environment = jinja2.Environment()
            factory = docxtpl_native._extension()._join_attribute_getter if native else filters.make_attrgetter
            with patch.dict(filters.__dict__, {'_prepare_attribute_parts': custom_parts}):
                getter = factory(environment, 'ignored')
            events.append('constructed')
            return [getter({'x': 'first'}), getter({'x': 'second'})], events

        self.assertEqual(run(True), run(False))

    def test_getitem_replacement_during_lookup_is_observed(self) -> None:
        def run(native: bool) -> tuple[object, list[str]]:
            environment = jinja2.Environment()
            events: list[str] = []
            factory = docxtpl_native._extension()._join_attribute_getter if native else filters.make_attrgetter
            getter = factory(environment, 'x.y')

            def second(obj: object, argument: object) -> object:
                events.append(f'second:{argument}')
                return 'changed'

            def first(obj: object, argument: object) -> object:
                events.append(f'first:{argument}')
                environment.getitem = second  # type: ignore[method-assign]
                return {'y': 'old'}

            environment.getitem = first  # type: ignore[method-assign]
            return getter({'x': {}}), events

        self.assertEqual(run(True), run(False))

    def test_paths_and_errors(self) -> None:
        for attribute, item in [(None, {'a': 1}), (0, ['zero']), ('a.0', {'a': ['zero']}),
                                ('a.00', {'a': ['zero']}), ('²', {})]:
            with self.subTest(attribute=attribute):
                def run(native: bool, attribute: str | int | None = attribute,
                        item: object = item) -> tuple[str, object]:
                    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
                    factory = docxtpl_native._extension()._join_attribute_getter if native else filters.make_attrgetter
                    try:
                        return 'ok', factory(environment, attribute)(item)
                    except (TypeError, ValueError, jinja2.UndefinedError) as error:
                        return type(error).__name__, str(error)

                self.assertEqual(run(True), run(False))

    def test_getitem_lookup_remains_live(self) -> None:
        def run(native: bool) -> tuple[object, list[tuple[str, object]]]:
            environment = jinja2.Environment()
            factory = docxtpl_native._extension()._join_attribute_getter if native else filters.make_attrgetter
            getter = factory(environment, 'x')
            calls: list[tuple[str, object]] = []

            def getitem(obj: object, argument: object) -> object:
                calls.append(('getitem', argument))
                return 'replacement'

            environment.getitem = getitem  # type: ignore[method-assign]
            return getter({}), calls

        self.assertEqual(run(True), run(False))

    def test_factory_defaults_are_bound_live(self) -> None:
        def run(native: bool) -> tuple[object, list[object]]:
            events: list[object] = []

            def postprocess(value: object) -> object:
                events.append(value)
                return value

            environment = jinja2.Environment()
            factory = docxtpl_native._extension()._join_attribute_getter if native else filters.make_attrgetter
            with patch.object(filters.make_attrgetter, '__defaults__', (postprocess, 'fallback')):
                getter = factory(environment, 'missing')
            return getter({}), events

        self.assertEqual(run(True), run(False))

    def test_sandbox_security_policy_is_observed(self) -> None:
        from jinja2.sandbox import SandboxedEnvironment

        def run(native: bool) -> tuple[tuple[str, str], list[tuple[str, str]]]:
            events: list[tuple[str, str]] = []

            class Value:
                public = 'visible'
                _private = 'secret'

            class Environment(SandboxedEnvironment):
                def is_safe_attribute(self, obj: object, attr: str, value: object) -> bool:
                    events.append((attr, str(value)))
                    return super().is_safe_attribute(obj, attr, value)

            environment = Environment(undefined=jinja2.StrictUndefined)
            factory = docxtpl_native._extension()._join_attribute_getter if native else filters.make_attrgetter
            result = (str(factory(environment, 'public')(Value())),
                      str(factory(environment, '_private', default='blocked')(Value())))
            return result, events

        expected = run(False)
        self.assertEqual(expected, (('visible', 'blocked'), [('public', 'visible'), ('_private', 'secret')]))
        self.assertEqual(run(True), expected)

    def test_sandbox_standard_lookup_is_native(self) -> None:
        from jinja2.sandbox import SandboxedEnvironment

        code = SandboxedEnvironment.getitem.__code__

        class Value:
            public = 'visible'

        def run(native: bool) -> tuple[object, list[str]]:
            calls: list[str] = []
            previous = sys.getprofile()

            def profile(frame: FrameType, event: str, argument: object) -> None:
                if event == 'call' and frame.f_code is code:
                    calls.append(frame.f_code.co_name)

            try:
                sys.setprofile(profile)
                environment = SandboxedEnvironment()
                factory = docxtpl_native._extension()._join_attribute_getter if native else filters.make_attrgetter
                result = factory(environment, 'public')(Value())
            finally:
                sys.setprofile(previous)
            return result, calls

        expected, reference_calls = run(False)
        actual, candidate_calls = run(True)
        self.assertEqual(actual, expected)
        self.assertTrue(reference_calls)
        self.assertEqual(candidate_calls, [])

if __name__ == '__main__':
    unittest.main()
