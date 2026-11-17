"""The standard Context.get_exported body, without method dispatch."""

import sys
import unittest
import weakref
from typing import Any

import docxtpl_native
import jinja2


def run_body(native: bool, context: jinja2.runtime.Context) -> Any:
    if native:
        return docxtpl_native._extension().python_context_exported_body(context)
    return jinja2.runtime.Context.get_exported(context)


class ContextExportBodyTests(unittest.TestCase):
    def test_mapping_callback_observes_export_iterator_reference_count(self) -> None:
        def observe(native: bool) -> list[int]:
            events: list[int] = []
            iterator_ref: Any = None

            class Iterator:
                def __init__(self) -> None:
                    self.done = False

                def __iter__(self) -> Any:
                    return self

                def __next__(self) -> str:
                    if self.done:
                        raise StopIteration
                    self.done = True
                    return 'value'

            class Exports:
                def __iter__(self) -> Any:
                    nonlocal iterator_ref
                    iterator = Iterator()
                    iterator_ref = weakref.ref(iterator)
                    return iterator

            class Variables(dict[str, object]):
                def __getitem__(self, key: str) -> object:
                    events.append(sys.getrefcount(iterator_ref()))
                    return super().__getitem__(key)

            context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})
            context.vars = Variables(value='ok')
            context.exported_vars = Exports()  # type: ignore[assignment]
            run_body(native, context)
            return events

        self.assertEqual(observe(True), observe(False))

    def test_iterator_reference_count_during_next(self) -> None:
        def observe(native: bool) -> list[int]:
            events: list[int] = []

            class Iterator:
                def __init__(self) -> None:
                    self.done = False

                def __iter__(self) -> Any:
                    return self

                def __next__(self) -> str:
                    events.append(sys.getrefcount(self))
                    if self.done:
                        raise StopIteration
                    self.done = True
                    return 'value'

            class Exports:
                def __iter__(self) -> Any:
                    return Iterator()

            context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})
            context.vars['value'] = 'ok'
            context.exported_vars = Exports()  # type: ignore[assignment]
            run_body(native, context)
            return events

        self.assertEqual(observe(True), observe(False))

    def test_attribute_error_retains_active_key_until_traceback_release(self) -> None:
        def observe(native: bool) -> list[str]:
            events: list[str] = []

            class Key(str):
                def __del__(self) -> None:
                    events.append('destroyed')

            class Exports:
                def __iter__(self) -> Any:
                    yield Key('value')

            class Context(jinja2.runtime.Context):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'vars':
                        raise ValueError('vars failed')
                    return super().__getattribute__(name)

            context = Context(jinja2.Environment(), {}, None, {})
            context.exported_vars = Exports()  # type: ignore[assignment]
            retained: list[BaseException] = []
            try:
                run_body(native, context)
            except ValueError as error:
                retained.append(error)
                events.append('caught')
            events.append('retained')
            retained.clear()
            events.append('released')
            return events

        self.assertEqual(observe(True), observe(False))

    def test_mapping_callback_observes_reference_frame(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            frames: list[tuple[str, list[str]]] = []

            class Variables(dict[str, object]):
                def __getitem__(self, key: str) -> object:
                    frame = sys._getframe(1)
                    frames.append((frame.f_code.co_name, sorted(frame.f_locals)))
                    return super().__getitem__(key)

            context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})
            context.vars = Variables(value='ok')
            context.exported_vars.add('value')
            run_body(native, context)
            return frames[0]

        self.assertEqual(observe(True), observe(False))

    def test_mapping_error_retains_iterator_until_traceback_release(self) -> None:
        def observe(native: bool) -> list[str]:
            events: list[str] = []

            class Iterator:
                def __iter__(self) -> Any:
                    return self

                def __next__(self) -> str:
                    return 'value'

                def __del__(self) -> None:
                    events.append('destroyed')

            class Exports:
                def __iter__(self) -> Any:
                    return Iterator()

            class Variables(dict[str, object]):
                def __getitem__(self, key: str) -> object:
                    raise ValueError('lookup failed')

            context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})
            context.exported_vars = Exports()  # type: ignore[assignment]
            context.vars = Variables()
            retained: list[BaseException] = []
            try:
                run_body(native, context)
            except ValueError as error:
                retained.append(error)
                events.append('caught')
            events.append('retained')
            retained.clear()
            events.append('released')
            return events

        self.assertEqual(observe(True), observe(False))

    def test_failed_iteration_retains_iterator_until_traceback_release(self) -> None:
        def observe(native: bool) -> list[str]:
            events: list[str] = []

            class Iterator:
                def __iter__(self) -> Any:
                    return self

                def __next__(self) -> str:
                    raise ValueError('stop')

                def __del__(self) -> None:
                    events.append('destroyed')

            class Exports:
                def __iter__(self) -> Any:
                    return Iterator()

            context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})
            context.exported_vars = Exports()  # type: ignore[assignment]
            retained: list[BaseException] = []
            try:
                run_body(native, context)
            except ValueError as error:
                retained.append(error)
                events.append('caught')
            events.append('retained')
            retained.clear()
            events.append('released')
            return events

        self.assertEqual(observe(True), observe(False))

    def test_empty_exports_do_not_read_vars(self) -> None:
        def observe(native: bool) -> tuple[dict[str, object], list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'vars':
                        events.append('vars')
                    return super().__getattribute__(name)

            context = Context(jinja2.Environment(), {}, None, {})
            events.clear()
            return run_body(native, context), events

        self.assertEqual(observe(True), observe(False))

    def test_export_iterator_is_retained_if_attribute_is_replaced(self) -> None:
        def observe(native: bool) -> tuple[dict[str, object], list[str]]:
            events: list[str] = []
            context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})

            class Variables(dict[str, object]):
                def __getitem__(self, key: str) -> object:
                    events.append(key)
                    context.exported_vars = ['different']  # type: ignore[assignment]
                    return super().__getitem__(key)

            context.vars = Variables(first=1, second=2, different=3)
            context.exported_vars = ['first', 'second']  # type: ignore[assignment]
            return run_body(native, context), events

        self.assertEqual(observe(True), observe(False))

    def test_context_reference_count_during_mapping_lookup(self) -> None:
        def observe(native: bool) -> list[int]:
            events: list[int] = []
            context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})

            class Variables(dict[str, object]):
                def __getitem__(self, key: str) -> object:
                    events.append(sys.getrefcount(context))
                    return super().__getitem__(key)

            context.vars = Variables(value='ok')
            context.exported_vars.add('value')
            run_body(native, context)
            return events

        self.assertEqual(observe(True), observe(False))

    def test_key_hash_and_mapping_callbacks_follow_evaluation_order(self) -> None:
        def observe(native: bool) -> tuple[dict[str, object], list[str]]:
            events: list[str] = []

            class Key(str):
                def __hash__(self) -> int:
                    events.append('hash')
                    return super().__hash__()

            class Variables(dict[str, object]):
                def __getitem__(self, key: str) -> object:
                    events.append('getitem')
                    return super().__getitem__(key)

            context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})
            context.vars = Variables(value='ok')
            context.exported_vars = [Key('value')]  # type: ignore[assignment]
            result = run_body(native, context)
            return result, events

        self.assertEqual(observe(True), observe(False))

    def test_each_lookup_observes_replaced_vars(self) -> None:
        def observe(native: bool) -> tuple[dict[str, object], list[str]]:
            events: list[str] = []
            context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})

            class Variables(dict[str, object]):
                def __getitem__(self, key: str) -> object:
                    events.append('first:' + key)
                    context.vars = {'second': 'replacement'}
                    return super().__getitem__(key)

            context.vars = Variables(first='original', second='stale')
            context.exported_vars = ['first', 'second']  # type: ignore[assignment]
            return run_body(native, context), events

        self.assertEqual(observe(True), observe(False))

    def test_iterator_failure_propagates_without_reading_vars(self) -> None:
        def observe(native: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            failure = ValueError('iterator failed')

            class Exports:
                def __iter__(self) -> Any:
                    events.append('iter')
                    raise failure

            class Context(jinja2.runtime.Context):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'vars':
                        events.append('vars')
                    return super().__getattribute__(name)

            context = Context(jinja2.Environment(), {}, None, {})
            context.exported_vars = Exports()  # type: ignore[assignment]
            events.clear()
            try:
                run_body(native, context)
            except ValueError as error:
                return error is failure, events
            return False, events

        self.assertEqual(observe(True), observe(False))

    def test_key_reference_count_during_mapping_lookup(self) -> None:
        def observe(native: bool) -> list[int]:
            events: list[int] = []

            class Key(str):
                pass

            key = Key('value')

            class Variables(dict[str, object]):
                def __getitem__(self, item: str) -> object:
                    events.append(sys.getrefcount(key))
                    return super().__getitem__(item)

            context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})
            context.vars = Variables(value='ok')
            context.exported_vars = [key]  # type: ignore[assignment]
            run_body(native, context)
            return events

        self.assertEqual(observe(True), observe(False))

    def test_live_mapping_and_iteration_order(self) -> None:
        def observe(native: bool) -> tuple[list[tuple[str, object]], list[str], list[bool]]:
            events: list[str] = []
            first, second = object(), object()

            class Variables(dict[str, object]):
                def __getitem__(self, key: str) -> object:
                    events.append('getitem:' + key)
                    return super().__getitem__(key)

            class Exported:
                def __iter__(self) -> Any:
                    events.append('iter')
                    yield 'second'
                    events.append('next')
                    yield 'first'

            class Context(jinja2.runtime.Context):
                def __getattribute__(self, name: str) -> Any:
                    if name in ('exported_vars', 'vars'):
                        events.append('attr:' + name)
                    return super().__getattribute__(name)

            context = Context(jinja2.Environment(), {}, None, {})
            context.vars = Variables(first=first, second=second)
            context.exported_vars = Exported()  # type: ignore[assignment]
            events.clear()
            result = run_body(native, context)
            return list(result.items()), events, [result['first'] is first, result['second'] is second]

        actual, actual_events, actual_identity = observe(True)
        expected, expected_events, expected_identity = observe(False)
        self.assertEqual([name for name, _ in actual], [name for name, _ in expected])
        self.assertEqual(actual_events, expected_events)
        self.assertEqual(actual_identity, expected_identity)

    def test_exception_identity_and_later_access(self) -> None:
        def observe(native: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            failure = LookupError('same exception')

            class Variables(dict[str, object]):
                def __getitem__(self, key: str) -> object:
                    events.append(key)
                    if key == 'broken':
                        raise failure
                    return super().__getitem__(key)

            context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})
            context.vars = Variables(first=object())
            context.exported_vars = ['first', 'broken', 'later']  # type: ignore[assignment]
            try:
                run_body(native, context)
            except LookupError as error:
                return error is failure, events
            return False, events

        self.assertEqual(observe(True), observe(False))

    def test_key_error_is_not_suppressed(self) -> None:
        context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})
        context.exported_vars.add('missing')
        for native in (False, True):
            with self.subTest(native=native), self.assertRaises(KeyError) as error:
                run_body(native, context)
            self.assertEqual(error.exception.args, ('missing',))

    def test_default_body_is_not_executed_in_python(self) -> None:
        context = jinja2.runtime.Context(jinja2.Environment(), {}, None, {})
        context.vars['value'] = 'ok'
        context.exported_vars.add('value')
        code = jinja2.runtime.Context.get_exported.__code__
        calls: list[str] = []
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: object) -> None:
            if event == 'call' and frame.f_code is code:
                calls.append('get_exported')

        try:
            sys.setprofile(profile)
            self.assertEqual(run_body(False, context), {'value': 'ok'})
            self.assertEqual(calls, ['get_exported'])
            calls.clear()
            self.assertEqual(run_body(True, context), {'value': 'ok'})
        finally:
            sys.setprofile(previous)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
