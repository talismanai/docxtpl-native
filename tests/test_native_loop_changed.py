"""Differential checks for Python equality in native loop.changed."""

import sys
import unittest
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import runtime
from jinja2.runtime import LoopContext
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


class LoopChangedTests(unittest.TestCase):
    def test_sandbox_changed_retains_context_call_selected_before_comparison(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[Any, ...]:
            contexts: list[Any] = []
            events: list[str] = []

            @jinja2.pass_context
            def capture(context: Any) -> str:
                contexts[:] = [context]
                return ''

            class Item:
                def __eq__(self, other: object) -> bool:
                    def replaced(*args: Any, **kwargs: Any) -> str:
                        events.append('replacement')
                        return 'REPLACED'

                    contexts[0].call = replaced
                    raise StopIteration('stop')

            def undefined(hint: str) -> str:
                events.append('undefined')
                return 'HANDLED'

            environment = SandboxedEnvironment(enable_async=asynchronous)
            environment.undefined = undefined  # ty: ignore[invalid-assignment]
            source = ('{{ capture() }}{% for x in values %}'
                      '{{ loop.changed(x) }};{% endfor %}')
            values = {'values': [Item(), Item()], 'capture': capture}
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
                self.assertEqual(expected, ('True;HANDLED;', ['undefined']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_reentrant_changed_handler_failures_preserve_exception_context(self) -> None:
        def observe(native: bool, asynchronous: bool, invalid_target: bool) -> tuple[Any, ...]:
            retained: list[Any] = []
            events: list[str] = []
            marker = StopIteration('stop')
            failure = ValueError('undefined failed')

            class Item:
                def __eq__(self, other: object) -> bool:
                    result = retained[-1]()
                    if asynchronous:
                        try:
                            result.send(None)
                        except StopIteration:
                            pass
                        else:
                            raise AssertionError('macro unexpectedly suspended')
                    raise marker

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            def undefined(hint: str) -> str:
                events.append('undefined')
                raise failure

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.undefined = undefined  # ty: ignore[invalid-assignment]
            source = ('{% for x in values %}{% macro nested() %}ok{% endmacro %}'
                      '{{ hold(nested) }}{{ loop.changed(x) }}{% endfor %}')
            values = {'values': [Item(), Item()], 'hold': hold}
            with patch.dict(runtime.__dict__, {'StopIteration': 17} if invalid_target else {}):
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), values
                        ).render('case', source)
                    else:
                        environment.from_string(source).render(values)
                except Exception as error:  # noqa: BLE001 - compare compatibility boundary
                    return type(error).__name__, str(error), error is failure, error.__context__ is marker, events
            raise AssertionError('expected handler failure')

        for asynchronous in (False, True):
            for invalid_target in (False, True):
                with self.subTest(asynchronous=asynchronous, invalid_target=invalid_target):
                    self.assertEqual(observe(True, asynchronous, invalid_target),
                                     observe(False, asynchronous, invalid_target))

    def test_reentrant_changed_uses_live_context_exception_handler(self) -> None:
        def observe(native: bool, asynchronous: bool, custom_exception: bool, sandboxed: bool) -> tuple[Any, ...]:
            events: list[Any] = []
            retained: list[Any] = []

            class CustomStop(Exception):
                pass

            marker: Exception = CustomStop('stop') if custom_exception else StopIteration('stop')

            class Item:
                def __eq__(self, other: object) -> bool:
                    result = retained[-1]()
                    if asynchronous:
                        try:
                            result.send(None)
                        except StopIteration as done:
                            result = done.value
                        else:
                            raise AssertionError('macro unexpectedly suspended')
                    events.append(('nested', result))
                    raise marker

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            def undefined(hint: str) -> str:
                events.append(('undefined', hint, sys.exception() is marker))
                result = retained[-1]()
                if asynchronous:
                    try:
                        result.send(None)
                    except StopIteration as done:
                        result = done.value
                    else:
                        raise AssertionError('macro unexpectedly suspended')
                events.append(('undefined:nested', result, sys.exception() is marker))
                return 'HANDLED'

            environment = (SandboxedEnvironment if sandboxed else jinja2.Environment)(enable_async=asynchronous)
            environment.undefined = undefined  # ty: ignore[invalid-assignment]
            source = ('{% for x in values %}{% macro nested() %}ok{% endmacro %}'
                      '{{ hold(nested) }}{{ loop.changed(x) }};{% endfor %}')
            values = {'values': [Item(), Item()], 'hold': hold}
            replacements = {'StopIteration': CustomStop} if custom_exception else {}
            with patch.dict(runtime.__dict__, replacements):
                try:
                    if native:
                        output = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), values
                        ).render('case', source)
                    else:
                        output = environment.from_string(source).render(values)
                    return 'ok', output, events
                except Exception as error:  # noqa: BLE001 - compare compatibility boundary
                    return type(error).__name__, str(error), error is marker, events

        for asynchronous in (False, True):
            for custom_exception in (False, True):
                for sandboxed in (False, True):
                    with self.subTest(asynchronous=asynchronous, custom_exception=custom_exception, sandboxed=sandboxed):
                        expected = observe(False, asynchronous, custom_exception, sandboxed)
                        self.assertEqual(expected, ('ok', 'True;HANDLED;', [
                            ('nested', 'ok'),
                            ('undefined', 'value was undefined because a callable raised a StopIteration exception', True),
                            ('undefined:nested', 'ok', True),
                        ]))
                        self.assertEqual(observe(True, asynchronous, custom_exception, sandboxed), expected)

    def test_reentrant_comparison_iteration_errors_keep_boundary_and_cause(self) -> None:
        def observe(native: bool, asynchronous: bool, kind: type[Exception]) -> tuple[Any, ...]:
            retained: list[Any] = []
            events: list[str] = []
            marker = kind('comparison stopped')

            class Item:
                def __eq__(self, other: object) -> bool:
                    result = retained[-1]()
                    if asynchronous:
                        try:
                            result.send(None)
                        except StopIteration as done:
                            result = done.value
                        else:
                            raise AssertionError('macro unexpectedly suspended')
                    events.append('nested:' + result)
                    raise marker

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% for x in values %}{% macro nested() %}ok{% endmacro %}'
                      '{{ hold(nested) }}{{ loop.changed(x) }}{% endfor %}')
            values = {'values': [Item(), Item()], 'hold': hold}
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), values
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values)
            except Exception as error:  # noqa: BLE001 - compare generator boundary
                return type(error).__name__, str(error), error is marker, error.__cause__ is marker, events
            return 'ok', output, False, False, events

        for asynchronous in (False, True):
            for kind in (StopIteration, StopAsyncIteration):
                with self.subTest(asynchronous=asynchronous, kind=kind):
                    expected = observe(False, asynchronous, kind)
                    self.assertEqual(expected[-1], ['nested:ok'])
                    self.assertEqual(observe(True, asynchronous, kind), expected)

    def test_suspending_reentrant_macro_lets_outer_render_continue(self) -> None:
        def observe(native: bool) -> tuple[tuple[str, str], list[str]]:
            events: list[str] = []
            retained: list[Any] = []

            class Waiter:
                def __await__(self) -> Generator[str, None, str]:
                    events.append('pause')
                    yield 'tick'
                    return ''

            class Item:
                def __eq__(self, other: object) -> bool:
                    try:
                        events.append('yield:' + retained[-1]().send(None))
                    except Exception as error:  # noqa: BLE001 - compared between renderers
                        events.append('error:' + type(error).__name__)
                    return False

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            async def later() -> str:
                import asyncio
                await asyncio.sleep(0)
                return 'next'

            environment = jinja2.Environment(enable_async=True)
            source = ('{% for x in values %}{% macro nested() %}{{ wait() }}{% endmacro %}'
                      '{{ hold(nested) }}{{ loop.changed(x) }};{% endfor %}{{ later() }}')
            values = {'values': [Item(), Item()], 'hold': hold, 'wait': Waiter, 'later': later}
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), values).render('case', source)
                else:
                    output = environment.from_string(source).render(values)
                return ('ok', output), events
            except Exception as error:  # noqa: BLE001 - compared between renderers
                return (type(error).__name__, str(error)), events

        expected = observe(False)
        self.assertEqual(expected, (('ok', 'True;True;next'), ['pause', 'yield:tick']))
        self.assertEqual(observe(True), expected)

    def test_reentrant_comparison_truth_error_keeps_identity_and_session(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, str, list[str]]:
            events: list[str] = []
            retained: list[Any] = []
            marker = ValueError('truth failed')

            class Truth:
                def __bool__(self) -> bool:
                    events.append('bool')
                    result = retained[-1]()
                    if asynchronous:
                        try:
                            result.send(None)
                        except StopIteration as done:
                            result = done.value
                        else:
                            raise AssertionError('macro unexpectedly suspended')
                    events.append('nested:' + result)
                    raise marker

            class Item:
                def __eq__(self, other: object) -> Any:
                    events.append('eq')
                    return Truth()

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% for x in values %}{% macro nested() %}ok{% endmacro %}'
                      '{{ hold(nested) }}{{ loop.changed(x) }}{% endfor %}')
            values = {'values': [Item(), Item()], 'hold': hold, 'value': 'next'}
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values)
            try:
                if native:
                    session.render('first', source)
                else:
                    environment.from_string(source).render(values)
            except ValueError as error:
                identical = error is marker
            else:
                raise AssertionError('expected truth callback error')
            output = session.render('next', '{{ value }}') if native else environment.from_string('{{ value }}').render(values)
            return identical, output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = (True, 'next', ['eq', 'bool', 'nested:ok'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_comparison_truth_callback_can_reenter_macro(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[tuple[str, str], list[str]]:
            events: list[str] = []
            retained: list[Any] = []
            armed = True

            class Truth:
                def __bool__(self) -> bool:
                    nonlocal armed
                    events.append('bool')
                    if armed:
                        armed = False
                        result = retained[-1]()
                        if asynchronous:
                            try:
                                result.send(None)
                            except StopIteration as done:
                                result = done.value
                            else:
                                raise AssertionError('macro unexpectedly suspended')
                        events.append('nested:' + result)
                    return False

            class Item:
                def __eq__(self, other: object) -> Any:
                    events.append('eq')
                    return Truth()

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% for x in values %}{% macro nested() %}{{ loop.changed(9) }}{% endmacro %}'
                      '{{ hold(nested) }}{{ loop.changed(x) }};{% endfor %}')
            values = {'values': [Item(), Item()], 'hold': hold}
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), values
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values)
                return ('ok', output), events
            except Exception as error:  # noqa: BLE001 - compare compatibility boundary
                return (type(error).__name__, str(error)), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, (('ok', 'True;True;'), ['eq', 'bool', 'eq', 'bool', 'nested:True']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_reentrant_changed_macro_in_complete_document(self) -> None:
        source = ('{% for x in values %}{% macro nested() %}{{ loop.changed(9) }}{% endmacro %}'
                  '{{ hold(nested) }}{{ loop.changed(x) }};{% endfor %}')
        document = template(source)

        def observe(engine: Any, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []
            retained: list[Any] = []
            armed = True

            class Item:
                def __eq__(self, other: object) -> bool:
                    nonlocal armed
                    events.append('eq')
                    if armed:
                        armed = False
                        result = retained[-1]()
                        if asynchronous:
                            try:
                                result.send(None)
                            except StopIteration as done:
                                result = done.value
                            else:
                                raise AssertionError('macro unexpectedly suspended')
                        events.append('nested:' + result)
                    return False

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            output = generate(engine, source, {'values': [Item(), Item()], 'hold': hold},
                              environment, source=document)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, expected = observe(docxtpl, asynchronous)
                candidate, actual = observe(docxtpl_native, asynchronous)
                self.assertEqual(actual, expected)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_changed_value_finalizer_can_reenter_macro(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[tuple[str, str], list[str]]:
            events: list[str] = []
            retained: list[Any] = []
            compare_armed = True
            drop_armed = True

            def call_macro() -> str:
                result = retained[-1]()
                if asynchronous:
                    try:
                        result.send(None)
                    except StopIteration as done:
                        return done.value
                    raise AssertionError('macro unexpectedly suspended')
                return result

            class Transient:
                def __del__(self) -> None:
                    nonlocal drop_armed
                    events.append('drop')
                    if drop_armed:
                        drop_armed = False
                        try:
                            events.append('drop:nested:' + call_macro())
                        except Exception as error:  # noqa: BLE001 - compare destructor callback
                            events.append('drop:error:' + type(error).__name__)

            class Item:
                def __eq__(self, other: object) -> bool:
                    nonlocal compare_armed
                    events.append('eq')
                    if compare_armed:
                        compare_armed = False
                        events.append('nested:' + call_macro())
                    return False

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            def create() -> Transient:
                events.append('create')
                return Transient()

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% for x in values %}{% macro nested() %}{{ loop.changed(create()) }}{% endmacro %}'
                      '{{ hold(nested) }}{{ loop.changed(x) }};{% endfor %}')
            values = {'values': [Item(), Item()], 'hold': hold, 'create': create}
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), values
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values)
                return ('ok', output), events
            except Exception as error:  # noqa: BLE001 - compare compatibility boundary
                return (type(error).__name__, str(error)), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_suspended_reentrant_macro_does_not_affect_next_render(self) -> None:
        def observe(native: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []
            retained: list[Any] = []

            class Waiter:
                def __await__(self) -> Generator[str, None, str]:
                    events.append('pause')
                    try:
                        yield 'tick'
                    finally:
                        events.append('closed')
                    return ''

            class Item:
                def __eq__(self, other: object) -> bool:
                    retained[-1]().send(None)
                    return False

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            async def later() -> str:
                import asyncio
                await asyncio.sleep(0)
                return 'next'

            environment = jinja2.Environment(enable_async=True)
            source = ('{% for x in values %}{% macro nested() %}{{ wait() }}{% endmacro %}'
                      '{{ hold(nested) }}{{ loop.changed(x) }}{% endfor %}')
            values = {'values': [Item(), Item()], 'hold': hold, 'wait': Waiter, 'later': later}
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values)
                first = session.render('first', source)
                second = session.render('next', '{{ later() }}')
            else:
                first = environment.from_string(source).render(values)
                second = environment.from_string('{{ later() }}').render(values)
            retained.clear()
            return first, second, events

        expected = observe(False)
        self.assertEqual(expected, ('TrueTrue', 'next', ['pause', 'closed']))
        self.assertEqual(observe(True), expected)

    def test_reentrant_comparison_macro_can_suspend(self) -> None:
        def observe(native: bool) -> tuple[tuple[str, str], list[str]]:
            events: list[str] = []
            retained: list[Any] = []
            armed = True

            class Waiter:
                def __await__(self) -> Generator[str, None, str]:
                    events.append('pause')
                    yield 'tick'
                    events.append('resume')
                    return ''

            class Item:
                def __eq__(self, other: object) -> bool:
                    nonlocal armed
                    events.append('eq')
                    if armed:
                        armed = False
                        coroutine = retained[-1]()
                        events.append('yield:' + coroutine.send(None))
                        try:
                            coroutine.send(None)
                        except StopIteration as done:
                            events.append('nested:' + done.value)
                    return False

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            environment = jinja2.Environment(enable_async=True)
            source = ('{% for x in values %}{% macro nested() %}{{ wait() }}{{ loop.changed(9) }}{% endmacro %}'
                      '{{ hold(nested) }}{{ loop.changed(x) }};{% endfor %}')
            values = {'values': [Item(), Item()], 'hold': hold, 'wait': Waiter}
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), values
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values)
                return ('ok', output), events
            except Exception as error:  # noqa: BLE001 - compare compatibility boundary
                return (type(error).__name__, str(error)), events

        expected = observe(False)
        self.assertEqual(expected, (('ok', 'True;True;'), ['eq', 'pause', 'yield:tick', 'resume', 'eq', 'nested:True']))
        self.assertEqual(observe(True), expected)

    def test_comparison_callback_can_reenter_macro_calling_changed(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[tuple[str, str], list[str]]:
            events: list[str] = []
            retained: list[Any] = []
            armed = True

            class Transient:
                def __del__(self) -> None:
                    events.append('drop')

            class Item:
                def __eq__(self, other: object) -> bool:
                    nonlocal armed
                    events.append('eq')
                    if armed:
                        armed = False
                        result = retained[-1]()
                        if asynchronous:
                            try:
                                result.send(None)
                            except StopIteration as done:
                                result = done.value
                            else:
                                raise AssertionError('macro unexpectedly suspended')
                        events.append('nested:' + result)
                    return False

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            def create() -> Transient:
                events.append('create')
                return Transient()

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% for x in values %}{% macro nested() %}{{ loop.changed(create()) }}{% endmacro %}'
                      '{{ hold(nested) }}{{ loop.changed(x) }};{% endfor %}')
            values = {'values': [Item(), Item()], 'hold': hold, 'create': create}
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), values
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(values)
                return ('ok', output), events
            except Exception as error:  # noqa: BLE001 - compare compatibility boundary
                return (type(error).__name__, str(error)), events

        expected = (('ok', 'True;True;'), ['eq', 'create', 'eq', 'nested:True', 'drop'])
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_reentrant_comparison_macro_error_keeps_identity_and_session(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, str, list[str]]:
            events: list[str] = []
            retained: list[Any] = []
            marker = ValueError('nested macro failed')

            class Item:
                def __eq__(self, other: object) -> bool:
                    events.append('eq')
                    result = retained[-1]()
                    if asynchronous:
                        result.send(None)
                    return False

            def hold(value: Any) -> str:
                retained[:] = [value]
                return ''

            def explode() -> str:
                events.append('explode')
                raise marker

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% for x in values %}{% macro nested() %}{{ explode() }}{% endmacro %}'
                      '{{ hold(nested) }}{{ loop.changed(x) }}{% endfor %}')
            values = {'values': [Item(), Item()], 'hold': hold, 'explode': explode, 'value': 'next'}
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values)
            try:
                if native:
                    session.render('first', source)
                else:
                    environment.from_string(source).render(values)
            except ValueError as error:
                identical = error is marker
            else:
                raise AssertionError('expected nested macro error')
            output = session.render('next', '{{ value }}') if native else environment.from_string('{{ value }}').render(values)
            return identical, output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = (True, 'next', ['eq', 'explode'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def observe(self, native: bool, asynchronous: bool, fail: bool = False) -> tuple[Any, list[Any]]:
        events: list[Any] = []
        failure = RuntimeError("equality failed")

        class Value:
            def __init__(self, number: int) -> None:
                self.number = number

            def __eq__(self, other: object) -> bool:
                if not isinstance(other, Value):
                    return NotImplemented
                events.append((self.number, other.number))
                if fail:
                    raise failure
                return self.number == other.number

        repeated = Value(1)
        values = [repeated, repeated, Value(1), Value(2)]
        source = "{% for value in values %}{{ loop.changed(value) }};{% endfor %}"
        environment = jinja2.Environment(enable_async=asynchronous)
        try:
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {"values": values}
                ).render("case", source)
            else:
                output = environment.from_string(source).render(values=values)
            result: Any = ("ok", output)
        except Exception as error:  # noqa: BLE001 - compare the actual exception type and identity
            result = (type(error).__name__, str(error), error is failure)
        return result, events

    def test_equality_identity_and_exceptions(self) -> None:
        for asynchronous in (False, True):
            for fail in (False, True):
                with self.subTest(asynchronous=asynchronous, fail=fail):
                    self.assertEqual(
                        self.observe(True, asynchronous, fail),
                        self.observe(False, asynchronous, fail),
                    )

    def test_argument_count_and_other_loop_methods(self) -> None:
        sources = (
            "{% for value in values %}{{ loop.changed() }};{% endfor %}",
            "{% for value in values %}{{ loop.changed(value, value % 2) }};{% endfor %}",
            "{% for value in values %}{{ loop.cycle('a', 'b') }};{% endfor %}",
        )
        for asynchronous in (False, True):
            for source in sources:
                with self.subTest(asynchronous=asynchronous, source=source):
                    environment = jinja2.Environment(enable_async=asynchronous)
                    expected = environment.from_string(source).render(values=[1, 1, 2])
                    actual = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {"values": [1, 1, 2]}
                    ).render("case", source)
                    self.assertEqual(actual, expected)

    def test_native_loop_methods_do_not_run_python_implementations(self) -> None:
        forbidden = {LoopContext.changed.__code__, LoopContext.cycle.__code__}
        source = "{% for x in values %}{{ loop.changed(x) }}{{ loop.cycle('a', 'b') }}{% endfor %}"
        previous = sys.getprofile()

        def profile(frame: Any, event: str, _: Any) -> None:
            if event == "call" and frame.f_code in forbidden:
                raise AssertionError("Python loop algorithm executed")

        try:
            sys.setprofile(profile)
            for asynchronous in (False, True):
                environment = jinja2.Environment(enable_async=asynchronous)
                actual = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {"values": [1, 1]}
                ).render("case", source)
                self.assertEqual(actual, "TrueaFalseb")
        finally:
            sys.setprofile(previous)

    def test_comparison_result_uses_truth_protocol(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Truth:
                def __bool__(self) -> bool:
                    events.append("bool")
                    return True

            class Value:
                def __eq__(self, other: object) -> Any:
                    events.append("eq")
                    return Truth()

            environment = jinja2.Environment(enable_async=asynchronous)
            source = "{% for x in values %}{{ loop.changed(x) }};{% endfor %}"
            values = [Value(), Value()]
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {"values": values}
                ).render("case", source)
            else:
                output = environment.from_string(source).render(values=values)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))


if __name__ == "__main__":
    unittest.main()
