"""Security callbacks must also observe native loop objects and methods."""

import asyncio
import re
import unittest
import warnings
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
import jinja2.sandbox as sandbox_module
from docxtpl_native.bridge import BridgeConfig
from jinja2.runtime import AsyncLoopContext, LoopContext
from jinja2.sandbox import SandboxedEnvironment, SecurityError, unsafe
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate
from test_native_sandbox_call import native_sandbox_only


def _outcome(render: Callable[[], str]) -> tuple[str, str]:
    """The output of a render, or its exception type and message."""
    try:
        return ('output', render())
    except Exception as error:  # noqa: BLE001 - compared between renderers
        # Messages can name objects by address, as in a coroutine's repr.
        return (type(error).__name__, re.sub(r' at 0x[0-9a-f]+', '', str(error)))


class LoopPolicyTests(unittest.TestCase):
    def test_falsy_flag_complete_document_uses_native_policy(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            class Flag:
                def __bool__(self) -> bool:
                    events.append('flag')
                    return False

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for item in [1, 1] %}{{ loop.changed(item) }};{% endfor %}'
            with patch.object(LoopContext.changed, 'unsafe_callable', Flag(), create=True):
                if native:
                    with native_sandbox_only():
                        document = generate(docxtpl_native, source, {}, environment)
                else:
                    document = generate(docxtpl, source, {}, environment)
            return document, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, expected_events = observe(False, asynchronous)
                candidate, actual_events = observe(True, asynchronous)
                self.assertEqual(expected_events, ['flag', 'flag'])
                self.assertEqual(actual_events, expected_events)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_falsy_method_flag_truth_runs_after_arguments(self) -> None:
        def observe(native: bool, asynchronous: bool, method: str, flag: str) -> tuple[str, list[str]]:
            events: list[str] = []

            class Flag:
                def __bool__(self) -> bool:
                    events.append('flag')
                    return False

            def argument() -> int:
                events.append('argument')
                return 1

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for item in [1, 2] %}{{ loop.' + method + '(argument()) }};{% endfor %}'
            with patch.object(getattr(LoopContext, method), flag, Flag(), create=True):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'argument': argument}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(argument=argument)
            return output, events

        for asynchronous in (False, True):
            for method, flag, output in (('changed', 'unsafe_callable', 'True;False;'),
                                         ('cycle', 'alters_data', '1;1;')):
                with self.subTest(asynchronous=asynchronous, method=method):
                    expected = observe(False, asynchronous, method, flag)
                    self.assertEqual(expected, (output, ['argument', 'flag', 'argument', 'flag']))
                    self.assertEqual(observe(True, asynchronous, method, flag), expected)

    def test_first_flag_can_replace_second_flag(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Second:
                def __bool__(self) -> bool:
                    events.append('second')
                    return False

            class First:
                def __bool__(self) -> bool:
                    events.append('first')
                    setattr(LoopContext.changed, 'alters_data', Second())  # noqa: B010
                    return False

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for item in [1] %}{{ loop.changed(1) }}{% endfor %}'
            with (patch.object(LoopContext.changed, 'unsafe_callable', First(), create=True),
                  patch.object(LoopContext.changed, 'alters_data', None, create=True)):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('True', ['first', 'second']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_flag_can_replace_policy_getattr(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def replacement(obj: Any, name: str, default: Any = None) -> Any:
                events.append('getattr:' + name)
                return getattr(obj, name, default)

            class Flag:
                def __bool__(self) -> bool:
                    events.append('flag')
                    sandbox_module.__dict__['getattr'] = replacement
                    return False

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for item in [1] %}{{ loop.changed(item) }}{% endfor %}'
            with (patch.dict(sandbox_module.__dict__),
                  patch.object(LoopContext.changed, 'unsafe_callable', Flag(), create=True)):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('True', ['flag', 'getattr:alters_data']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_flag_finalizer_global_mutation_matches_reference(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    events: list[str] = []

                    def replacement(obj: Any, name: str, default: Any = None,
                                    trace: list[str] = events) -> Any:
                        trace.append('getattr:' + name)
                        return getattr(obj, name, default)

                    class Flag:
                        def __bool__(self) -> bool:
                            events.append('truth')  # noqa: B023
                            delattr(LoopContext.changed, 'unsafe_callable')
                            return False

                        def __del__(self) -> None:
                            events.append('drop')  # noqa: B023
                            sandbox_module.__dict__['getattr'] = replacement

                    environment = SandboxedEnvironment(enable_async=asynchronous)
                    source = '{% for item in [1] %}{{ loop.changed(item) }}{% endfor %}'
                    with patch.dict(sandbox_module.__dict__):
                        setattr(LoopContext.changed, 'unsafe_callable', Flag())  # noqa: B010
                        try:
                            if native:
                                session = docxtpl_native._extension().PythonRenderSession(
                                    BridgeConfig(environment, False), {}
                                )
                                # The sandbox's policy runs on the live loop's method.
                                self.assertEqual(session.render('case', source), 'True')
                                self.assertEqual(events, ['truth', 'drop', 'getattr:alters_data'])
                            else:
                                self.assertEqual(environment.from_string(source).render(), 'True')
                                self.assertEqual(events, ['truth', 'drop', 'getattr:alters_data'])
                        finally:
                            if hasattr(LoopContext.changed, 'unsafe_callable'):
                                delattr(LoopContext.changed, 'unsafe_callable')

    def test_flag_replacing_method_dict_with_unsafe_flag_matches_reference(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    method = LoopContext.changed
                    original = method.__dict__
                    missing = object()
                    previous = original.get('unsafe_callable', missing)
                    events: list[str] = []

                    class Flag:
                        def __bool__(self) -> bool:
                            events.append('truth')  # noqa: B023
                            method.__dict__ = {'alters_data': True}  # noqa: B023
                            return False

                    environment = SandboxedEnvironment(enable_async=asynchronous)
                    source = '{% for item in [1] %}{{ loop.changed(item) }}{% endfor %}'
                    try:
                        setattr(method, 'unsafe_callable', Flag())  # noqa: B010
                        with self.assertRaises(SecurityError):
                            if native:
                                docxtpl_native._extension().PythonRenderSession(
                                    BridgeConfig(environment, False), {}
                                ).render('case', source)
                            else:
                                environment.from_string(source).render()
                        self.assertEqual(events, ['truth'])
                    finally:
                        method.__dict__ = original
                        if previous is missing:
                            original.pop('unsafe_callable', None)
                        else:
                            original['unsafe_callable'] = previous

    def test_replaced_method_dict_finalizer_mutation_matches_reference(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[tuple[str, str], list[str]]:
                method = LoopContext.changed
                original = method.__dict__
                events: list[str] = []

                def replacement(obj: Any, name: str, default: Any = None) -> Any:
                    return getattr(obj, name, default)

                class Sentinel:
                    def __del__(self) -> None:
                        events.append('drop')
                        sandbox_module.__dict__['getattr'] = replacement

                class Flag:
                    def __bool__(self) -> bool:
                        events.append('truth')
                        method.__dict__ = {}
                        return False

                environment = SandboxedEnvironment(enable_async=asynchronous)
                with patch.dict(sandbox_module.__dict__):
                    try:
                        method.__dict__ = {'unsafe_callable': Flag(), 'sentinel': Sentinel()}
                        source = '{% for item in [1] %}{{ loop.changed(item) }}{% endfor %}'
                        if native:
                            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                            outcome = _outcome(lambda: session.render('case', source))
                        else:
                            outcome = _outcome(lambda: environment.from_string(source).render())
                        return outcome, events
                    finally:
                        method.__dict__ = original

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, (('output', 'True'), ['truth', 'drop']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_flag_mutating_loop_method_matches_reference(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[tuple[str, str], list[str]]:
            events: list[str] = []

            class Flag:
                def __bool__(self) -> bool:
                    events.append('flag')
                    setattr(LoopContext, 'changed', lambda self, *args: False)  # noqa: B010
                    return False

            original = LoopContext.changed
            source = '{% for item in [1] %}{{ loop.changed(1) }}{% endfor %}'
            try:
                with patch.object(original, 'unsafe_callable', Flag(), create=True):
                    environment = SandboxedEnvironment(enable_async=asynchronous)
                    if native:
                        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                        outcome = _outcome(lambda: session.render('case', source))
                    else:
                        outcome = _outcome(lambda: environment.from_string(source).render())
            finally:
                LoopContext.changed = original
            return outcome, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                # The bound method was taken before the flag replaced it.
                expected = observe(False, asynchronous)
                self.assertEqual(expected, (('output', 'True'), ['flag']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_falsy_method_flag_can_render_reentrantly(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            environment = SandboxedEnvironment()
            session = (docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), {'value': 'nested'}
            ) if native else None)

            class Flag:
                def __bool__(self) -> bool:
                    result = (session.render('nested', '{{ value }}') if session is not None
                              else environment.from_string('{{ value }}').render(value='nested'))
                    events.append(result)
                    return False

            source = '{% for item in [1] %}{{ loop.changed(item) }}{% endfor %}'
            with patch.object(LoopContext.changed, 'unsafe_callable', Flag(), create=True):
                output = (session.render('outer', source) if session is not None
                          else environment.from_string(source).render())
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('True', ['nested']))
        self.assertEqual(observe(True), expected)

    def test_falsy_standard_method_flags_allow_calls(self) -> None:
        for asynchronous in (False, True):
            for name, flag in (('changed', 'unsafe_callable'), ('cycle', 'alters_data')):
                for value in (False, None):
                    for item_lookup in (False, True):
                        with self.subTest(asynchronous=asynchronous, name=name, flag=flag,
                                          value=value, item_lookup=item_lookup):
                            method = getattr(LoopContext, name)
                            expression = f"loop['{name}'](1)" if item_lookup else f'loop.{name}(1)'
                            source = '{% for item in [1, 2] %}{{ ' + expression + ' }}{% endfor %}'
                            environment = SandboxedEnvironment(enable_async=asynchronous)
                            with patch.object(method, flag, value, create=True):
                                with warnings.catch_warnings():
                                    warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                                    expected = environment.from_string(source).render()
                                session = docxtpl_native._extension().PythonRenderSession(
                                    BridgeConfig(environment, False), {}
                                )
                                self.assertEqual(session.render('case', source), expected)

    def test_method_flag_truth_error_preserves_identity(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    marker = ValueError('flag error')
                    events: list[str] = []

                    class Flag:
                        def __bool__(self, trace: list[str] = events, error: ValueError = marker) -> bool:
                            trace.append('truthiness')
                            raise error

                    environment = SandboxedEnvironment(enable_async=asynchronous)
                    source = '{% for item in [1] %}{{ loop.changed(1) }}{% endfor %}'
                    with (patch.object(LoopContext.changed, 'unsafe_callable', Flag(), create=True),
                          self.assertRaises(ValueError) as error):
                        if native:
                            session = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            )
                            session.render('case', source)
                        else:
                            environment.from_string(source).render()
                    self.assertIs(error.exception, marker)
                    self.assertEqual(events, ['truthiness'])

    def test_falsy_first_flag_does_not_hide_unsafe_second_flag(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous), \
                 patch.object(LoopContext.changed, 'unsafe_callable', False, create=True), \
                 patch.object(LoopContext.changed, 'alters_data', True, create=True):
                environment = SandboxedEnvironment(enable_async=asynchronous)
                source = '{% for item in [1] %}{{ loop.changed(1) }}{% endfor %}'
                with warnings.catch_warnings():
                    warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                    with self.assertRaises(SecurityError):
                        environment.from_string(source).render()
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                with warnings.catch_warnings():
                    warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                    with self.assertRaises(SecurityError):
                        session.render('case', source)

    def test_argument_can_clear_method_flag_before_sandbox_call(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool, async_mode: bool = asynchronous) -> tuple[str, list[str]]:
                    events: list[str] = []

                    def clear() -> int:
                        events.append('argument')
                        setattr(LoopContext.changed, 'unsafe_callable', False)  # noqa: B010 - dynamic function attribute
                        return 1

                    environment = SandboxedEnvironment(enable_async=async_mode)
                    source = '{% for item in [1] %}{{ loop.changed(clear()) }}{% endfor %}'
                    with patch.object(LoopContext.changed, 'unsafe_callable', True, create=True):
                        if native:
                            session = docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {'clear': clear}
                            )
                            result = session.render('case', source)
                        else:
                            result = environment.from_string(source).render(clear=clear)
                    return result, events

                self.assertEqual(observe(True), observe(False))

    def observe(self, native: bool, asynchronous: bool, method: bool) -> tuple[Any, list[tuple[str, str]]]:
        events: list[tuple[str, str]] = []

        class Environment(SandboxedEnvironment):
            def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                events.append(('attribute', attr))
                return method

            def is_safe_callable(self, obj: Any) -> bool:
                events.append(('callable', getattr(obj, '__name__', type(obj).__name__)))
                return False

        environment = Environment(enable_async=asynchronous)
        expression = 'loop.changed(1)' if method else 'loop.index'
        source = '{% for item in [1] %}{{ ' + expression + ' }}{% endfor %}'
        try:
            if native:
                result: Any = ('ok', docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {}
                ).render('case', source))
            else:
                result = ('ok', environment.from_string(source).render())
        except Exception as error:  # noqa: BLE001 - compare policy exception and message
            message: Any = str(error)
            if type(error).__name__ == 'SecurityError':
                message = message.endswith(' is not safely callable')
            result = (type(error).__name__, message)
        return result, events

    def test_sandbox_policies_apply_to_loop(self) -> None:
        for asynchronous in (False, True):
            for method in (False, True):
                with self.subTest(asynchronous=asynchronous, method=method):
                    self.assertEqual(
                        self.observe(True, asynchronous, method),
                        self.observe(False, asynchronous, method),
                    )

    def test_live_loop_methods_keep_sandbox_policy(self) -> None:
        """A loop that crossed into Python is still called through the sandbox."""
        def keep(loop: Any) -> str:
            return ''

        original = LoopContext.__dict__['changed']
        setattr(LoopContext, 'changed', unsafe(lambda self, *value: True))  # noqa: B010 - temporary class mutation
        try:
            for method in ('changed', 'cycle'):
                with self.subTest(method=method):
                    environment = SandboxedEnvironment()
                    source = '{% for item in [1] %}{{ keep(loop) }}{{ loop.' + method + '(1) }}{% endfor %}'
                    expected = _outcome(lambda: environment.from_string(source).render(keep=keep))
                    self.assertEqual(expected[0], 'SecurityError' if method == 'changed' else 'output')
                    session = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'keep': keep})
                    self.assertEqual(_outcome(lambda: session.render('case', source)), expected)
        finally:
            setattr(LoopContext, 'changed', original)  # noqa: B010 - restore original method

    def test_custom_policy_applies_to_live_loop(self) -> None:
        class Environment(SandboxedEnvironment):
            def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
                return False

            def is_safe_callable(self, obj: Any) -> bool:
                return False

        for asynchronous in (False, True):
            for expression in ('loop.index', 'loop.changed(1)', "loop['index']", "loop['changed'](1)"):
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    environment = Environment(enable_async=asynchronous)
                    session = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    )
                    self.assertEqual(_outcome(lambda: session.render('case', '{% for item in [1] %}{{ ' + expression + ' }}{% endfor %}')),
                                     _outcome(lambda: environment.from_string('{% for item in [1] %}{{ ' + expression + ' }}{% endfor %}').render()))

    def test_in_place_policy_code_change_matches_reference(self) -> None:
        def replacement(self: Any, obj: Any, attr: str, value: Any) -> bool:
            return False

        original = SandboxedEnvironment.is_safe_attribute.__code__
        try:
            SandboxedEnvironment.is_safe_attribute.__code__ = replacement.__code__
            environment = SandboxedEnvironment()
            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            self.assertEqual(_outcome(lambda: session.render('case', '{% for item in [1] %}{{ loop.index }}{% endfor %}')),
                             _outcome(lambda: environment.from_string('{% for item in [1] %}{{ loop.index }}{% endfor %}').render()))
        finally:
            SandboxedEnvironment.is_safe_attribute.__code__ = original

    def test_custom_lookup_does_not_trigger_filtered_lookahead(self) -> None:
        def observe(native: bool) -> tuple[str, list[int]]:
            events: list[int] = []

            class Environment(SandboxedEnvironment):
                def getattr(self, obj: Any, attribute: str) -> Any:
                    return 'custom'

            def selected(value: int) -> bool:
                events.append(value)
                return True

            environment = Environment()
            environment.tests['selected'] = selected
            source = '{% for x in items if x is selected %}{{ loop.last }};{% endfor %}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'items': [1, 2, 3]}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(items=[1, 2, 3])
            return output, events

        expected = observe(False)
        self.assertEqual(expected, ('custom;custom;custom;', [1, 2, 3]))
        self.assertEqual(observe(True), expected)

    def test_standard_sandbox_blocks_marked_loop_method(self) -> None:
        for name, flag in (('changed', 'unsafe_callable'), ('cycle', 'alters_data')):
            method = getattr(LoopContext, name)
            missing = object()
            previous = getattr(method, flag, missing)
            setattr(method, flag, True)
            try:
                for asynchronous in (False, True):
                    for expression in ('loop.' + name + '(1)', "loop['" + name + "'](1)"):
                        with self.subTest(asynchronous=asynchronous, expression=expression, flag=flag):
                            environment = SandboxedEnvironment(enable_async=asynchronous)
                            source = '{% for item in [1] %}{{ ' + expression + ' }}{% endfor %}'
                            with warnings.catch_warnings():
                                warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                                with self.assertRaises(SecurityError):
                                    environment.from_string(source).render()
                            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                            with warnings.catch_warnings():
                                warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                                with self.assertRaises(SecurityError):
                                    session.render('case', source)
            finally:
                if previous is missing:
                    delattr(method, flag)
                else:
                    setattr(method, flag, previous)

    def test_policy_change_in_argument_is_checked_at_call_time(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    method = LoopContext.changed
                    missing = object()
                    previous = getattr(method, 'unsafe_callable', missing)
                    events: list[str] = []

                    def mark(trace: list[str] = events, target: Any = method) -> int:
                        trace.append('argument')
                        setattr(target, 'unsafe_callable', True)  # noqa: B010 - function attributes are dynamic
                        return 1

                    async def delayed(callback: Callable[[], int] = mark) -> int:
                        await asyncio.sleep(0)
                        return callback()

                    callback = delayed if asynchronous else mark
                    environment = SandboxedEnvironment(enable_async=asynchronous)
                    source = '{% for item in [1] %}{{ loop.changed(mark()) }}{% endfor %}'
                    try:
                        with warnings.catch_warnings():
                            warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                            with self.assertRaises(SecurityError):
                                if native:
                                    session = docxtpl_native._extension().PythonRenderSession(
                                        BridgeConfig(environment, False), {'mark': callback}
                                    )
                                    session.render('case', source)
                                else:
                                    environment.from_string(source).render(mark=callback)
                        self.assertEqual(events, ['argument'])
                    finally:
                        if previous is missing:
                            if hasattr(method, 'unsafe_callable'):
                                delattr(method, 'unsafe_callable')
                        else:
                            setattr(method, 'unsafe_callable', previous)  # noqa: B010 - restore dynamic function attribute

    def test_callable_policy_replaced_by_argument_matches_reference(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    environment = SandboxedEnvironment(enable_async=asynchronous)
                    events: list[str] = []

                    def change(env: Any = environment, trace: list[str] = events) -> int:
                        trace.append('argument')
                        env.is_safe_callable = lambda _: False
                        return 1

                    source = '{% for item in [1] %}{{ loop.changed(change()) }}{% endfor %}'
                    with warnings.catch_warnings():
                        warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                        with self.assertRaises(SecurityError):
                            if native:
                                session = docxtpl_native._extension().PythonRenderSession(
                                    BridgeConfig(environment, False), {'change': change}
                                )
                                session.render('case', source)
                            else:
                                environment.from_string(source).render(change=change)
                    self.assertEqual(events, ['argument'])

    def test_changed_sandbox_globals_match_reference(self) -> None:
        def deny_internal(obj: object, attribute: str) -> bool:
            return True

        def deny_callable(obj: object, name: str, default: object = None) -> object:
            return True if name == 'unsafe_callable' else getattr(obj, name, default)

        for asynchronous in (False, True):
            for mode in ('attribute', 'callable'):
                with self.subTest(asynchronous=asynchronous, mode=mode):
                    environment = SandboxedEnvironment(enable_async=asynchronous)
                    expression = 'loop.index' if mode == 'attribute' else 'loop.changed(1)'
                    source = '{% for item in [1] %}{{ ' + expression + ' }}{% endfor %}'
                    name = 'is_internal_attribute' if mode == 'attribute' else 'getattr'
                    replacement = deny_internal if mode == 'attribute' else deny_callable
                    with patch.object(sandbox_module, name, replacement, create=True):
                        with warnings.catch_warnings():
                            warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                            if mode == 'attribute':
                                self.assertEqual(environment.from_string(source).render(), '')
                            else:
                                with self.assertRaises(SecurityError):
                                    environment.from_string(source).render()
                        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                        self.assertEqual(_outcome(lambda: session.render('case', source)),
                                         _outcome(lambda: environment.from_string(source).render()))

    def test_overridden_async_loop_method_matches_reference(self) -> None:
        @unsafe
        def replacement(self: Any, *value: object) -> bool:
            return True

        missing = object()
        previous = AsyncLoopContext.__dict__.get('changed', missing)
        setattr(AsyncLoopContext, 'changed', replacement)  # noqa: B010 - temporary class mutation
        try:
            environment = SandboxedEnvironment(enable_async=True)
            source = '{% for item in [1] %}{{ loop.changed(1) }}{% endfor %}'
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', message="coroutine 'AsyncLoopContext.length' was never awaited")
                with self.assertRaises(SecurityError):
                    environment.from_string(source).render()
            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            self.assertEqual(_outcome(lambda: session.render('case', source)),
                             _outcome(lambda: environment.from_string(source).render()))
        finally:
            if previous is missing:
                delattr(AsyncLoopContext, 'changed')
            else:
                setattr(AsyncLoopContext, 'changed', previous)  # noqa: B010 - restore previous class attribute

    def test_changed_internal_type_registry_matches_reference(self) -> None:
        original_types = sandbox_module.types

        class ModifiedTypes:
            FunctionType = LoopContext

            def __getattr__(self, name: str) -> Any:
                return getattr(original_types, name)

        modified_types = ModifiedTypes()
        environment = SandboxedEnvironment()
        source = '{% for item in [1] %}{{ loop.index }}{% endfor %}'
        with patch.object(sandbox_module, 'types', modified_types), patch.object(
            sandbox_module, 'UNSAFE_FUNCTION_ATTRIBUTES', {'index'}
        ):
            self.assertEqual(environment.from_string(source).render(), '')
            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            self.assertEqual(_outcome(lambda: session.render('case', source)),
                             _outcome(lambda: environment.from_string(source).render()))

    def test_mutated_loop_class_descriptor_runs_as_in_reference(self) -> None:
        events: list[str] = []

        class Descriptor:
            def __get__(self, instance: object, owner: object) -> Any:
                events.append('descriptor')
                raise RuntimeError('descriptor executed')

        original = LoopContext.__dict__['changed']
        setattr(LoopContext, 'changed', Descriptor())  # noqa: B010 - temporary class mutation
        try:
            environment = SandboxedEnvironment()
            source = '{% for item in [1] %}{{ loop.changed(1) }}{% endfor %}'
            expected = (_outcome(lambda: environment.from_string(source).render()), events.copy())
            self.assertEqual(expected, (('RuntimeError', 'descriptor executed'), ['descriptor']))
            events.clear()
            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            self.assertEqual((_outcome(lambda: session.render('case', source)), events), expected)
        finally:
            setattr(LoopContext, 'changed', original)  # noqa: B010 - restore original descriptor

    def test_mutated_loop_property_does_not_silently_return_old_value(self) -> None:
        original = LoopContext.__dict__['index']
        setattr(LoopContext, 'index', property(lambda _: 99))  # noqa: B010 - temporary class mutation
        try:
            environment = SandboxedEnvironment()
            source = '{% for item in [1] %}{{ loop.index }}{% endfor %}'
            self.assertEqual(environment.from_string(source).render(), '99')
            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            self.assertEqual(_outcome(lambda: session.render('case', source)),
                             _outcome(lambda: environment.from_string(source).render()))
        finally:
            setattr(LoopContext, 'index', original)  # noqa: B010 - restore original property

    def test_in_place_loop_property_code_change_matches_reference(self) -> None:
        def replacement(self: Any) -> int:
            return 99

        function = LoopContext.__dict__['index'].fget
        original = function.__code__
        try:
            function.__code__ = replacement.__code__
            environment = SandboxedEnvironment()
            source = '{% for item in [1] %}{{ loop.index }}{% endfor %}'
            self.assertEqual(environment.from_string(source).render(), '99')
            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            self.assertEqual(_outcome(lambda: session.render('case', source)),
                             _outcome(lambda: environment.from_string(source).render()))
        finally:
            function.__code__ = original

    def test_normal_environment_loop_class_change_matches_reference(self) -> None:
        original = LoopContext.__dict__['index']
        setattr(LoopContext, 'index', property(lambda _: 99))  # noqa: B010 - temporary class mutation
        try:
            environment = jinja2.Environment()
            source = '{% for item in [1] %}{{ loop.index }}{% endfor %}'
            self.assertEqual(environment.from_string(source).render(), '99')
            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
            self.assertEqual(_outcome(lambda: session.render('case', source)),
                             _outcome(lambda: environment.from_string(source).render()))
        finally:
            setattr(LoopContext, 'index', original)  # noqa: B010 - restore original property

    def test_custom_sandbox_format_wrapper_matches_reference(self) -> None:
        class Environment(SandboxedEnvironment):
            def wrap_str_format(self, value: Any) -> Any:
                return 77

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                environment = Environment(enable_async=asynchronous)
                source = '{% for item in [1] %}{{ loop.index }}{% endfor %}'
                self.assertEqual(environment.from_string(source).render(), '77')
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                self.assertEqual(_outcome(lambda: session.render('case', source)),
                                 _outcome(lambda: environment.from_string(source).render()))

    def test_lookahead_policy_mutation_fails_closed(self) -> None:
        def observe(native: bool, filtered: bool, asynchronous: bool, item_access: bool) -> tuple[Any, list[int]]:
            environment = SandboxedEnvironment(extensions=['jinja2.ext.loopcontrols'], enable_async=asynchronous)
            events: list[int] = []

            def values() -> Any:
                for value in (1, 2):
                    events.append(value)
                    if value == 2:
                        setattr(environment, 'is_safe_attribute', lambda obj, attr, result: False)  # noqa: B010 - runtime policy mutation
                    yield value

            suffix = ' if item is selected' if filtered else ''
            environment.tests['selected'] = lambda value: True
            access = "loop['last']" if item_access else 'loop.last'
            source = '{% for item in items' + suffix + ' %}{{ ' + access + ' }}{% break %}{% endfor %}'
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'items': values()}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(items=values())
                result: Any = ('ok', output)
            except Exception as error:  # noqa: BLE001 - check rejection after lookahead
                result = (type(error).__name__, str(error))
            return result, events

        for asynchronous in (False, True):
            for filtered in (False, True):
                for item_access in (False, True):
                    with self.subTest(filtered=filtered, asynchronous=asynchronous, item_access=item_access):
                        expected = 'False' if asynchronous else ''
                        self.assertEqual(observe(False, filtered, asynchronous, item_access), (('ok', expected), [1, 2]))
                        result, events = observe(True, filtered, asynchronous, item_access)
                        if asynchronous:
                            self.assertEqual(result, ('ok', 'False'))
                        else:
                            self.assertEqual(result[0], 'UnsupportedFeature')
                        self.assertEqual(events, [1, 2])


if __name__ == '__main__':
    unittest.main()
