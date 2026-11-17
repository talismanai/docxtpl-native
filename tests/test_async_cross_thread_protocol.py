"""Observable coroutine operations after suspension on a different thread."""

import threading
import unittest
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native import bridge


def observe(operation: str, native: bool) -> tuple[list[str], list[str]]:
    events: list[str] = []
    outcomes: list[str] = []
    retained: list[Any] = []
    ready = threading.Event()
    release = threading.Event()

    class Waiter:
        def __await__(self) -> Generator[str, None, str]:
            try:
                events.append('start:' + threading.current_thread().name)
                try:
                    yield 'paused'
                except ValueError:
                    events.append('caught:' + threading.current_thread().name)
                    return 'recovered'
                events.append('resume:' + threading.current_thread().name)
                return 'finished'
            finally:
                events.append('finally:' + threading.current_thread().name)

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
            render = environment.from_string('{{ wait() }}').render_async(wait=Waiter)
            retained.append(render)
            outcomes.append(render.send(None))
            outcomes.append('held')
        ready.set()
        if release.wait(timeout=30) and retained:
            retained.pop().close()

    with patch.object(bridge, '_await_native_render', retain):
        thread = threading.Thread(target=worker, name='origin', daemon=True)
        thread.start()
        try:
            if not ready.wait(timeout=30):
                raise AssertionError('origin thread did not suspend')
            try:
                if operation == 'send':
                    retained[0].send(None)
                elif operation == 'throw':
                    retained[0].throw(ValueError('injected'))
                else:
                    retained[0].close()
                    outcomes.append('closed')
            except StopIteration as error:
                outcomes.append('result:' + str(error.value))
            except Exception as error:  # noqa: BLE001 - compare observed boundary errors
                outcomes.append('error:' + type(error).__name__)
            observed_events = list(events)
            observed_outcomes = list(outcomes)
        finally:
            release.set()
            thread.join(timeout=30)
        if thread.is_alive():
            raise AssertionError('origin thread did not finish')
    return observed_outcomes, observed_events


class AsyncCrossThreadProtocolTests(unittest.TestCase):
    def test_reference_operations_run_callbacks_on_resuming_thread(self) -> None:
        destination = threading.current_thread().name
        for operation, result, middle in (
            ('send', 'result:finished', 'resume:'),
            ('throw', 'result:recovered', 'caught:'),
            ('close', 'closed', None),
        ):
            with self.subTest(operation=operation):
                expected_events = ['start:origin']
                if middle is not None:
                    expected_events.append(middle + destination)
                expected_events.append('finally:' + destination)
                self.assertEqual(observe(operation, False), (['paused', 'held', result], expected_events))

    def test_native_operations_match_reference(self) -> None:
        for operation in ('send', 'throw', 'close'):
            with self.subTest(operation=operation):
                self.assertEqual(observe(operation, True), observe(operation, False))
