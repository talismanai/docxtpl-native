"""Differential checks for root-frame name resolution and context visibility."""

import unittest
from typing import Any

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig


class RootSymbolActivationTests(unittest.TestCase):
    def test_cached_template_binding_does_not_hide_live_context_mutation(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            @jinja2.pass_context
            def mutate(context: Any) -> str:
                context.vars['value'] = 'new'
                events.append('mutate')
                return ''

            @jinja2.pass_context
            def live(context: Any) -> str:
                events.append(f'live:{context.get("value")}')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{{mutate()}}{{value}}{{live()}}'
            context = {'mutate': mutate, 'value': 'old', 'live': live}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('root-bindings', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('old', ['resolve:mutate', 'resolve:value', 'resolve:live',
                                                     'mutate', 'resolve:value', 'live:new']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_root_assignment_and_unassigned_branch_match_context_visibility(self) -> None:
        def observe(native: bool, asynchronous: bool, condition: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% if flag %}{% set value = "assigned" %}{% endif %}{{value}}'
            context = {'flag': condition, 'value': 'original'}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('root-bindings', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            for condition in (False, True):
                with self.subTest(asynchronous=asynchronous, condition=condition):
                    self.assertEqual(observe(True, asynchronous, condition), observe(False, asynchronous, condition))

    def test_missing_binding_materializes_once_per_actual_use(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(f'undefined:{kwargs.get("name")}')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            source = '{{missing}}{{missing}}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {},
                ).render('root-bindings', source)
            else:
                output = environment.from_string(source).render({})
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_include_captures_child_context_without_overwriting_parent_binding(self) -> None:
        def observe(native: bool, asynchronous: bool) -> str:
            @jinja2.pass_context
            def mutate(context: Any) -> str:
                context.vars['value'] = 'new'
                return ''

            environment = jinja2.Environment(enable_async=asynchronous, loader=jinja2.DictLoader({'child': '{{value}}'}))
            source = "{{mutate()}}{% include 'child' %}{{value}}"
            context = {'mutate': mutate, 'value': 'old'}
            if native:
                return docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('root-bindings', source)
            return environment.from_string(source).render(context)

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, 'newold')
                self.assertEqual(observe(True, asynchronous), expected)

    def test_unexecuted_branch_resolves_name_without_constructing_undefined(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append(f'resolve:{key}')
                    return super().resolve_or_missing(key)

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('undefined')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            environment.context_class = Context
            source = '{% if flag %}{{absent}}{% endif %}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'flag': False},
                ).render('root-bindings', source)
            else:
                output = environment.from_string(source).render({'flag': False})
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_root_bindings_keep_values_and_release_in_reference_order(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            context: dict[str, Any] = {}

            class Number:
                def __init__(self, name: str) -> None:
                    self.name = name

                @property
                def value(self) -> str:
                    events.append(f'value:{self.name}')
                    return self.name

                def __del__(self) -> None:
                    events.append(f'drop:{self.name}')

            @jinja2.pass_context
            def mutate(jinja_context: Any) -> str:
                events.append('mutate')
                for name in ('omega', 'alpha'):
                    jinja_context.parent[name] = None
                    context[name] = None
                return ''

            def tick() -> str:
                events.append('tick')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{mutate()}}{{omega.value}}{{alpha.value}}{{tick()}}'
            context.update({'mutate': mutate, 'omega': Number('omega'), 'alpha': Number('alpha'), 'tick': tick})
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('root-bindings', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_missing_named_callable_materializes_before_argument_callback(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Undefined(jinja2.Undefined):
                jinja_pass_arg = None

                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(f'undefined:{kwargs.get("name")}')
                    super().__init__(*args, **kwargs)

                # Deliberately exercise a user-defined callable Undefined.
                def __call__(self, value: Any) -> str:  # ty: ignore[invalid-method-override]
                    events.append(f'call:{value}')
                    return 'done'

            def argument() -> str:
                events.append('argument')
                return 'arg'

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            source = '{{absent(argument())}}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'argument': argument},
                ).render('root-bindings', source)
            else:
                output = environment.from_string(source).render({'argument': argument})
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('done', ['undefined:absent', 'argument', 'call:arg']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_missing_callable_constructor_failure_prevents_argument_callback(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            failure = RuntimeError('construct callable')

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(f'undefined:{kwargs.get("name")}')
                    raise failure

            def argument() -> str:
                events.append('argument')
                return 'arg'

            environment = jinja2.Environment(enable_async=asynchronous, undefined=Undefined)
            source = '{{absent(argument())}}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'argument': argument},
                    ).render('root-bindings', source)
                else:
                    environment.from_string(source).render({'argument': argument})
            except RuntimeError as error:
                return error is failure, events
            raise AssertionError('expected callable constructor failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, (True, ['undefined:absent']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_named_callable_is_retained_when_argument_rebinds_context(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def original(value: str) -> str:
                events.append(f'original:{value}')
                return 'old'

            def replacement(value: str) -> str:
                events.append(f'replacement:{value}')
                return 'new'

            @jinja2.pass_context
            def argument(context: Any) -> str:
                events.append('argument')
                context.vars['func'] = replacement
                return 'arg'

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{func(argument())}}'
            context = {'func': original, 'argument': argument}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), context,
                ).render('root-bindings', source)
            else:
                output = environment.from_string(source).render(context)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('old', ['argument', 'original:arg']))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_root_binding_lifetime_when_render_raises(self) -> None:
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
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), context,
                    ).render('root-bindings', source)
                else:
                    environment.from_string(source).render(context)
            except RuntimeError as error:
                events.append('caught')
                error.__traceback__ = None
            else:
                raise AssertionError('expected rendering failure')
            events.append('after')
            return events.copy()

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))


if __name__ == '__main__':
    unittest.main()
