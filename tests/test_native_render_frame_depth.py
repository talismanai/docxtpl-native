"""Private VM frame-depth probe across async suspension and reentry."""

import unittest
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native import bridge


class RenderFrameDepthTests(unittest.TestCase):
    def test_filtered_loop_depth_survives_await_and_restores(self) -> None:
        for source, expected in (
            ('{% if 1 is selected %}yes{% endif %}', 0),
            ('{% for x in [1] if x is selected %}{{loop.index}}{% endfor %}', 1),
            ('{% for x in [1] if x is selected %}{{x}}{% endfor %}', 1),
            ('{% for x in [1] if x is selected %}{{loop.index}}{% else %}empty{% endfor %}', 1),
        ):
            for cancel in (False, True):
                with self.subTest(source=source, cancel=cancel):
                    events: list[tuple[str, int | None]] = []
                    retained: list[Any] = []

                    class Waiter:
                        def __init__(self, trace: list[tuple[str, int | None]] = events) -> None:
                            self.trace = trace

                        def __await__(self) -> Generator[str, None, bool]:
                            try:
                                self.trace.append(('before', docxtpl_native._extension()._debug_filtered_loop_depth()))
                                yield 'paused'
                                self.trace.append(('after', docxtpl_native._extension()._debug_filtered_loop_depth()))
                                return True
                            finally:
                                self.trace.append(('finally', docxtpl_native._extension()._debug_filtered_loop_depth()))

                    async def selected(value: int) -> bool:
                        return await Waiter()

                    def retain(render: Any, captured: list[Any] = retained) -> Any:
                        captured.append(render)

                        async def held() -> str:
                            return 'held'

                        return held()

                    environment = jinja2.Environment(enable_async=True)
                    tests: Any = environment.tests
                    tests['selected'] = selected
                    config = bridge.BridgeConfig(environment, False)
                    config.package_values_live = True
                    session = docxtpl_native._extension().PythonRenderSession(config, {})
                    with patch.object(bridge, '_await_native_render', retain):
                        self.assertEqual(session.render('case', source), 'held')
                    coroutine = retained.pop()
                    self.assertEqual(coroutine.send(None), 'paused')
                    self.assertIsNone(docxtpl_native._extension()._debug_filtered_loop_depth())
                    if cancel:
                        coroutine.close()
                        self.assertEqual(events, [('before', expected), ('finally', expected)])
                    else:
                        with self.assertRaises(StopIteration):
                            coroutine.send(None)
                        self.assertEqual(events, [('before', expected), ('after', expected), ('finally', expected)])
                    self.assertIsNone(docxtpl_native._extension()._debug_filtered_loop_depth())

    def test_interleaved_filtered_and_root_awaits_keep_separate_depths(self) -> None:
        events: list[tuple[str, int | None]] = []
        environment = jinja2.Environment(enable_async=True)

        class Waiter:
            def __init__(self, label: str) -> None:
                self.label = label

            def __await__(self) -> Generator[str, None, bool]:
                events.append((self.label + ':before', docxtpl_native._extension()._debug_filtered_loop_depth()))
                yield self.label
                events.append((self.label + ':after', docxtpl_native._extension()._debug_filtered_loop_depth()))
                return True

        async def selected(value: int) -> bool:
            return await Waiter('filtered')

        def wait() -> Waiter:
            return Waiter('root')

        tests: Any = environment.tests
        tests['selected'] = selected

        def prepare(source: str) -> Any:
            retained: list[Any] = []

            def retain(render: Any) -> Any:
                retained.append(render)

                async def held() -> str:
                    return 'held'

                return held()

            config = bridge.BridgeConfig(environment, False)
            config.package_values_live = True
            session = docxtpl_native._extension().PythonRenderSession(config, {'wait': wait})
            with patch.object(bridge, '_await_native_render', retain):
                self.assertEqual(session.render('case', source), 'held')
            return retained.pop()

        filtered = prepare('{% for x in [1] if x is selected %}{{loop.index}}{% endfor %}')
        root = prepare('{{wait()}}')
        self.assertEqual(filtered.send(None), 'filtered')
        self.assertEqual(root.send(None), 'root')
        self.assertIsNone(docxtpl_native._extension()._debug_filtered_loop_depth())
        for coroutine in (filtered, root):
            with self.assertRaises(StopIteration):
                coroutine.send(None)
        self.assertEqual(events, [('filtered:before', 1), ('root:before', 0),
                                  ('filtered:after', 1), ('root:after', 0)])
        self.assertIsNone(docxtpl_native._extension()._debug_filtered_loop_depth())

    def test_filtered_loop_depth_survives_async_input_advance(self) -> None:
        sources = (
            '{% for x in items if x is selected %}{{x}}{% endfor %}',
            '{% for x in items if x is selected %}{{loop.index}}{% endfor %}',
            '{% for x in items if x is selected %}{{loop.index}}{% else %}empty{% endfor %}',
        )
        for source in sources:
            with self.subTest(source=source):
                events: list[tuple[str, int | None]] = []
                retained: list[Any] = []

                class Waiter:
                    def __init__(self, trace: list[tuple[str, int | None]] = events) -> None:
                        self.trace = trace

                    def __await__(self) -> Generator[str, None, None]:
                        self.trace.append(('before', docxtpl_native._extension()._debug_filtered_loop_depth()))
                        yield 'paused'
                        self.trace.append(('after', docxtpl_native._extension()._debug_filtered_loop_depth()))

                class Items:
                    def __init__(self) -> None:
                        self.used = False

                    def __aiter__(self) -> 'Items':
                        return self

                    async def __anext__(self) -> int:
                        if self.used:
                            raise StopAsyncIteration
                        self.used = True
                        await Waiter()
                        return 1

                def selected(value: int) -> bool:
                    return True

                def retain(render: Any, captured: list[Any] = retained) -> Any:
                    captured.append(render)

                    async def held() -> str:
                        return 'held'

                    return held()

                environment = jinja2.Environment(enable_async=True)
                environment.tests['selected'] = selected
                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {'items': Items()})
                with patch.object(bridge, '_await_native_render', retain):
                    self.assertEqual(session.render('case', source), 'held')
                coroutine = retained.pop()
                self.assertEqual(coroutine.send(None), 'paused')
                self.assertIsNone(docxtpl_native._extension()._debug_filtered_loop_depth())
                with self.assertRaises(StopIteration):
                    coroutine.send(None)
                self.assertEqual(events, [('before', 1), ('after', 1)])
                self.assertIsNone(docxtpl_native._extension()._debug_filtered_loop_depth())

    def test_filtered_loop_depth_restores_on_async_input_close(self) -> None:
        events: list[tuple[str, int | None]] = []
        retained: list[Any] = []

        class Waiter:
            def __await__(self) -> Generator[str, None, None]:
                try:
                    events.append(('before', docxtpl_native._extension()._debug_filtered_loop_depth()))
                    yield 'paused'
                finally:
                    events.append(('finally', docxtpl_native._extension()._debug_filtered_loop_depth()))

        class Items:
            def __aiter__(self) -> 'Items':
                return self

            async def __anext__(self) -> int:
                await Waiter()
                return 1

        def selected(value: int) -> bool:
            return True

        def retain(render: Any) -> Any:
            retained.append(render)

            async def held() -> str:
                return 'held'

            return held()

        environment = jinja2.Environment(enable_async=True)
        environment.tests['selected'] = selected
        config = bridge.BridgeConfig(environment, False)
        config.package_values_live = True
        session = docxtpl_native._extension().PythonRenderSession(config, {'items': Items()})
        with patch.object(bridge, '_await_native_render', retain):
            self.assertEqual(session.render('case', '{% for x in items if x is selected %}{{x}}{% endfor %}'), 'held')
        coroutine = retained.pop()
        self.assertEqual(coroutine.send(None), 'paused')
        coroutine.close()
        self.assertEqual(events, [('before', 1), ('finally', 1)])
        self.assertIsNone(docxtpl_native._extension()._debug_filtered_loop_depth())

    def test_interleaved_filtered_input_and_root_awaits_keep_separate_depths(self) -> None:
        events: list[tuple[str, int | None]] = []
        environment = jinja2.Environment(enable_async=True)

        class Waiter:
            def __init__(self, label: str) -> None:
                self.label = label

            def __await__(self) -> Generator[str, None, bool]:
                events.append((self.label + ':before', docxtpl_native._extension()._debug_filtered_loop_depth()))
                yield self.label
                events.append((self.label + ':after', docxtpl_native._extension()._debug_filtered_loop_depth()))
                return True

        class Items:
            def __init__(self) -> None:
                self.used = False

            def __aiter__(self) -> 'Items':
                return self

            async def __anext__(self) -> int:
                if self.used:
                    raise StopAsyncIteration
                self.used = True
                await Waiter('input')
                return 1

        def selected(value: int) -> bool:
            return True

        def wait() -> Waiter:
            return Waiter('root')

        environment.tests['selected'] = selected

        def prepare(source: str) -> Any:
            retained: list[Any] = []

            def retain(render: Any) -> Any:
                retained.append(render)

                async def held() -> str:
                    return 'held'

                return held()

            config = bridge.BridgeConfig(environment, False)
            config.package_values_live = True
            session = docxtpl_native._extension().PythonRenderSession(config, {'items': Items(), 'wait': wait})
            with patch.object(bridge, '_await_native_render', retain):
                self.assertEqual(session.render('case', source), 'held')
            return retained.pop()

        filtered = prepare('{% for x in items if x is selected %}{{x}}{% endfor %}')
        root = prepare('{{wait()}}')
        self.assertEqual(filtered.send(None), 'input')
        self.assertEqual(root.send(None), 'root')
        self.assertIsNone(docxtpl_native._extension()._debug_filtered_loop_depth())
        for coroutine in (filtered, root):
            with self.assertRaises(StopIteration):
                coroutine.send(None)
        self.assertEqual(events, [('input:before', 1), ('root:before', 0),
                                  ('input:after', 1), ('root:after', 0)])
        self.assertIsNone(docxtpl_native._extension()._debug_filtered_loop_depth())

    def make_coroutine(self, source: str, events: list[tuple[str, int | None]], label: str,
                       snapshots: list[list[tuple[int, str, str]]] | None = None) -> Any:
        retained: list[Any] = []
        loader = jinja2.DictLoader({
            'inner': '{{wait()}}',
            'outer': "{% include 'inner' %}",
            'base': '{% block b %}{{wait()}}{% endblock %}',
        })
        environment = jinja2.Environment(enable_async=True, loader=loader)

        class Waiter:
            def __await__(self) -> Generator[str, None, str]:
                def capture() -> None:
                    if snapshots is not None:
                        current = docxtpl_native._extension()._debug_render_frames()
                        if current is None:
                            raise AssertionError('missing active frame snapshot')
                        snapshots.append(current)

                try:
                    capture()
                    events.append((label + ':before', docxtpl_native._extension()._debug_render_frame_depth()))
                    yield 'paused:' + label
                    capture()
                    events.append((label + ':after', docxtpl_native._extension()._debug_render_frame_depth()))
                    return label
                finally:
                    capture()
                    events.append((label + ':finally', docxtpl_native._extension()._debug_render_frame_depth()))

        def retain(render: Any) -> Any:
            retained.append(render)

            async def held() -> str:
                return 'held'

            return held()

        config = bridge.BridgeConfig(environment, False)
        config.package_values_live = True
        session = docxtpl_native._extension().PythonRenderSession(config, {'wait': Waiter})
        with patch.object(bridge, '_await_native_render', retain):
            self.assertEqual(session.render('source', source), 'held')
        return retained[0]

    def test_depth_for_roots_includes_blocks_and_macros(self) -> None:
        cases = (
            ('{{wait()}}', 1),
            ("{% include 'inner' %}", 2),
            ("{% include 'outer' %}", 3),
            ("{% extends 'base' %}{% block b %}{{wait()}}{% endblock %}", 3),
            ('{% macro m() %}{{wait()}}{% endmacro %}{{m()}}', 1),
            ("{% macro m() %}{% include 'inner' %}{% endmacro %}{{m()}}", 2),
        )
        for source, depth in cases:
            with self.subTest(source=source):
                events: list[tuple[str, int | None]] = []
                coroutine = self.make_coroutine(source, events, 'case')
                self.assertEqual(coroutine.send(None), 'paused:case')
                self.assertIsNone(docxtpl_native._extension()._debug_render_frame_depth())
                with self.assertRaises(StopIteration) as done:
                    coroutine.send(None)
                self.assertEqual(done.exception.value, 'case')
                self.assertEqual(events, [('case:before', depth), ('case:after', depth), ('case:finally', depth)])
                self.assertIsNone(docxtpl_native._extension()._debug_render_frame_depth())

    def test_interleaved_fibers_keep_independent_depths(self) -> None:
        events: list[tuple[str, int | None]] = []
        first_frames: list[list[tuple[int, str, str]]] = []
        second_frames: list[list[tuple[int, str, str]]] = []
        first = self.make_coroutine('{{wait()}}', events, 'root', first_frames)
        second = self.make_coroutine("{% include 'inner' %}", events, 'include', second_frames)
        self.assertEqual(first.send(None), 'paused:root')
        self.assertIsNone(docxtpl_native._extension()._debug_render_frame_depth())
        self.assertEqual(second.send(None), 'paused:include')
        self.assertIsNone(docxtpl_native._extension()._debug_render_frame_depth())
        for coroutine in (first, second):
            with self.assertRaises(StopIteration):
                coroutine.send(None)
            self.assertIsNone(docxtpl_native._extension()._debug_render_frame_depth())
        self.assertEqual(events, [
            ('root:before', 1), ('include:before', 2), ('root:after', 1), ('root:finally', 1),
            ('include:after', 2), ('include:finally', 2),
        ])
        self.assertEqual(first_frames[0], first_frames[1])
        self.assertEqual(first_frames[1], first_frames[2])
        self.assertEqual(second_frames[0], second_frames[1])
        self.assertEqual(second_frames[1], second_frames[2])
        self.assertNotEqual(first_frames[0][0][0], second_frames[0][0][0])
        self.assertEqual([frame[1] for frame in first_frames[0]], ['root'])
        self.assertEqual([frame[1] for frame in second_frames[0]], ['root', 'include'])
        self.assertIsNone(docxtpl_native._extension()._debug_render_frames())

    def test_repeated_includes_have_distinct_frame_tokens(self) -> None:
        events: list[tuple[str, int | None]] = []
        snapshots: list[list[tuple[int, str, str]]] = []
        coroutine = self.make_coroutine("{% include 'inner' %}{% include 'inner' %}", events, 'case', snapshots)
        self.assertEqual(coroutine.send(None), 'paused:case')
        self.assertEqual(coroutine.send(None), 'paused:case')
        with self.assertRaises(StopIteration):
            coroutine.send(None)
        self.assertEqual(len(snapshots), 6)
        self.assertTrue(all(snapshot[0] == snapshots[0][0] for snapshot in snapshots))
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[1], snapshots[2])
        self.assertEqual(snapshots[3], snapshots[4])
        self.assertEqual(snapshots[4], snapshots[5])
        self.assertEqual(snapshots[0][1][1:], ('include', 'inner'))
        self.assertEqual(snapshots[3][1][1:], ('include', 'inner'))
        self.assertNotEqual(snapshots[0][1][0], snapshots[3][1][0])

    def test_snapshot_identifies_inheritance_and_block_frames(self) -> None:
        events: list[tuple[str, int | None]] = []
        snapshots: list[list[tuple[int, str, str]]] = []
        source = "{% extends 'base' %}{% block b %}{{wait()}}{% endblock %}"
        coroutine = self.make_coroutine(source, events, 'case', snapshots)
        self.assertEqual(coroutine.send(None), 'paused:case')
        with self.assertRaises(StopIteration):
            coroutine.send(None)
        self.assertEqual([frame[1] for frame in snapshots[0]], ['root', 'inheritance', 'block'])
        self.assertEqual(snapshots[0][1][2], 'base')
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[1], snapshots[2])

    def test_close_restores_depth_after_finally(self) -> None:
        events: list[tuple[str, int | None]] = []
        coroutine = self.make_coroutine("{% include 'inner' %}", events, 'include')
        self.assertEqual(coroutine.send(None), 'paused:include')
        coroutine.close()
        self.assertEqual(events, [('include:before', 2), ('include:finally', 2)])
        self.assertIsNone(docxtpl_native._extension()._debug_render_frame_depth())


if __name__ == '__main__':
    unittest.main()
