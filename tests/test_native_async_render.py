"""Native task execution, including output and observable await lifecycles."""

import asyncio
import contextvars
import io
import unittest
import warnings
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any, cast
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
from paritylab.async_spec_probes import PROBES
from paritylab.compare import compare_docx
from paritylab.protocol_base import Result, generate, observed, template
from paritylab.protocols import PROBES as ALL_PROBES


class NativeAsyncRenderTests(unittest.TestCase):
    def test_repeated_include_sees_concurrent_cache_replacement(self) -> None:
        import threading

        from docxtpl_native.bridge import BridgeConfig

        def run(native: bool, asynchronous: bool) -> tuple[dict[str, str], list[bool]]:
            barrier = threading.Barrier(2, timeout=10)
            b_ready = threading.Event()
            a_done = threading.Event()
            outputs: dict[str, str] = {}

            class Loader(jinja2.BaseLoader):
                def get_source(self, environment: jinja2.Environment, template: str) -> tuple[str, str, Callable[[], bool]]:
                    label = threading.current_thread().name
                    barrier.wait()
                    if label == 'A' and not b_ready.wait(timeout=10):
                        raise TimeoutError('B did not reach the gate')
                    return label, template, lambda: True

            def gate() -> str:
                if threading.current_thread().name == 'B':
                    b_ready.set()
                    if not a_done.wait(timeout=10):
                        raise TimeoutError('A did not finish')
                return ''

            environment = jinja2.Environment(loader=Loader(), auto_reload=False, enable_async=asynchronous)
            context = {'gate': gate}
            source = "{% include 'part' %}{{ gate() }}{% include 'part' %}"
            session: Any = None
            if native:
                configuration = BridgeConfig(environment, False)
                configuration.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(configuration, context)

            def worker() -> None:
                label = threading.current_thread().name
                try:
                    outputs[label] = (cast(Any, session).render('source', source) if native
                                      else environment.from_string(source).render(context))
                except Exception as error:  # noqa: BLE001 - capture any callback failure
                    outputs[label] = type(error).__name__ + ':' + str(error)
                    barrier.abort()
                finally:
                    if label == 'A':
                        a_done.set()

            threads = [threading.Thread(target=worker, name=name, daemon=True) for name in ('A', 'B')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            return outputs, [thread.is_alive() for thread in threads]

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ({'A': 'AA', 'B': 'BA'}, [False, False]))
                self.assertEqual(run(True, asynchronous), expected)

    def test_delayed_nested_coroutine_errors_remain_distinct(self) -> None:
        from docxtpl_native import bridge

        def run(native: bool) -> list[str]:
            events: list[str] = []
            errors = {'outer': ValueError('outer'), 'inner': ValueError('inner')}
            handles: list[Any] = []

            def fail(name: str) -> None:
                raise errors[name]

            class Iterator:
                def __init__(self) -> None:
                    self.used = False

                def __iter__(self) -> Any:
                    return self

                def __next__(self) -> str:
                    if self.used:
                        raise StopIteration
                    self.used = True
                    return 'item'

                def __del__(self) -> None:
                    try:
                        handles[1].send(None)
                    except BaseException as error:  # noqa: BLE001 - compare exception identity
                        events.append('inner:' + str(error is errors['inner']))

            context = {'fail': fail, 'make': Iterator}
            sources = ("{% for x in make() %}{{ fail('outer') }}{% endfor %}", "{{ fail('inner') }}")
            environment = jinja2.Environment(enable_async=True)
            if native:
                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)

                def retain(render: Any) -> Any:
                    handles.append(render)

                    async def ignored() -> str:
                        return 'created'

                    return ignored()

                with patch.object(bridge, '_await_native_render', retain):
                    for source in sources:
                        session.render('source', source)
            else:
                handles.extend(environment.from_string(source).render_async(context) for source in sources)
            try:
                handles[0].send(None)
            except BaseException as error:  # noqa: BLE001 - compare exception identity
                events.append('outer:' + str(error is errors['outer']))
            return events

        expected = run(False)
        self.assertEqual(expected, ['inner:True', 'outer:True'])
        self.assertEqual(run(True), expected)

    def test_interleaved_delayed_coroutines_keep_errors_after_suspension(self) -> None:
        from collections.abc import Generator

        from docxtpl_native import bridge

        def run(native: bool) -> list[str]:
            events: list[str] = []
            errors = {'outer': ValueError('outer'), 'inner': ValueError('inner')}
            handles: list[Any] = []

            class Pause:
                def __await__(self) -> Generator[str, None, None]:
                    yield 'paused'

            async def outer() -> None:
                await Pause()
                raise errors['outer']

            def inner() -> None:
                raise errors['inner']

            context = {'outer': outer, 'inner': inner}
            sources = ('{{ outer() }}', '{{ inner() }}')
            environment = jinja2.Environment(enable_async=True)
            if native:
                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)

                def retain(render: Any) -> Any:
                    handles.append(render)

                    async def ignored() -> str:
                        return 'created'

                    return ignored()

                with patch.object(bridge, '_await_native_render', retain):
                    for source in sources:
                        session.render('source', source)
            else:
                handles.extend(environment.from_string(source).render_async(context) for source in sources)
            events.append(str(handles[0].send(None)))
            for handle, name in ((handles[1], 'inner'), (handles[0], 'outer')):
                try:
                    handle.send(None)
                except BaseException as error:  # noqa: BLE001 - compare exception identity
                    events.append(name + ':' + str(error is errors[name]))
            return events

        expected = run(False)
        self.assertEqual(expected, ['paused', 'inner:True', 'outer:True'])
        self.assertEqual(run(True), expected)

    def test_delayed_concurrent_coroutines_keep_exception_identity(self) -> None:
        import threading

        from docxtpl_native import bridge

        count = 400
        barrier = threading.Barrier(2, timeout=15)
        errors = {'A': ValueError('A'), 'B': ValueError('B')}
        results: dict[str, list[bool]] = {'A': [], 'B': []}
        handles: list[Any] = []

        def fail() -> None:
            barrier.wait()
            raise errors[threading.current_thread().name]

        def retain(render: Any) -> Any:
            handles.append(render)

            async def ignored() -> str:
                return 'created'

            return ignored()

        environment = jinja2.Environment(enable_async=True)
        config = bridge.BridgeConfig(environment, False)
        config.package_values_live = True
        session = docxtpl_native._extension().PythonRenderSession(config, {'fail': fail})
        with patch.object(bridge, '_await_native_render', retain):
            for _ in range(count * 2):
                self.assertEqual(session.render('source', '{{ fail() }}'), 'created')

        def worker(label: str, offset: int) -> None:
            for handle in handles[offset::2]:
                try:
                    handle.send(None)
                except BaseException as error:  # noqa: BLE001 - verify the exact exception object
                    results[label].append(error is errors[label])
                else:
                    results[label].append(False)

        threads = [threading.Thread(target=worker, name=label, args=(label, offset), daemon=True)
                   for label, offset in (('A', 0), ('B', 1))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual([thread.is_alive() for thread in threads], [False, False])
        self.assertEqual(results, {'A': [True] * count, 'B': [True] * count})

    def test_completed_native_coroutine_can_be_collected_from_another_thread(self) -> None:
        import gc
        import sys
        import threading

        from docxtpl_native import bridge

        retained: list[Any] = []
        unraisable: list[BaseException] = []
        original = bridge._await_native_render
        outputs: list[str] = []

        def retain(render: Any) -> Any:
            retained.append(render)
            return original(render)

        def report(args: Any) -> None:
            unraisable.append(args.exc_value)

        def worker() -> None:
            environment = jinja2.Environment(enable_async=True)
            config = bridge.BridgeConfig(environment, False)
            config.package_values_live = True
            session = docxtpl_native._extension().PythonRenderSession(config, {})
            outputs.append(session.render('source', 'hello'))

        with patch.object(bridge, '_await_native_render', retain), patch.object(sys, 'unraisablehook', report):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outputs, ['hello'])
            self.assertEqual(len(retained), 1)
            with self.assertRaisesRegex(RuntimeError, 'cannot reuse already awaited coroutine'):
                retained[0].send(None)
            self.assertIsNone(retained[0].close())
            retained.clear()
            gc.collect()
        self.assertEqual(unraisable, [])

    def test_failed_native_coroutine_can_be_collected_from_another_thread(self) -> None:
        import gc
        import sys
        import threading

        from docxtpl_native import bridge

        retained: list[Any] = []
        unraisable: list[BaseException] = []
        errors: list[str] = []
        original = bridge._await_native_render

        def retain(render: Any) -> Any:
            retained.append(render)
            return original(render)

        def fail() -> None:
            raise ValueError('failure from render')

        def worker() -> None:
            environment = jinja2.Environment(enable_async=True)
            config = bridge.BridgeConfig(environment, False)
            config.package_values_live = True
            session = docxtpl_native._extension().PythonRenderSession(config, {'fail': fail})
            try:
                session.render('source', '{{ fail() }}')
            except ValueError as error:
                errors.append(str(error))

        with patch.object(bridge, '_await_native_render', retain), patch.object(
            sys, 'unraisablehook', lambda args: unraisable.append(args.exc_value),
        ):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ['failure from render'])
            self.assertEqual(len(retained), 1)
            with self.assertRaisesRegex(RuntimeError, 'cannot reuse already awaited coroutine'):
                retained[0].send(None)
            retained.clear()
            gc.collect()
        self.assertEqual(unraisable, [])

    def test_closed_native_coroutine_can_be_collected_from_another_thread(self) -> None:
        import gc
        import sys
        import threading

        from docxtpl_native import bridge

        retained: list[Any] = []
        unraisable: list[BaseException] = []
        outputs: list[str] = []

        def retain(render: Any) -> Any:
            retained.append(render)
            render.close()

            async def closed() -> str:
                return 'closed'

            return closed()

        def worker() -> None:
            environment = jinja2.Environment(enable_async=True)
            config = bridge.BridgeConfig(environment, False)
            config.package_values_live = True
            session = docxtpl_native._extension().PythonRenderSession(config, {})
            outputs.append(session.render('source', 'not rendered'))

        with patch.object(bridge, '_await_native_render', retain), patch.object(
            sys, 'unraisablehook', lambda args: unraisable.append(args.exc_value),
        ):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outputs, ['closed'])
            self.assertEqual(len(retained), 1)
            retained.clear()
            gc.collect()
        self.assertEqual(unraisable, [])

    def test_suspended_native_coroutine_can_be_collected_from_another_thread(self) -> None:
        import gc
        import sys
        import threading
        from collections.abc import Generator

        from docxtpl_native import bridge

        retained: list[Any] = []
        unraisable: list[BaseException] = []
        outputs: list[str] = []

        class Waiter:
            def __await__(self) -> Generator[str, None, None]:
                yield 'paused'

        def retain(render: Any) -> Any:
            retained.append(render)

            async def suspend() -> str:
                self.assertEqual(render.send(None), 'paused')
                return 'held'

            return suspend()

        def worker() -> None:
            environment = jinja2.Environment(enable_async=True)
            config = bridge.BridgeConfig(environment, False)
            config.package_values_live = True
            session = docxtpl_native._extension().PythonRenderSession(config, {'wait': Waiter})
            outputs.append(session.render('source', '{{ wait() }}'))

        with patch.object(bridge, '_await_native_render', retain), patch.object(
            sys, 'unraisablehook', lambda args: unraisable.append(args.exc_value),
        ):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outputs, ['held'])
            self.assertEqual(len(retained), 1)
            retained.clear()
            gc.collect()
        self.assertEqual(unraisable, [])

    def test_suspended_coroutine_can_resume_on_another_thread(self) -> None:
        import gc
        import sys
        import threading
        from collections.abc import Generator

        from docxtpl_native import bridge

        def observe(native: bool) -> tuple[list[str], list[str], list[str]]:
            events: list[str] = []
            outcomes: list[str] = []
            unraisable: list[str] = []
            retained: list[Any] = []

            class Waiter:
                def __await__(self) -> Generator[str, None, str]:
                    try:
                        events.append('start')
                        yield 'paused'
                        events.append('resumed')
                        return 'finished'
                    finally:
                        events.append('finally')

            def retain(render: Any) -> Any:
                retained.append(render)

                async def suspend() -> str:
                    outcomes.append(render.send(None))
                    return 'held'

                return suspend()

            def worker() -> None:
                environment = jinja2.Environment(enable_async=True)
                if native:
                    config = bridge.BridgeConfig(environment, False)
                    config.package_values_live = True
                    session = docxtpl_native._extension().PythonRenderSession(config, {'wait': Waiter})
                    outcomes.append(session.render('source', '{{ wait() }}'))
                else:
                    coroutine = environment.from_string('{{ wait() }}').render_async(wait=Waiter)
                    retained.append(coroutine)
                    outcomes.append(coroutine.send(None))
                    outcomes.append('held')

            with patch.object(bridge, '_await_native_render', retain), patch.object(
                sys, 'unraisablehook', lambda args: unraisable.append(type(args.exc_value).__name__),
            ):
                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive())
                try:
                    retained[0].send(None)
                except StopIteration as error:
                    outcomes.append('result:' + str(error.value))
                except BaseException as error:  # noqa: BLE001 - compare the entire boundary
                    outcomes.append('error:' + type(error).__name__)
                retained.clear()
                gc.collect()
            return outcomes, events, unraisable

        expected = observe(False)
        self.assertEqual(expected, (['paused', 'held', 'result:finished'], ['start', 'resumed', 'finally'], []))
        self.assertEqual(observe(True), expected)

    def test_cross_thread_resume_rejection_keeps_fiber_on_owner_thread(self) -> None:
        # A suspension under a host call (async join awaits inside its own
        # awaitable) keeps the fiber on its thread; the owner can finish it.
        import threading
        from collections.abc import AsyncIterator, Generator

        from docxtpl_native import bridge

        retained: list[Any] = []
        events: list[str] = []
        ready = threading.Event()
        proceed = threading.Event()

        class Waiter:
            def __await__(self) -> Generator[str, None, str]:
                try:
                    yield 'paused'
                    return 'finished'
                finally:
                    events.append('finally')

        async def items() -> AsyncIterator[str]:
            yield await Waiter()

        def retain(render: Any) -> Any:
            retained.append(render)

            async def suspend() -> str:
                events.append(render.send(None))
                return 'held'

            return suspend()

        def worker() -> None:
            environment = jinja2.Environment(enable_async=True)
            config = bridge.BridgeConfig(environment, False)
            config.package_values_live = True
            session = docxtpl_native._extension().PythonRenderSession(config, {'items': items})
            events.append(session.render('source', '{{ items()|join(",") }}'))
            ready.set()
            if not proceed.wait(timeout=30):
                events.append('timeout')
                return
            try:
                retained.pop().send(None)
            except StopIteration as error:
                events.append('result:' + str(error.value))

        with patch.object(bridge, '_await_native_render', retain):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(ready.wait(timeout=30))
                for operation, arguments in (
                    ('send', (None,)), ('throw', (ValueError('injected'),)), ('close', ()),
                ):
                    with self.subTest(operation=operation), self.assertRaisesRegex(
                        docxtpl_native.UnsupportedFeature, 'cannot yet resume on another thread',
                    ):
                        getattr(retained[0], operation)(*arguments)
            finally:
                proceed.set()
                thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
        self.assertEqual(events, ['paused', 'held', 'finally', 'result:finished'])

    def test_clean_suspension_resumes_on_another_thread_once(self) -> None:
        import threading
        from collections.abc import Generator

        from docxtpl_native import bridge

        retained: list[Any] = []
        events: list[str] = []
        ready = threading.Event()
        proceed = threading.Event()

        class Waiter:
            def __await__(self) -> Generator[str, None, str]:
                try:
                    yield 'paused'
                    return 'finished'
                finally:
                    events.append('finally:' + threading.current_thread().name)

        def retain(render: Any) -> Any:
            retained.append(render)

            async def suspend() -> str:
                events.append(render.send(None))
                return 'held'

            return suspend()

        def worker() -> None:
            environment = jinja2.Environment(enable_async=True)
            config = bridge.BridgeConfig(environment, False)
            config.package_values_live = True
            session = docxtpl_native._extension().PythonRenderSession(config, {'wait': Waiter})
            events.append(session.render('source', '{{ wait() }}'))
            ready.set()
            if not proceed.wait(timeout=30):
                events.append('timeout')
                return
            try:
                retained[0].send(None)
            except RuntimeError as error:
                events.append('reuse:' + str(error))

        with patch.object(bridge, '_await_native_render', retain):
            thread = threading.Thread(target=worker, name='origin')
            thread.start()
            try:
                self.assertTrue(ready.wait(timeout=30))
                with self.assertRaises(StopIteration) as finished:
                    retained[0].send(None)
                events.append('result:' + str(finished.exception.value))
            finally:
                proceed.set()
                thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
        self.assertEqual(events, ['paused', 'held', 'finally:' + threading.current_thread().name,
                                  'result:finished', 'reuse:cannot reuse already awaited coroutine'])

    def test_unstarted_native_coroutine_can_be_collected_from_another_thread(self) -> None:
        import gc
        import sys
        import threading

        from docxtpl_native import bridge

        retained: list[Any] = []
        unraisable: list[BaseException] = []
        outputs: list[str] = []

        def retain(render: Any) -> Any:
            retained.append(render)

            async def skipped() -> str:
                return 'not started'

            return skipped()

        def worker() -> None:
            environment = jinja2.Environment(enable_async=True)
            config = bridge.BridgeConfig(environment, False)
            config.package_values_live = True
            session = docxtpl_native._extension().PythonRenderSession(config, {})
            outputs.append(session.render('source', 'not rendered'))

        with patch.object(bridge, '_await_native_render', retain), patch.object(
            sys, 'unraisablehook', lambda args: unraisable.append(args.exc_value),
        ):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outputs, ['not started'])
            self.assertEqual(len(retained), 1)
            retained.clear()
            gc.collect()
        self.assertEqual(unraisable, [])

    def test_document_include_can_use_its_part_name(self) -> None:
        source = template("A{% include 'word/document.xml' %}B")
        for asynchronous in (False, True):
            def execute(engine: Any, *, async_mode: bool = asynchronous) -> Result:
                environment = jinja2.Environment(
                    loader=jinja2.DictLoader({'word/document.xml': 'P'}),
                    auto_reload=False, enable_async=async_mode,
                )
                return observed(generate(engine, '', {}, environment=environment, source=source))

            with self.subTest(asynchronous=asynchronous):
                self.compare(execute)

    def test_anonymous_source_does_not_replace_same_named_loader_template(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[list[str], list[str]]:
            from docxtpl_native.bridge import BridgeConfig

            events: list[str] = []

            class Loader(jinja2.DictLoader):
                def get_source(self, environment: Any, template: str) -> Any:
                    events.append('load:' + template)
                    return super().get_source(environment, template)

            name = 'word/document.xml'
            environment = jinja2.Environment(
                loader=Loader({name: 'P'}), auto_reload=False, enable_async=asynchronous,
            )
            source = "A{% include 'word/document.xml' %}B"
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {})
                outputs = [session.render(name, source), session.render(name, source)]
            else:
                outputs = [environment.from_string(source).render() for _ in range(2)]
            return outputs, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, (['APB', 'APB'], ['load:word/document.xml']))
                self.assertEqual(run(True, asynchronous), expected)

    def test_same_named_loader_keeps_source_finalizer_plan(self) -> None:
        def run(native: bool) -> str:
            from docxtpl_native.bridge import BridgeConfig

            class Loader(jinja2.DictLoader):
                def get_source(self, environment: Any, template: str) -> Any:
                    environment.finalize = lambda value: 'I' + str(value)
                    return super().get_source(environment, template)

            name = 'word/document.xml'
            environment = jinja2.Environment(loader=Loader({name: '{{ value }}'}), auto_reload=False)
            source = "{% include 'word/document.xml' %}{{ value }}"
            context = {'value': 'x'}
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)
                return session.render(name, source)
            return environment.from_string(source).render(context)

        self.assertEqual(run(False), 'Ixx')
        self.assertEqual(run(True), 'Ixx')

    def test_same_named_loader_keeps_finalizer_kind_in_blocks(self) -> None:
        def run(native: bool, asynchronous: bool, mode: str) -> str:
            from docxtpl_native.bridge import BridgeConfig

            @jinja2.pass_context
            def outer_finalizer(context: Any, value: object) -> str:
                return 'C' + str(value)

            @jinja2.pass_environment
            def inner_finalizer(environment: Any, value: object) -> str:
                return ('E' if isinstance(environment, jinja2.Environment) else 'K') + str(value)

            class Loader(jinja2.DictLoader):
                def get_source(self, environment: Any, template: str) -> Any:
                    environment.finalize = inner_finalizer
                    return super().get_source(environment, template)

            name = 'word/document.xml'
            environment = jinja2.Environment(
                loader=Loader({name: '{{ value }}'}), auto_reload=False,
                finalize=outer_finalizer, enable_async=asynchronous,
            )
            source = "{{ value }}{% include 'word/document.xml' %}{{ value }}"
            if mode == 'block':
                source = '{% block body %}' + source + '{% endblock %}'
            elif mode == 'macro':
                source = ("{% macro item() %}{{ value }}{% endmacro %}"
                          "{{ item() }}{% include 'word/document.xml' %}{{ item() }}")
            context = {'value': 'x'}
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)
                return session.render(name, source)
            return environment.from_string(source).render(context)

        for asynchronous in (False, True):
            for mode in ('root', 'block', 'macro'):
                with self.subTest(asynchronous=asynchronous, mode=mode):
                    expected = 'CCxExKKx' if mode == 'macro' else 'CxExKx'
                    self.assertEqual(run(False, asynchronous, mode), expected)
                    self.assertEqual(run(True, asynchronous, mode), expected)

    def test_document_same_named_loader_keeps_finalizer_plan(self) -> None:
        source = template("{% include 'word/document.xml' %}{{ value }}")
        for asynchronous in (False, True):
            def execute(engine: Any, *, async_mode: bool = asynchronous) -> Result:
                class Loader(jinja2.DictLoader):
                    def get_source(self, environment: Any, template: str) -> Any:
                        environment.finalize = lambda value: 'I' + str(value)
                        return super().get_source(environment, template)

                environment = jinja2.Environment(
                    loader=Loader({'word/document.xml': '{{ value }}'}),
                    auto_reload=False, enable_async=async_mode,
                )
                return observed(generate(engine, '', {'value': 'x'}, environment=environment, source=source))

            with self.subTest(asynchronous=asynchronous):
                self.compare(execute)

    def test_loader_can_reenter_the_same_session(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            from docxtpl_native.bridge import BridgeConfig

            events: list[str] = []
            session: Any = None
            environment: jinja2.Environment

            class Loader(jinja2.DictLoader):
                def get_source(self, current: Any, name: str) -> Any:
                    events.append('load:' + name)
                    if name == 'outer_part':
                        try:
                            if native:
                                result = session.render('inner', "{% include 'inner_part' %}")
                            else:
                                result = environment.from_string("{% include 'inner_part' %}").render()
                            events.append('nested:' + result)
                        except RuntimeError as error:
                            events.append('nested:error:' + str(error))
                    return super().get_source(current, name)

            environment = jinja2.Environment(
                loader=Loader({'outer_part': 'O', 'inner_part': 'I'}),
                auto_reload=False, enable_async=asynchronous,
            )
            if native:
                configuration = BridgeConfig(environment, False)
                configuration.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(configuration, {})
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', RuntimeWarning)
                if native:
                    output = session.render('outer', "{% include 'outer_part' %}")
                else:
                    output = environment.from_string("{% include 'outer_part' %}").render()
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                if asynchronous:
                    self.assertEqual(expected, ('O', ['load:outer_part',
                        'nested:error:asyncio.run() cannot be called from a running event loop']))
                else:
                    self.assertEqual(expected, ('O', ['load:outer_part', 'load:inner_part', 'nested:I']))
                self.assertEqual(run(True, asynchronous), expected)

    def test_session_retains_loader_cache_between_renders(self) -> None:
        def run(native: bool) -> tuple[list[str], list[str]]:
            from docxtpl_native.bridge import BridgeConfig

            events: list[str] = []

            class Loader(jinja2.DictLoader):
                def get_source(self, environment: Any, template: str) -> Any:
                    events.append('load:' + template)
                    return super().get_source(environment, template)

            environment = jinja2.Environment(loader=Loader({'part': 'P'}), auto_reload=False)
            source = "{% include 'part' %}"
            if native:
                configuration = BridgeConfig(environment, False)
                configuration.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(configuration, {})
                outputs = [session.render('first', source), session.render('second', source)]
            else:
                outputs = [environment.from_string(source).render(), environment.from_string(source).render()]
            return outputs, events

        expected = run(False)
        self.assertEqual(expected, (['P', 'P'], ['load:part']))
        self.assertEqual(run(True), expected)

    def test_session_reentry_catches_nested_error_and_continues(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            from docxtpl_native.bridge import BridgeConfig

            environment = jinja2.Environment()
            events: list[object] = []
            marker = ValueError('nested failure')
            session: Any = None

            def fail() -> str:
                events.append('inner:fail')
                raise marker

            def callback() -> str:
                events.append('outer:enter')
                try:
                    if native:
                        session.render('inner', '{{ fail() }}')
                    else:
                        environment.from_string('{{ fail() }}').render(fail=fail)
                except ValueError as error:
                    events.append(('caught', error is marker, str(error)))
                events.append('outer:exit')
                return 'OK'

            def later() -> str:
                events.append('later')
                return 'L'

            context = {'callback': callback, 'fail': fail, 'later': later}
            source = '{{ callback() }}{{ later() }}'
            if native:
                configuration = BridgeConfig(environment, False)
                configuration.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(configuration, context)
                return session.render('outer', source), events
            return environment.from_string(source).render(context), events

        expected = run(False)
        self.assertEqual(expected, ('OKL', ['outer:enter', 'inner:fail', ('caught', True, 'nested failure'),
                                          'outer:exit', 'later']))
        self.assertEqual(run(True), expected)

    def test_reentry_during_constant_folding_with_same_name(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            from docxtpl_native.bridge import BridgeConfig

            events: list[str] = []
            environment = jinja2.Environment(enable_async=asynchronous)
            session: Any

            def fold(value: str) -> str:
                events.append('fold:' + value)
                if value == 'outer':
                    if native:
                        nested = session.render('same', "{{ 'inner'|fold }}")
                    else:
                        nested = environment.from_string("{{ 'inner'|fold }}").render()
                    events.append('nested:' + nested)
                return value.upper()

            environment.filters['fold'] = fold
            source = "{{ 'outer'|fold }}:{{ 'after'|fold }}:{{ value }}"
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {'value': 'V'})
                output = session.render('same', source)
            else:
                output = environment.from_string(source).render(value='V')
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ('OUTER:AFTER:V',
                                            ['fold:outer', 'fold:inner', 'nested:INNER', 'fold:after']))
                self.assertEqual(run(True, asynchronous), expected)

    def test_reentrant_compilation_preserves_outer_folding_order(self) -> None:
        def run(native: bool, asynchronous: bool, inner_name: str) -> tuple[str, list[str]]:
            from docxtpl_native.bridge import BridgeConfig

            events: list[str] = []
            environment = jinja2.Environment(enable_async=asynchronous)
            session: Any

            class Context:
                def keys(self) -> list[str]:
                    events.append('context:keys')
                    return ['value']

                def __getitem__(self, key: str) -> str:
                    events.append('context:get:' + key)
                    return 'V'

            context = Context()

            def fold(value: str) -> str:
                events.append('fold:' + value)
                if value == 'outer':
                    if native:
                        nested = session.render(inner_name, "{{ 'inner'|fold }}")
                    else:
                        nested = environment.from_string("{{ 'inner'|fold }}").render(context)
                    events.append('nested:' + nested)
                return value.upper()

            environment.filters['fold'] = fold
            source = "{{ 'outer'|fold }}:{{ 'after'|fold }}:{{ value }}"
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)
                output = session.render('same', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for inner_name in ('same', 'inner'):
                with self.subTest(asynchronous=asynchronous, inner_name=inner_name):
                    expected = run(False, asynchronous, inner_name)
                    self.assertEqual(expected, ('OUTER:AFTER:V', [
                        'fold:outer', 'fold:inner', 'context:keys', 'context:get:value',
                        'nested:INNER', 'fold:after', 'context:keys', 'context:get:value',
                    ]))
                    self.assertEqual(run(True, asynchronous, inner_name), expected)

    def test_reentrant_compilation_catches_nested_syntax_error(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            from docxtpl_native.bridge import BridgeConfig

            events: list[str] = []
            environment = jinja2.Environment(enable_async=asynchronous)
            session: Any

            def fold(value: str) -> str:
                events.append('fold:' + value)
                if value == 'outer':
                    try:
                        if native:
                            session.render('same', '{{')
                        else:
                            environment.from_string('{{').render()
                    except jinja2.TemplateSyntaxError as error:
                        events.append('syntax:' + str(error.message))
                return value.upper()

            environment.filters['fold'] = fold
            source = "{{ 'outer'|fold }}:{{ 'after'|fold }}:{{ value }}"
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {'value': 'V'})
                output = session.render('same', source)
            else:
                output = environment.from_string(source).render(value='V')
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected[0], 'OUTER:AFTER:V')
                self.assertEqual(expected[1][0], 'fold:outer')
                self.assertTrue(expected[1][1].startswith('syntax:'))
                self.assertEqual(expected[1][2], 'fold:after')
                self.assertEqual(run(True, asynchronous), expected)

    def test_reentrant_unpack_error_keeps_reference_message(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            from docxtpl_native.bridge import BridgeConfig

            events: list[str] = []
            environment = jinja2.Environment(enable_async=asynchronous)
            session: Any
            context: dict[str, object] = {'bad': None, 'value': 'V'}

            def fold(value: str) -> str:
                try:
                    if native:
                        session.render('same', '{% set a, b = bad %}')
                    else:
                        environment.from_string('{% set a, b = bad %}').render(context)
                except TypeError as error:
                    events.append(str(error))
                return value

            environment.filters['fold'] = fold
            source = "{{ 'x'|fold }}:{{ value }}"
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)
                output = session.render('same', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ('x:V', ['cannot unpack non-iterable NoneType object']))
                self.assertEqual(run(True, asynchronous), expected)

    def test_session_releases_replaced_finalizers_after_render(self) -> None:
        import gc
        import weakref

        from docxtpl_native.bridge import BridgeConfig

        def run(native: bool, asynchronous: bool) -> int:
            environment = jinja2.Environment(enable_async=asynchronous)
            references: list[weakref.ReferenceType[Any]] = []

            class Finalizer:
                def __call__(self, value: object) -> object:
                    return value

            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {'value': 'x'})
            for _ in range(12):
                finalizer = Finalizer()
                references.append(weakref.ref(finalizer))
                environment.finalize = finalizer
                if native:
                    session.render('same', '{{ value }}')
                else:
                    environment.from_string('{{ value }}').render(value='x')
            environment.finalize = None
            del finalizer
            gc.collect()
            return sum(reference() is not None for reference in references)

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, 0)
                self.assertEqual(run(True, asynchronous), expected)

    def test_compilation_releases_finalizer_before_context_access(self) -> None:
        from docxtpl_native.bridge import BridgeConfig

        def run(native: bool, asynchronous: bool) -> list[str]:
            events: list[str] = []
            environment = jinja2.Environment(enable_async=asynchronous)

            class Finalizer:
                def __call__(self, value: object) -> object:
                    events.append('finalize:' + str(value))
                    environment.finalize = None
                    return value

                def __del__(self) -> None:
                    events.append('finalizer:del')

            class Context:
                def keys(self) -> list[str]:
                    events.append('context:keys')
                    return ['value']

                def __getitem__(self, key: str) -> str:
                    events.append('context:get:' + key)
                    return 'x'

            environment.finalize = Finalizer()
            context = Context()
            source = "{{ 'constant' }}:{{ value }}"
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)
            try:
                if native:
                    session.render('same', source)
                else:
                    environment.from_string(source).render(context)
            except TypeError as error:
                events.append('error:' + type(error).__name__ + ':' + str(error))
            return events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, ['finalize:constant', 'finalizer:del',
                                            'context:keys', 'context:get:value',
                                            "error:TypeError:'NoneType' object is not callable"])
                self.assertEqual(run(True, asynchronous), expected)

    def test_concurrent_session_errors_preserve_exception_identity(self) -> None:
        import threading

        from docxtpl_native.bridge import BridgeConfig

        def run(native: bool) -> tuple[list[tuple[int, int, str, str]], list[bool]]:
            environment = jinja2.Environment()
            barrier = threading.Barrier(2, timeout=10)
            markers = [ValueError('first'), KeyError('second')]
            mismatches: list[tuple[int, int, str, str]] = []

            def callback(index: int) -> str:
                barrier.wait()
                raise markers[index]

            context = {'callback': callback}
            session: Any
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)

            def worker(index: int) -> None:
                for iteration in range(200):
                    source = '{{ callback(' + str(index) + ') }}'
                    try:
                        if native:
                            session.render('same', source)
                        else:
                            environment.from_string(source).render(context)
                    except Exception as error:  # noqa: BLE001 - compare every returned exception
                        if error is not markers[index]:
                            mismatches.append((index, iteration, type(error).__name__, str(error)))
                            barrier.abort()
                            return
                    else:
                        mismatches.append((index, iteration, 'no exception', ''))
                        barrier.abort()
                        return

            threads = [threading.Thread(target=worker, args=(index,), daemon=True) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            return mismatches, [thread.is_alive() for thread in threads]

        self.assertEqual(run(False), ([], [False, False]))
        self.assertEqual(run(True), ([], [False, False]))

    def test_concurrent_loader_misses_return_each_loaded_source(self) -> None:
        import threading

        from docxtpl_native.bridge import BridgeConfig

        def run(native: bool, asynchronous: bool) -> tuple[dict[str, str], list[str], list[bool]]:
            barrier = threading.Barrier(2, timeout=10)
            second_finished = threading.Event()
            events: list[str] = []
            outputs: dict[str, str] = {}

            class Loader(jinja2.BaseLoader):
                def get_source(self, environment: Any, template: str) -> tuple[str, str, Any]:
                    label = threading.current_thread().name
                    events.append('load:' + label + ':' + template)
                    if label not in ('A', 'B'):
                        return 'unexpected reload', template, lambda: True
                    barrier.wait()
                    if label == 'A' and not second_finished.wait(timeout=10):
                        raise TimeoutError('second render did not finish')
                    return label, template, lambda: True

            environment = jinja2.Environment(loader=Loader(), auto_reload=False, enable_async=asynchronous)
            session: Any
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {})

            def worker() -> None:
                label = threading.current_thread().name
                try:
                    source = "{% include 'part' %}"
                    outputs[label] = (session.render('same', source) if native
                                      else environment.from_string(source).render())
                except Exception as error:  # noqa: BLE001 - report any callback failure
                    outputs[label] = type(error).__name__ + ':' + str(error)
                    barrier.abort()
                finally:
                    if label == 'B':
                        second_finished.set()

            threads = [threading.Thread(target=worker, name=label, daemon=True) for label in ('A', 'B')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            if all(not thread.is_alive() for thread in threads):
                source = "{% include 'part' %}"
                outputs['cached'] = (session.render('same', source) if native
                                     else environment.from_string(source).render())
            return outputs, sorted(events), [thread.is_alive() for thread in threads]

        expected = ({'A': 'A', 'B': 'B', 'cached': 'A'}, ['load:A:part', 'load:B:part'], [False, False])
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(run(False, asynchronous), expected)
                self.assertEqual(run(True, asynchronous), expected)

    def test_loader_cache_replacement_keeps_active_render_alive(self) -> None:
        import threading

        from docxtpl_native.bridge import BridgeConfig

        def run(native: bool, asynchronous: bool) -> tuple[dict[str, str], list[bool]]:
            barrier = threading.Barrier(2, timeout=10)
            second_started = threading.Event()
            first_finished = threading.Event()
            outputs: dict[str, str] = {}

            def wait_for_first() -> str:
                second_started.set()
                if not first_finished.wait(timeout=10):
                    raise TimeoutError('first render did not finish')
                return ''

            class Loader(jinja2.BaseLoader):
                def get_source(self, environment: Any, template: str) -> tuple[str, str, Any]:
                    label = threading.current_thread().name
                    barrier.wait()
                    if label == 'A':
                        if not second_started.wait(timeout=10):
                            raise TimeoutError('second render did not start')
                        return 'A', template, lambda: True
                    return '{{ wait_for_first() }}B', template, lambda: True

            environment = jinja2.Environment(loader=Loader(), auto_reload=False, enable_async=asynchronous)
            context = {'wait_for_first': wait_for_first}
            session: Any
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)

            def worker() -> None:
                label = threading.current_thread().name
                try:
                    source = "{% include 'part' %}"
                    outputs[label] = (session.render('same', source) if native
                                      else environment.from_string(source).render(context))
                except Exception as error:  # noqa: BLE001 - report any callback failure
                    outputs[label] = type(error).__name__ + ':' + str(error)
                    barrier.abort()
                finally:
                    if label == 'A':
                        first_finished.set()

            threads = [threading.Thread(target=worker, name=label, daemon=True) for label in ('A', 'B')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            return outputs, [thread.is_alive() for thread in threads]

        expected = ({'A': 'A', 'B': 'B'}, [False, False])
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(run(False, asynchronous), expected)
                self.assertEqual(run(True, asynchronous), expected)

    def test_nested_sessions_restore_each_error_scope(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            from docxtpl_native.bridge import BridgeConfig

            events: list[object] = []
            first = jinja2.Environment()
            second = jinja2.Environment()
            first_session: Any
            second_session: Any
            marker = ValueError('inner')

            def fail() -> str:
                events.append('fail')
                raise marker

            def second_callback() -> str:
                events.append('second')
                try:
                    if native:
                        first_session.render('same', '{{ fail() }}')
                    else:
                        first.from_string('{{ fail() }}').render(first_context)
                except ValueError as error:
                    events.append(('caught', error is marker))
                return 'B'

            def first_callback() -> str:
                events.append('first')
                if native:
                    return second_session.render('same', '{{ second_callback() }}')
                return second.from_string('{{ second_callback() }}').render(second_context)

            first_context = {'first_callback': first_callback, 'fail': fail}
            second_context = {'second_callback': second_callback}
            if native:
                first_config = BridgeConfig(first, False)
                second_config = BridgeConfig(second, False)
                first_config.package_values_live = second_config.package_values_live = True
                first_session = docxtpl_native._extension().PythonRenderSession(first_config, first_context)
                second_session = docxtpl_native._extension().PythonRenderSession(second_config, second_context)
                output = first_session.render('same', '{{ first_callback() }}:A')
            else:
                output = first.from_string('{{ first_callback() }}:A').render(first_context)
            return output, events

        expected = run(False)
        self.assertEqual(expected, ('B:A', ['first', 'second', 'fail', ('caught', True)]))
        self.assertEqual(run(True), expected)

    def test_compile_failure_clears_state_before_next_render(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[bool, str, str]:
            from docxtpl_native.bridge import BridgeConfig

            class Abort(BaseException):
                pass

            marker = Abort('compile stopped')

            def finalize(value: object) -> object:
                raise marker

            environment = jinja2.Environment(finalize=finalize, enable_async=asynchronous)
            session: Any
            if native:
                config = BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {'value': 'V'})
            try:
                if native:
                    session.render('same', "{{ 'constant' }}")
                else:
                    environment.from_string("{{ 'constant' }}").render(value='V')
            except Abort as error:
                same, message = error is marker, str(error)
            else:
                raise AssertionError('expected compile failure')
            environment.finalize = None
            if native:
                output = session.render('same', '{{ value }}')
            else:
                output = environment.from_string('{{ value }}').render(value='V')
            return same, message, output

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(expected, (True, 'compile stopped', 'V'))
                self.assertEqual(run(True, asynchronous), expected)

    def test_reentrant_document_render_sync_and_async(self) -> None:
        for asynchronous in (False, True):
            def execute(engine: Any, *, async_mode: bool = asynchronous) -> Result:
                events: list[str] = []
                document = engine.DocxTemplate(io.BytesIO(template('{{ callback() }}:{{ value }}')))
                environment = jinja2.Environment(enable_async=async_mode)

                def inner() -> str:
                    events.append('inner')
                    return 'I'

                def outer() -> str:
                    events.append('outer:enter')
                    try:
                        document.render({'callback': inner, 'value': 'nested'}, jinja_env=environment)
                        events.append('nested:ok')
                    except RuntimeError as error:
                        events.append('nested:' + type(error).__name__ + ':' + str(error))
                    events.append('outer:exit')
                    return 'O'

                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', RuntimeWarning)
                    document.render({'callback': outer, 'value': 'root'}, jinja_env=environment)
                output = io.BytesIO()
                document.save(output)
                result = observed(output.getvalue(), events)
                self.assertEqual(result.observation['text'], 'O:root')
                nested = ('nested:RuntimeError:asyncio.run() cannot be called from a running event loop'
                          if async_mode else 'nested:ok')
                expected = (['outer:enter', nested, 'outer:exit'] if async_mode
                            else ['outer:enter', 'inner', nested, 'outer:exit'])
                self.assertEqual(events, expected)
                return result

            with self.subTest(asynchronous=asynchronous):
                self.compare(execute)

    def test_reentry_keeps_loader_cache_and_outer_compilation(self) -> None:
        def run(native: bool, inner_name: str) -> tuple[str, list[str]]:
            from docxtpl_native.bridge import BridgeConfig

            events: list[str] = []

            class Loader(jinja2.DictLoader):
                def get_source(self, environment: Any, template: str) -> Any:
                    events.append('load:' + template)
                    return super().get_source(environment, template)

            environment = jinja2.Environment(loader=Loader({'part': 'P'}), auto_reload=False)
            session: Any = None

            def callback() -> str:
                environment.finalize = lambda value: 'I' + str(value)
                try:
                    if native:
                        result = session.render(inner_name, "{% include 'part' %}{{ value }}")
                    else:
                        result = environment.from_string("{% include 'part' %}{{ value }}").render(value='x')
                    events.append('inner:' + result)
                finally:
                    environment.finalize = None
                return ''

            source = "{% include 'part' %}{{ callback() }}{{ value }}"
            if native:
                configuration = BridgeConfig(environment, False)
                configuration.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(
                    configuration, {'callback': callback, 'value': 'x'}
                )
                return session.render('outer', source), events
            return environment.from_string(source).render(callback=callback, value='x'), events

        for inner_name in ('inner', 'outer'):
            with self.subTest(inner_name=inner_name):
                expected = run(False, inner_name)
                self.assertEqual(expected, ('Px', ['load:part', 'inner:PIx']))
                self.assertEqual(run(True, inner_name), expected)

    def test_session_can_be_reentered_from_callback(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[tuple[str, str]]]:
            from docxtpl_native.bridge import BridgeConfig

            environment = jinja2.Environment(enable_async=asynchronous)
            events: list[tuple[str, str]] = []
            session: Any = None

            def callback() -> str:
                try:
                    if native:
                        result = session.render('inner', 'inner')
                    else:
                        result = environment.from_string('inner').render()
                    events.append(('result', result))
                except RuntimeError as error:
                    events.append((type(error).__name__, str(error)))
                return 'outer'

            if native:
                configuration = BridgeConfig(environment, False)
                configuration.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(
                    configuration, {'callback': callback}
                )
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', RuntimeWarning)
                if native:
                    result = session.render('outer', '{{ callback() }}')
                else:
                    result = environment.from_string('{{ callback() }}').render(callback=callback)
            return result, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = run(False, asynchronous)
                self.assertEqual(run(True, asynchronous), expected)

    def compare(self, execute):
        reference = execute(docxtpl)
        with ExitStack() as blocked:
            for method in ('jinja2.Template.render', 'jinja2.Template.render_async',
                           'jinja2.Template.generate_async'):
                blocked.enter_context(patch(method, side_effect=AssertionError('Python template executor called')))
            native = execute(docxtpl_native)
        self.assertEqual(native.observation, reference.observation)
        self.assertEqual(set(native.outputs), set(reference.outputs))
        for name in reference.outputs:
            difference = compare_docx(reference.outputs[name], native.outputs[name])
            self.assertTrue(difference['equivalent'], difference)

    def test_existing_async_contracts(self):
        for name, probe in {'async_callback': ALL_PROBES['async_callback'], **PROBES}.items():
            with self.subTest(case=name):
                self.assertEqual(probe.execute(docxtpl).observation, probe.expected)
                self.compare(probe.execute)

    def test_context_task_future_and_generator_coroutine(self):
        def execute(engine):
            import types
            trace = []
            variable = contextvars.ContextVar('render-value', default='outside')
            tasks = []
            async def callback(value):
                tasks.append(asyncio.current_task())
                coroutine = tasks[-1].get_coro()
                trace.append(['coroutine', type(coroutine) is types.CoroutineType,
                              getattr(coroutine, 'cr_running', None)])
                trace.append([value, variable.get()])
                variable.set(value)
                future = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(future.set_result, value)
                return await future
            @types.coroutine
            def legacy():
                yield None
                trace.append(['legacy', variable.get()])
                return 'L'
            blob = generate(engine, "{{ callback('A') }}{{ legacy() }}{{ callback('B') }}",
                            {'callback': callback, 'legacy': legacy}, jinja2.Environment(enable_async=True))
            return observed(blob, trace, same_task=tasks[0] is tasks[1], outside=variable.get())
        self.compare(execute)

    def test_cancellation_recovered_and_propagated(self):
        for recover in (False, True):
            def execute(engine):
                trace = []
                async def callback():
                    try:
                        asyncio.current_task().cancel('cancel render')
                        await asyncio.sleep(0)
                    except asyncio.CancelledError as error:
                        trace.append(['cancelled', str(error)])
                        if not recover:
                            raise
                        return 'R'
                    finally:
                        trace.append('finally')
                def later():
                    trace.append('later')
                    return 'L'
                try:
                    blob = generate(engine, '{{ callback() }}{{ later() }}',
                                    {'callback': callback, 'later': later}, jinja2.Environment(enable_async=True))
                except asyncio.CancelledError as error:
                    from paritylab.protocol_base import Result
                    return Result({'trace': trace, 'exception': str(error)}, {})
                return observed(blob, trace)
            with self.subTest(recover=recover):
                self.compare(execute)

    def test_running_task_rejects_reentrant_coroutine_operations(self):
        def execute(engine):
            trace = []
            async def callback():
                coroutine = asyncio.current_task().get_coro()
                for operation, arguments in (('send', (None,)), ('throw', (ValueError('injected'),)), ('close', ())):
                    try:
                        getattr(coroutine, operation)(*arguments)
                    except Exception as error:
                        trace.append([operation, type(error).__name__, str(error)])
                await asyncio.sleep(0)
                return 'resumed'
            return observed(generate(engine, '{{ callback() }}', {'callback': callback},
                                     jinja2.Environment(enable_async=True)), trace)
        self.compare(execute)

    def test_nested_macro_include_attribute_filter_and_test(self):
        def execute(engine):
            trace = []
            async def delay(value):
                trace.append(value)
                await asyncio.sleep(0)
                return value
            class Box:
                @property
                def value(self):
                    return delay('A')
                def __getitem__(self, key):
                    return delay(key)
            environment = jinja2.Environment(enable_async=True,
                loader=jinja2.DictLoader({'fragment': "{{ box['B']|decorate }}{% if box.value is accepted %}Y{% endif %}"}))
            async def decorate(value):
                return await delay(value.lower())
            async def accepted(value):
                return bool(await delay(value))
            environment.filters['decorate'] = decorate
            environment.tests['accepted'] = accepted
            blob = generate(engine, "{% macro m() %}{{ box.value }}{% include 'fragment' %}{% endmacro %}{{ m() }}",
                            {'box': Box()}, environment)
            return observed(blob, trace)
        self.compare(execute)

    def test_async_iteration_protocol_and_lazy_metadata(self):
        bodies = (
            '{% for x in values %}{{ x }}{% endfor %}',
            '{% for x in values %}{{ loop.index }}:{{ x }}{% endfor %}',
            '{% for x in values %}{{ x }}:{{ loop.last }}{% endfor %}',
            '{% for x in values %}{{ x }}:{{ loop.length }}{% endfor %}',
            '{% for x in values if accept(x) %}{{ x }}:{{ loop.last }}{% endfor %}',
        )
        for source in bodies:
            def execute(engine):
                trace = []
                class Values:
                    def __init__(self):
                        self.index = 0
                    def __getattribute__(self, name):
                        if name in ('__aiter__', '__anext__'):
                            trace.append('lookup:' + name)
                        return object.__getattribute__(self, name)
                    def __aiter__(self):
                        trace.append('aiter')
                        return self
                    async def __anext__(self):
                        trace.append('next:' + str(self.index))
                        await asyncio.sleep(0)
                        if self.index == 3:
                            raise StopAsyncIteration
                        self.index += 1
                        return self.index
                async def accept(value):
                    trace.append('test:' + str(value))
                    await asyncio.sleep(0)
                    return value != 2
                return observed(generate(engine, source, {'values': Values(), 'accept': accept},
                                         jinja2.Environment(enable_async=True)), trace)
            with self.subTest(source=source):
                self.compare(execute)

    def test_async_generator_closes_after_break_and_exception(self):
        for fail in (False, True):
            def execute(engine):
                from paritylab.protocol_base import Result
                trace = []
                failure = ValueError('stop in body')
                async def values():
                    try:
                        for value in range(3):
                            trace.append('yield:' + str(value))
                            yield value
                    finally:
                        trace.append('closed')
                def callback(value):
                    if fail:
                        raise failure
                    return value
                try:
                    blob = generate(engine, '{% for x in values %}{{ callback(x) }}{% break %}{% endfor %}',
                                    {'values': values(), 'callback': callback},
                                    jinja2.Environment(enable_async=True, extensions=['jinja2.ext.loopcontrols']))
                except ValueError as error:
                    return Result({'trace': trace, 'same_exception': error is failure}, {})
                return observed(blob, trace)
            with self.subTest(fail=fail):
                self.compare(execute)

    def test_async_iterator_invalid_await_error_and_cause(self):
        for metadata in ('', '{{ loop.index }}'):
            for failure in (False, True):
                def execute(engine):
                    from paritylab.protocol_base import Result
                    class Bad:
                        def __await__(self):
                            if failure:
                                raise ValueError('await setup failed')
                            return 42
                    class Values:
                        def __aiter__(self): return self
                        def __anext__(self): return Bad()
                    try:
                        generate(engine, '{% for x in values %}' + metadata + '{{ x }}{% endfor %}',
                                 {'values': Values()}, jinja2.Environment(enable_async=True))
                    except Exception as error:
                        cause = error.__cause__
                        return Result({'type': type(error).__name__, 'message': str(error),
                                       'cause_type': type(cause).__name__, 'cause': str(cause)}, {})
                    raise AssertionError('expected invalid await iterator')
                with self.subTest(metadata=metadata, failure=failure):
                    self.compare(execute)

    def test_cancellation_precedes_scheduled_protocol_error(self):
        for wrong_loop in (False, True):
            def execute(engine):
                import types
                trace = []
                other = asyncio.new_event_loop()
                future = other.create_future()
                @types.coroutine
                def callback():
                    asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
                    try:
                        if wrong_loop:
                            yield from future.__await__()
                        else:
                            yield 'invalid yield'
                    except asyncio.CancelledError:
                        trace.append('cancelled')
                        yield None
                        return 'recovered'
                    except RuntimeError:
                        return 'wrong error'
                try:
                    return observed(generate(engine, '{{ callback() }}', {'callback': callback},
                                             jinja2.Environment(enable_async=True)), trace)
                finally:
                    future.cancel()
                    other.close()
            with self.subTest(wrong_loop=wrong_loop):
                self.compare(execute)

    def test_await_and_sync_iteration_use_type_slots(self):
        def execute(engine):
            class AwaitIterator:
                def __iter__(self): return self
                def __next__(self): raise StopIteration('A')
                def __getattribute__(self, name):
                    if name == '__next__': raise AssertionError('instance next used')
                    return object.__getattribute__(self, name)
            class Awaitable:
                def __await__(self): return AwaitIterator()
                def __getattribute__(self, name):
                    if name == '__await__': raise AssertionError('instance await used')
                    return object.__getattribute__(self, name)
            class Values:
                def __init__(self): self.index = 0
                def __iter__(self): return self
                def __next__(self):
                    if self.index == 2: raise StopIteration
                    self.index += 1
                    return self.index
                def __getattribute__(self, name):
                    if name == '__next__': raise AssertionError('instance next used')
                    return object.__getattribute__(self, name)
            return observed(generate(engine, '{{ callback() }}{% for x in values %}{{ x }}{% endfor %}',
                                     {'callback': Awaitable, 'values': Values()}, jinja2.Environment(enable_async=True)))
        self.compare(execute)

    def test_awaited_method_lookup_precedes_invocation(self):
        def execute(engine):
            trace = []
            async def resolve():
                trace.append('lookup:start')
                await asyncio.sleep(0)
                trace.append('lookup:finish')
                def callback(value):
                    trace.append('call:' + value)
                    return value
                return callback
            class Box:
                @property
                def callback(self): return resolve()
            def argument():
                trace.append('argument')
                return 'X'
            return observed(generate(engine, "{{ box.callback(argument()) }}", {'box': Box(), 'argument': argument},
                                     jinja2.Environment(enable_async=True)), trace)
        self.compare(execute)


if __name__ == '__main__':
    unittest.main()
