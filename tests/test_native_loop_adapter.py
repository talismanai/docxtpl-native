"""Acceptance checks for the Python identity of native loop callbacks."""

import asyncio
import gc
import re
import sys
import unittest
import warnings
import weakref
from collections.abc import Iterator
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2.runtime import AsyncLoopContext, LoopContext
from jinja2.sandbox import SandboxedEnvironment


class LoopAdapterTests(unittest.TestCase):
    def test_interleaved_async_renders_keep_filtered_loop_continuations_separate(self) -> None:
        def observe(native: bool) -> tuple[list[str], list[tuple[str, int]]]:
            events: list[tuple[str, int]] = []
            handles: list[Any] = []
            source = '{% for item in items if selected(item) %}{{ advance(loop) }}{{ item }}{% endfor %}'
            environment = jinja2.Environment(enable_async=True)

            def values(label: str, numbers: list[int]) -> dict[str, Any]:
                def selected(value: int) -> bool:
                    events.append((label, -value))
                    return True

                async def advance(loop: Any) -> str:
                    await asyncio.sleep(0)
                    item, _ = await loop.__anext__()
                    events.append((label, item))
                    return ''

                return {'items': numbers, 'advance': advance, 'selected': selected}

            contexts = [values('A', [1, 2]), values('B', [11, 12])]
            if native:
                from docxtpl_native import bridge

                def retain(render: Any) -> Any:
                    handles.append(render)

                    async def ignored() -> str:
                        return ''

                    return ignored()

                with patch.object(bridge, '_await_native_render', retain):
                    for context in contexts:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), context
                        ).render('case', source)
            else:
                handles.extend(environment.from_string(source).render_async(context) for context in contexts)

            async def run() -> list[str]:
                return list(await asyncio.gather(*handles))

            return asyncio.run(run()), events

        expected = observe(False)
        self.assertEqual(expected, (['1', '11'], [('A', -1), ('B', -11), ('A', -2), ('A', 2),
                                                 ('B', -12), ('B', 12)]))
        self.assertEqual(observe(True), expected)

    def test_async_filtered_predicate_can_suspend_during_callback_advance(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def selected(value: int) -> bool:
                events.append(f'filter-start:{value}')
                await asyncio.sleep(0)
                events.append(f'filter-end:{value}')
                return value % 2 == 1

            async def advance(loop: Any) -> str:
                await asyncio.sleep(0)
                item, _ = await loop.__anext__()
                events.append(f'next:{item}')
                return ''

            environment = jinja2.Environment(enable_async=True)
            source = '{% for item in items if selected(item) %}{{ advance(loop) }}{{ item }}{% endfor %}'
            values = {'items': [1, 2, 3, 4], 'selected': selected, 'advance': advance}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('1', ['filter-start:1', 'filter-end:1', 'filter-start:2', 'filter-end:2',
                                         'filter-start:3', 'filter-end:3', 'next:3', 'filter-start:4', 'filter-end:4']))
        self.assertEqual(observe(True), expected)

    def test_child_task_can_advance_suspended_filtered_loop(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                return value % 2 == 1

            async def advance(loop: Any) -> str:
                async def child() -> None:
                    item, _ = await loop.__anext__()
                    events.append(f'child:{item}')

                await asyncio.create_task(child())
                return ''

            environment = jinja2.Environment(enable_async=True)
            environment.tests['selected'] = selected
            source = '{% for item in items if item is selected %}{{ advance(loop) }}{{ item }}{% endfor %}'
            values = {'items': [1, 2, 3, 4], 'advance': advance}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('1', ['filter:1', 'filter:2', 'filter:3', 'child:3', 'filter:4']))
        self.assertEqual(observe(True), expected)

    def test_async_callback_can_advance_filtered_loop_after_suspension(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                return value % 2 == 1

            async def advance(loop: Any) -> str:
                await asyncio.sleep(0)
                item, returned = await loop.__anext__()
                events.append(f'next:{item}:{returned is loop}')
                return ''

            environment = jinja2.Environment(enable_async=True)
            environment.tests['selected'] = selected
            source = '{% for item in items if item is selected %}{{ advance(loop) }}{{ item }}{% endfor %}'
            values = {'items': [1, 2, 3, 4], 'advance': advance}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('1', ['filter:1', 'filter:2', 'filter:3', 'next:3:True', 'filter:4']))
        self.assertEqual(observe(True), expected)

    def test_loop_object_lifetime_matches_reference(self) -> None:
        # The loop object is released when the render ends unless a callback
        # keeps it; a kept one is collected once dropped.
        for header in ('{% for x in items %}', '{% for x in items if x is odd %}'):
            for body in ('{{ capture(loop) }}', '{{ capture(loop) }}{% break %}', '{{ capture(loop) }}{{ fail() }}'):
                for retain in (False, True):
                    with self.subTest(header=header, body=body, retain=retain):
                        def observe(native: bool, source: str = header + body + '{% endfor %}',
                                    retain: bool = retain) -> tuple[bool, bool, bool]:
                            references: list[Any] = []
                            kept: list[Any] = []

                            def capture(loop: Any) -> str:
                                references.append(weakref.ref(loop))
                                if retain:
                                    kept.append(loop)
                                return ''

                            def fail() -> str:
                                raise RuntimeError('stop')

                            environment = jinja2.Environment(extensions=['jinja2.ext.loopcontrols'])
                            environment.tests['odd'] = lambda value: value % 2 == 1
                            values = {'items': [1, 2, 3], 'capture': capture, 'fail': fail}
                            failed = False
                            try:
                                if native:
                                    docxtpl_native._extension().PythonRenderSession(
                                        BridgeConfig(environment, False), values).render('case', source)
                                else:
                                    environment.from_string(source).render(values)
                            except RuntimeError:
                                failed = True
                            alive = references[0]() is not None
                            kept.clear()
                            gc.collect()
                            return failed, alive, references[0]() is not None

                        expected = observe(False)
                        self.assertEqual(expected[2], False)
                        self.assertEqual(observe(True), expected)

    def test_filtered_loop_retained_after_render_error_can_advance(self) -> None:
        def observe(native: bool) -> tuple[int, list[str]]:
            events: list[str] = []
            retained: list[Any] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                return value % 2 == 1

            def retain_and_fail(loop: Any) -> str:
                retained.append(loop)
                raise RuntimeError('stop render')

            environment = jinja2.Environment()
            environment.tests['selected'] = selected
            source = '{% for item in items if item is selected %}{{ retain_and_fail(loop) }}{% endfor %}'
            values = {'items': [1, 2, 3], 'retain_and_fail': retain_and_fail}
            with self.assertRaisesRegex(RuntimeError, 'stop render'):
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), values
                    ).render('case', source)
                else:
                    environment.from_string(source).render(values)
            item, _ = next(retained[0])
            return item, events

        expected = observe(False)
        self.assertEqual(expected, (3, ['filter:1', 'filter:2', 'filter:3']))
        self.assertEqual(observe(True), expected)

    def test_filtered_loop_retained_after_break_reads_mutated_context(self) -> None:
        def observe(native: bool) -> tuple[str, int, bool, list[int]]:
            retained: list[Any] = []
            reads: list[int] = []

            class Settings:
                limit = 1

                @property
                def selected_limit(self) -> int:
                    reads.append(self.limit)
                    return self.limit

            settings = Settings()

            def remember(loop: Any) -> str:
                retained.append(loop)
                return ''

            environment = jinja2.Environment(extensions=['jinja2.ext.loopcontrols'])
            source = ('{% for item in items if item > settings.selected_limit %}'
                      '{{ remember(loop) }}{{ item }}{% break %}{% endfor %}')
            values = {'items': [2, 3, 5], 'settings': settings, 'remember': remember}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            settings.limit = 3
            item, loop = next(retained[0])
            return output, item, loop is retained[0], reads

        expected = observe(False)
        self.assertEqual(expected, ('2', 5, True, [1, 3, 3]))
        self.assertEqual(observe(True), expected)

    def test_filtered_predicate_error_releases_source_before_callback_catches(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Items:
                def __iter__(self) -> Iterator[int]:
                    try:
                        for value in (1, 2, 3):
                            events.append(f'yield:{value}')
                            yield value
                    finally:
                        events.append('closed')

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                if value == 2:
                    raise ValueError('predicate failed')
                return True

            def advance(loop: Any) -> str:
                try:
                    next(loop)
                except ValueError:
                    events.append('caught')
                return ''

            environment = jinja2.Environment()
            environment.tests['selected'] = selected
            source = '{% for item in items if item is selected %}{{ advance(loop) }}{{ item }}{% endfor %}'
            values = {'items': Items(), 'advance': advance}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('1', ['yield:1', 'filter:1', 'yield:2', 'filter:2', 'closed', 'caught']))
        self.assertEqual(observe(True), expected)

    def test_callback_can_advance_filtered_loop_repeatedly(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                return value % 2 == 1

            def advance(loop: Any) -> str:
                for _ in range(2):
                    item, returned = next(loop)
                    events.append(f'next:{item}:{loop.index0}:{returned is loop}')
                return ''

            environment = jinja2.Environment()
            environment.tests['selected'] = selected
            source = '{% for item in items if item is selected %}{{ advance(loop) }}{{ item }}{% endfor %}'
            values = {'items': [1, 2, 3, 4, 5, 6], 'advance': advance}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('1', ['filter:1', 'filter:2', 'filter:3', 'next:3:1:True',
                                         'filter:4', 'filter:5', 'next:5:2:True', 'filter:6']))
        self.assertEqual(observe(True), expected)

    def test_callback_can_catch_filtered_predicate_exception(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                if value == 2:
                    raise ValueError('predicate failed')
                return True

            def advance(loop: Any) -> str:
                try:
                    next(loop)
                except ValueError as error:
                    events.append(f'caught:{error}:{loop.index0}')
                return ''

            environment = jinja2.Environment()
            environment.tests['selected'] = selected
            source = '{% for item in items if item is selected %}{{ advance(loop) }}{{ item }}{% endfor %}'
            values = {'items': [1, 2, 3], 'advance': advance}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('1', ['filter:1', 'filter:2', 'caught:predicate failed:0']))
        self.assertEqual(observe(True), expected)

    def test_filtered_loop_finalizer_runs_before_length_materialization(self) -> None:
        def observe(native: bool, replace: bool, autoescape: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                return value % 2 == 1

            def finalize(value: Any) -> Any:
                if type(value) is LoopContext:
                    events.append('finalize')
                    if replace:
                        return 'changed'
                return value

            environment = jinja2.Environment(finalize=finalize, autoescape=autoescape)
            environment.tests['selected'] = selected
            source = '{% for item in items if item is selected %}{{ loop }};{% endfor %}'
            values = {'items': [1, 2, 3]}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        for replace in (False, True):
            for autoescape in (False, True):
                with self.subTest(replace=replace, autoescape=autoescape):
                    expected = observe(False, replace, autoescape)
                    text = ('changed;changed;' if replace else
                            '&lt;LoopContext 1/2&gt;;&lt;LoopContext 2/2&gt;;' if autoescape else
                            '<LoopContext 1/2>;<LoopContext 2/2>;')
                    self.assertEqual(expected, (text, ['filter:1', 'finalize', 'filter:2', 'filter:3', 'finalize']))
                    self.assertEqual(observe(True, replace, autoescape), expected)

    def test_filtered_loop_repr_uses_filtered_length(self) -> None:
        def observe(native: bool) -> str:
            environment = jinja2.Environment()
            source = '{% for item in items if item > 1 %}{{ loop }};{% endfor %}'
            values = {'items': [1, 2, 3]}
            if native:
                return docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            return environment.from_string(source).render(values)

        expected = observe(False)
        self.assertEqual(expected, '<LoopContext 1/2>;<LoopContext 2/2>;')
        self.assertEqual(observe(True), expected)

    def test_filtered_loop_argument_preserves_predicate_order(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                return value % 2 == 1

            def inspect(loop: Any) -> str:
                events.append(f'inspect:{loop.index0}')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.tests['selected'] = selected
            source = '{% for item in items if item is selected %}{{ inspect(loop) }}{{ item }}{% endfor %}'
            values = {'items': [1, 2, 3], 'inspect': inspect}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('13', ['filter:1', 'inspect:0', 'filter:2', 'filter:3', 'inspect:1']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_callback_can_advance_filtered_loop(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                return value % 2 == 1

            def advance(loop: Any) -> str:
                item, _ = next(loop)
                events.append(f'next:{item}')
                return ''

            environment = jinja2.Environment()
            environment.tests['selected'] = selected
            source = '{% for item in items if item is selected %}{{ advance(loop) }}{{ item }}{% endfor %}'
            values = {'items': [1, 2, 3, 4], 'advance': advance}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('1', ['filter:1', 'filter:2', 'filter:3', 'next:3', 'filter:4']))
        self.assertEqual(observe(True), expected)

    def test_async_loop_repr_warning_origin_matches_reference(self) -> None:
        def observe(native: bool) -> list[tuple[str, int]]:
            environment = jinja2.Environment(enable_async=True)
            source = '{% for item in [1] %}{{ loop }}{% endfor %}'
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter('always', RuntimeWarning)
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    environment.from_string(source).render()
                gc.collect()
            return [(Path(warning.filename).name, warning.lineno) for warning in recorded]

        expected = observe(False)
        self.assertEqual(len(expected), 1)
        self.assertEqual(observe(True), expected)

    def test_async_loop_repr_preserves_coroutine_repr_and_warning(self) -> None:
        def observe(native: bool) -> tuple[str, list[tuple[type[Warning], str]]]:
            environment = jinja2.Environment(enable_async=True)
            source = '{% for item in [1] %}{{ loop }}{% endfor %}'
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter('always', RuntimeWarning)
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
                gc.collect()
            return re.sub(r'0x[0-9a-fA-F]+', 'ADDRESS', output), [
                (warning.category, str(warning.message)) for warning in recorded
            ]

        expected = observe(False)
        self.assertIn('AsyncLoopContext', expected[0])
        self.assertEqual(len(expected[1]), 1)
        self.assertEqual(observe(True), expected)

    def test_async_loop_repr_does_not_execute_reference_repr(self) -> None:
        calls: list[str] = []

        def profile(frame: FrameType, event: str, arg: object) -> None:
            del arg
            if event == 'call' and frame.f_code is LoopContext.__repr__.__code__:
                calls.append('repr')

        environment = jinja2.Environment(enable_async=True)
        session = docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(environment, False), {}
        )
        previous = sys.getprofile()
        try:
            sys.setprofile(profile)
            session.render('case', '{% for item in [1] %}{{ loop }}{% endfor %}')
        finally:
            sys.setprofile(previous)
        self.assertEqual(calls, [])

    def test_custom_loop_repr_remains_user_callback(self) -> None:
        def observe(native: bool) -> tuple[str, list[bool]]:
            seen: list[bool] = []

            def custom(loop: LoopContext) -> str:
                seen.append(type(loop) is LoopContext)
                return 'custom-loop'

            environment = jinja2.Environment()
            with patch.object(LoopContext, '__repr__', custom):
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', '{% for item in [1] %}{{ loop }}{% endfor %}')
                else:
                    result = environment.from_string('{% for item in [1] %}{{ loop }}{% endfor %}').render()
            return result, seen

        expected = observe(False)
        self.assertEqual(expected, ('custom-loop', [True]))
        self.assertEqual(observe(True), expected)

    def test_identity_finalizer_does_not_run_reference_loop_repr(self) -> None:
        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                calls: list[str] = []

                def finalize(value: Any) -> Any:
                    return value

                def profile(frame: FrameType, event: str, arg: object, recorded: list[str] = calls) -> None:
                    del arg
                    if event == 'call' and frame.f_code is LoopContext.__repr__.__code__:
                        recorded.append('repr')

                environment = jinja2.Environment(autoescape=autoescape, finalize=finalize)
                session = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {}
                )
                previous = sys.getprofile()
                try:
                    sys.setprofile(profile)
                    result = session.render('case', '{% for item in [1] %}{{ loop }}{% endfor %}')
                finally:
                    sys.setprofile(previous)
                expected = '&lt;LoopContext 1/1&gt;' if autoescape else '<LoopContext 1/1>'
                self.assertEqual(result, expected)
                self.assertEqual(calls, [])

    def test_rendering_loop_repr_does_not_execute_reference_repr(self) -> None:
        calls: list[str] = []

        def profile(frame: FrameType, event: str, arg: object) -> None:
            del arg
            if event == 'call' and frame.f_code is LoopContext.__repr__.__code__:
                calls.append('repr')

        session = docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(jinja2.Environment(), False), {}
        )
        previous = sys.getprofile()
        try:
            sys.setprofile(profile)
            result = session.render('case', '{% for item in [1] %}{{ loop }}{% endfor %}')
        finally:
            sys.setprofile(previous)
        self.assertEqual(result, '<LoopContext 1/1>')
        self.assertEqual(calls, [])

    def test_loop_is_released_before_following_template_code(self) -> None:
        def observe(native: bool) -> tuple[str, list[bool]]:
            references: list[Any] = []
            events: list[bool] = []

            def capture(loop: Any) -> str:
                references.append(weakref.ref(loop))
                return ''

            def check() -> str:
                gc.collect()
                events.append(references[0]() is None)
                return ''

            environment = jinja2.Environment()
            source = '{% for item in [1] %}{{ capture(loop) }}{% endfor %}{{ check() }}'
            values = {'capture': capture, 'check': check}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('', [True]))
        self.assertEqual(observe(True), expected)

    def test_loop_is_released_after_render_when_only_weakly_referenced(self) -> None:
        def observe(native: bool) -> bool:
            references: list[Any] = []

            def capture(loop: Any) -> str:
                references.append(weakref.ref(loop))
                return ''

            environment = jinja2.Environment()
            source = '{% for item in [1] %}{{ capture(loop) }}{% endfor %}'
            values = {'capture': capture}
            if native:
                session = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                )
                session.render('case', source)
            else:
                environment.from_string(source).render(values)
            gc.collect()
            return references[0]() is None

        self.assertTrue(observe(False))
        self.assertTrue(observe(True))

    def test_mutated_index_controls_filtered_loop_cached_length(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[int]]:
            calls: list[int] = []

            def change(loop: Any) -> str:
                calls.append(loop.index0)
                loop.index0 = -4
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% for item in items if item > 0 %}'
                      '{% if loop.first %}{{ change(loop) }}{% endif %}'
                      '{{ loop.length }}:{{ item }};{% endfor %}')
            values = {'items': [1, 2, 3], 'change': change}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, calls

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('-1:1;-1:2;-1:3;', [0]))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_loop_repr_calls_custom_type_with_live_loop(self) -> None:
        def observe(native: bool) -> tuple[str, list[bool]]:
            events: list[bool] = []

            def custom_type(value: object) -> SimpleNamespace:
                events.append(type(value) is LoopContext)
                return SimpleNamespace(__name__='Alias')

            environment = jinja2.Environment()
            source = '{% for item in [1] %}{{ loop }}{% endfor %}'
            with patch.dict(jinja2.runtime.__dict__, {'type': custom_type}):
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    result = environment.from_string(source).render()
            return result, events

        expected = observe(False)
        self.assertEqual(expected, ('<Alias 1/1>', [True]))
        self.assertEqual(observe(True), expected)

    def test_loop_repr_formats_custom_length_through_python_protocol(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Length:
                def __format__(self, specification: str) -> str:
                    events.append(f'format:{specification}')
                    return 'custom-length'

            def length(value: object) -> Length:
                events.append('length')
                return Length()

            environment = jinja2.Environment()
            source = '{% for item in items %}{{ loop }}{% endfor %}'
            values = {'items': [1]}
            with patch.dict(jinja2.runtime.__dict__, {'len': length}):
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), values
                    ).render('case', source)
                else:
                    result = environment.from_string(source).render(values)
            return result, events

        expected = observe(False)
        self.assertEqual(expected, ('<LoopContext 1/custom-length>', ['length', 'format:']))
        self.assertEqual(observe(True), expected)

    def test_rendering_loop_formats_jinja_repr_and_consumes_length_lazily(self) -> None:
        def observe(native: bool, autoescape: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def items() -> Iterator[int]:
                for item in (1, 2):
                    events.append(f'yield:{item}')
                    yield item

            def mark(item: int) -> str:
                events.append(f'body:{item}')
                return ''

            environment = jinja2.Environment(autoescape=autoescape)
            source = '{% for item in items %}{{ mark(item) }}{{ loop }};{% endfor %}'
            values = {'items': items(), 'mark': mark}
            if native:
                result = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                result = environment.from_string(source).render(values)
            return result, events

        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                expected = observe(False, autoescape)
                rendered = ('&lt;LoopContext 1/2&gt;;&lt;LoopContext 2/2&gt;;' if autoescape
                            else '<LoopContext 1/2>;<LoopContext 2/2>;')
                self.assertEqual(expected, (rendered, ['yield:1', 'body:1', 'yield:2', 'body:2']))
                self.assertEqual(observe(True, autoescape), expected)

    def test_custom_context_call_receives_live_loop_method(self) -> None:
        def observe(native: bool, asynchronous: bool, sandboxed: bool) -> tuple[str, list[tuple[Any, ...]]]:
            events: list[tuple[Any, ...]] = []
            retained: list[Any] = []

            class Context(jinja2.runtime.Context):
                def call(self, obj: Any, *args: Any, **kwargs: Any) -> Any:
                    owner: Any = getattr(obj, '__self__', None)
                    if not retained:
                        retained.append(owner)
                    events.append((obj.__name__, type(owner) is (AsyncLoopContext if asynchronous else LoopContext),
                                   owner is retained[0], owner.index0, args))
                    return super().call(obj, *args, **kwargs)

            environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in [1, 2] %}{{ loop.changed(x) }};{% endfor %}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {}
                ).render('case', source)
            else:
                output = environment.from_string(source).render()
            return output, events

        for asynchronous in (False, True):
            for sandboxed in (False, True):
                with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed):
                    expected = observe(False, asynchronous, sandboxed)
                    self.assertEqual(expected, ('True;True;', [
                        ('changed', True, True, 0, (1,)),
                        ('changed', True, True, 1, (2,)),
                    ]))
                    self.assertEqual(observe(True, asynchronous, sandboxed), expected)

    def test_passing_loop_does_not_consume_iterator_before_callback(self) -> None:
        def observe(native: bool) -> tuple[str, list[tuple[str, int]]]:
            events: list[tuple[str, int]] = []

            def items() -> Iterator[int]:
                for item in range(3):
                    events.append(('yield', item))
                    yield item

            def callback(loop: Any) -> str:
                events.append(('callback', getattr(loop, 'index0', -1)))
                return ''

            environment = SandboxedEnvironment()
            source = '{% for item in items %}{{ callback(loop) }}{{ item }}{% endfor %}'
            values = {'items': items(), 'callback': callback}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('012', [('yield', 0), ('callback', 0), ('yield', 1),
                                             ('callback', 1), ('yield', 2), ('callback', 2)]))
        self.assertEqual(observe(True), expected)

    def test_user_callback_receives_live_loop_argument(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[tuple[Any, ...]]]:
            retained: list[Any] = []
            events: list[tuple[Any, ...]] = []

            def inspect_loop(loop: Any) -> str:
                if not retained:
                    retained.append(loop)
                events.append((type(loop) is (AsyncLoopContext if asynchronous else LoopContext),
                               loop is retained[0], loop.index0))
                if len(events) == 1:
                    loop.index0 = 4
                return ''

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for item in [1, 2] %}{{ inspect_loop(loop) }}{{ loop.index }};{% endfor %}'
            values = {'inspect_loop': inspect_loop}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            events.append(('retained', retained[0].index0))
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('5;6;', [(True, True, 0), (True, True, 5), ('retained', 5)]))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_callback_can_set_arbitrary_precision_loop_index(self) -> None:
        def observe(native: bool, asynchronous: bool, index: int) -> tuple[str, list[int]]:
            observed: list[int] = []

            def adjust(loop: Any) -> str:
                observed.append(loop.index0)
                if len(observed) == 1:
                    loop.index0 = index
                return ''

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for item in [1, 2] %}{{ adjust(loop) }}{{ loop.index }};{% endfor %}'
            values = {'adjust': adjust}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, observed

        for asynchronous in (False, True):
            for index in (2**200, -(2**200)):
                with self.subTest(asynchronous=asynchronous, index=index):
                    expected = observe(False, asynchronous, index)
                    self.assertEqual(expected, (f'{index + 1};{index + 2};', [0, index + 1]))
                    self.assertEqual(observe(True, asynchronous, index), expected)

    def test_loop_index_add_can_reenter_and_mutate_same_loop(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Index:
                def __init__(self, loop: Any) -> None:
                    self.loop = loop

                def __add__(self, other: object) -> int:
                    events.append(f'add:{other}')
                    self.loop.index0 = 99
                    return 7

                def __str__(self) -> str:
                    return 'custom'

            def adjust(loop: Any) -> str:
                if not events:
                    loop.index0 = Index(loop)
                    events.append('assigned')
                return ''

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for item in [1, 2] %}{{ adjust(loop) }}{{ loop.index0 }};{% endfor %}'
            values = {'adjust': adjust}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('custom;7;', ['assigned', 'add:1']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_mutated_index_controls_generator_length_and_cycle(self) -> None:
        def observe(native: bool, asynchronous: bool) -> str:
            observed: list[int] = []

            def adjust(loop: Any) -> str:
                observed.append(loop.index0)
                if len(observed) == 1:
                    loop.index0 = -4
                return ''

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = ('{% for item in items %}{{ adjust(loop) }}'
                      '{{ loop.index0 }}:{{ loop.length }}:{{ loop.cycle("A", "B", "C") }};'
                      '{% endfor %}')
            values = {'adjust': adjust, 'items': iter([1, 2, 3])}
            if native:
                return docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            return environment.from_string(source).render(values)

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, '-4:-1:C;-3:-1:A;-2:-1:B;')
                self.assertEqual(observe(True, asynchronous), expected)

    def test_cycle_modulo_result_can_be_a_slice(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[int]]:
            events: list[int] = []

            class Index:
                def __mod__(self, value: int) -> slice:
                    events.append(value)
                    return slice(1, 3)

            def adjust(loop: Any) -> str:
                loop.index0 = Index()
                return ''

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for item in [1] %}{{ adjust(loop) }}{{ loop.cycle("A", "B", "C") }}{% endfor %}'
            if native:
                result = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'adjust': adjust}
                ).render('case', source)
            else:
                result = environment.from_string(source).render(adjust=adjust)
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ("('B', 'C')", [3]))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_first_preserves_custom_equality_result_and_reentry(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[object]]:
            events: list[object] = []

            class Index:
                def __init__(self, loop: Any) -> None:
                    self.loop = loop

                def __eq__(self, other: object) -> str:  # type: ignore[override]
                    events.append(other)
                    self.loop.index0 = 9
                    return 'custom-result'

            def adjust(loop: Any) -> str:
                loop.index0 = Index(loop)
                return ''

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for item in [1] %}{{ adjust(loop) }}{{ loop.first }}:{{ loop.index0 }}{% endfor %}'
            if native:
                result = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'adjust': adjust}
                ).render('case', source)
            else:
                result = environment.from_string(source).render(adjust=adjust)
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('custom-result:9', [0]))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_reverse_index_reads_index_after_length_callback(self) -> None:
        def observe(native: bool, asynchronous: bool, attribute: str) -> tuple[str, list[str]]:
            events: list[str] = []
            retained: list[Any] = []

            class Items:
                def __iter__(self) -> Iterator[int]:
                    return iter([1])

                def __len__(self) -> int:
                    events.append('length')
                    retained[0].index0 = 3
                    return 5

            def adjust(loop: Any) -> str:
                retained.append(loop)
                loop.index0 = 8
                return ''

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = ('{% for item in items %}{{ adjust(loop) }}{{ loop.' + attribute
                      + ' }}:{{ loop.index0 }}{% endfor %}')
            values = {'adjust': adjust, 'items': Items()}
            if native:
                result = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                result = environment.from_string(source).render(values)
            return result, events

        for asynchronous in (False, True):
            for attribute, result in (('revindex', '2:3'), ('revindex0', '1:3')):
                with self.subTest(asynchronous=asynchronous, attribute=attribute):
                    expected = observe(False, asynchronous, attribute)
                    self.assertEqual(expected, (result, ['length']))
                    self.assertEqual(observe(True, asynchronous, attribute), expected)

    def test_reverse_index_length_callback_can_advance_loop(self) -> None:
        def observe(native: bool, attribute: str) -> tuple[str, list[int]]:
            retained: list[Any] = []
            consumed: list[int] = []

            class Items:
                def __iter__(self) -> Iterator[int]:
                    return iter([1, 2, 3])

                def __len__(self) -> int:
                    item, _ = next(retained[0])
                    consumed.append(item)
                    return 5

            def retain(loop: Any) -> str:
                if not retained:
                    retained.append(loop)
                return ''

            environment = SandboxedEnvironment()
            source = ('{% for item in items %}{{ retain(loop) }}{{ item }}:{{ loop.' + attribute
                      + ' }}:{{ loop.index0 }};{% endfor %}')
            values = {'retain': retain, 'items': Items()}
            if native:
                result = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                result = environment.from_string(source).render(values)
            return result, consumed

        for attribute, output in (('revindex', '1:4:1;3:3:2;'), ('revindex0', '1:3:1;3:2:2;')):
            with self.subTest(attribute=attribute):
                expected = observe(False, attribute)
                self.assertEqual(expected, (output, [2]))
                self.assertEqual(observe(True, attribute), expected)

    def test_materialized_iterator_finalizer_can_advance_loop(self) -> None:
        def observe(native: bool) -> tuple[str, list[object]]:
            retained: list[Any] = []
            events: list[object] = []

            class ItemsIterator:
                def __init__(self) -> None:
                    self.position = 0

                def __iter__(self) -> 'ItemsIterator':
                    return self

                def __next__(self) -> int:
                    self.position += 1
                    if self.position > 2:
                        raise StopIteration
                    return self.position

                def __del__(self) -> None:
                    try:
                        item, _ = next(retained[0])
                        events.append(item)
                    except Exception as error:
                        events.append(type(error).__name__)

            class Items:
                def __iter__(self) -> ItemsIterator:
                    return ItemsIterator()

            def retain(loop: Any) -> str:
                if not retained:
                    retained.append(loop)
                return ''

            environment = jinja2.Environment()
            source = ('{% for item in items %}{{ retain(loop) }}'
                      '{{ item }}:{{ loop.length }}:{{ loop.index0 }};{% endfor %}')
            values = {'items': Items(), 'retain': retain}
            if native:
                result = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), values
                ).render('case', source)
            else:
                result = environment.from_string(source).render(values)
            return result, events

        expected = observe(False)
        self.assertEqual(expected, ('1:3:1;', [2]))
        self.assertEqual(observe(True), expected)

    def test_policy_mutation_changes_subsequent_loop_state(self) -> None:
        for asynchronous in (False, True):
            for replacement in (9, -2):
                with self.subTest(asynchronous=asynchronous, replacement=replacement):
                    def observe(native: bool, async_mode: bool = asynchronous,
                                new_index: int = replacement) -> tuple[str, list[int]]:
                        indexes: list[int] = []

                        class Environment(SandboxedEnvironment):
                            def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                                if attr == 'index':
                                    indexes.append(obj.index0)
                                    if len(indexes) == 1:
                                        obj.index0 = new_index
                                return True

                        environment = Environment(enable_async=async_mode)
                        source = '{% for item in [1, 2] %}{{ loop.index }};{% endfor %}'
                        if native:
                            result = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', source)
                        else:
                            result = environment.from_string(source).render()
                        return result, indexes

                    expected = observe(False)
                    self.assertEqual(expected, (f'1;{replacement + 2};', [0, replacement + 1]))
                    self.assertEqual(observe(True), expected)

    def test_policy_can_advance_original_iterator(self) -> None:
        def observe(native: bool) -> tuple[str, list[tuple[int, int]]]:
            consumed: list[tuple[int, int]] = []

            class Environment(SandboxedEnvironment):
                def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                    if attr == 'index' and not consumed:
                        item, loop = next(obj)
                        consumed.append((item, loop.index))
                    return True

            environment = Environment()
            source = '{% for item in [1, 2, 3] %}{{ item }}:{{ loop.index }};{% endfor %}'
            if native:
                result = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {}
                ).render('case', source)
            else:
                result = environment.from_string(source).render()
            return result, consumed

        expected = observe(False)
        self.assertEqual(expected, ('1:1;3:3;', [(2, 2)]))
        self.assertEqual(observe(True), expected)

    def observe(self, native: bool, asynchronous: bool) -> tuple[str, list[tuple[Any, ...]]]:
        events: list[tuple[Any, ...]] = []
        retained: list[Any] = []

        class Environment(SandboxedEnvironment):
            def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                retained.append(obj)
                expected_type = AsyncLoopContext if asynchronous else LoopContext
                events.append(('attribute', attr, type(obj) is expected_type,
                               obj is retained[0], obj.index,
                               getattr(value, '__self__', None) is obj if attr == 'changed' else value))
                return True

            def is_safe_callable(self, obj: Any) -> bool:
                events.append(('callable', getattr(obj, '__name__', ''),
                               getattr(obj, '__self__', None) is retained[0]))
                return True

        environment = Environment(enable_async=asynchronous)
        source = '{% for item in [1, 1] %}{{ loop.index }}:{{ loop.changed(item) }};{% endfor %}'
        if native:
            output = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), {}
            ).render('case', source)
        else:
            output = environment.from_string(source).render()
        events.append(('retained', len(retained), all(obj is retained[0] for obj in retained), retained[0].index))
        return output, events

    def test_sandbox_callbacks_receive_live_original_loop_and_bound_method(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = self.observe(False, asynchronous)
                self.assertEqual(expected[0], '1:True;2:False;')
                self.assertEqual(self.observe(True, asynchronous), expected)


if __name__ == '__main__':
    unittest.main()
