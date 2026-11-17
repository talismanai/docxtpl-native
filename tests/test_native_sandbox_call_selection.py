"""Jinja selects sandbox call dispatch while compiling each call site."""

import asyncio
import itertools
import unittest
from types import SimpleNamespace
from typing import Any

import docxtpl
import docxtpl_native
from jinja2 import Environment, pass_context
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template
from test_native_filter_evaluation import render


class SandboxCallSelectionTests(unittest.TestCase):
    def test_runtime_item_dispatch_is_captured_before_key(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            environment = SandboxedEnvironment(enable_async=asynchronous)

            def replacement(obj: Any, argument: Any) -> str:
                events.append('replacement')
                return 'changed'

            def key() -> str:
                events.append('key')
                environment.getitem = replacement
                return 'value'

            return render('{{ mapping[key()] }}', {'mapping': {'value': 'original'}, 'key': key},
                          native, environment), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(run(True, asynchronous), run(False, asynchronous))

    def test_runtime_item_dispatch_is_captured_before_receiver(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            environment = SandboxedEnvironment()

            def replacement(obj: Any, argument: Any) -> str:
                events.append('replacement')
                return 'changed'

            def receiver() -> dict[str, str]:
                events.append('receiver')
                environment.getitem = replacement
                return {'value': 'original'}

            return render('{{ receiver()["value"] }}', {'receiver': receiver}, native, environment), events

        self.assertEqual(run(True), run(False))

    def test_runtime_item_dispatch_survives_async_key(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            environment = SandboxedEnvironment(enable_async=True)

            def replacement(obj: Any, argument: Any) -> str:
                events.append('replacement')
                return 'changed'

            async def key() -> str:
                await asyncio.sleep(0)
                events.append('key')
                environment.getitem = replacement
                return 'value'

            return render('{{ mapping[key()] }}', {'mapping': {'value': 'original'}, 'key': key},
                          native, environment), events

        self.assertEqual(run(True), run(False))

    def test_runtime_item_lookup_error_precedes_operands(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            armed = False

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'getitem' and armed:
                        events.append('lookup')
                        raise ValueError('item dispatcher')
                    return super().__getattribute__(name)

            def arm() -> str:
                nonlocal armed
                armed = True
                return ''

            def key() -> str:
                events.append('key')
                return 'value'

            try:
                render('{{ arm() }}{{ mapping[key()] }}', {'arm': arm, 'mapping': {'value': 1}, 'key': key},
                       native, Sandbox())
            except ValueError as error:
                return str(error), events
            self.fail('expected dispatcher error')

        self.assertEqual(run(True), run(False))

    def test_runtime_nested_item_dispatcher_lifetime(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            events: list[object] = []
            counter = 0

            class Lookup:
                def __init__(self, callback: Any, number: int) -> None:
                    self.callback = callback
                    self.number = number

                def __call__(self, *args: Any, **kwargs: Any) -> Any:
                    events.append(('call', self.number))
                    return self.callback(*args, **kwargs)

                def __del__(self) -> None:
                    events.append(('drop', self.number))

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    nonlocal counter
                    method = super().__getattribute__(name)
                    if name == 'getitem':
                        counter += 1
                        events.append(('lookup', counter))
                        return Lookup(method, counter)
                    return method

            output = render('{{ mapping["inner"]["value"] }}',
                            {'mapping': {'inner': {'value': 'original'}}}, native, Sandbox())
            return output, events

        self.assertEqual(run(True), run(False))

    def test_runtime_item_capture_in_complete_document(self) -> None:
        source = '{{ mapping[key()] }}'
        document = template(source)

        def run(engine: Any) -> bytes:
            environment = SandboxedEnvironment()

            def replacement(obj: Any, argument: Any) -> str:
                return 'changed'

            def key() -> str:
                environment.getitem = replacement
                return 'value'

            return generate(engine, source, {'mapping': {'value': 'original'}, 'key': key},
                            environment, source=document)

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_runtime_attribute_dispatch_is_captured_before_receiver(self) -> None:
        def run(native: bool, asynchronous: bool, source: str) -> tuple[str, list[object]]:
            events: list[object] = []
            environment = SandboxedEnvironment(enable_async=asynchronous)

            def replacement(obj: Any, attribute: str) -> str:
                events.append(('replacement', attribute))
                return 'changed'

            class Holder:
                @property
                def inner(self) -> SimpleNamespace:
                    events.append('inner')
                    environment.getattr = replacement
                    return SimpleNamespace(value='original', method=lambda: 'original')

            return render(source, {'holder': Holder()}, native, environment), events

        for asynchronous in (False, True):
            for source in ('{{ holder.inner.value }}', '{{ holder.inner.method() }}'):
                with self.subTest(asynchronous=asynchronous, source=source):
                    self.assertEqual(run(True, asynchronous, source), run(False, asynchronous, source))

    def test_runtime_attribute_lookup_error_precedes_receiver(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            armed = False

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'getattr' and armed:
                        events.append('lookup')
                        raise ValueError('attribute dispatcher')
                    return super().__getattribute__(name)

            class Holder:
                @property
                def inner(self) -> object:
                    events.append('receiver')
                    return SimpleNamespace(value='unused')

            def arm() -> str:
                nonlocal armed
                armed = True
                return ''

            try:
                render('{{ arm() }}{{ holder.inner.value }}', {'arm': arm, 'holder': Holder()}, native, Sandbox())
            except ValueError as error:
                return str(error), events
            self.fail('expected dispatcher error')

        self.assertEqual(run(True), run(False))

    def test_runtime_attribute_dispatch_survives_async_receiver(self) -> None:
        def run(native: bool, source: str) -> tuple[str, list[str]]:
            events: list[str] = []
            environment = SandboxedEnvironment(enable_async=True)

            def replacement(obj: Any, attribute: str) -> str:
                events.append('replacement')
                return 'changed'

            async def receiver() -> SimpleNamespace:
                await asyncio.sleep(0)
                events.append('receiver')
                environment.getattr = replacement
                return SimpleNamespace(value='original', method=lambda: 'original')

            return render(source, {'receiver': receiver}, native, environment), events

        for source in ('{{ receiver().value }}', '{{ receiver().method() }}'):
            with self.subTest(source=source):
                self.assertEqual(run(True, source), run(False, source))

    def test_runtime_nested_attribute_dispatcher_lifetime(self) -> None:
        def run(native: bool) -> tuple[str, list[object]]:
            events: list[object] = []
            counter = 0

            class Lookup:
                def __init__(self, callback: Any, number: int) -> None:
                    self.callback = callback
                    self.number = number

                def __call__(self, *args: Any, **kwargs: Any) -> Any:
                    events.append(('call', self.number))
                    return self.callback(*args, **kwargs)

                def __del__(self) -> None:
                    events.append(('drop', self.number))

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    nonlocal counter
                    method = super().__getattribute__(name)
                    if name == 'getattr':
                        counter += 1
                        events.append(('lookup', counter))
                        return Lookup(method, counter)
                    return method

            output = render('{{ capture(holder.inner.value + (999 + 1)) }}',
                            {'capture': str, 'holder': SimpleNamespace(inner=SimpleNamespace(value=2))},
                            native, Sandbox())
            return output, events

        self.assertEqual(run(True), run(False))

    def test_runtime_attribute_capture_in_complete_document(self) -> None:
        source = '{{ holder.inner.value }}'
        document = template(source)

        def run(engine: Any) -> bytes:
            environment = SandboxedEnvironment()

            def replacement(obj: Any, attribute: str) -> str:
                return 'changed'

            class Holder:
                @property
                def inner(self) -> SimpleNamespace:
                    environment.getattr = replacement
                    return SimpleNamespace(value='original')

            return generate(engine, source, {'holder': Holder()}, environment, source=document)

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_optimized_nested_policy_sequences(self) -> None:
        def run(native: bool, source: str, policy: tuple[bool, ...], asynchronous: bool) -> tuple[object, ...]:
            events: list[object] = []
            decisions = iter(policy)

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name in ('intercepted_binops', 'intercepted_unops'):
                        intercepted = next(decisions, False)
                        events.append((name, intercepted))
                        return frozenset({'+', '-', '*'}) if intercepted else frozenset()
                    if name in ('getattr', 'getitem'):
                        events.append(('lookup', name))
                    return super().__getattribute__(name)

                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    events.append(('binop', operator, left, right))
                    return super().call_binop(context, operator, left, right) + 10

                def call_unop(self, context: Any, operator: str, arg: Any) -> Any:
                    events.append(('unop', operator, arg))
                    return super().call_unop(context, operator, arg) + 10

            try:
                output = render('{{ capture(' + source + ') }}', {'capture': str, 'value': 2,
                                'holder': SimpleNamespace(value=2, inner=SimpleNamespace(value=2)),
                                'mapping': {'value': 2, 'inner': {'value': 2}, 1: 2, 11: 2}}, native,
                                Sandbox(enable_async=asynchronous))
                return 'ok', output, events
            except ZeroDivisionError as error:
                return type(error).__name__, str(error), events

        expressions = ('(999 + 1) * 2', '-(999 + 1)', '(999 + 1) * (2 + 3)', '(999 / 0) + 1',
                       'value + (999 + 1)', '(999 + 1) * value', '-(value + (999 + 1))',
                       '(value + 1) * (2 + 3)', 'holder.value + (999 + 1)',
                       '-(holder.value + (999 + 1))', '(999 + 1) + holder.value',
                       'holder.inner.value', 'mapping["value"] + (999 + 1)',
                       '-(mapping["value"] + (999 + 1))', '(999 + 1) + mapping["value"]',
                       'mapping["inner"]["value"]', 'mapping[1 + 0] + (999 + 1)',
                       'mapping[value - 1] + (999 + 1)')
        for source in expressions:
            for policy in itertools.product((False, True), repeat=6):
                for asynchronous in (False, True):
                    with self.subTest(source=source, policy=policy, asynchronous=asynchronous):
                        self.assertEqual(run(True, source, policy, asynchronous),
                                         run(False, source, policy, asynchronous))

    def test_optimized_nested_policy_errors_preserve_visit_order(self) -> None:
        def run(native: bool, fail_at: int, source: str) -> tuple[object, ...]:
            events: list[str] = []

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name in ('intercepted_binops', 'intercepted_unops'):
                        events.append(name)
                        if len(events) == fail_at:
                            raise ValueError('policy lookup')
                        return frozenset({'+', '*'})
                    return super().__getattribute__(name)

            try:
                output = render('{{ capture(' + source + ') }}', {'capture': str, 'value': 2}, native, Sandbox())
                return 'ok', output, events
            except ValueError as error:
                return type(error).__name__, str(error), events

        for source in ('(999 + 1) * (2 + 3)', 'value + (999 + 1)'):
            for fail_at in range(1, 11):
                with self.subTest(fail_at=fail_at, source=source):
                    self.assertEqual(run(True, fail_at, source), run(False, fail_at, source))

    def test_optimized_nested_mutable_policy_in_complete_document(self) -> None:
        source = '{{ capture((999 + 1) * 2) }}'
        document = template(source)

        def run(engine: Any) -> bytes:
            decisions = iter((True, False, True, False, True, True))

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'intercepted_binops':
                        return frozenset({'+', '*'}) if next(decisions, False) else frozenset()
                    return super().__getattribute__(name)

                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    return super().call_binop(context, operator, left, right) + 10

            return generate(engine, source, {'capture': str}, Sandbox(), source=document)

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_optimized_mixed_mutable_policy_in_complete_document(self) -> None:
        source = '{{ capture(value + (999 + 1)) }}'
        document = template(source)

        def run(engine: Any) -> bytes:
            decisions = iter((False, False, False, True, False, False))

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'intercepted_binops':
                        return frozenset({'+'}) if next(decisions, False) else frozenset()
                    return super().__getattribute__(name)

                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    return super().call_binop(context, operator, left, right) + 10

            return generate(engine, source, {'capture': str, 'value': 2}, Sandbox(), source=document)

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_optimized_attribute_mutable_policy_in_complete_document(self) -> None:
        source = '{{ capture(holder.value + (999 + 1)) }}'
        document = template(source)

        def run(engine: Any) -> bytes:
            decisions = iter((False, False, False, True, False, False))

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'intercepted_binops':
                        return frozenset({'+'}) if next(decisions, False) else frozenset()
                    return super().__getattribute__(name)

                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    return super().call_binop(context, operator, left, right) + 10

            return generate(engine, source, {'capture': str, 'holder': SimpleNamespace(value=2)},
                            Sandbox(), source=document)

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_optimized_item_mutable_policy_in_complete_document(self) -> None:
        source = '{{ capture(mapping["value"] + (999 + 1)) }}'
        document = template(source)

        def run(engine: Any) -> bytes:
            decisions = iter((False, False, False, True, False, False))

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'intercepted_binops':
                        return frozenset({'+'}) if next(decisions, False) else frozenset()
                    return super().__getattribute__(name)

                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    return super().call_binop(context, operator, left, right) + 10

            return generate(engine, source, {'capture': str, 'mapping': {'value': 2}}, Sandbox(), source=document)

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_optimized_mixed_expression_preserves_user_operator(self) -> None:
        def run(native: bool, fail: bool) -> tuple[object, ...]:
            events: list[object] = []

            class Number:
                def __add__(self, other: object) -> int:
                    events.append(('add', other))
                    if fail:
                        raise ValueError('user add')
                    return 17

            try:
                output = render('{{ capture(value + (999 + 1)) }}', {'capture': str, 'value': Number()}, native,
                                SandboxedEnvironment())
                return 'ok', output, events
            except ValueError as error:
                return type(error).__name__, str(error), events

        for fail in (False, True):
            with self.subTest(fail=fail):
                self.assertEqual(run(True, fail), run(False, fail))

    def test_optimized_attribute_lookup_errors_follow_reference(self) -> None:
        def run(native: bool, fail_at: int, fatal: bool, lookup_name: str, source: str) -> tuple[object, ...]:
            events: list[str] = []

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == lookup_name:
                        events.append(name)
                        if len(events) == fail_at:
                            if fatal:
                                raise KeyboardInterrupt('lookup interrupted')
                            raise ValueError('lookup failed')
                    return super().__getattribute__(name)

            try:
                output = render(source, {'capture': str, 'holder': SimpleNamespace(value=2),
                                         'mapping': {'value': 2}}, native, Sandbox())
                return 'ok', output, events
            except (ValueError, KeyboardInterrupt) as error:
                return type(error).__name__, str(error), events

        for lookup_name, source in (('getattr', '{{ capture(holder.value + (999 + 1)) }}'),
                                    ('getitem', '{{ capture(mapping["value"] + (999 + 1)) }}')):
            for fail_at in range(1, 9):
                for fatal in (False, True):
                    with self.subTest(fail_at=fail_at, fatal=fatal, lookup_name=lookup_name):
                        self.assertEqual(run(True, fail_at, fatal, lookup_name, source),
                                         run(False, fail_at, fatal, lookup_name, source))

    def test_optimized_attribute_lookup_can_mutate_operator_policy(self) -> None:
        def run(native: bool, change_at: int) -> tuple[str, list[object]]:
            events: list[object] = []
            lookups = 0

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    nonlocal lookups
                    if name == 'getattr':
                        lookups += 1
                        events.append(('lookup', lookups))
                        if lookups == change_at:
                            object.__setattr__(self, 'intercepted_binops', frozenset({'+'}))
                    if name == 'intercepted_binops':
                        events.append(('policy', lookups))
                    return super().__getattribute__(name)

                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    events.append(('call', operator))
                    return super().call_binop(context, operator, left, right) + 10

            output = render('{{ capture(holder.value + (999 + 1)) }}',
                            {'capture': str, 'holder': SimpleNamespace(value=2)}, native, Sandbox())
            return output, events

        for change_at in range(1, 8):
            with self.subTest(change_at=change_at):
                self.assertEqual(run(True, change_at), run(False, change_at))

    def test_optimized_attribute_lookup_lifetime_during_compilation(self) -> None:
        def run(native: bool, lookup_name: str, source: str) -> list[object]:
            events: list[object] = []
            counter = 0

            class Lookup:
                def __init__(self, callback: Any, number: int) -> None:
                    self.callback = callback
                    self.number = number

                def __call__(self, *args: Any, **kwargs: Any) -> Any:
                    return self.callback(*args, **kwargs)

                def __del__(self) -> None:
                    events.append(('drop', self.number))

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    nonlocal counter
                    method = super().__getattribute__(name)
                    if name == lookup_name:
                        counter += 1
                        events.append(('lookup', counter))
                        return Lookup(method, counter)
                    return method

            def mark() -> str:
                events.append('runtime')
                return ''

            render(source, {'mark': mark, 'capture': str,
                            'holder': SimpleNamespace(inner=SimpleNamespace(value=2)),
                            'mapping': {'inner': {'value': 2}}}, native, Sandbox())
            return events[:events.index('runtime')]

        for lookup_name, source in (('getattr', '{{ mark() }}{{ capture(holder.inner.value + (999 + 1)) }}'),
                                    ('getitem', '{{ mark() }}{{ capture(mapping["inner"]["value"] + (999 + 1)) }}')):
            with self.subTest(lookup_name=lookup_name):
                self.assertEqual(run(True, lookup_name, source), run(False, lookup_name, source))

    def test_optimized_nested_operator_keeps_partially_folded_child(self) -> None:
        def run(native: bool, policy: tuple[bool, ...]) -> tuple[str, list[object]]:
            events: list[object] = []
            decisions = iter(policy)

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'intercepted_binops':
                        intercepted = next(decisions, False)
                        events.append(('policy', intercepted))
                        return frozenset({'+', '*'}) if intercepted else frozenset()
                    return super().__getattribute__(name)

                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    events.append(('call', operator, left, right))
                    return super().call_binop(context, operator, left, right) + 10

            return render('{{ capture((999 + 1) * 2) }}', {'capture': str}, native, Sandbox()), events

        for policy in ((False, True, False, True, True, False),
                       (True, False, True, False, True, True)):
            with self.subTest(policy=policy):
                self.assertEqual(run(True, policy), run(False, policy))

    def test_unoptimized_nested_operator_decisions_follow_source_order(self) -> None:
        def run(native: bool, source: str, policies: tuple[frozenset[str], ...],
                asynchronous: bool) -> tuple[object, ...]:
            events: list[object] = []
            decisions = iter(policies)

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name in ('intercepted_binops', 'intercepted_unops'):
                        policy = next(decisions)
                        events.append((name, tuple(sorted(policy))))
                        return policy
                    return super().__getattribute__(name)

                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    events.append(('binop', operator, left, right))
                    return super().call_binop(context, operator, left, right) + 10

                def call_unop(self, context: Any, operator: str, arg: Any) -> Any:
                    events.append(('unop', operator, arg))
                    return super().call_unop(context, operator, arg) + 10

            output = render('{{ capture(' + source + ') }}', {'capture': str}, native,
                            Sandbox(optimized=False, enable_async=asynchronous))
            return output, events

        cases = (
            ('(999 + 1) * 2', (frozenset(), frozenset())),
            ('(999 + 1) * 2', (frozenset({'*'}), frozenset({'+'}))),
            ('(999 + 1) * 2', (frozenset(), frozenset({'+'}))),
            ('(999 + 1) * 2', (frozenset({'*'}), frozenset())),
            ('-(999 + 1)', (frozenset({'-'}), frozenset({'+'}))),
            ('(999 + 1) * (2 + 3)', (frozenset({'*'}), frozenset({'+'}), frozenset({'+'}))),
        )
        for source, policies in cases:
            for asynchronous in (False, True):
                with self.subTest(source=source, policies=policies, asynchronous=asynchronous):
                    self.assertEqual(run(True, source, policies, asynchronous),
                                     run(False, source, policies, asynchronous))

    def test_intercepted_numeric_operator_policy_is_read_once_without_optimizer(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name in ('sandboxed', 'intercepted_binops'):
                        events.append(name)
                    if name == 'intercepted_binops':
                        return frozenset({'+'}) if events.count(name) == 1 else frozenset()
                    return super().__getattribute__(name)

                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    return 42

            environment = Sandbox(optimized=False)
            return render('{{ capture(999 + 1) }}', {'capture': str}, native, environment), events

        self.assertEqual(run(True), run(False))

    def test_intercepted_nested_operator_in_complete_document(self) -> None:
        source = '{{ capture((999 + 1) * 2) }}'
        document = template(source)

        def run(engine: Any) -> bytes:
            class Sandbox(SandboxedEnvironment):
                intercepted_binops = frozenset({'+', '*'})

                def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
                    return super().call_binop(context, operator, left, right) + 10

            return generate(engine, source, {'capture': str}, Sandbox(optimized=False), source=document)

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_nested_operator_policy_error_stops_compilation_in_order(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'intercepted_binops':
                        events.append(name)
                        if len(events) == 2:
                            raise ValueError('second operator')
                    return super().__getattribute__(name)

            try:
                render('{{ capture((999 + 1) * (2 + 3)) }}', {'capture': str}, native,
                       Sandbox(optimized=False))
            except ValueError as error:
                return str(error), events
            self.fail('expected policy lookup to fail')

        self.assertEqual(run(True), run(False))

    def test_numeric_arguments_preserve_compile_policy_reads(self) -> None:
        def run(native: bool, optimized: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name in ('sandboxed', 'intercepted_binops'):
                        events.append(name)
                    return super().__getattribute__(name)

            def capture(value: int, *, other: int) -> str:
                return f'{value}:{other}'

            source = '{{ capture(1000 + 1, other=1000 + 2) }}'
            return render(source, {'capture': capture}, native, Sandbox(optimized=optimized)), events

        for optimized in (False, True):
            with self.subTest(optimized=optimized):
                self.assertEqual(run(True, optimized), run(False, optimized))

    def test_dispatch_lookup_error_precedes_argument(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Sandbox(SandboxedEnvironment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'call':
                        events.append('lookup')
                        raise ValueError('dispatcher failed')
                    return super().__getattribute__(name)

            def mutate() -> int:
                events.append('mutate')
                return 1

            try:
                render('{{ target(mutate()) }}', {'target': str, 'mutate': mutate}, native, Sandbox())
            except ValueError as error:
                return str(error), events
            self.fail('expected dispatcher failure')

        self.assertEqual(run(True), run(False))

    def test_sandbox_dispatch_is_captured_across_async_argument(self) -> None:
        def run(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            environment = SandboxedEnvironment(enable_async=True)

            def target(value: int) -> str:
                events.append('target')
                return 'old'

            async def mutate() -> int:
                await asyncio.sleep(0)
                events.append('mutate')
                environment.call = lambda context, callback, *args, **kwargs: 'new'  # type: ignore[method-assign]
                return 1

            return render('{{ target(mutate()) }}', {'target': target, 'mutate': mutate}, native, environment), events

        self.assertEqual(run(True), run(False))

    def test_context_call_method_is_captured_before_arguments(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            environment = Environment(enable_async=asynchronous)

            def target(value: int) -> str:
                events.append('target')
                return 'old'

            @pass_context
            def mutate(context: Any) -> int:
                events.append('mutate')
                context.call = lambda callback, *args, **kwargs: 'new'
                return 1

            output = render('{{ target(mutate()) }}', {'target': target, 'mutate': mutate}, native, environment)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(run(True, asynchronous), run(False, asynchronous))

    def test_sandbox_call_method_is_captured_before_arguments(self) -> None:
        def run(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            environment = SandboxedEnvironment(enable_async=asynchronous)

            def target(value: int) -> str:
                events.append('target')
                return 'old'

            def mutate() -> int:
                events.append('mutate')
                environment.call = lambda context, callback, *args, **kwargs: 'new'  # type: ignore[method-assign]
                return 1

            output = render('{{ target(mutate()) }}', {'target': target, 'mutate': mutate}, native, environment)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(run(True, asynchronous), run(False, asynchronous))

    def test_complete_document_keeps_compiled_policy(self) -> None:
        source = '{{ target(mutate()) }}'
        document = template(source)

        def run(engine: Any) -> bytes:
            environment = SandboxedEnvironment()
            environment.sandboxed = False

            def target(value: int) -> str:
                return str(value)

            def mutate() -> int:
                environment.sandboxed = True
                return 42

            return generate(engine, source, {'target': target, 'mutate': mutate}, environment, source=document)

        self.assertTrue(compare_docx(run(docxtpl), run(docxtpl_native))['equivalent'])

    def test_sandbox_mode_is_selected_before_argument_evaluation(self) -> None:
        def run(native: bool, initial: bool, asynchronous: bool, source: str) -> tuple[object, ...]:
            events: list[object] = []

            def target(value: int) -> str:
                events.append(('target', value))
                return 'ok'

            class Sandbox(SandboxedEnvironment):
                def is_safe_callable(self, obj: Any) -> bool:
                    events.append(('safe', obj is target))
                    return obj is not target

            environment = Sandbox(enable_async=asynchronous)
            environment.sandboxed = initial

            def mutate() -> int:
                events.append('mutate')
                environment.sandboxed = not initial
                return 1

            def factory() -> Any:
                events.append('factory')
                return target

            try:
                output = render(source, {'target': target, 'mutate': mutate, 'factory': factory,
                                         'holder': SimpleNamespace(method=target), 'items': range(2)}, native, environment)
                return 'ok', output, events
            except SecurityError as error:
                return 'error', type(error).__name__, events

        for initial in (False, True):
            for asynchronous in (False, True):
                for source in ('{{ target(mutate()) }}', '{{ holder.method(mutate()) }}',
                               '{{ factory()(mutate()) }}',
                               '{% for item in items %}{{ target(mutate()) }}{% endfor %}'):
                    with self.subTest(initial=initial, asynchronous=asynchronous, source=source):
                        self.assertEqual(run(True, initial, asynchronous, source),
                                         run(False, initial, asynchronous, source))
