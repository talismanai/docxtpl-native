"""Native template frames in render tracebacks; enabling the probe again is a no-op."""

import gc
import unittest
from types import TracebackType
from typing import Any

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig


class TracebackProbeTests(unittest.TestCase):
    def test_nested_include_cleared_traceback_releases_values_during_collection(self) -> None:
        def observe(native: bool, asynchronous: bool) -> list[str]:
            events: list[str] = []
            context: dict[str, Any] = {}

            class Value:
                def __del__(self) -> None:
                    events.append('drop')

            @jinja2.pass_context
            def mutate(jinja_context: Any) -> str:
                jinja_context.parent['value'] = None
                context['value'] = None
                return ''

            def crash() -> str:
                raise RuntimeError('nested failure')

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'child': '{{ mutate() }}{{ crash() }}{{ value }}'}),
                enable_async=asynchronous,
            )
            context.update({'mutate': mutate, 'crash': crash, 'value': Value()})
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                    session._enable_traceback_probe()
                    session.render('parent', '{% include "child" %}{{ value }}')
                else:
                    environment.from_string('{% include "child" %}{{ value }}').render(context)
            except RuntimeError as error:
                events.append('caught')
                error.__traceback__ = None
                events.append('cleared')
                gc.collect()
                events.append('collected')
            else:
                raise AssertionError('expected callback failure')
            return events.copy()

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                gc_was_enabled = gc.isenabled()
                gc.disable()
                try:
                    expected = observe(False, asynchronous)
                    self.assertEqual(expected, ['caught', 'cleared', 'drop', 'collected'])
                    self.assertEqual(observe(True, asynchronous), expected)
                finally:
                    if gc_was_enabled:
                        gc.enable()

    def test_probe_preserves_nested_include_template_frames(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[tuple[str, int, bool]]]:
            marker = RuntimeError('nested failure')
            value = object()

            def crash() -> str:
                raise marker

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'child': 'inside\n{{ crash() }}{{ value }}'}),
                enable_async=asynchronous,
            )
            source = 'parent\nmiddle\n{% include "child" %}'
            context = {'crash': crash, 'value': value}
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                    session._enable_traceback_probe()
                    session.render('parent', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                frames: list[tuple[str, int, bool]] = []
                traceback: TracebackType | None = error.__traceback__
                while traceback is not None:
                    frame = traceback.tb_frame
                    if frame.f_code.co_filename == '<template>':
                        frames.append((frame.f_code.co_name, traceback.tb_lineno,
                                       frame.f_locals.get('value') is value))
                    traceback = traceback.tb_next
                return error is marker, frames
            raise AssertionError('expected callback failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, (True, [('top-level template code', 3, True),
                                                  ('top-level template code', 2, True)]))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_probe_preserves_exception_when_context_uses_reserved_name(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[tuple[str, bool]]]:
            marker = RuntimeError('boom')
            collision = object()

            def crash() -> str:
                raise marker

            environment = jinja2.Environment(enable_async=asynchronous)
            values = {'crash': crash, '__jinja_exception__': collision}
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values)
                    session._enable_traceback_probe()
                    session.render('traceback-context', '{{ crash() }}{{ __jinja_exception__ }}')
                else:
                    environment.from_string('{{ crash() }}{{ __jinja_exception__ }}').render(values)
            except RuntimeError as error:
                frames: list[tuple[str, bool]] = []
                traceback: TracebackType | None = error.__traceback__
                while traceback is not None:
                    frame = traceback.tb_frame
                    if frame.f_code.co_filename == '<template>':
                        frames.append((frame.f_code.co_name, '__jinja_exception__' in frame.f_locals))
                    traceback = traceback.tb_next
                return error is marker, frames
            raise AssertionError('expected callback failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, (True, [('top-level template code', False)]))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_probe_does_not_read_exception_traceback_through_user_getattribute(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[str]]:
            events: list[str] = []

            class Failure(RuntimeError):
                def __getattribute__(self, name: str) -> Any:
                    if name == '__traceback__':
                        events.append('traceback:get')
                    return super().__getattribute__(name)

            failure = Failure('boom')

            def crash() -> str:
                events.append('crash')
                raise failure

            environment = jinja2.Environment(enable_async=asynchronous)
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'crash': crash}
                    )
                    session._enable_traceback_probe()
                    session.render('traceback-context', '{{ crash() }}')
                else:
                    environment.from_string('{{ crash() }}').render(crash=crash)
            except Failure as error:
                events.append('caught')
                return error is failure, events
            raise AssertionError('expected callback failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, (True, ['crash', 'caught']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_cleared_traceback_releases_value_during_cycle_collection(self) -> None:
        def observe(native: bool, asynchronous: bool) -> list[str]:
            events: list[str] = []
            context: dict[str, Any] = {}

            class Value:
                def __del__(self) -> None:
                    events.append('drop')

            @jinja2.pass_context
            def mutate(jinja_context: Any) -> str:
                jinja_context.parent['value'] = None
                context['value'] = None
                return ''

            def crash() -> str:
                raise RuntimeError('boom')

            environment = jinja2.Environment(enable_async=asynchronous)
            context.update({'mutate': mutate, 'crash': crash, 'value': Value()})
            source = '{{mutate()}}{{crash()}}{{value}}'
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                    session._enable_traceback_probe()
                    session.render('traceback-context', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                events.append('caught')
                error.__traceback__ = None
                events.append('cleared')
                gc.collect()
                events.append('collected')
            else:
                raise AssertionError('expected callback failure')
            return events.copy()

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                gc_was_enabled = gc.isenabled()
                gc.disable()
                try:
                    expected = observe(False, asynchronous)
                    self.assertEqual(expected, ['caught', 'cleared', 'drop', 'collected'])
                    self.assertEqual(observe(True, asynchronous), expected)
                finally:
                    if gc_was_enabled:
                        gc.enable()

    def test_root_frame_preserves_exception_and_lexical_locals(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[tuple[str, int, bool, bool]]]:
            original = object()
            replacement = object()
            failure = RuntimeError('callback failed')

            @jinja2.pass_context
            def mutate(context: Any) -> str:
                context.vars['value'] = replacement
                return ''

            def crash() -> str:
                raise failure

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{mutate()}}\n{{crash()}}\n{{value}}'
            context = {'mutate': mutate, 'crash': crash, 'value': original}
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                    session._enable_traceback_probe()
                    session.render('traceback-context', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                frames: list[tuple[str, int, bool, bool]] = []
                traceback: TracebackType | None = error.__traceback__
                while traceback is not None:
                    frame = traceback.tb_frame
                    if frame.f_code.co_filename == '<template>':
                        frames.append((frame.f_code.co_name, traceback.tb_lineno,
                                       frame.f_locals.get('value') is original,
                                       frame.f_locals.get('value') is replacement))
                    traceback = traceback.tb_next
                return error is failure, frames
            raise AssertionError('expected callback failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_root_frame_retains_values_while_exception_is_handled(self) -> None:
        def observe(native: bool, asynchronous: bool) -> list[str]:
            events: list[str] = []
            context: dict[str, Any] = {}

            class Value:
                def __del__(self) -> None:
                    events.append('drop')

            @jinja2.pass_context
            def mutate(jinja_context: Any) -> str:
                jinja_context.parent['value'] = None
                context['value'] = None
                events.append('mutate')
                return ''

            def crash() -> str:
                events.append('crash')
                raise RuntimeError('boom')

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{mutate()}}{{crash()}}{{value}}'
            context.update({'mutate': mutate, 'crash': crash, 'value': Value()})
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                    session._enable_traceback_probe()
                    session.render('traceback-context', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                events.append('caught')
                error.__traceback__ = None
            else:
                raise AssertionError('expected callback failure')
            events.append('after')
            return events.copy()

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))


if __name__ == '__main__':
    unittest.main()
