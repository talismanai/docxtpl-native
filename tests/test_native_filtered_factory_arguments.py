"""Filtered factory calls must preserve argument order and callable identity."""

import io
import unittest
from collections.abc import AsyncIterator, Iterator
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2.sandbox import SandboxedEnvironment, unsafe
from paritylab.compare import compare_docx
from paritylab.protocol_base import template


class FilteredFactoryArgumentTests(unittest.TestCase):
    def test_factory_argument_text_does_not_impersonate_filter_variable(self) -> None:
        def observe(native: bool, asynchronous: bool, expression: str) -> tuple[str, list[str]]:
            events: list[str] = []

            class Threshold:
                limit = 0

                def __lt__(self, value: int) -> bool:
                    events.append(f'filter:{value}:{self.limit}')
                    return value > self.limit

            class Box:
                threshold = 17

            threshold = Threshold()

            def values(start: Any) -> Iterator[int]:
                events.append('factory')
                return iter((1, 2, 3))

            def tick() -> str:
                events.append('body')
                threshold.limit = 2
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% for x in ' + expression + ' if x > threshold %}'
                      '{{loop.index}}:{{x}}{{tick()}};{% endfor %}')
            context = {'values': values, 'threshold': threshold, 'threshold_extra': 17,
                       'box': Box(), 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                ).render('factory-argument-names', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for expression in ('values(threshold_extra)', 'values(start=threshold_extra)',
                               'values(box.threshold)', 'values("threshold")'):
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    expected = observe(False, asynchronous, expression)
                    self.assertEqual(expected, ('1:1;2:3;', ['factory', 'filter:1:0', 'body',
                                                               'filter:2:2', 'filter:3:2', 'body']))
                    self.assertEqual(observe(True, asynchronous, expression), expected)

    def test_factory_arguments_preserve_document_and_mutation(self) -> None:
        source = template('{% for x in values(1) if x > threshold %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}')

        def observe(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            class Threshold:
                value = 0

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}:{self.value}')
                    return bool(other > self.value)

            threshold = Threshold()

            def values(start: int) -> Iterator[int]:
                events.append(f'factory:{start}')
                return iter((1, 2, 3))

            def tick() -> str:
                events.append('body')
                threshold.value = 5
                return ''

            module: Any = docxtpl_native if native else docxtpl
            document = module.DocxTemplate(io.BytesIO(source))
            document.render({'values': values, 'threshold': threshold, 'tick': tick},
                            jinja_env=jinja2.Environment(enable_async=asynchronous))
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected, expected_events = observe(False, asynchronous)
                actual, actual_events = observe(True, asynchronous)
                self.assertEqual(actual_events, expected_events)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_positional_and_keyword_calls_preserve_lazy_order(self) -> None:
        def observe(native: bool, asynchronous: bool, expression: str) -> tuple[str, list[str]]:
            events: list[str] = []

            def values(start: int = 0) -> Iterator[int]:
                events.append(f'factory:{start}')
                for value in range(start, 4):
                    events.append(f'yield:{value}')
                    yield value

            def tick() -> str:
                events.append('body')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in ' + expression + ' if x > threshold %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': values, 'threshold': 1, 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for expression in ('values(1)', 'values(start=1)', 'values(*[1])', 'values(**{"start": 1})'):
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    self.assertEqual(observe(True, asynchronous, expression), observe(False, asynchronous, expression))

    def test_argument_rebinding_keeps_original_shared_callable(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Factory:
                def __init__(self, name: str) -> None:
                    self.name = name

                def __call__(self, start: int) -> Iterator[int]:
                    events.append(f'factory:{self.name}:{start}')
                    return iter((1, 2))

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{self.name}:{other}')
                    return True

            @jinja2.pass_context
            def argument(context: Any) -> int:
                events.append('argument')
                context.vars['values'] = Factory('replacement')
                return 1

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in values(argument()) if x > values %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': Factory('original'), 'argument': argument}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:1;2:2;', ['argument', 'factory:original:1',
                                                         'compare:original:1', 'compare:original:2']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_argument_and_filter_share_one_context_resolution(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            def values(threshold: int) -> Iterator[int]:
                events.append(f'factory:{threshold}')
                return iter((1, 2))

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values(threshold) if x > threshold %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': values, 'threshold': 0}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_shared_argument_keeps_binding_after_factory_reassignment(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            @jinja2.pass_context
            def values(context: Any, start: int) -> Iterator[int]:
                events.append(f'factory:{start}')
                context.vars['threshold'] = 9
                return iter((1, 2))

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in values(threshold) if x > threshold %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': values, 'threshold': 0}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:1;2:2;', ['factory:0']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_factory_and_direct_argument_are_the_filter_value(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Factory:
                def __call__(self, value: Any) -> Iterator[int]:
                    events.append(f'factory:{value is self}')
                    return iter((1, 2))

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}')
                    return True

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values(values) if x > values %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': Factory()}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:1;2:2;', ['resolve:values', 'factory:True', 'compare:1', 'compare:2']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_composite_argument_uses_same_filter_binding(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            def values(start: int) -> Iterator[int]:
                events.append(f'factory:{start}')
                return iter((1, 2))

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values(threshold + 0) if x > threshold %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': values, 'threshold': 0}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_composite_arguments_retain_binding_across_expression_stack(self) -> None:
        def observe(native: bool, asynchronous: bool, argument: str) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            def values(*args: Any, **kwargs: Any) -> Iterator[int]:
                events.append(f'factory:{args!r}:{kwargs!r}')
                return iter((1, 2))

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values(' + argument + ') if x > threshold %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': values, 'threshold': 0}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        arguments = ('threshold + threshold', '3, threshold + 1', 'start=threshold + 1',
                     '(threshold + 1) if threshold else 2', '[threshold, threshold + 1]')
        for asynchronous in (False, True):
            for argument in arguments:
                with self.subTest(asynchronous=asynchronous, argument=argument):
                    self.assertEqual(observe(True, asynchronous, argument), observe(False, asynchronous, argument))

    def test_composite_argument_keeps_binding_when_operator_rebinds_context(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            active: list[Any] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    if not active:
                        active.append(self)
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Threshold:
                def __add__(self, other: Any) -> int:
                    events.append(f'add:{other is self}')
                    active[0].vars['threshold'] = 99
                    return 0

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}')
                    return True

            def values(start: int) -> Iterator[int]:
                events.append(f'factory:{start}')
                return iter((1, 2))

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values(threshold + threshold) if x > threshold %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': values, 'threshold': Threshold()}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_composite_missing_binding_materializes_for_each_operand(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(f'undefined:{kwargs.get("name")}')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            source = '{% for x in values(threshold + threshold) if x > threshold %}{{loop.index}}{% endfor %}'

            def values(value: Any) -> Iterator[int]:
                return iter((1,))

            context = {'values': values}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render(context)
            except Exception as error:  # noqa: BLE001 - compare undefined constructor effects
                return type(error).__name__, events
            raise AssertionError('expected undefined operand failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('UndefinedError', ['undefined:threshold', 'undefined:threshold']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_composite_argument_resolves_multiple_bindings_in_order(self) -> None:
        def observe(native: bool, asynchronous: bool, expression: str) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            def values(start: int) -> Iterator[int]:
                events.append(f'factory:{start}')
                return iter((1, 2))

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values(' + expression + ') if x > threshold %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': values, 'threshold': 0, 'offset': 0}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for expression in ('threshold + offset', 'offset + threshold'):
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    self.assertEqual(observe(True, asynchronous, expression), observe(False, asynchronous, expression))

    def test_multi_binding_arguments_capture_unused_branches_and_factory_identity(self) -> None:
        def observe(native: bool, asynchronous: bool, argument: str) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            def values(*args: Any, **kwargs: Any) -> Iterator[int]:
                events.append(f'factory:{["self" if arg is values else arg for arg in args]!r}:{kwargs!r}')
                return iter((1, 2))

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values(' + argument + ') if x > threshold %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': values, 'threshold': 0, 'offset': 2, 'flag': False}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for argument in ('offset if flag else threshold', 'values, threshold + offset',
                             '[threshold, offset, threshold]', '*[threshold, offset]',
                             '**{"start": threshold + offset}', '*[offset], key=threshold',
                             'threshold, *[offset], key=flag'):
                with self.subTest(asynchronous=asynchronous, argument=argument):
                    self.assertEqual(observe(True, asynchronous, argument), observe(False, asynchronous, argument))

    def test_multi_binding_operator_rebinding_preserves_both_originals(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            active: list[Any] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    if not active:
                        active.append(self)
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Offset:
                def __add__(self, other: Any) -> int:
                    events.append(f'add:{other}')
                    active[0].vars.update({'threshold': 99, 'offset': 99})
                    return 0

            def values(start: int, second: Any) -> Iterator[int]:
                events.append(f'factory:{start}:{second is original}')
                return iter((1, 2))

            original = Offset()
            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values(offset + threshold, offset) if x > threshold %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': values, 'threshold': 0, 'offset': original}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_multi_binding_missing_constructor_follows_name_resolution(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(f'undefined:{kwargs.get("name")}')
                    super().__init__(*args, **kwargs)

            def values(value: Any) -> Iterator[int]:
                events.append('factory')
                return iter(())

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            environment.context_class = Context
            source = '{% for x in values(offset + threshold) if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values, 'threshold': 0}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render(context)
            except Exception as error:  # noqa: BLE001 - compare missing construction boundary
                return type(error).__name__, events
            raise AssertionError('expected missing argument failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('UndefinedError', ['resolve:values', 'resolve:offset',
                                                                'resolve:threshold', 'undefined:offset']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_multi_binding_resolution_precedes_earlier_template_callback(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            @jinja2.pass_context
            def mutate(context: Any) -> str:
                events.append('mutate')
                context.vars.update({'threshold': 99, 'offset': 99})
                return ''

            def values(value: int) -> Iterator[int]:
                events.append(f'factory:{value}')
                return iter((1, 2))

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = ('{{mutate()}}{% for x in values(offset + threshold) if x > threshold %}'
                      '{{loop.index}}:{{x}};{% endfor %}')
            context = {'values': values, 'threshold': 0, 'offset': 0, 'mutate': mutate}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_multi_binding_resolution_effects_follow_source_order(self) -> None:
        def observe(native: bool, asynchronous: bool, expression: str) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    if key == 'offset':
                        self.vars['threshold'] = 2
                    return super().resolve_or_missing(key)

            def values(start: int) -> Iterator[int]:
                events.append(f'factory:{start}')
                return iter((1, 2, 3))

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values(' + expression + ') if x > threshold %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': values, 'threshold': 0, 'offset': 1}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for expression in ('threshold + offset', 'offset + threshold'):
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    self.assertEqual(observe(True, asynchronous, expression), observe(False, asynchronous, expression))

    def test_multi_binding_splat_keeps_lookup_and_evaluation_order(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Threshold:
                @property
                def value(self) -> int:
                    events.append('attribute')
                    return 0

                def __lt__(self, other: Any) -> bool:
                    return True

            class Offset:
                def __iter__(self) -> Iterator[int]:
                    events.append('iterate')
                    return iter((1,))

            def values(*args: Any, **kwargs: Any) -> Iterator[int]:
                events.append(f'factory:{args!r}:{kwargs!r}')
                return iter((1,))

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values(*offset, key=threshold.value) if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values, 'threshold': Threshold(), 'offset': Offset()}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_multi_binding_lifetime_after_factory_removes_context_reference(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            context: dict[str, Any] = {}

            class Offset:
                def __add__(self, other: Any) -> int:
                    return 0

                def __del__(self) -> None:
                    events.append('drop')

            @jinja2.pass_context
            def values(jinja_context: Any, start: int) -> Iterator[int]:
                events.append('factory')
                jinja_context.parent['offset'] = None
                context['offset'] = None
                return iter((1, 2))

            def tick() -> str:
                events.append('body')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% for x in values(offset + threshold) if x > threshold %}'
                      '{{loop.index}}{{tick()}}{% endfor %}{{tick()}}')
            context.update({'values': values, 'threshold': 0, 'offset': Offset(), 'tick': tick})
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_multi_binding_lifetime_preserves_release_order(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            context: dict[str, Any] = {}

            class Number:
                def __init__(self, name: str) -> None:
                    self.name = name

                @property
                def value(self) -> int:
                    return 0

                def __del__(self) -> None:
                    events.append(f'drop:{self.name}')

            @jinja2.pass_context
            def values(jinja_context: Any, start: int) -> Iterator[int]:
                events.append('factory')
                for name in ('omega', 'alpha'):
                    jinja_context.parent[name] = None
                    context[name] = None
                return iter((1,))

            def tick() -> str:
                events.append('body')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = ('{% for x in values(omega.value + alpha.value + threshold) if x > threshold %}'
                      '{{loop.index}}{{tick()}}{% endfor %}{{tick()}}')
            context.update({'values': values, 'threshold': 0, 'omega': Number('omega'),
                            'alpha': Number('alpha'), 'tick': tick})
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_composite_missing_binding_preserves_truth_and_lookup(self) -> None:
        def observe(native: bool, asynchronous: bool, expression: str) -> tuple[str, list[str]]:
            events: list[str] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(f'undefined:{kwargs.get("name")}')
                    super().__init__(*args, **kwargs)

            def values(value: Any) -> Iterator[int]:
                events.append(f'factory:{value!r}')
                return iter(())

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            source = '{% for x in values(' + expression + ') if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values}
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('factory-arguments', source)
                else:
                    output = environment.from_string(source).render(context)
            except Exception as error:  # noqa: BLE001 - compare protocol and error
                return type(error).__name__, events
            return output, events

        for asynchronous in (False, True):
            for expression in ('1 if threshold else 2', 'not threshold', 'threshold.missing',
                               'threshold[0]', 'threshold[threshold]', 'threshold[threshold + 0]',
                               '[threshold]', '{"x": threshold}'):
                with self.subTest(asynchronous=asynchronous, expression=expression):
                    self.assertEqual(observe(True, asynchronous, expression), observe(False, asynchronous, expression))

    def test_missing_factory_is_materialized_before_missing_argument(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Undefined(jinja2.Undefined):
                jinja_pass_arg = None

                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    self.serial = sum(event.startswith('undefined:') for event in events)
                    events.append(f'undefined:{self.serial}')
                    super().__init__(*args, **kwargs)

                # Deliberately exercise a user-defined callable Undefined.
                def __call__(self, other: Any) -> Iterator[int]:  # ty: ignore[invalid-method-override]
                    events.append(f'call:{self.serial}:{other.serial}')
                    return iter(())

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            source = '{% for x in values(values) if x > values %}{{loop.index}}{% endfor %}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {},
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render({})
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('', ['undefined:0', 'undefined:1', 'call:0:1']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_composite_missing_truth_callback_preserves_exception_identity(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            failure = RuntimeError('undefined truth')

            class Undefined(jinja2.Undefined):
                def __bool__(self) -> bool:
                    events.append('bool')
                    raise failure

            def values(value: Any) -> Iterator[int]:
                events.append('factory')
                return iter(())

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            source = '{% for x in values(1 if threshold else 2) if x > threshold %}{{loop.index}}{% endfor %}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': values},
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render({'values': values})
            except RuntimeError as error:
                return error is failure, events
            raise AssertionError('expected undefined truth failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_argument_error_preserves_exception_and_prevents_factory(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            failure = RuntimeError('argument failed')

            def argument() -> int:
                events.append('argument')
                raise failure

            def values(start: int) -> Iterator[int]:
                events.append('factory')
                return iter((start,))

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in values(argument()) if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values, 'argument': argument, 'threshold': 0}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                return error is failure, events
            raise AssertionError('expected argument failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, (True, ['argument']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_async_factory_and_argument_preserve_lookahead(self) -> None:
        def observe(native: bool, field: str) -> tuple[str, list[str]]:
            events: list[str] = []

            async def values(start: int) -> AsyncIterator[int]:
                events.append(f'factory:{start}')

                async def items() -> AsyncIterator[int]:
                    for value in range(start, 4):
                        events.append(f'yield:{value}')
                        yield value

                return items()

            def tick() -> str:
                events.append('body')
                return ''

            environment = jinja2.Environment(enable_async=True)
            source = '{% for x in values(1) if x > threshold %}{{loop.' + field + '}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': values, 'threshold': 1, 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for field in ('index', 'last', 'nextitem', 'length'):
            with self.subTest(field=field):
                self.assertEqual(observe(True, field), observe(False, field))

    def test_sandbox_rejects_unsafe_factory_after_argument(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def argument() -> int:
                events.append('argument')
                return 1

            @unsafe
            def values(start: int) -> Iterator[int]:
                events.append('factory')
                return iter((start,))

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for x in values(argument()) if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values, 'argument': argument, 'threshold': 0}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render(context)
            except Exception as error:  # noqa: BLE001 - compare security boundary
                return type(error).__name__, events
            raise AssertionError('expected unsafe factory rejection')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('SecurityError', ['argument']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_missing_factory_evaluates_argument_before_error(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []

            def argument() -> int:
                events.append('argument')
                return 1

            environment = jinja2.Environment(enable_async=asynchronous, undefined=jinja2.StrictUndefined)
            source = '{% for x in values(argument()) if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'argument': argument, 'threshold': 0}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render(context)
            except Exception as error:  # noqa: BLE001 - compare missing factory boundary
                return type(error).__name__, str(error), events
            raise AssertionError('expected missing factory failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_explicit_undefined_factory_preserves_name_and_argument_effect(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []

            def argument() -> int:
                events.append('argument')
                return 1

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in values(argument()) if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': jinja2.Undefined(name='explicit'), 'argument': argument, 'threshold': 0}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render(context)
            except Exception as error:  # noqa: BLE001 - compare explicit undefined boundary
                return type(error).__name__, str(error), events
            raise AssertionError('expected undefined factory failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_missing_shared_factory_and_argument_preserve_undefined_construction(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(f'undefined:{kwargs.get("name")}')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            source = '{% for x in values(values) if x > values %}{{loop.index}}{% endfor %}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {},
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render()
            except Exception as error:  # noqa: BLE001 - compare undefined constructor effects
                return type(error).__name__, str(error), events
            raise AssertionError('expected missing factory failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_missing_shared_argument_and_filter_preserve_undefined_effects(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(f'undefined:{kwargs.get("name")}')
                    super().__init__(*args, **kwargs)

            def values(start: Any) -> Iterator[int]:
                events.append(f'factory:{type(start).__name__}')
                return iter((1,))

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            source = '{% for x in values(threshold) if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render(context)
            except Exception as error:  # noqa: BLE001 - compare undefined constructor effects
                return type(error).__name__, str(error), events
            raise AssertionError('expected undefined threshold failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_missing_shared_argument_second_undefined_failure_timing(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str, list[str]]:
            events: list[str] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('undefined')
                    if events.count('undefined') == 2:
                        raise RuntimeError('second undefined')
                    super().__init__(*args, **kwargs)

            def values(start: Any) -> Iterator[int]:
                events.append('factory')
                return iter((1,))

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            source = '{% for x in values(threshold) if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render(context)
            except Exception as error:  # noqa: BLE001 - compare constructor failure and order
                return type(error).__name__, str(error), events
            raise AssertionError('expected undefined constructor failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('RuntimeError', 'second undefined', ['undefined', 'factory', 'undefined']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_missing_filter_binding_is_not_constructed_for_empty_iterable(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('undefined')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            source = '{% for x in values if x > threshold %}{{loop.index}}{% endfor %}'
            context: dict[str, Any] = {'values': []}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_missing_binding_uses_undefined_class_captured_before_factory(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Original(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('original')
                    super().__init__(*args, **kwargs)

            class Replacement(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('replacement')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Original)

            def values() -> Iterator[int]:
                environment.undefined = Replacement
                return iter((1,))

            source = '{% for x in values() if x > threshold %}{{loop.index}}{% endfor %}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': values},
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render({'values': values})
            except Exception as error:  # noqa: BLE001 - compare exception and class lookup
                return type(error).__name__, events
            raise AssertionError('expected undefined threshold failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_undefined_class_is_captured_again_for_each_render(self) -> None:
        def observe(native: bool, asynchronous: bool) -> list[str]:
            events: list[str] = []

            class Original(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('original')
                    super().__init__(*args, **kwargs)

            class Replacement(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('replacement')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Original)

            def values() -> Iterator[int]:
                environment.undefined = Replacement
                return iter((1,))

            source = '{% for x in values() if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values}
            session = (docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                       if native else None)
            for _ in range(2):
                try:
                    if native:
                        assert session is not None
                        session.render('factory-arguments', source)
                    else:
                        environment.from_string(source).render(context)
                except jinja2.UndefinedError:
                    pass
                else:
                    raise AssertionError('expected undefined threshold failure')
            return events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ['original', 'replacement'])
                self.assertEqual(observe(True, asynchronous), expected)

    def test_undefined_class_is_captured_before_context_resolution(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Original(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('original')
                    super().__init__(*args, **kwargs)

            class Replacement(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('replacement')
                    super().__init__(*args, **kwargs)

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    if key == 'values':
                        self.environment.undefined = Replacement
                    return super().resolve_or_missing(key)

            def values() -> Iterator[int]:
                return iter((1,))

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Original)
            environment.context_class = Context
            source = '{% for x in values() if x > threshold %}{{loop.index}}{% endfor %}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': values},
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render({'values': values})
            except Exception as error:  # noqa: BLE001 - compare resolution and class capture
                return type(error).__name__, events
            raise AssertionError('expected undefined threshold failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_included_template_captures_its_own_undefined_class(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class ChildUndefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('child')
                    super().__init__(*args, **kwargs)

            class LaterUndefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('later')
                    super().__init__(*args, **kwargs)

            child = '{% for x in values() if x > threshold %}{{loop.index}}{% endfor %}'
            environment = jinja2.Environment(enable_async=asynchronous, loader=jinja2.DictLoader({'child': child}))

            def enter() -> str:
                environment.undefined = ChildUndefined
                return ''

            def values() -> Iterator[int]:
                environment.undefined = LaterUndefined
                return iter((1,))

            source = "{{enter()}}{% include 'child' %}"
            context = {'enter': enter, 'values': values}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('parent', source)
                else:
                    environment.from_string(source).render(context)
            except Exception as error:  # noqa: BLE001 - compare nested activation capture
                return type(error).__name__, events
            raise AssertionError('expected undefined threshold failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('UndefinedError', ['child']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_reentrant_render_keeps_outer_undefined_class(self) -> None:
        def observe(native: bool) -> list[str]:
            events: list[str] = []

            class Outer(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('outer')
                    super().__init__(*args, **kwargs)

            class Inner(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('inner')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(undefined=Outer)
            nested = '{% for x in nested_values() if x > threshold %}{{loop.index}}{% endfor %}'
            source = '{% for x in values() if x > threshold %}{{loop.index}}{% endfor %}'
            session: Any = None
            context: dict[str, Any] = {'nested_values': lambda: iter((1,))}

            def values() -> Iterator[int]:
                environment.undefined = Inner
                try:
                    if native:
                        session.render('nested', nested)
                    else:
                        environment.from_string(nested).render(context)
                except jinja2.UndefinedError:
                    pass
                else:
                    raise AssertionError('expected nested undefined threshold failure')
                return iter((1,))

            context['values'] = values
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
            try:
                if native:
                    session.render('outer', source)
                else:
                    environment.from_string(source).render(context)
            except jinja2.UndefinedError:
                pass
            else:
                raise AssertionError('expected outer undefined threshold failure')
            return events

        expected = observe(False)
        self.assertEqual(expected, ['inner', 'outer'])
        self.assertEqual(observe(True), expected)

    def test_undefined_accessor_order_at_template_entry(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Environment(jinja2.Environment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'undefined':
                        events.append('undefined')
                    return super().__getattribute__(name)

            class Context(jinja2.runtime.Context):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'resolve_or_missing':
                        events.append('resolve-method')
                    return super().__getattribute__(name)

            environment = Environment()
            environment.context_class = Context
            source = '{% for x in values() if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': lambda: iter(())}
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                events.clear()
                output = session.render('factory-arguments', source)
            else:
                compiled = environment.from_string(source)
                events.clear()
                output = compiled.render(context)
            return output, events

        self.assertEqual(observe(True), observe(False))

    def test_context_resolver_is_retained_for_activation(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'original:{key}')

                    def replacement(key: str) -> Any:
                        events.append(f'replacement:{key}')
                        return jinja2.runtime.missing

                    self.resolve_or_missing = replacement
                    return super().resolve_or_missing(key)

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values() if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': lambda: iter((1,)), 'threshold': 0}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('factory-arguments', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_entry_accessor_stop_iteration_matches_generator_boundary(self) -> None:
        def observe(native: bool, asynchronous: bool, failing: str, exception: type[Exception]) -> tuple[str, str]:
            class Environment(jinja2.Environment):
                armed = False

                def __getattribute__(self, name: str) -> Any:
                    if name == 'undefined' and object.__getattribute__(self, 'armed') and failing == 'undefined':
                        raise exception('undefined accessor')
                    return super().__getattribute__(name)

            class Context(jinja2.runtime.Context):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'resolve_or_missing' and failing == 'resolver':
                        raise exception('resolver accessor')
                    return super().__getattribute__(name)

            environment = Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{{value}}'
            context = {'value': 'ok'}
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
            else:
                compiled = environment.from_string(source)
            environment.armed = True
            try:
                if native:
                    session.render('entry', source)
                else:
                    compiled.render(context)
            except Exception as error:  # noqa: BLE001 - compare generator boundary
                return type(error).__name__, str(error)
            raise AssertionError('expected accessor failure')

        for asynchronous in (False, True):
            for failing in ('resolver', 'undefined'):
                for exception in (StopIteration, StopAsyncIteration):
                    with self.subTest(asynchronous=asynchronous, failing=failing, exception=exception.__name__):
                        self.assertEqual(observe(True, asynchronous, failing, exception),
                                         observe(False, asynchronous, failing, exception))

    def test_explicit_undefined_argument_retains_identity(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str, list[bool]]:
            seen: list[bool] = []
            explicit = jinja2.Undefined(name='explicit')

            def values(start: Any) -> Iterator[int]:
                seen.append(start is explicit)
                return iter((1,))

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in values(threshold) if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values, 'threshold': explicit}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('factory-arguments', source)
                else:
                    environment.from_string(source).render(context)
            except Exception as error:  # noqa: BLE001 - compare explicit undefined boundary
                return type(error).__name__, str(error), seen
            raise AssertionError('expected explicit undefined failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))


if __name__ == '__main__':
    unittest.main()
