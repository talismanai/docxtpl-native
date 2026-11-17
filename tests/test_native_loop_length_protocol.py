"""Differential checks for materializing a loop's remaining iterator."""

import asyncio
import sys
import unittest
import builtins
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
import jinja2.runtime as runtime
from docxtpl_native.bridge import BridgeConfig
from jinja2.runtime import LoopContext
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


class LoopLengthProtocolTests(unittest.TestCase):
    def test_filtered_length_callback_catches_error_and_iterator_closes(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'select:{value}')
                if value == 2:
                    raise ValueError('predicate failed')
                return True

            def length(iterator: Any) -> int:
                events.append('length')
                try:
                    next(iterator)
                except ValueError as error:
                    events.append(f'caught:{error}')
                try:
                    next(iterator)
                except StopIteration:
                    events.append('exhausted')
                return 7

            environment = jinja2.Environment()
            environment.tests['selected'] = selected
            source = '{% for x in values if x is selected %}{{loop.length}}:{{x}};{% endfor %}'
            with patch.dict(runtime.__dict__, {'len': length}):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': [1, 2, 3]}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=[1, 2, 3])
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('7:1;', ['select:1', 'length', 'select:2',
                                             'caught:predicate failed', 'exhausted']))
        self.assertEqual(observe(True), expected)

    def test_replaced_length_property_is_called(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[str]]:
                    events: list[str] = []

                    def length(instance: Any) -> int:
                        events.append(type(instance).__name__)
                        return 42

                    environment = jinja2.Environment(enable_async=asynchronous)
                    loop_class = runtime.AsyncLoopContext if asynchronous else runtime.LoopContext
                    source = '{% for x in values %}{{ loop.length }};{% endfor %}'
                    with patch.object(loop_class, 'length', property(length)):
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': [1]}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=[1])
                    return output, events

                expected = ('42;', ['AsyncLoopContext' if asynchronous else 'LoopContext'])
                self.assertEqual(observe(False), expected)
                self.assertEqual(observe(True), expected)

    def test_length_calls_live_runtime_len_for_sized_iterable(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[tuple[str, bool]]]:
                    values = [1, 2]
                    events: list[tuple[str, bool]] = []

                    def length(value: Any) -> int:
                        events.append(('len', value is values))
                        return builtins.len(value)

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% for x in values %}{{ loop.length }}:{{ loop.revindex }};{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': length}):
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values)
                    return output, events

                expected = observe(False)
                self.assertEqual(expected, ('2:2;2:1;', [('len', True)]))
                self.assertEqual(observe(True), expected)

    def test_length_live_runtime_len_exception_keeps_identity(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    failure = ValueError('runtime len failed')
                    values = [1]
                    events: list[bool] = []

                    def length(value: Any) -> int:
                        events.append(value is values)
                        raise failure

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% for x in values %}{{ loop.length }}{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': length}):
                        with self.assertRaises(ValueError) as caught:
                            if native:
                                docxtpl_native._extension().PythonRenderSession(
                                    BridgeConfig(environment, False), {'values': values}
                                ).render('case', source)
                            else:
                                environment.from_string(source).render(values=values)
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(events, [True])

    def test_length_runtime_len_type_error_collects_remaining_items(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[str]]:
                    values = [1, 2, 3]
                    events: list[str] = []

                    def length(value: Any) -> int:
                        if value is values:
                            events.append('original')
                            raise TypeError('force materialization')
                        events.append('collected')
                        return builtins.len(value)

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% for x in values %}{{ loop.length }}:{{ x }};{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': length}):
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values)
                    return output, events

                expected = observe(False)
                self.assertEqual(expected, ('3:1;3:2;3:3;', ['original', 'collected']))
                self.assertEqual(observe(True), expected)

    def test_length_preserves_live_runtime_len_return_value(self) -> None:
        for asynchronous in (False, True):
            for returned in (None, True, -2, 'custom', 2 ** 100):
                with self.subTest(asynchronous=asynchronous, returned=returned):
                    def observe(native: bool) -> str:
                        environment = jinja2.Environment(enable_async=asynchronous)
                        source = '{% for x in values %}{{ loop.length }}{% endfor %}'
                        with patch.dict(runtime.__dict__, {'len': lambda value: returned}):
                            if native:
                                return docxtpl_native._extension().PythonRenderSession(
                                    BridgeConfig(environment, False), {'values': [1]}
                                ).render('case', source)
                            return environment.from_string(source).render(values=[1])

                    self.assertEqual(observe(True), observe(False))

    def test_none_length_is_recomputed_on_each_access(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, int]:
                    events: list[None] = []

                    def length(value: Any) -> None:
                        events.append(None)
                        return None

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% for x in values %}{{ loop.length }};{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': length}):
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': [1, 2]}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=[1, 2])
                    return output, builtins.len(events)

                expected = observe(False)
                self.assertEqual(expected, ('None;None;', 2))
                self.assertEqual(observe(True), expected)

    def test_length_callback_receives_template_literal(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[Any]]:
                    events: list[Any] = []

                    def length(value: Any) -> int:
                        events.append(value)
                        return 7

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% for x in [1, 2] %}{{ loop.length }};{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': length}):
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render()
                    return output, events

                expected = observe(False)
                self.assertEqual(expected, ('7;7;', [[1, 2]]))
                self.assertEqual(observe(True), expected)

    def test_filtered_loop_length_preserves_live_len_and_evaluation_order(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[str]]:
                    events: list[str] = []

                    def values() -> Any:
                        for value in (1, 2, 3):
                            events.append(f'yield:{value}')
                            yield value

                    def selected(value: int) -> bool:
                        events.append(f'select:{value}')
                        return value != 2

                    def length(value: Any) -> int:
                        events.append(f'len:{type(value).__name__}')
                        return builtins.len(value)

                    environment = jinja2.Environment(enable_async=asynchronous)
                    environment.tests['selected'] = selected
                    source = '{% for x in values if x is selected %}{{ loop.length }}:{{ x }};{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': length}):
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values()}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values())
                    return output, events

                expected = observe(False)
                self.assertEqual(expected[0], '2:1;2:3;')
                self.assertEqual(observe(True), expected)

    def test_filtered_length_custom_len_can_return_without_consuming(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[str]]:
                    events: list[str] = []

                    def values() -> Any:
                        for value in (1, 2, 3):
                            events.append(f'yield:{value}')
                            yield value

                    def selected(value: int) -> bool:
                        events.append(f'select:{value}')
                        return value != 2

                    def length(value: Any) -> int:
                        events.append(f'len:{type(value).__name__}')
                        return 7

                    environment = jinja2.Environment(enable_async=asynchronous)
                    environment.tests['selected'] = selected
                    source = '{% for x in values if x is selected %}{{ loop.length }}:{{ x }};{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': length}):
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values()}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values())
                    return output, events

                expected = observe(False)
                self.assertEqual(expected[0], '7:1;7:3;')
                self.assertEqual(observe(True), expected)

    def test_filtered_length_callback_can_consume_and_retain_generator(self) -> None:
        def observe(native: bool, asynchronous: bool, consume: bool, break_loop: bool) -> tuple[str, list[str], list[int]]:
            events: list[str] = []
            captured: list[Any] = []

            def values() -> Any:
                for value in (1, 2, 3, 4):
                    events.append(f'yield:{value}')
                    yield value

            def selected(value: int) -> bool:
                events.append(f'select:{value}')
                return value % 2 == 1

            def length(iterator: Any) -> int:
                captured.append(iterator)
                events.append(f'len:{type(iterator).__name__}')
                if consume:
                    events.append(f'next:{next(iterator)}')
                return 7

            environment = jinja2.Environment(enable_async=asynchronous, extensions=['jinja2.ext.loopcontrols'])
            environment.tests['selected'] = selected
            source = '{% for x in values if x is selected %}{{loop.length}}:{{x}};'
            source += '{% break %}' if break_loop else ''
            source += '{% endfor %}'
            with patch.dict(runtime.__dict__, {'len': length}):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': values()}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=values())

            async def collect_async() -> list[int]:
                return [value async for value in captured[0]]

            remaining = (asyncio.run(collect_async()) if asynchronous else list(captured[0])) if captured else []
            return output, events, remaining

        for asynchronous, consume, break_loop in ((False, False, True), (False, True, True),
                                                   (False, True, False), (True, False, True)):
            with self.subTest(asynchronous=asynchronous, consume=consume, break_loop=break_loop):
                expected = observe(False, asynchronous, consume, break_loop)
                self.assertEqual(expected[0], '7:1;')
                self.assertEqual(expected[2], [] if asynchronous or consume else [3])
                self.assertEqual(observe(True, asynchronous, consume, break_loop), expected)

    def test_filtered_length_custom_len_preserves_exception(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    failure = ValueError('filtered length failed')
                    events: list[str] = []

                    def length(value: Any) -> int:
                        events.append(type(value).__name__)
                        raise failure

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% for x in values if x %}{{ loop.length }}{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': length}):
                        with self.assertRaises(ValueError) as caught:
                            if native:
                                docxtpl_native._extension().PythonRenderSession(
                                    BridgeConfig(environment, False), {'values': iter((1, 2))}
                                ).render('case', source)
                            else:
                                environment.from_string(source).render(values=iter((1, 2)))
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(events, ['async_generator' if asynchronous else 'generator'])

    def test_collected_length_exception_keeps_identity(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    values = [1, 2]
                    failure = ValueError('collected length failed')
                    events: list[str] = []

                    def length(value: Any) -> int:
                        if value is values:
                            events.append('original')
                            raise TypeError('force materialization')
                        events.append('collected')
                        raise failure

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% for x in values %}{{ loop.length }}{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': length}):
                        with self.assertRaises(ValueError) as caught:
                            if native:
                                docxtpl_native._extension().PythonRenderSession(
                                    BridgeConfig(environment, False), {'values': values}
                                ).render('case', source)
                            else:
                                environment.from_string(source).render(values=values)
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(events, ['original', 'collected'])

    def test_async_iterable_collection_calls_live_len(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Items:
                def __aiter__(self) -> Any:
                    events.append('aiter')

                    async def generate() -> Any:
                        for item in (1, 2):
                            events.append(f'yield:{item}')
                            yield item
                    return generate()

            values = Items()

            def length(value: Any) -> int:
                if value is values:
                    events.append('original')
                    raise TypeError('force materialization')
                events.append('collected')
                return builtins.len(value)

            environment = jinja2.Environment(enable_async=True)
            source = '{% for x in values %}{{ loop.length }}:{{ x }};{% endfor %}'
            with patch.dict(runtime.__dict__, {'len': length}):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': values}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values=values)
            return output, events

        self.assertEqual(observe(True), observe(False))

    def test_collected_length_preserves_addition_protocol(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[tuple[str, Any]]]:
                    values = [1, 2]
                    events: list[tuple[str, Any]] = []

                    class Stage:
                        def __add__(self, other: Any) -> str:
                            events.append(('after', (type(other).__name__, other)))
                            return 'length-result'

                    class Count:
                        def __add__(self, other: Any) -> Stage:
                            events.append(('index', (type(other).__name__, other)))
                            return Stage()

                    def length(value: Any) -> Any:
                        if value is values:
                            raise TypeError('force materialization')
                        return Count()

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% for x in values %}{{ loop.length }};{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': length}):
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': values}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=values)
                    return output, events

                expected = observe(False)
                self.assertEqual(expected, ('length-result;length-result;',
                                            [('index', ('int', 1)), ('after', ('bool', False))]))
                self.assertEqual(observe(True), expected)

    def test_length_rechecks_missing_after_custom_addition(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[Any]]:
                    events: list[Any] = []
                    original = runtime.missing
                    marker = object()

                    def values() -> Any:
                        yield 1
                        yield marker
                        yield 3

                    iterator = values()

                    class Partial:
                        def __add__(self, other: Any) -> int:
                            events.append(('after', type(other).__name__, other))
                            return 2

                    class Count:
                        def __add__(self, other: Any) -> Partial:
                            events.append(('index', other))
                            runtime.missing = marker
                            return Partial()

                    def length(value: Any) -> Any:
                        if value is iterator:
                            raise TypeError('force materialization')
                        return Count()

                    environment = jinja2.Environment(enable_async=asynchronous, extensions=['jinja2.ext.loopcontrols'])
                    source = '{% for x in values %}{{ loop.last }}:{{ loop.length }}{% break %}{% endfor %}'
                    try:
                        with patch.dict(runtime.__dict__, {'len': length}):
                            if native:
                                output = docxtpl_native._extension().PythonRenderSession(
                                    BridgeConfig(environment, False), {'values': iterator}
                                ).render('case', source)
                            else:
                                output = environment.from_string(source).render(values=iterator)
                        return output, events
                    finally:
                        runtime.missing = original

                expected = observe(False)
                self.assertEqual(expected, ('False:2', [('index', 1), ('after', 'bool', False)]))
                self.assertEqual(observe(True), expected)

    def test_revindex_uses_returned_length_subtraction(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[int]]:
                    events: list[int] = []

                    class Length:
                        def __sub__(self, index: int) -> str:
                            events.append(index)
                            return f'index-{index}'

                    length = Length()
                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% for x in values %}{{ loop.revindex }}:{{ loop.revindex0 }};{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': lambda value: length}):
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': [1, 2]}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=[1, 2])
                    return output, events

                expected = observe(False)
                self.assertEqual(expected, ('index-0:index-1;index-1:index-2;', [0, 1, 1, 2]))
                self.assertEqual(observe(True), expected)

    def test_length_retains_returned_object_identity(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool) -> tuple[str, list[bool]]:
                    value = object()
                    events: list[bool] = []

                    def inspect(item: Any) -> str:
                        events.append(item is value)
                        return 'same' if item is value else 'different'

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% for x in values %}{{ inspect(loop.length) }};{% endfor %}'
                    with patch.dict(runtime.__dict__, {'len': lambda item: value}):
                        if native:
                            output = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'values': [1, 2], 'inspect': inspect}
                            ).render('case', source)
                        else:
                            output = environment.from_string(source).render(values=[1, 2], inspect=inspect)
                    return output, events

                expected = observe(False)
                self.assertEqual(expected, ('same;same;', [True, True]))
                self.assertEqual(observe(True), expected)

    def observe(self, native: bool, asynchronous: bool, fail: bool) -> tuple[Any, list[str]]:
        events: list[str] = []
        failure = ValueError('length hint failed')

        class Items:
            def __init__(self) -> None:
                self.index = 0

            def __iter__(self) -> Any:
                events.append('iter')
                return self

            def __next__(self) -> int:
                events.append('next')
                if self.index == 3:
                    raise StopIteration
                self.index += 1
                return self.index

            def __length_hint__(self) -> int:
                events.append('hint')
                if fail:
                    raise failure
                return 3 - self.index

        environment = jinja2.Environment(enable_async=asynchronous)
        source = '{% for x in values %}{{ loop.length }};{% endfor %}'
        values = Items()
        try:
            if native:
                result: Any = ('ok', docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'values': values}
                ).render('case', source))
            else:
                result = ('ok', environment.from_string(source).render(values=values))
        except Exception as error:  # noqa: BLE001 - compare exception identity and side effects
            result = (type(error).__name__, str(error), error is failure)
        return result, events

    def test_length_materialization_preserves_iterator_protocol(self) -> None:
        for asynchronous in (False, True):
            for fail in (False, True):
                with self.subTest(asynchronous=asynchronous, fail=fail):
                    self.assertEqual(self.observe(True, asynchronous, fail),
                                     self.observe(False, asynchronous, fail))


    def test_length_uses_live_runtime_list_callback(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def collect(iterator: Any) -> list[Any]:
                events.append('list')
                return list(iterator)

            environment = jinja2.Environment()
            source = '{% for x in values %}{{ loop.length }};{% endfor %}'
            values = (x for x in (1, 2, 3))
            with patch.dict(runtime.__dict__, {'list': collect}):
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': values}
                    ).render('case', source)
                else:
                    result = environment.from_string(source).render(values=values)
            return result, events

        self.assertEqual(observe(True), observe(False))


    def test_length_uses_length_of_custom_collector_result(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Collected:
                def __init__(self, values: Any) -> None:
                    self.values = list(values)

                def __iter__(self) -> Any:
                    events.append('iter-result')
                    def generate() -> Any:
                        for value in self.values:
                            events.append(f'next-result:{value}')
                            yield value
                    return generate()

                def __len__(self) -> int:
                    events.append('len-result')
                    return 10

            def collect(iterator: Any) -> Collected:
                events.append('collect')
                return Collected(iterator)

            environment = jinja2.Environment()
            def emit(value: int) -> int:
                events.append(f'emit:{value}')
                return value

            source = '{% for x in values %}{{ loop.length }}:{{ emit(x) }};{% endfor %}'
            values = (x for x in (1, 2, 3))
            with patch.dict(runtime.__dict__, {'list': collect}):
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': values, 'emit': emit}
                    ).render('case', source)
                else:
                    result = environment.from_string(source).render(values=values, emit=emit)
            return result, events

        self.assertEqual(observe(True), observe(False))


    def test_standard_loop_length_body_stays_native(self) -> None:
        property_object = LoopContext.__dict__['length']
        forbidden = property_object.fget.__code__
        previous = sys.getprofile()
        calls: list[str] = []

        def profile(frame: Any, event: str, argument: Any) -> None:
            if event == 'call' and frame.f_code is forbidden:
                calls.append(frame.f_code.co_name)

        try:
            sys.setprofile(profile)
            environment = jinja2.Environment()
            values = (x for x in (1, 2, 3))
            result = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), {'values': values}
            ).render('case', '{% for x in values %}{{ loop.length }};{% endfor %}')
        finally:
            sys.setprofile(previous)
        self.assertEqual(result, '3;3;3;')
        self.assertEqual(calls, [])


    def test_loop_iteration_uses_next_slot_not_instance_override(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Items:
                def __init__(self) -> None:
                    self.count = 0
                    self.__next__ = self.shadow  # type: ignore[method-assign]

                def __iter__(self) -> Any:
                    return self

                def __next__(self) -> int:
                    events.append('slot')
                    if self.count == 2:
                        raise StopIteration
                    self.count += 1
                    return self.count

                def shadow(self) -> int:
                    events.append('shadow')
                    raise StopIteration

            environment = jinja2.Environment()
            source = '{% for x in values %}{{ loop.index }}:{{ x }};{% endfor %}'
            values = Items()
            if native:
                result = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'values': values}
                ).render('case', source)
            else:
                result = environment.from_string(source).render(values=values)
            return result, events

        self.assertEqual(observe(True), observe(False))


    def test_loop_iteration_uses_live_runtime_next_callback(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def step(iterator: Any) -> Any:
                events.append('next')
                return next(iterator)

            environment = jinja2.Environment()
            source = '{% for x in values %}{{ loop.index }}:{{ x }};{% endfor %}'
            values = iter((1, 2))
            with patch.dict(runtime.__dict__, {'next': step}):
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': values}
                    ).render('case', source)
                else:
                    result = environment.from_string(source).render(values=values)
            return result, events

        self.assertEqual(observe(True), observe(False))


    def test_lookahead_uses_next_with_default(self) -> None:
        def observe(native: bool) -> tuple[str, list[int]]:
            events: list[int] = []

            def step(*args: Any) -> Any:
                events.append(len(args))
                return next(*args)

            environment = jinja2.Environment()
            source = '{% for x in values %}{{ loop.last }};{% endfor %}'
            values = iter((1, 2))
            with patch.dict(runtime.__dict__, {'next': step}):
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': values}
                    ).render('case', source)
                else:
                    result = environment.from_string(source).render(values=values)
            return result, events

        self.assertEqual(observe(True), observe(False))


    def test_complete_document_with_iterator_length_and_lookahead(self) -> None:
        source = '{% for x in values %}{{ loop.length }}:{{ loop.last }}:{{ x }};{% endfor %}'
        document = template(source)
        expected = generate(docxtpl, source, {'values': iter((1, 2, 3))}, source=document)
        actual = generate(docxtpl_native, source, {'values': iter((1, 2, 3))}, source=document)
        self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_complete_document_with_live_runtime_len(self) -> None:
        source = '{% for x in values %}{{ loop.length }}:{{ x }};{% endfor %}'
        document = template(source)
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def run(native: bool) -> tuple[bytes, list[bool]]:
                    values = [1, 2]
                    events: list[bool] = []

                    def length(value: Any) -> int:
                        events.append(value is values)
                        return builtins.len(value)

                    with patch.dict(runtime.__dict__, {'len': length}):
                        result = generate(docxtpl_native if native else docxtpl, source, {'values': values},
                                          jinja2.Environment(enable_async=asynchronous), source=document)
                    return result, events

                expected, expected_events = run(False)
                actual, actual_events = run(True)
                self.assertEqual(expected_events, [True])
                self.assertEqual(actual_events, expected_events)
                comparison = compare_docx(expected, actual)
                self.assertTrue(comparison['equivalent'], comparison)


if __name__ == '__main__':
    unittest.main()
