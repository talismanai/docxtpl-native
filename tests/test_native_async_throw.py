"""Differential checks for exception injection into native render coroutines."""

import unittest
import sys
import warnings
import gc
import queue
import threading
from collections.abc import Awaitable, Generator
from typing import Any, cast
from unittest.mock import patch

import docxtpl_native
import jinja2
import jinja2.async_utils
from docxtpl_native import bridge


class AsyncThrowTests(unittest.TestCase):
    def test_asyncgen_hooks_observe_template_generator_lifetime(self) -> None:
        def observe(native: bool) -> list[tuple[str, str]]:
            events: list[tuple[str, str]] = []
            retained: list[Any] = []

            class Waiter:
                def __await__(self) -> Generator[str, None, str]:
                    yield 'paused'
                    return 'ok'

            def firstiter(generator: Any) -> None:
                events.append(('firstiter', generator.ag_code.co_name))

            def finalizer(generator: Any) -> None:
                events.append(('finalizer', generator.ag_code.co_name))

            environment = jinja2.Environment(enable_async=True)
            source = '{{wait()}}|{{wait()}}'
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
                    self.assertEqual(session.render('source', source), 'held')
                coroutine = retained.pop()
            else:
                coroutine = environment.from_string(source).render_async(wait=Waiter)
            previous = sys.get_asyncgen_hooks()
            try:
                sys.set_asyncgen_hooks(firstiter=firstiter, finalizer=finalizer)
                self.assertEqual(coroutine.send(None), 'paused')
                self.assertEqual(coroutine.send(None), 'paused')
                with self.assertRaises(StopIteration) as done:
                    coroutine.send(None)
                self.assertEqual(done.exception.value, 'ok|ok')
            finally:
                sys.set_asyncgen_hooks(*previous)
            return events

        expected = observe(False)
        self.assertEqual(expected, [('firstiter', 'root')])
        self.assertEqual(observe(True), expected)

    def test_asyncgen_firstiter_exception_precedes_await(self) -> None:
        def observe(native: bool) -> list[tuple[str, ...]]:
            events: list[tuple[str, ...]] = []
            retained: list[Any] = []

            class Waiter:
                def __await__(self) -> Generator[str, None, str]:
                    events.append(('await',))
                    yield 'paused'
                    return 'ok'

            def firstiter(generator: Any) -> None:
                events.append(('firstiter', generator.ag_code.co_name))
                raise ValueError('firstiter failed')

            def finalizer(generator: Any) -> None:
                events.append(('finalizer', generator.ag_code.co_name))

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
                coroutine = retained.pop()
            else:
                coroutine = environment.from_string('{{wait()}}').render_async(wait=Waiter)
            previous = sys.get_asyncgen_hooks()
            try:
                sys.set_asyncgen_hooks(firstiter=firstiter, finalizer=finalizer)
                try:
                    events.append(('yield', coroutine.send(None)))
                except BaseException as error:  # noqa: BLE001 - compare firstiter callback effects
                    events.append(('error', type(error).__name__, str(error)))
                coroutine.close()
            finally:
                sys.set_asyncgen_hooks(*previous)
            return events

        expected = observe(False)
        self.assertEqual(expected, [('firstiter', 'root'), ('error', 'ValueError', 'firstiter failed'),
                                    ('finalizer', 'root')])
        self.assertEqual(observe(True), expected)

    def test_second_warning_callback_failure_closes_delegated_coroutine(self) -> None:
        def observe(native: bool) -> tuple[list[str], str, str]:
            retained: list[Any] = []
            events: list[str] = []
            environment = jinja2.Environment(enable_async=True)

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

                def throw(self, *args: Any) -> str:
                    events.append('throw')
                    return 'again'

                def close(self) -> None:
                    events.append('close')

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
                if events.count('warning') == 2:
                    raise RuntimeError('warning failed')

            with warnings.catch_warnings(), patch.object(warnings, 'showwarning', showwarning):
                warnings.simplefilter('always', DeprecationWarning)
                try:
                    result = 'yield:' + coroutine.throw(ValueError, 'message')
                except BaseException as error:  # noqa: BLE001 - compare callback error and state
                    result = 'error:' + type(error).__name__ + ':' + str(error)
            try:
                coroutine.send(None)
            except StopIteration as done:
                final = 'result:' + str(done.value)
            except BaseException as error:  # noqa: BLE001 - compare coroutine state
                final = 'error:' + type(error).__name__
            else:
                final = 'yielded'
            coroutine.close()
            return events, result, final

        self.assertEqual(observe(True), observe(False))

    def test_legacy_throw_warning_tracks_template_delegation_layers(self) -> None:
        def observe(native: bool, case: str) -> tuple[list[str], str, str]:
            retained: list[Any] = []
            loader = jinja2.DictLoader({'inc': '{{wait()}}', 'base': '{% block b %}{{wait()}}{% endblock %}'})
            environment = jinja2.Environment(enable_async=True, loader=loader)

            class Waiter:
                def __init__(self) -> None:
                    self.count = 0

                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    self.count += 1
                    if self.count == 1:
                        return 'paused'
                    raise StopIteration('finished')

                def throw(self, *args: Any) -> str:
                    return 'again'

                def close(self) -> None:
                    pass

            async def afilter(value: str) -> str:
                await cast(Awaitable[str], Waiter())
                return value

            async def selected(value: int) -> bool:
                await cast(Awaitable[str], Waiter())
                return True

            environment.filters['afilter'] = afilter
            cast(Any, environment.tests)['selected'] = selected
            sources = {
                'call': '{{wait()}}',
                'filter': '{{value|afilter}}',
                'include': "{% include 'inc' %}",
                'block': "{% extends 'base' %}{% block b %}{{wait()}}{% endblock %}",
                'condition': '{% if 1 is selected %}ok{% endif %}',
                'filtered_test': '{% for x in [1] if x is selected %}{{x}}{% endfor %}',
            }
            context = {'wait': Waiter, 'value': 'x'}
            if native:
                def retain(render: Any) -> Any:
                    retained.append(render)

                    async def held() -> str:
                        return 'held'

                    return held()

                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, context)
                with patch.object(bridge, '_await_native_render', retain):
                    self.assertEqual(session.render('source', sources[case]), 'held')
                coroutine = retained[0]
            else:
                coroutine = environment.from_string(sources[case]).render_async(context)

            self.assertEqual(coroutine.send(None), 'paused')
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter('always', DeprecationWarning)
                result = coroutine.throw(ValueError, 'message')
            captured = [f'{type(item.message).__name__}:{item.message}:{item.filename}:{item.lineno}' for item in recorded]
            try:
                coroutine.send(None)
            except StopIteration as done:
                final = str(done.value)
            else:
                raise AssertionError('expected coroutine completion')
            coroutine.close()
            return captured, result, final

        for case in ('call', 'filter', 'include', 'block', 'condition', 'filtered_test'):
            with self.subTest(case=case):
                expected = observe(False, case)
                self.assertEqual(len(expected[0]), {'call': 2, 'filter': 2, 'include': 3, 'block': 4,
                                                    'condition': 2, 'filtered_test': 3}[case]
                                 if sys.version_info >= (3, 12) else 0)
                self.assertEqual(observe(True, case), expected)

    def test_legacy_throw_warnings_in_macro_super_and_nested_includes(self) -> None:
        sources = {
            'macro': '{% macro m() %}{{wait()}}{% endmacro %}{{m()}}',
            'super': '{% extends "base" %}{% block b %}{{super()}}{% endblock %}',
            'nested': '{% include "outer" %}',
        }
        templates = {
            'base': '{% block b %}{{wait()}}{% endblock %}',
            'outer': '{% include "inner" %}',
            'inner': '{{wait()}}',
        }

        def observe(native: bool, case: str) -> tuple[list[str], str, str]:
            retained: list[Any] = []

            class Waiter:
                def __init__(self) -> None: self.count = 0
                def __await__(self) -> 'Waiter': return self
                def __iter__(self) -> 'Waiter': return self
                def __next__(self) -> str:
                    self.count += 1
                    if self.count == 1: return 'paused'
                    raise StopIteration('finished')
                def throw(self, *args: Any) -> str: return 'again'
                def close(self) -> None: pass

            environment = jinja2.Environment(enable_async=True, loader=jinja2.DictLoader(templates))
            if native:
                def retain(render: Any) -> Any:
                    retained.append(render)
                    async def held() -> str: return 'held'
                    return held()
                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {'wait': Waiter})
                with patch.object(bridge, '_await_native_render', retain):
                    self.assertEqual(session.render('source', sources[case]), 'held')
                coroutine = retained[0]
            else:
                coroutine = environment.from_string(sources[case]).render_async(wait=Waiter)
            self.assertEqual(coroutine.send(None), 'paused')
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter('always', DeprecationWarning)
                result = coroutine.throw(ValueError, 'message')
            captured = [str(item.message) for item in recorded]
            try:
                coroutine.send(None)
            except StopIteration:
                pass
            coroutine.close()
            return captured, result, case

        for case in sources:
            with self.subTest(case=case):
                self.assertEqual(observe(True, case), observe(False, case))

    def test_delegated_warning_reentry_observes_running_coroutine(self) -> None:
        def observe(native: bool, operation: str) -> tuple[list[str], str, str]:
            retained: list[Any] = []
            events: list[str] = []

            class Waiter:
                def __init__(self) -> None: self.count = 0
                def __await__(self) -> 'Waiter': return self
                def __iter__(self) -> 'Waiter': return self
                def __next__(self) -> str:
                    self.count += 1
                    if self.count == 1: return 'paused'
                    raise StopIteration('finished')
                def throw(self, *args: Any) -> str:
                    events.append('delegate throw')
                    return 'again'

            environment = jinja2.Environment(enable_async=True)
            if native:
                def retain(render: Any) -> Any:
                    retained.append(render)
                    async def held() -> str: return 'held'
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
                if events.count('warning') == 2:
                    try:
                        if operation == 'close':
                            coroutine.close()
                        elif operation == 'send':
                            coroutine.send(None)
                        else:
                            coroutine.throw(ValueError('reentrant'))
                    except BaseException as error:  # noqa: BLE001 - compare reentrant behavior
                        events.append(type(error).__name__ + ':' + str(error))
                    else:
                        events.append('reentrant call succeeded')

            with warnings.catch_warnings(), patch.object(warnings, 'showwarning', showwarning):
                warnings.simplefilter('always', DeprecationWarning)
                result = coroutine.throw(ValueError, 'message')
            try:
                coroutine.send(None)
            except StopIteration as done:
                final = 'result:' + str(done.value)
            except BaseException as error:  # noqa: BLE001 - compare reentrant state
                final = type(error).__name__ + ':' + str(error)
            else:
                raise AssertionError('expected completion')
            return events, result, final

        for operation in ('close', 'send', 'throw'):
            with self.subTest(operation=operation):
                self.assertEqual(observe(True, operation), observe(False, operation))

    def test_legacy_throw_warning_callback_can_close_unstarted_coroutine(self) -> None:
        def observe(native: bool) -> tuple[str, str, list[str], str]:
            retained: list[Any] = []
            events: list[str] = []
            environment = jinja2.Environment(enable_async=True)
            if native:
                def retain(render: Any) -> Any:
                    retained.append(render)

                    async def held() -> str:
                        return 'held'

                    return held()

                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {})
                with patch.object(bridge, '_await_native_render', retain):
                    self.assertEqual(session.render('source', 'finished'), 'held')
                coroutine = retained[0]
            else:
                coroutine = environment.from_string('finished').render_async()

            def showwarning(*args: Any, **kwargs: Any) -> None:
                events.append('warning')
                coroutine.close()

            with warnings.catch_warnings(), patch.object(warnings, 'showwarning', showwarning):
                warnings.simplefilter('always', DeprecationWarning)
                try:
                    getattr(coroutine, 'throw')(ValueError, 'failure')
                except BaseException as error:  # noqa: BLE001 - compare warning callback reentry
                    error_type, message = type(error).__name__, str(error)
                else:
                    raise AssertionError('expected throw to raise')
            try:
                coroutine.send(None)
            except BaseException as error:  # noqa: BLE001 - compare coroutine state
                after = type(error).__name__ + ':' + str(error)
            else:
                after = 'unexpected yield'
            coroutine.close()
            return error_type, message, events, after

        self.assertEqual(observe(True), observe(False))

    def test_legacy_throw_warning_for_suspended_generator(self) -> None:
        def observe(native: bool, warning_is_error: bool) -> tuple[str, list[str], list[str], str]:
            retained: list[Any] = []
            events: list[str] = []
            environment = jinja2.Environment(enable_async=True)

            class Waiter:
                def __await__(self) -> Generator[str, None, str]:
                    try:
                        events.append('start')
                        yield 'paused'
                        events.append('resume')
                        return 'finished'
                    finally:
                        events.append('finally')

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
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter('error' if warning_is_error else 'always', DeprecationWarning)
                try:
                    coroutine.throw(ValueError, 'message')
                except BaseException as error:  # noqa: BLE001 - compare warning and suspension
                    result = type(error).__name__ + ':' + str(error)
                else:
                    raise AssertionError('expected throw to raise')
            captured = [f'{type(item.message).__name__}:{item.message}:{item.filename}:{item.lineno}' for item in recorded]
            try:
                coroutine.send(None)
            except StopIteration as done:
                final = 'result:' + str(done.value)
            except BaseException as error:  # noqa: BLE001 - compare coroutine state
                final = 'error:' + type(error).__name__
            else:
                final = 'yielded'
            coroutine.close()
            return result, captured, events, final

        for warning_is_error in (False, True):
            with self.subTest(warning_is_error=warning_is_error):
                self.assertEqual(observe(True, warning_is_error), observe(False, warning_is_error))

    def test_legacy_throw_warning_for_suspended_custom_iterator(self) -> None:
        def observe(native: bool, warning_is_error: bool) -> tuple[str, list[str], list[str], str]:
            retained: list[Any] = []
            events: list[str] = []
            environment = jinja2.Environment(enable_async=True)

            class Waiter:
                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    events.append('next')
                    if events.count('next') == 1:
                        return 'paused'
                    raise StopIteration('finished')

                def throw(self, *args: Any) -> str:
                    events.append('throw')
                    return 'again'

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
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter('error' if warning_is_error else 'always', DeprecationWarning)
                try:
                    result = 'yield:' + coroutine.throw(ValueError, 'message')
                except BaseException as error:  # noqa: BLE001 - compare warning and suspension
                    result = 'error:' + type(error).__name__ + ':' + str(error)
            captured = [f'{type(item.message).__name__}:{item.message}:{item.filename}:{item.lineno}' for item in recorded]
            try:
                coroutine.send(None)
            except StopIteration as done:
                final = 'result:' + str(done.value)
            except BaseException as error:  # noqa: BLE001 - compare coroutine state
                final = 'error:' + type(error).__name__
            else:
                final = 'yielded'
            coroutine.close()
            return result, captured, events, final

        for warning_is_error in (False, True):
            with self.subTest(warning_is_error=warning_is_error):
                self.assertEqual(observe(True, warning_is_error), observe(False, warning_is_error))

    def test_legacy_throw_warning_as_error_preserves_coroutine_state(self) -> None:
        def observe(native: bool) -> tuple[str, str, str]:
            retained: list[Any] = []
            environment = jinja2.Environment(enable_async=True)
            if native:
                def retain(render: Any) -> Any:
                    retained.append(render)

                    async def held() -> str:
                        return 'held'

                    return held()

                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {})
                with patch.object(bridge, '_await_native_render', retain):
                    self.assertEqual(session.render('source', 'finished'), 'held')
                coroutine = retained[0]
            else:
                coroutine = environment.from_string('finished').render_async()
            with warnings.catch_warnings():
                warnings.simplefilter('error', DeprecationWarning)
                try:
                    getattr(coroutine, 'throw')(ValueError, 'failure')
                except BaseException as error:  # noqa: BLE001 - compare warning/error priority
                    error_type, message = type(error).__name__, str(error)
                else:
                    raise AssertionError('expected throw to raise')
            try:
                coroutine.send(None)
            except StopIteration as done:
                after = 'result:' + str(done.value)
            except BaseException as error:  # noqa: BLE001 - compare coroutine state
                after = 'error:' + type(error).__name__ + ':' + str(error)
            else:
                after = 'yielded'
            coroutine.close()
            return error_type, message, after

        self.assertEqual(observe(True), observe(False))

    def test_legacy_throw_signature_preserves_warnings(self) -> None:
        def observe(native: bool, with_traceback: bool) -> list[tuple[str, str, str, int]]:
            retained: list[Any] = []
            environment = jinja2.Environment(enable_async=True)
            if native:
                def retain(render: Any) -> Any:
                    retained.append(render)

                    async def held() -> str:
                        return 'held'

                    return held()

                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {})
                with patch.object(bridge, '_await_native_render', retain):
                    self.assertEqual(session.render('source', 'unused'), 'held')
                coroutine = retained[0]
            else:
                coroutine = environment.from_string('unused').render_async()
            arguments: tuple[Any, ...] = (ValueError, 'message', None) if with_traceback else (ValueError, 'message')
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter('always')
                try:
                    getattr(coroutine, 'throw')(*arguments)
                except ValueError:
                    pass
                else:
                    raise AssertionError('expected ValueError')
            coroutine.close()
            return [(type(item.message).__name__, str(item.message), item.filename, item.lineno) for item in recorded]

        for with_traceback in (False, True):
            with self.subTest(with_traceback=with_traceback):
                self.assertEqual(observe(True, with_traceback), observe(False, with_traceback))

    def test_missing_throw_cleanup_failure_keeps_original_error(self) -> None:
        def observe(native: bool) -> tuple[str, str, list[str], list[tuple[str, str]], list[tuple[str, Any, Any, bool]]]:
            events: list[str] = []
            unraisable: list[tuple[str, str]] = []
            unraisable_objects: list[tuple[str, Any, Any, bool]] = []
            retained: list[Any] = []
            environment = jinja2.Environment(enable_async=True)

            class Waiter:
                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    return 'paused'

                def close(self) -> None:
                    events.append('close')
                    raise RuntimeError('close failed')

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
            def capture_unraisable(arg: Any) -> None:
                unraisable.append((type(arg.exc_value).__name__, str(arg.exc_value)))
                code = getattr(arg.object, 'cr_code', None)
                unraisable_objects.append((type(arg.object).__name__, getattr(arg.object, '__qualname__', None),
                                           getattr(code, 'co_name', None),
                                           code is jinja2.async_utils.auto_await.__code__))

            with patch.object(sys, 'unraisablehook', capture_unraisable):
                try:
                    getattr(coroutine, 'throw')(1)
                except BaseException as error:  # noqa: BLE001 - compare cleanup failure boundary
                    error_type, message = type(error).__name__, str(error)
                else:
                    raise AssertionError('expected invalid throw to raise')
                coroutine.close()
            return error_type, message, events, unraisable, unraisable_objects

        expected = observe(False)
        self.assertEqual(expected[0], 'TypeError')
        self.assertEqual(expected[2:4], (['close'], [('RuntimeError', 'close failed')]))
        self.assertEqual(expected[4], [('coroutine', 'auto_await', 'auto_await', True)])
        self.assertEqual(observe(True), expected)

    def test_valid_throw_without_delegate_does_not_close_iterator(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            retained: list[Any] = []

            class Waiter:
                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    events.append('next')
                    return 'paused'

                def close(self) -> None:
                    events.append('close')

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
            with self.assertRaises(ValueError) as caught:
                coroutine.throw(ValueError('failure'))
            return str(caught.exception), events

        expected = observe(False)
        self.assertEqual(expected, ('failure', ['next']))
        self.assertEqual(observe(True), expected)

    def test_rejected_throw_without_delegate_matches_jinja_cleanup(self) -> None:
        def observe(native: bool, case: str) -> tuple[str, str, list[str], str, list[str]]:
            events: list[str] = []
            retained: list[Any] = []
            environment = jinja2.Environment(enable_async=True)

            class Waiter:
                def __init__(self) -> None:
                    self.closed = False

                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    events.append('next')
                    if self.closed:
                        raise StopIteration('finished')
                    return 'paused'

                def close(self) -> None:
                    events.append('close')
                    self.closed = True

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
            args: tuple[Any, ...] = ((GeneratorExit, 'value', 42) if case == 'generator_exit' else (1,))
            try:
                getattr(coroutine, 'throw')(*args)
            except BaseException as error:  # noqa: BLE001 - compare rejected throw
                error_type, message = type(error).__name__, str(error)
            else:
                raise AssertionError('expected invalid throw to raise')
            after_throw = events.copy()
            try:
                after = 'yield:' + coroutine.send(None)
            except StopIteration as done:
                after = 'result:' + str(done.value)
            except BaseException as error:  # noqa: BLE001 - compare coroutine state
                after = 'error:' + type(error).__name__
            after_resume = events.copy()
            coroutine.close()
            return error_type, message, after_throw, after, after_resume

        for case in ('generator_exit', 'missing_throw'):
            with self.subTest(case=case):
                expected = observe(False, case)
                self.assertEqual(observe(True, case), expected)

    def test_invalid_generator_exit_closes_delegate_at_reference_boundary(self) -> None:
        def observe(native: bool, followup: str) -> tuple[list[str], str]:
            events: list[str] = []
            retained: list[Any] = []

            class Waiter:
                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    events.append('next')
                    return 'paused'

                def close(self) -> None:
                    events.append('close')

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
                coroutine = retained.pop()
            else:
                coroutine = environment.from_string('{{wait()}}').render_async(wait=Waiter)
            self.assertEqual(coroutine.send(None), 'paused')
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                with self.assertRaises(TypeError):
                    getattr(coroutine, 'throw')(GeneratorExit, 'value', 42)  # noqa: B009 - invalid argument probe
            events.append('after throw')
            try:
                if followup == 'send':
                    coroutine.send(None)
                elif followup == 'throw':
                    coroutine.throw(ValueError('later'))
                elif followup == 'close':
                    coroutine.close()
                else:
                    del coroutine
                    gc.collect()
            except BaseException as error:  # noqa: BLE001 - compare the reuse boundary
                result = f'{type(error).__name__}: {error}'
            else:
                result = 'ok'
            events.append('after followup')
            return events, result

        for followup in ('send', 'throw', 'close', 'gc'):
            with self.subTest(followup=followup):
                self.assertEqual(observe(True, followup), observe(False, followup))

    def test_invalid_generator_exit_unraisable_identifies_async_generator(self) -> None:
        def observe(native: bool) -> list[tuple[str, str, str | None]]:
            reported: list[tuple[str, str, str | None]] = []
            retained: list[Any] = []

            class Waiter:
                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    return 'paused'

                def close(self) -> None:
                    raise ValueError('close failed')

            def capture(arg: Any) -> None:
                reported.append((type(arg.exc_value).__name__, type(arg.object).__name__,
                                 getattr(arg.object, '__qualname__', None)))

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
                coroutine = retained.pop()
            else:
                coroutine = environment.from_string('{{wait()}}').render_async(wait=Waiter)
            self.assertEqual(coroutine.send(None), 'paused')
            with warnings.catch_warnings(), patch.object(sys, 'unraisablehook', capture):
                warnings.simplefilter('ignore', DeprecationWarning)
                with self.assertRaises(TypeError):
                    getattr(coroutine, 'throw')(GeneratorExit, 'value', 42)  # noqa: B009 - invalid argument probe
                coroutine.close()
            return reported

        expected = observe(False)
        self.assertEqual(expected, [('ValueError', 'async_generator', 'root')])
        self.assertEqual(observe(True), expected)

    def test_invalid_generator_exit_repeated_throw_validation_order(self) -> None:
        def observe(native: bool, case: str) -> list[Any]:
            events: list[Any] = []
            retained: list[Any] = []

            class Waiter:
                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    return 'paused'

                def close(self) -> None:
                    events.append('close')

            class Broken(Exception):
                def __new__(cls) -> Any:
                    events.append('construct')
                    raise ValueError('constructor failed')

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
                coroutine = retained.pop()
            else:
                coroutine = environment.from_string('{{wait()}}').render_async(wait=Waiter)
            self.assertEqual(coroutine.send(None), 'paused')
            arguments: dict[str, tuple[Any, ...]] = {
                'zero': (), 'four': (1, 2, 3, 4), 'bad': (1,),
                'bad_traceback': (ValueError, 'x', 42),
                'bad_instance': (ValueError('x'), 'y'),
                'bad_exit': (GeneratorExit, 'x', 42), 'constructor': (Broken,),
            }
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                with self.assertRaises(TypeError):
                    getattr(coroutine, 'throw')(GeneratorExit, 'x', 42)  # noqa: B009 - invalid argument probe
                try:
                    getattr(coroutine, 'throw')(*arguments[case])  # noqa: B009 - dynamic argument probe
                except BaseException as error:  # noqa: BLE001 - compare exception boundary
                    events.append((type(error).__name__, str(error)))
                else:
                    events.append('unexpected success')
                events.append('after throw')
                coroutine.close()
            return events

        for case in ('zero', 'four', 'bad', 'bad_traceback', 'bad_instance', 'bad_exit', 'constructor'):
            with self.subTest(case=case):
                self.assertEqual(observe(True, case), observe(False, case))

    def test_invalid_generator_exit_collects_cyclic_delegate(self) -> None:
        def observe(native: bool) -> list[str]:
            events: list[str] = []
            instances: list[Any] = []
            retained: list[Any] = []

            class Waiter:
                def __init__(self) -> None:
                    instances.append(self)

                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    events.append('next')
                    return 'paused'

                def close(self) -> None:
                    events.append('close')

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
                coroutine = retained.pop()
            else:
                coroutine = environment.from_string('{{wait()}}').render_async(wait=Waiter)
            self.assertEqual(coroutine.send(None), 'paused')
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                with self.assertRaises(TypeError):
                    getattr(coroutine, 'throw')(GeneratorExit, 'value', 42)  # noqa: B009 - invalid argument probe
            instances.pop().owner = coroutine
            events.append('before gc')
            del coroutine
            gc.collect()
            events.append('after gc')
            return events

        self.assertEqual(observe(True), observe(False))

    def test_invalid_generator_exit_finalizes_on_thread_releasing_coroutine(self) -> None:
        def observe(native: bool) -> list[str]:
            events: list[str] = []
            retained: list[Any] = []

            class Waiter:
                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    return 'paused'

                def close(self) -> None:
                    events.append(threading.current_thread().name)

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
                coroutine = retained.pop()
            else:
                coroutine = environment.from_string('{{wait()}}').render_async(wait=Waiter)
            self.assertEqual(coroutine.send(None), 'paused')
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                with self.assertRaises(TypeError):
                    getattr(coroutine, 'throw')(GeneratorExit, 'value', 42)  # noqa: B009 - invalid argument probe
            pending: queue.Queue[Any] = queue.Queue()
            pending.put(coroutine)
            del coroutine

            def release() -> None:
                item = pending.get_nowait()
                del item
                gc.collect()

            thread = threading.Thread(target=release, name='cleanup-worker')
            unraisable: list[str] = []

            def capture(arg: Any) -> None:
                unraisable.append(f'{type(arg.exc_value).__name__}: {arg.exc_value}')

            with patch.object(sys, 'unraisablehook', capture):
                thread.start()
                thread.join()
            self.assertEqual(unraisable, [])
            return events

        self.assertEqual(observe(True), observe(False))

    def test_throw_constructor_failure_does_not_close_delegate(self) -> None:
        def observe(native: bool, returns_non_exception: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []
            retained: list[Any] = []

            class Waiter:
                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    events.append('next')
                    return 'paused'

                def close(self) -> None:
                    events.append('close')

            class Broken(Exception):
                def __new__(cls) -> Any:
                    events.append('construct')
                    if returns_non_exception:
                        return 42
                    raise ValueError('constructor failed')

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
            try:
                coroutine.throw(Broken)
            except BaseException as error:  # noqa: BLE001 - compare constructor failure
                outcome = (type(error).__name__, str(error), events.copy())
            else:
                raise AssertionError('expected constructor failure')
            coroutine.close()
            self.assertEqual(events, ['next', 'construct'])
            return outcome

        for returns_non_exception in (False, True):
            with self.subTest(returns_non_exception=returns_non_exception):
                expected = observe(False, returns_non_exception)
                self.assertEqual(expected[2], ['next', 'construct'])
                self.assertEqual(observe(True, returns_non_exception), expected)

    def test_throw_constructor_error_closes_unstarted_coroutine(self) -> None:
        def observe(native: bool, case: str) -> tuple[str, str, bool, str]:
            retained: list[Any] = []
            failure = ValueError('constructor failed')
            environment = jinja2.Environment(enable_async=True)

            class Broken(Exception):
                def __new__(cls) -> Any:
                    if case == 'raises':
                        raise failure
                    return 42

            if native:
                def retain(render: Any) -> Any:
                    retained.append(render)

                    async def held() -> str:
                        return 'held'

                    return held()

                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {})
                with patch.object(bridge, '_await_native_render', retain):
                    self.assertEqual(session.render('source', 'unused'), 'held')
                coroutine = retained[0]
            else:
                coroutine = environment.from_string('unused').render_async()

            try:
                coroutine.throw(Broken)
            except BaseException as error:  # noqa: BLE001 - compare constructor failures
                error_type, message, same = type(error).__name__, str(error), error is failure
            else:
                raise AssertionError('expected throw to raise')
            try:
                coroutine.send(None)
            except BaseException as error:  # noqa: BLE001 - compare the closed coroutine
                after = type(error).__name__ + ':' + str(error)
            else:
                after = 'unexpected success'
            coroutine.close()
            return error_type, message, same, after

        for case in ('raises', 'returns_non_exception'):
            with self.subTest(case=case):
                self.assertEqual(observe(True, case), observe(False, case))

    def test_suspended_throw_delegates_invalid_arguments(self) -> None:
        def observe(native: bool, case: str) -> tuple[str, str, list[str], list[str], str]:
            events: list[str] = []
            retained: list[Any] = []
            environment = jinja2.Environment(enable_async=True)

            class Waiter:
                def __await__(self) -> Generator[str, None, str]:
                    try:
                        events.append('start')
                        yield 'paused'
                        events.append('resume')
                        return 'finished'
                    finally:
                        events.append('finally')

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
            args: tuple[Any, ...] = ((ValueError('first'), ValueError('second'))
                                     if case == 'instance_and_value' else (ValueError, 'first', 42))
            try:
                getattr(coroutine, 'throw')(*args)
            except BaseException as error:  # noqa: BLE001 - compare exception boundary
                error_type, message = type(error).__name__, str(error)
            else:
                raise AssertionError('expected throw to raise')
            events_after_throw = events.copy()
            try:
                coroutine.send(None)
            except StopIteration as done:
                result = 'result:' + str(done.value)
            except BaseException as error:  # noqa: BLE001 - report premature coroutine completion
                result = 'error:' + type(error).__name__
            else:
                result = 'yielded'
            coroutine.close()
            return error_type, message, events_after_throw, events, result

        for case in ('instance_and_value', 'invalid_traceback'):
            with self.subTest(case=case):
                expected = observe(False, case)
                self.assertEqual(observe(True, case), expected)

    def test_suspended_throw_preserves_arguments_for_custom_iterator(self) -> None:
        def observe(native: bool, case: str) -> tuple[list[str], str, str]:
            events: list[str] = []
            retained: list[Any] = []
            environment = jinja2.Environment(enable_async=True)
            marker = ValueError('marker')
            expected_args: tuple[Any, ...] = ((marker, None, None) if case == 'explicit_none'
                                              else (1, 'value', 42))

            class Waiter:
                def __await__(self) -> 'Waiter':
                    return self

                def __iter__(self) -> 'Waiter':
                    return self

                def __next__(self) -> str:
                    events.append('next')
                    if events.count('next') == 1:
                        return 'paused'
                    raise StopIteration('finished')

                def __getattribute__(self, name: str) -> Any:
                    if name == 'throw':
                        events.append('lookup:throw')
                    return object.__getattribute__(self, name)

                def throw(self, *args: Any) -> str:
                    events.append('args:' + str(len(args)) + ':' + str(all(a is b for a, b in zip(args, expected_args))))
                    return 'again'

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
            yielded = getattr(coroutine, 'throw')(*expected_args)
            try:
                coroutine.send(None)
            except StopIteration as done:
                result = str(done.value)
            else:
                raise AssertionError('expected coroutine completion')
            coroutine.close()
            return events, yielded, result

        for case in ('explicit_none', 'invalid'):
            with self.subTest(case=case):
                expected = observe(False, case)
                self.assertEqual(expected, (['next', 'lookup:throw', 'args:3:True', 'next'], 'again', 'finished'))
                self.assertEqual(observe(True, case), expected)

    def test_valid_throw_normalizes_without_replacing_existing_exception(self) -> None:
        def observe(native: bool, case: str) -> tuple[str, tuple[Any, ...], bool, list[str]]:
            events: list[str] = []
            retained: list[Any] = []
            environment = jinja2.Environment(enable_async=True)
            exception = ValueError('original')

            def tick() -> str:
                events.append('tick')
                return 'finished'

            if native:
                def retain(render: Any) -> Any:
                    retained.append(render)

                    async def held() -> str:
                        return 'held'

                    return held()

                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {'tick': tick})
                with patch.object(bridge, '_await_native_render', retain):
                    self.assertEqual(session.render('source', '{{tick()}}'), 'held')
                coroutine = retained[0]
            else:
                coroutine = environment.from_string('{{tick()}}').render_async(tick=tick)

            cases: dict[str, tuple[Any, ...]] = {
                'instance': (exception,),
                'class_and_instance': (ValueError, exception),
                'class_as_value': (ValueError, ValueError),
            }
            try:
                coroutine.throw(*cases[case])
            except BaseException as error:  # noqa: BLE001 - compare exception identity and arguments
                outcome = (type(error).__name__, error.args, error is exception, events.copy())
            else:
                raise AssertionError('expected throw to raise')
            coroutine.close()
            return outcome

        for case in ('instance', 'class_and_instance', 'class_as_value'):
            with self.subTest(case=case):
                expected = observe(False, case)
                self.assertEqual(expected[0], 'ValueError')
                self.assertEqual(expected[3], [])
                self.assertEqual(observe(True, case), expected)

    def test_invalid_throw_keeps_unstarted_coroutine_usable(self) -> None:
        def observe(native: bool, case: str) -> tuple[str, str, list[str], str]:
            events: list[str] = []
            retained: list[Any] = []
            environment = jinja2.Environment(enable_async=True)

            def tick() -> str:
                events.append('tick')
                return 'finished'

            if native:
                def retain(render: Any) -> Any:
                    retained.append(render)

                    async def held() -> str:
                        return 'held'

                    return held()

                config = bridge.BridgeConfig(environment, False)
                config.package_values_live = True
                session = docxtpl_native._extension().PythonRenderSession(config, {'tick': tick})
                with patch.object(bridge, '_await_native_render', retain):
                    self.assertEqual(session.render('source', '{{tick()}}'), 'held')
                coroutine = retained[0]
            else:
                coroutine = environment.from_string('{{tick()}}').render_async(tick=tick)

            cases: dict[str, tuple[Any, ...]] = {
                'instance_and_value': (ValueError('first'), ValueError('second')),
                'invalid_traceback': (ValueError, 'first', 42),
                'not_exception_class': (int,),
                'not_exception_instance': (1,),
                'no_arguments': (),
                'too_many_arguments': (ValueError, None, None, None),
            }
            try:
                coroutine.throw(*cases[case])
            except BaseException as error:  # noqa: BLE001 - compare the public exception boundary
                error_type, message = type(error).__name__, str(error)
            else:
                raise AssertionError('expected throw to raise')
            try:
                coroutine.send(None)
            except StopIteration as done:
                outcome = 'result:' + str(done.value)
            except BaseException as error:  # noqa: BLE001 - report premature coroutine completion
                outcome = 'error:' + type(error).__name__ + ':' + str(error)
            else:
                outcome = 'yielded'
            coroutine.close()
            return error_type, message, events, outcome

        for case in ('instance_and_value', 'invalid_traceback', 'not_exception_class', 'not_exception_instance',
                     'no_arguments', 'too_many_arguments'):
            with self.subTest(case=case):
                expected = observe(False, case)
                self.assertEqual(expected[0], 'TypeError')
                self.assertEqual(expected[2:], (['tick'], 'result:finished'))
                self.assertEqual(observe(True, case), expected)


if __name__ == '__main__':
    unittest.main()
