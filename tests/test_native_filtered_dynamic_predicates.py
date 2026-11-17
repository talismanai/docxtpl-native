"""A comparison against a live context value must retain filter laziness."""

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


class FilteredDynamicPredicateTests(unittest.TestCase):
    def test_attribute_predicate_keeps_missing_binding_after_context_is_updated(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            @jinja2.pass_context
            def tick(context: Any) -> str:
                events.append('body')
                context.vars['settings'] = {'current_limit': 0}
                return ''

            source = ('{% for x in values if x == 1 or x > settings.current_limit %}'
                      '{{loop.index}}{{tick()}}{% endfor %}')
            environment = jinja2.Environment(enable_async=asynchronous)
            context = {'values': [1, 2], 'tick': tick}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('missing-attribute-binding', source)
                else:
                    environment.from_string(source).render(context)
            except jinja2.UndefinedError as error:
                return str(error), events
            self.fail('missing captured settings must raise UndefinedError')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ("'settings' is undefined", ['body']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_attribute_predicate_resolves_context_before_empty_iteration(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Values:
                def __iter__(self) -> Iterator[int]:
                    events.append('iterate')
                    return iter(())

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values if x > settings.current_limit %}{{loop.index}}{% endfor %}'
            context = {'values': Values(), 'settings': {'current_limit': 0}}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                ).render('empty-attribute-binding', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('', ['resolve:values', 'resolve:settings', 'iterate']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_general_predicate_resolves_multiple_bindings_in_order(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Values:
                def __iter__(self) -> Iterator[int]:
                    events.append('iterate')
                    return iter(())

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values if x > alpha.limit + beta.limit %}{{loop.index}}{% endfor %}'
            context = {'values': Values(), 'alpha': {'limit': 0}, 'beta': {'limit': 1}}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                ).render('multiple-attribute-bindings', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('', ['resolve:values', 'resolve:alpha', 'resolve:beta', 'iterate']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_general_predicate_resolves_after_custom_loop_constructor(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            contexts: list[Any] = []

            @jinja2.pass_context
            def retain_context(context: Any) -> str:
                contexts.append(context)
                return ''

            original = jinja2.runtime.LoopContext.__init__

            def constructor(instance: Any, *args: Any, **kwargs: Any) -> None:
                events.append('constructor')
                contexts[0].vars['settings'] = {'current_limit': 2}
                original(instance, *args, **kwargs)

            source = ('{{retain_context()}}{% for x in values if x > settings.current_limit %}'
                      '{{loop.index}}:{{x}};{% endfor %}')
            environment = jinja2.Environment(enable_async=asynchronous)
            context = {'values': [1, 2, 3], 'settings': {'current_limit': 0},
                       'retain_context': retain_context}
            from unittest.mock import patch
            with patch.object(jinja2.runtime.LoopContext, '__init__', constructor):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('constructor-before-predicate', source)
                else:
                    output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:3;', ['constructor']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_captured_predicate_name_resolves_after_custom_loop_constructor(self) -> None:
        def observe(native: bool, asynchronous: bool, source: str) -> str:
            contexts: list[Any] = []

            @jinja2.pass_context
            def retain_context(context: Any) -> str:
                contexts.append(context)
                return ''

            original = jinja2.runtime.LoopContext.__init__

            def constructor(instance: Any, *args: Any, **kwargs: Any) -> None:
                contexts[0].vars['limit'] = 2
                original(instance, *args, **kwargs)

            environment = jinja2.Environment(enable_async=asynchronous)
            context = {'values': [1, 2, 3], 'limit': 0, 'retain_context': retain_context}
            from unittest.mock import patch
            with patch.object(jinja2.runtime.LoopContext, '__init__', constructor):
                if native:
                    return docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context
                    ).render('captured-after-constructor', source)
                return environment.from_string(source).render(context)

        # Comparisons and tests against one context name take the captured path.
        for predicate in ('x > limit', 'limit < x', 'x is gt(limit)'):
            source = f'{{{{retain_context()}}}}{{% for x in values if {predicate} %}}{{{{loop.index}}}}:{{{{x}}}};{{% endfor %}}'
            for asynchronous in (False, True):
                with self.subTest(predicate=predicate, asynchronous=asynchronous):
                    expected = observe(False, asynchronous, source)
                    self.assertEqual(expected, '1:3;')
                    self.assertEqual(observe(True, asynchronous, source), expected)

    def test_attribute_predicate_keeps_binding_when_context_is_reassigned(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Settings:
                def __init__(self, name: str, limit: int) -> None:
                    self.name = name
                    self.limit = limit

                @property
                def current_limit(self) -> int:
                    events.append(f'limit:{self.name}:{self.limit}')
                    return self.limit

            settings = Settings('original', 0)

            @jinja2.pass_context
            def tick(context: Any) -> str:
                events.append('body')
                context.vars['settings'] = Settings('replacement', 100)
                settings.limit = 2
                return ''

            source = ('{% for x in values if x > settings.current_limit %}'
                      '{{loop.index}}:{{x}}{{tick()}};{% endfor %}')
            environment = jinja2.Environment(enable_async=asynchronous)
            context = {'values': [1, 2, 3], 'settings': settings, 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                ).render('attribute-rebinding', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:1;2:3;', ['limit:original:0', 'body',
                                                          'limit:original:2', 'limit:original:2', 'body']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_unrelated_template_constructs_do_not_eagerly_filter_metadata_loop(self) -> None:
        def observe(native: bool, asynchronous: bool, sandboxed: bool, prefix: str) -> tuple[str, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                return value % 2 == 1

            def emit(value: int) -> int:
                events.append(f'body:{value}')
                return value

            environment_type = SandboxedEnvironment if sandboxed else jinja2.Environment
            environment = environment_type(
                enable_async=asynchronous, loader=jinja2.DictLoader({'empty': ''})
            )
            environment.tests['selected'] = selected
            source = (prefix + '{% for x in values if x is selected %}'
                      '{{loop.index}}:{{emit(x)}};{% endfor %}')
            context = {'values': [1, 2, 3], 'emit': emit}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                ).render('unrelated-construct', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        prefixes = ('{% include "empty" %}', '{% macro unused() %}{% endmacro %}',
                    '{% if false %}{% include "empty" %}{% endif %}')
        for asynchronous in (False, True):
            for sandboxed in (False, True):
                for prefix in prefixes:
                    with self.subTest(asynchronous=asynchronous, sandboxed=sandboxed, prefix=prefix):
                        expected = observe(False, asynchronous, sandboxed, prefix)
                        self.assertEqual(expected, ('1:1;2:3;', ['filter:1', 'body:1', 'filter:2',
                                                                   'filter:3', 'body:3']))
                        self.assertEqual(observe(True, asynchronous, sandboxed, prefix), expected)

    def test_predicate_identifier_containing_loop_remains_lazy(self) -> None:
        def observe(native: bool, asynchronous: bool, as_test: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            state = {'limit': 0}

            def loopy(value: int) -> bool:
                events.append(f'filter:{value}:{state["limit"]}')
                return value > state['limit']

            def update() -> str:
                events.append('body')
                state['limit'] = 2
                return ''

            predicate = 'x is loopy' if as_test else 'loopy(x)'
            source = ('{% for x in values if ' + predicate + ' %}'
                      '{{loop.index}}:{{x}}{{update()}};{% endfor %}')
            environment = jinja2.Environment(enable_async=asynchronous)
            environment.tests['loopy'] = loopy
            context = {'values': [1, 2, 3], 'loopy': loopy, 'update': update}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                ).render('predicate-name', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for as_test in (False, True):
                with self.subTest(asynchronous=asynchronous, as_test=as_test):
                    expected = observe(False, asynchronous, as_test)
                    self.assertEqual(expected, ('1:1;2:3;', ['filter:1:0', 'body', 'filter:2:2',
                                                              'filter:3:2', 'body']))
                    self.assertEqual(observe(True, asynchronous, as_test), expected)

    def test_predicate_call_to_outer_loop_keeps_reference_error(self) -> None:
        source = ('{% for y in [1] %}'
                  '{% for x in [1,2] if loop(x) %}{{loop.index}}{% endfor %}'
                  '{% endfor %}')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                environment = jinja2.Environment(enable_async=asynchronous)
                with self.assertRaises(TypeError) as expected:
                    environment.from_string(source).render()
                with self.assertRaises(TypeError) as actual:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('outer-loop-call', source)
                self.assertEqual(str(actual.exception), str(expected.exception))

    def test_unrelated_loop_reference_does_not_eagerly_filter(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                return value % 2 == 1

            def emit(value: int) -> int:
                events.append(f'body:{value}')
                return value

            source = ('{% if false %}{{loop}}{% endif %}'
                      '{% for x in values if x is selected %}{{emit(x)}}{% endfor %}')
            environment = jinja2.Environment(enable_async=asynchronous)
            environment.tests['selected'] = selected
            context = {'values': [1, 2, 3], 'emit': emit}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                ).render('unrelated-loop', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('13', ['filter:1', 'body:1', 'filter:2', 'filter:3', 'body:3']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_filtered_inner_loop_uses_outer_loop_in_predicate(self) -> None:
        source = ('{% for y in [1,2] %}'
                  '{% for x in [1,2,3] if x > loop.index %}{{x}}{% endfor %};'
                  '{% endfor %}')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                environment = jinja2.Environment(enable_async=asynchronous)
                expected = environment.from_string(source).render()
                self.assertEqual(expected, '23;3;')
                actual = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {}
                ).render('nested-filtered-loop', source)
                self.assertEqual(actual, expected)

    def test_streamed_filter_scope_with_includes_macros_and_control_flow(self) -> None:
        cases = {
            'include': '{% include "part" %}',
            'macro': '{% macro show(v) %}{{emit(v)}}{% endmacro %}{{show(x)}}',
            'else': '{{emit(x)}}{% else %}empty',
            'break': '{{emit(x)}}{% break %}',
        }

        def observe(native: bool, asynchronous: bool, body: str) -> tuple[str, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                return value % 2 == 1

            def emit(value: int) -> int:
                events.append(f'body:{value}')
                return value

            environment = jinja2.Environment(
                enable_async=asynchronous,
                loader=jinja2.DictLoader({'part': '{{emit(x)}}'}),
                extensions=['jinja2.ext.loopcontrols'],
            )
            source = ('{% if false %}{{loop}}{% endif %}'
                      '{% for x in values if selected(x) %}' + body + '{% endfor %}')
            context = {'values': [1, 2, 3], 'selected': selected, 'emit': emit}
            if native:
                result = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                ).render('filtered-scope', source)
            else:
                result = environment.from_string(source).render(context)
            return result, events

        for asynchronous in (False, True):
            for case, body in cases.items():
                with self.subTest(asynchronous=asynchronous, case=case):
                    expected = observe(False, asynchronous, body)
                    if case == 'break':
                        self.assertEqual(expected, ('1', ['filter:1', 'body:1']))
                    else:
                        self.assertEqual(expected, ('13', ['filter:1', 'body:1', 'filter:2',
                                                          'filter:3', 'body:3']))
                    self.assertEqual(observe(True, asynchronous, body), expected)

    def test_unrelated_loop_reference_preserves_document_and_callback_order(self) -> None:
        source = template('{% if false %}{{loop}}{% endif %}'
                          '{% for x in values if selected(x) %}{{emit(x)}}{% endfor %}')

        def observe(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            def selected(value: int) -> bool:
                events.append(f'filter:{value}')
                return value % 2 == 1

            def emit(value: int) -> int:
                events.append(f'body:{value}')
                return value

            module: Any = docxtpl_native if native else docxtpl
            document = module.DocxTemplate(io.BytesIO(source))
            document.render({'values': [1, 2, 3], 'selected': selected, 'emit': emit},
                            jinja_env=jinja2.Environment(enable_async=asynchronous))
            output = io.BytesIO()
            document.save(output)
            return output.getvalue(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, expected = observe(False, asynchronous)
                actual, observed = observe(True, asynchronous)
                self.assertEqual(expected, ['filter:1', 'body:1', 'filter:2', 'filter:3', 'body:3'])
                self.assertEqual(observed, expected)
                self.assertTrue(compare_docx(reference, actual)['equivalent'])

    def test_attribute_predicate_observes_mutation_during_body(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Settings:
                limit = 0

                @property
                def current_limit(self) -> int:
                    events.append(f'limit:{self.limit}')
                    return self.limit

            settings = Settings()

            def tick() -> str:
                events.append('body')
                settings.limit = 2
                return ''

            source = ('{% for x in values if x > settings.current_limit %}'
                      '{{loop.index}}:{{x}}{{tick()}};{% endfor %}')
            context = {'values': [1, 2, 3], 'settings': settings, 'tick': tick}
            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context
                ).render('dynamic-attribute', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:1;2:3;', ['limit:0', 'body', 'limit:2', 'limit:2', 'body']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_context_comparison_preserves_document_and_mutation(self) -> None:
        source = template('{% for x in values if x > threshold %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}')

        def observe(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            class Threshold:
                value = 0

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}:{self.value}')
                    return bool(other > self.value)

            threshold = Threshold()

            def tick() -> str:
                events.append('body')
                threshold.value = 5
                return ''

            module: Any = docxtpl_native if native else docxtpl
            document = module.DocxTemplate(io.BytesIO(source))
            document.render({'values': iter((1, 2, 3)), 'threshold': threshold, 'tick': tick},
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

    def test_context_value_comparison_preserves_source_order(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def values() -> Iterator[int]:
                for value in range(3):
                    events.append(f'yield:{value}')
                    yield value

            def tick() -> str:
                events.append('body')
                return ''

            source = '{% for x in values if x > threshold %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': values(), 'threshold': 0, 'tick': tick}
            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:1;2:2;', ['yield:0', 'yield:1', 'body', 'yield:2', 'body']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_context_comparison_observes_body_mutation(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Threshold:
                value = 0

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}:{self.value}')
                    return bool(other > self.value)

            threshold = Threshold()

            def tick() -> str:
                events.append('body')
                threshold.value = 5
                return ''

            source = '{% for x in values if x > threshold %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': iter((1, 2, 3)), 'threshold': threshold, 'tick': tick}
            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:1;', ['compare:1:0', 'body', 'compare:2:5', 'compare:3:5']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_context_mutation_during_metadata_lookahead(self) -> None:
        def observe(native: bool, asynchronous: bool, field: str) -> tuple[str, list[str]]:
            events: list[str] = []

            class Threshold:
                value = 0

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}:{self.value}')
                    return bool(other > self.value)

            threshold = Threshold()

            def tick() -> str:
                events.append('body')
                threshold.value = 5
                return ''

            source = '{% for x in values if x > threshold %}{{loop.' + field + '}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': iter((1, 2, 3)), 'threshold': threshold, 'tick': tick}
            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for field in ('last', 'nextitem', 'length'):
                with self.subTest(asynchronous=asynchronous, field=field):
                    self.assertEqual(observe(True, asynchronous, field), observe(False, asynchronous, field))

    def test_filter_does_not_read_loop_body_local(self) -> None:
        def observe(native: bool, asynchronous: bool) -> str:
            source = ('{% for x in values if x > threshold %}'
                      '{{loop.index}}:{{x}};{% set threshold = 9 %}{% endfor %}')
            context = {'values': iter((1, 2, 3)), 'threshold': 0}
            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                return docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            return environment.from_string(source).render(context)

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, '1:1;2:2;3:3;')
                self.assertEqual(observe(True, asynchronous), expected)

    def test_context_rebinding_does_not_replace_captured_threshold(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            @jinja2.pass_context
            def tick(context: Any) -> str:
                events.append('body')
                context.vars['threshold'] = 99
                return ''

            source = '{% for x in values if x > threshold %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': iter((1, 2)), 'threshold': 0, 'tick': tick}
            environment = jinja2.Environment(enable_async=asynchronous)
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:1;2:2;', ['body', 'body']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_threshold_resolved_before_empty_iterable(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            def values() -> Iterator[int]:
                events.append('iterate')
                yield from ()

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values(), 'threshold': 0}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('', ['resolve:values', 'resolve:threshold', 'iterate']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_context_comparisons_in_both_operand_orders(self) -> None:
        def observe(native: bool, asynchronous: bool, expression: str) -> tuple[str, list[str]]:
            events: list[str] = []

            def values() -> Iterator[int]:
                for value in range(3):
                    events.append(f'yield:{value}')
                    yield value

            def tick() -> str:
                events.append('body')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in values if ' + expression + ' %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': values(), 'threshold': 1, 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for operator in ('==', '!=', '<', '<=', '>', '>='):
                for expression in (f'x {operator} threshold', f'threshold {operator} x'):
                    with self.subTest(asynchronous=asynchronous, expression=expression):
                        self.assertEqual(observe(True, asynchronous, expression), observe(False, asynchronous, expression))

    def test_context_threshold_with_async_iterator_and_lookahead(self) -> None:
        def observe(native: bool, field: str) -> tuple[str, list[str]]:
            events: list[str] = []

            async def values() -> AsyncIterator[int]:
                for value in range(4):
                    events.append(f'yield:{value}')
                    yield value

            def tick() -> str:
                events.append('body')
                return ''

            environment = jinja2.Environment(enable_async=True)
            source = '{% for x in values if x > threshold %}{{loop.' + field + '}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': values(), 'threshold': 0, 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for field in ('index', 'last', 'nextitem', 'length', 'revindex'):
            with self.subTest(field=field):
                self.assertEqual(observe(True, field), observe(False, field))

    def test_missing_threshold_raises_only_when_predicate_executes(self) -> None:
        def observe(native: bool, asynchronous: bool, values: list[int]) -> tuple[str, str]:
            environment = jinja2.Environment(enable_async=asynchronous, undefined=jinja2.StrictUndefined)
            source = '{% for x in values if x > threshold %}{{loop.index}}{% endfor %}'
            try:
                if native:
                    result = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'values': values},
                    ).render('dynamic-comparison', source)
                else:
                    result = environment.from_string(source).render(values=values)
                return 'ok', result
            except Exception as error:  # noqa: BLE001 - compare the original exception boundary
                return type(error).__name__, str(error)

        for asynchronous in (False, True):
            for values in ([], [1]):
                with self.subTest(asynchronous=asynchronous, values=values):
                    self.assertEqual(observe(True, asynchronous, values), observe(False, asynchronous, values))

    def test_threshold_resolution_order_with_iterable_factory_call(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            def values() -> Iterator[int]:
                events.append('factory')
                return iter([1])

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values() if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values, 'threshold': 0}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1', ['resolve:values', 'factory', 'resolve:threshold']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_factory_source_preserves_filter_body_order(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def values() -> Iterator[int]:
                events.append('factory')
                for value in range(3):
                    events.append(f'yield:{value}')
                    yield value

            def tick() -> str:
                events.append('body')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in values() if x > threshold %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': values, 'threshold': 0, 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:1;2:2;', ['factory', 'yield:0', 'yield:1', 'body', 'yield:2', 'body']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_callable_factory_instance_is_called_once(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Factory:
                def __call__(self) -> Iterator[int]:
                    events.append('factory')
                    return iter((1, 2, 3))

            def tick() -> str:
                events.append('body')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in values() if x > threshold %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': Factory(), 'threshold': 1, 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_factory_and_body_mutations_reach_filtered_document(self) -> None:
        source = template('{% for x in values() if x > threshold %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}')

        def observe(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            class Threshold:
                value = 0

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}:{self.value}')
                    return bool(other > self.value)

            threshold = Threshold()

            def values() -> Iterator[int]:
                events.append('factory')
                threshold.value = 1
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

    def test_factory_source_opens_after_threshold_resolution(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Source:
                def __iter__(self) -> Iterator[int]:
                    events.append('iter')
                    raise RuntimeError('source failed')

            def values() -> Source:
                events.append('factory')
                return Source()

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values() if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values, 'threshold': 0}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('dynamic-comparison', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                return str(error), events
            raise AssertionError('expected source failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_factory_exception_precedes_threshold_resolution(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            def values() -> Iterator[int]:
                events.append('factory')
                raise RuntimeError('factory failed')

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values() if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values, 'threshold': 0}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('dynamic-comparison', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                return str(error), events
            raise AssertionError('expected factory failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('factory failed', ['resolve:values', 'factory']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_factory_can_rebind_threshold_before_filter_resolution(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            @jinja2.pass_context
            def values(context: Any) -> Iterator[int]:
                events.append('factory')
                context.vars['threshold'] = 1
                return iter((1, 2, 3))

            def tick() -> str:
                events.append('body')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in values() if x > threshold %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': values, 'threshold': 0, 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_sandbox_rejects_unsafe_factory_before_filter(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            @unsafe
            def values() -> Iterator[int]:
                events.append('factory')
                return iter((1, 2))

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for x in values() if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': values, 'threshold': 0}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('dynamic-comparison', source)
                else:
                    environment.from_string(source).render(context)
            except Exception as error:  # noqa: BLE001 - compare security boundary
                return type(error).__name__, events
            raise AssertionError('expected unsafe factory rejection')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('SecurityError', []))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_sandbox_allows_safe_factory_with_lazy_filter(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def values() -> Iterator[int]:
                events.append('factory')
                for value in range(3):
                    events.append(f'yield:{value}')
                    yield value

            def tick() -> str:
                events.append('body')
                return ''

            environment = SandboxedEnvironment(enable_async=asynchronous)
            source = '{% for x in values() if x > threshold %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': values, 'threshold': 0, 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_custom_callable_policy_observes_factory(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Environment(SandboxedEnvironment):
                def is_safe_callable(self, obj: Any) -> bool:
                    events.append(f'call:{getattr(obj, "__name__", type(obj).__name__)}')
                    return True

            def values() -> Iterator[int]:
                events.append('factory')
                return iter((1, 2))

            environment = Environment(enable_async=asynchronous)
            source = '{% for x in values() if x > threshold %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': values, 'threshold': 0}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_factory_and_threshold_same_binding_preserves_identity(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Factory:
                def __call__(self) -> Iterator[int]:
                    events.append('factory')
                    return iter((1, 2))

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}')
                    return True

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values() if x > values %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': Factory()}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_shared_factory_filter_observes_body_mutation(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Factory:
                limit = 0

                def __call__(self) -> Iterator[int]:
                    events.append('factory')
                    return iter((1, 2, 3))

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}:{self.limit}')
                    return bool(other > self.limit)

            factory = Factory()

            def tick() -> str:
                events.append('body')
                factory.limit = 5
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in values() if x > values %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': factory, 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('shared-factory', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:1;', ['factory', 'compare:1:0', 'body', 'compare:2:5', 'compare:3:5']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_shared_async_factory_preserves_lookahead(self) -> None:
        def observe(native: bool, field: str) -> tuple[str, list[str]]:
            events: list[str] = []

            class Factory:
                async def __call__(self) -> AsyncIterator[int]:
                    events.append('factory')

                    async def items() -> AsyncIterator[int]:
                        for value in (1, 2, 3):
                            events.append(f'yield:{value}')
                            yield value

                    return items()

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}')
                    return True

            def tick() -> str:
                events.append('body')
                return ''

            environment = jinja2.Environment(enable_async=True)
            source = '{% for x in values() if x > values %}{{loop.' + field + '}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': Factory(), 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('shared-factory', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for field in ('index', 'last', 'nextitem', 'length'):
            with self.subTest(field=field):
                self.assertEqual(observe(True, field), observe(False, field))

    def test_shared_factory_mutation_preserves_complete_document(self) -> None:
        source = template('{% for x in values() if x > values %}{{loop.index}}:{{x}}{{tick()}};{% endfor %}')

        def observe(native: bool, asynchronous: bool) -> tuple[bytes, list[str]]:
            events: list[str] = []

            class Factory:
                limit = 0

                def __call__(self) -> Iterator[int]:
                    events.append('factory')
                    return iter((1, 2, 3))

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}:{self.limit}')
                    return bool(other > self.limit)

            factory = Factory()

            def tick() -> str:
                events.append('body')
                factory.limit = 5
                return ''

            module: Any = docxtpl_native if native else docxtpl
            document = module.DocxTemplate(io.BytesIO(source))
            document.render({'values': factory, 'tick': tick},
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

    def test_shared_factory_obeys_custom_sandbox_callable_policy(self) -> None:
        def observe(native: bool, asynchronous: bool, allowed: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Environment(SandboxedEnvironment):
                def is_safe_callable(self, obj: Any) -> bool:
                    events.append('policy')
                    return allowed

            class Factory:
                def __call__(self) -> Iterator[int]:
                    events.append('factory')
                    return iter((1, 2))

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}')
                    return True

            environment = Environment(enable_async=asynchronous)
            source = '{% for x in values() if x > values %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': Factory()}
            try:
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('shared-factory', source)
                else:
                    output = environment.from_string(source).render(context)
                return output, events
            except Exception as error:  # noqa: BLE001 - compare security boundary
                return type(error).__name__, events

        for asynchronous in (False, True):
            for allowed in (False, True):
                with self.subTest(asynchronous=asynchronous, allowed=allowed):
                    self.assertEqual(observe(True, asynchronous, allowed), observe(False, asynchronous, allowed))

    def test_shared_factory_rebinding_during_call_keeps_original_object(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Factory:
                @jinja2.pass_context
                def __call__(self, context: Any) -> Iterator[int]:
                    events.append('factory')
                    context.vars['values'] = object()
                    return iter((1, 2))

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}')
                    return True

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% for x in values() if x > values %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': Factory()}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('shared-factory', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('1:1;2:2;', ['factory', 'compare:1', 'compare:2']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_shared_factory_exception_preserves_object_and_resolution_order(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, str, list[str]]:
            events: list[str] = []
            failure = RuntimeError('factory failed')

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Factory:
                def __call__(self) -> Iterator[int]:
                    events.append('factory')
                    raise failure

                def __lt__(self, other: Any) -> bool:
                    events.append('compare')
                    return True

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values() if x > values %}{{loop.index}}{% endfor %}'
            context = {'values': Factory()}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('shared-factory', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                return error is failure, str(error), events
            raise AssertionError('expected factory failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, (True, 'factory failed', ['resolve:values', 'factory']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_shared_factory_missing_value_preserves_exception(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str]:
            environment = jinja2.Environment(enable_async=asynchronous, undefined=jinja2.StrictUndefined)
            source = '{% for x in values() if x > values %}{{loop.index}}{% endfor %}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {},
                    ).render('shared-factory', source)
                else:
                    environment.from_string(source).render()
            except Exception as error:  # noqa: BLE001 - compare original exception
                return type(error).__name__, str(error)
            raise AssertionError('expected missing factory failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_async_factory_preserves_lookahead_and_body_order(self) -> None:
        def observe(native: bool, field: str) -> tuple[str, list[str]]:
            events: list[str] = []

            async def items() -> AsyncIterator[int]:
                for value in range(4):
                    events.append(f'yield:{value}')
                    yield value

            async def values() -> AsyncIterator[int]:
                events.append('factory')
                return items()

            def tick() -> str:
                events.append('body')
                return ''

            environment = jinja2.Environment(enable_async=True)
            source = '{% for x in values() if x > threshold %}{{loop.' + field + '}}:{{x}}{{tick()}};{% endfor %}'
            context = {'values': values, 'threshold': 0, 'tick': tick}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for field in ('index', 'last', 'nextitem', 'length'):
            with self.subTest(field=field):
                self.assertEqual(observe(True, field), observe(False, field))

    def test_source_and_threshold_same_binding_are_resolved_once(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Source:
                def __iter__(self) -> Iterator[int]:
                    events.append('iter')
                    return iter((1, 2))

                def __lt__(self, other: Any) -> bool:
                    events.append(f'compare:{other}')
                    return True

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values if x > values %}{{loop.index}}:{{x}};{% endfor %}'
            context = {'values': Source()}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('dynamic-comparison', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_threshold_resolves_before_iterable_error(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Source:
                def __iter__(self) -> Iterator[int]:
                    events.append('iter')
                    raise RuntimeError('source failed')

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% for x in values if x > threshold %}{{loop.index}}{% endfor %}'
            context = {'values': Source(), 'threshold': 0}
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('dynamic-comparison', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                return str(error), events
            raise AssertionError('expected source failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('source failed', ['resolve:values', 'resolve:threshold', 'iter']))
                self.assertEqual(observe(True, asynchronous), expected)


if __name__ == '__main__':
    unittest.main()
