"""Compare coroutine reentry during the first legacy-throw warning."""

import sys
import unittest
import warnings
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native import bridge


class AsyncWarningReentryTests(unittest.TestCase):
    @unittest.skipUnless(sys.version_info >= (3, 12), 'legacy throw has no warning before Python 3.12')
    def test_first_warning_reentry_matches_reference(self) -> None:
        def observe(native: bool, operation: str) -> tuple[list[str], str, str]:
            events: list[str] = []
            retained: list[Any] = []

            class Waiter:
                def __init__(self) -> None:
                    self.count = 0

                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    self.count += 1
                    events.append('next')
                    if self.count == 1:
                        return 'paused'
                    raise StopIteration('finished')

                def throw(self, *args: object) -> str:
                    events.append('delegate throw')
                    return 'again'

                def close(self) -> None:
                    events.append('delegate close')

            environment = jinja2.Environment(enable_async=True)
            if native:
                def retain(render: Any) -> Any:
                    retained.append(render)

                    async def held() -> str:
                        return 'held'

                    return held()

                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {'wait': Waiter})
                with patch.object(bridge, '_await_native_render', retain):
                    self.assertEqual(session.render('source', '{{wait()}}'), 'held')
                coroutine = retained[0]
            else:
                coroutine = environment.from_string('{{wait()}}').render_async(wait=Waiter)
            self.assertEqual(coroutine.send(None), 'paused')

            def showwarning(*args: Any, **kwargs: Any) -> None:
                events.append('warning')
                if events.count('warning') != 1:
                    return
                try:
                    if operation == 'close':
                        result = coroutine.close()
                    elif operation == 'send':
                        result = coroutine.send(None)
                    else:
                        result = coroutine.throw(ValueError('inner'))
                except BaseException as error:  # noqa: BLE001 - compare callback protocol
                    events.append(f'{type(error).__name__}:{error}')
                else:
                    events.append(f'return:{result}')

            with warnings.catch_warnings(), patch.object(warnings, 'showwarning', showwarning):
                warnings.simplefilter('always', DeprecationWarning)
                try:
                    outcome = 'return:' + str(coroutine.throw(ValueError, 'outer'))
                except BaseException as error:  # noqa: BLE001 - compare outer protocol
                    outcome = f'{type(error).__name__}:{error}'
            try:
                coroutine.send(None)
            except StopIteration as done:
                final = 'return:' + str(done.value)
            except BaseException as error:  # noqa: BLE001 - compare terminal state
                final = f'{type(error).__name__}:{error}'
            else:
                final = 'yielded'
            coroutine.close()
            return events, outcome, final

        for operation in ('close', 'send', 'throw'):
            with self.subTest(operation=operation):
                self.assertEqual(observe(True, operation), observe(False, operation))
