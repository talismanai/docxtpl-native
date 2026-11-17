"""Required block checks must use Python's observable len and comparison."""

import builtins
import types
import unittest
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import runtime
from jinja2.ext import Extension


class RequiredLengthTests(unittest.TestCase):
    def test_callable_from_code_exposes_its_actual_template_builtins(self) -> None:
        def observe(native: bool, asynchronous: bool) -> str:
            selected: list[Any] = []
            original_len = builtins.len

            def custom_len(value: Any) -> int:
                return 2 if value is selected else original_len(value)

            original = jinja2.Template.from_code.__func__
            globals_copy = dict(original.__globals__)
            globals_copy['__builtins__'] = {**vars(builtins), 'len': custom_len}
            cloned = types.FunctionType(original.__code__, globals_copy, original.__name__,
                                        original.__defaults__, original.__closure__)

            class FromCode:
                def __call__(self, environment: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                    return cloned(jinja2.Template, environment, code, globals, uptodate)

            def replacement(context: Any) -> Any:
                yield 'CUSTOM'

            async def async_replacement(context: Any) -> Any:
                yield 'CUSTOM'

            @jinja2.pass_context
            def install(context: Any) -> str:
                selected.append(async_replacement if asynchronous else replacement)
                context.blocks['body'] = selected
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{ install() }}{% block body required %}{% endblock %}'
            with patch.object(jinja2.Template, 'from_code', FromCode()):
                if native:
                    return docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'install': install}
                    ).render('case', source)
                return environment.from_string(source).render(install=install)

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(False, asynchronous), 'CUSTOM')
                self.assertEqual(observe(True, asynchronous), 'CUSTOM')

    def test_callable_from_code_with_required_replacement(self) -> None:
        original = jinja2.Template.from_code

        class FromCode:
            def __call__(self, environment: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                return original(environment, code, globals, uptodate)

        def replacement(context: Any) -> Any:
            yield 'CUSTOM'

        async def async_replacement(context: Any) -> Any:
            yield 'CUSTOM'

        for native in (False, True):
            for asynchronous in (False, True):
                with self.subTest(native=native, asynchronous=asynchronous), patch.object(
                    jinja2.Template, 'from_code', FromCode()
                ):
                    @jinja2.pass_context
                    def install(context: Any, async_mode: bool = asynchronous) -> str:
                        block = async_replacement if async_mode else replacement
                        context.blocks['body'] = [block, block]
                        return ''

                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{{ install() }}{% block body required %}{% endblock %}'
                    if native:
                        output = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'install': install}
                        ).render('case', source)
                    else:
                        output = environment.from_string(source).render(install=install)
                    self.assertEqual(output, 'CUSTOM')

    def test_callable_from_code_does_not_break_plain_render(self) -> None:
        original = jinja2.Template.from_code

        class FromCode:
            def __call__(self, environment: Any, code: Any, globals: Any, uptodate: Any) -> Any:
                return original(environment, code, globals, uptodate)

        for native in (False, True):
            for asynchronous in (False, True):
                with self.subTest(native=native, asynchronous=asynchronous), patch.object(
                    jinja2.Template, 'from_code', FromCode()
                ):
                    environment = jinja2.Environment(enable_async=asynchronous)
                    if native:
                        output = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {}
                        ).render('case', 'plain')
                    else:
                        output = environment.from_string('plain').render()
                    self.assertEqual(output, 'plain')

    def test_missing_len_raises_before_block_mapping_access(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str, Any, list[str]]:
            events: list[str] = []
            selected_builtins = dict(vars(builtins))

            class Blocks(dict[str, Any]):
                def __getitem__(self, key: str) -> Any:
                    events.append('lookup')
                    return super().__getitem__(key)

            @jinja2.pass_context
            def install(context: Any) -> str:
                context.blocks = Blocks(body=[])
                selected_builtins.pop('len')
                return ''

            original = jinja2.Template.from_code.__func__
            globals_copy = dict(original.__globals__)
            globals_copy['__builtins__'] = selected_builtins
            cloned = types.FunctionType(original.__code__, globals_copy, original.__name__,
                                        original.__defaults__, original.__closure__)
            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{ install() }}{% block body required %}{% endblock %}'
            with patch.object(jinja2.Template, 'from_code', classmethod(cloned)):
                try:
                    if native:
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {'install': install}
                        ).render('case', source)
                    else:
                        environment.from_string(source).render(install=install)
                except Exception as error:  # noqa: BLE001 - compare error and ordering
                    return type(error).__name__, str(error), getattr(error, 'name', None), events
            raise AssertionError('expected missing len')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('NameError', "name 'len' is not defined", 'len', [])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_plain_extension_reads_template_class_before_preprocessing(self) -> None:
        def observe(native: bool) -> list[str]:
            events: list[str] = []

            original_class = jinja2.Environment.template_class

            class TemplateClass:
                def __get__(self, instance: Any, owner: Any) -> Any:
                    events.append('template_class')
                    return original_class

            class Preprocess(Extension):
                def preprocess(self, source: str, name: str | None,
                               filename: str | None = None) -> str:
                    events.append('preprocess')
                    return source

            with patch.object(jinja2.Environment, 'template_class', TemplateClass()):
                environment = jinja2.Environment(extensions=[Preprocess])
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                    events.clear()
                    session.render('case', 'plain')
                else:
                    events.clear()
                    environment.from_string('plain').render()
            return events

        self.assertEqual(observe(False), ['template_class', 'preprocess'])
        self.assertEqual(observe(True), observe(False))

    def test_direct_template_retains_from_code_builtins_across_preprocessing(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            selected: list[Any] = []
            original_len = builtins.len

            def first(value: Any) -> int:
                if value is selected:
                    events.append('first')
                    return 2
                return original_len(value)

            def second(value: Any) -> int:
                if value is selected:
                    events.append('second')
                    return 1
                return original_len(value)

            original = jinja2.Template.from_code.__func__

            def copy_with_len(callback: Any) -> types.FunctionType:
                globals_copy = dict(original.__globals__)
                globals_copy['__builtins__'] = {**vars(builtins), 'len': callback}
                return types.FunctionType(original.__code__, globals_copy, original.__name__,
                                          original.__defaults__, original.__closure__)

            replacement_method = copy_with_len(second)

            class Preprocess(Extension):
                def preprocess(self, source: str, name: str | None,
                               filename: str | None = None) -> str:
                    jinja2.Template.from_code = classmethod(replacement_method)  # ty: ignore[invalid-assignment]
                    return source

            def replacement(context: Any) -> Any:
                yield 'CUSTOM'

            async def async_replacement(context: Any) -> Any:
                yield 'CUSTOM'

            @jinja2.pass_context
            def install(context: Any) -> str:
                selected.append(async_replacement if asynchronous else replacement)
                context.blocks['body'] = selected
                return ''

            environment = jinja2.Environment(enable_async=asynchronous, extensions=[Preprocess])
            source = '{{ install() }}{% block body required %}{% endblock %}'
            with patch.object(jinja2.Template, 'from_code', classmethod(copy_with_len(first))):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'install': install}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(install=install)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('CUSTOM', ['first'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_loader_template_class_lookup_follows_preprocessing(self) -> None:
        def observe(native: bool, asynchronous: bool) -> list[str]:
            events: list[str] = []

            class Environment(jinja2.Environment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'template_class':
                        events.append('template_class')
                    return super().__getattribute__(name)

            class Preprocess(Extension):
                def preprocess(self, source: str, name: str | None,
                               filename: str | None = None) -> str:
                    events.append('child' if source == 'child' else 'root')
                    return source

            environment = Environment(enable_async=asynchronous, loader=jinja2.DictLoader({'child': 'child'}),
                                      extensions=[Preprocess])
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                events.clear()
                session.render('case', '{% include "child" %}')
            else:
                events.clear()
                environment.from_string('{% include "child" %}').render()
            return events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ['template_class', 'root', 'child', 'template_class']
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_template_class_lookup_precedes_preprocessing(self) -> None:
        def observe(native: bool, asynchronous: bool) -> list[str]:
            events: list[str] = []

            class Environment(jinja2.Environment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'template_class':
                        events.append('template_class')
                    return super().__getattribute__(name)

            class Preprocess(Extension):
                def preprocess(self, source: str, name: str | None,
                               filename: str | None = None) -> str:
                    events.append('preprocess')
                    return source

            environment = Environment(enable_async=asynchronous, extensions=[Preprocess])
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                events.clear()
                session.render('case', '{{ value }}')
            else:
                events.clear()
                environment.from_string('{{ value }}').render()
            return events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_len_is_resolved_before_block_mapping_lookup(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original_len = builtins.len
            selected: list[Any] = []

            def first(value: Any) -> int:
                if value is selected:
                    events.append('first len')
                    return 2
                return original_len(value)

            def second(value: Any) -> int:
                if value is selected:
                    events.append('second len')
                    return 1
                return original_len(value)

            class Blocks(dict[str, Any]):
                def __getitem__(self, key: str) -> Any:
                    events.append('lookup')
                    builtins.len = second  # ty: ignore[invalid-assignment]
                    return super().__getitem__(key)

            def replacement(context: Any) -> Any:
                yield 'CUSTOM'

            async def async_replacement(context: Any) -> Any:
                yield 'CUSTOM'

            @jinja2.pass_context
            def install(context: Any) -> str:
                selected.append(async_replacement if asynchronous else replacement)
                context.blocks = Blocks(body=selected)
                builtins.len = first  # ty: ignore[invalid-assignment]
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{ install() }}{% block body required %}{% endblock %}'
            with patch.object(builtins, 'len', original_len):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'install': install}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(install=install)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('CUSTOM', ['lookup', 'first len', 'lookup'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_template_class_lookup_is_not_added_during_render(self) -> None:
        def observe(native: bool, asynchronous: bool) -> list[str]:
            events: list[str] = []

            class Environment(jinja2.Environment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'template_class':
                        events.append('template_class')
                    return super().__getattribute__(name)

            environment = Environment(enable_async=asynchronous)
            if native:
                configuration = BridgeConfig(environment, False)
                events.clear()
                docxtpl_native._extension().PythonRenderSession(configuration, {}).render('case', 'text')
            else:
                events.clear()
                environment.from_string('text').render()
            return events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_template_uses_builtins_captured_by_from_code(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            selected: list[Any] = []
            original_len = builtins.len

            def custom_len(value: Any) -> int:
                if value is selected:
                    events.append('captured builtins')
                    return 2
                return original_len(value)

            def replacement(context: Any) -> Any:
                yield 'CUSTOM'

            async def async_replacement(context: Any) -> Any:
                yield 'CUSTOM'

            @jinja2.pass_context
            def install(context: Any) -> str:
                selected.append(async_replacement if asynchronous else replacement)
                context.blocks['body'] = selected
                return ''

            from_code = jinja2.Template.from_code.__func__
            globals_copy = dict(from_code.__globals__)
            globals_copy['__builtins__'] = {**vars(builtins), 'len': custom_len}
            cloned = types.FunctionType(from_code.__code__, globals_copy, from_code.__name__,
                                        from_code.__defaults__, from_code.__closure__)
            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{ install() }}{% block body required %}{% endblock %}'
            with patch.object(jinja2.Template, 'from_code', classmethod(cloned)):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'install': install}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(install=install)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('CUSTOM', ['captured builtins'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_len_and_comparison_exceptions_keep_identity(self) -> None:
        def run(native: bool, asynchronous: bool, stage: str) -> None:
            original_len = builtins.len
            selected: list[Any] = []
            marker = ValueError(stage)

            class Truth:
                def __bool__(self) -> bool:
                    raise marker

            class Compared:
                def __le__(self, other: object) -> Any:
                    if stage == 'compare':
                        raise marker
                    return Truth()

            def custom_len(value: Any) -> Any:
                if value is selected:
                    if stage == 'len':
                        raise marker
                    return Compared()
                return original_len(value)

            @jinja2.pass_context
            def install(context: Any) -> str:
                context.blocks['body'] = selected
                builtins.len = custom_len  # ty: ignore[invalid-assignment]
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{ install() }}{% block body required %}{% endblock %}'
            with patch.object(builtins, 'len', original_len), self.assertRaises(ValueError) as caught:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'install': install}
                    ).render('case', source)
                else:
                    environment.from_string(source).render(install=install)
            self.assertIs(caught.exception, marker)

        for native in (False, True):
            for asynchronous in (False, True):
                for stage in ('len', 'compare', 'truth'):
                    with self.subTest(native=native, asynchronous=asynchronous, stage=stage):
                        run(native, asynchronous, stage)

    def test_runtime_exported_len_is_captured_at_compilation(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def captured(value: Any) -> int:
                events.append('captured')
                return 2

            def replacement(context: Any) -> Any:
                yield 'CUSTOM'

            async def async_replacement(context: Any) -> Any:
                yield 'CUSTOM'

            class Context(runtime.Context):
                def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                             globals: Any = None) -> None:
                    super().__init__(environment, parent, name, blocks, globals=globals)
                    self.blocks['body'] = [async_replacement if asynchronous else replacement]
                    setattr(runtime, 'len', lambda value: 1)  # noqa: B010 - dynamic runtime export

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% block body required %}{% endblock %}'
            with patch.object(runtime, 'exported', [*runtime.exported, 'len']), patch.object(
                runtime, 'len', captured, create=True
            ):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render()
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(False, asynchronous), ('CUSTOM', ['captured']))
                self.assertEqual(observe(True, asynchronous), ('CUSTOM', ['captured']))

    def test_custom_len_and_comparison_control_required_block(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original_len = builtins.len
            selected: list[Any] = []

            class Compared:
                def __le__(self, other: object) -> bool:
                    events.append(f'compare:{other}')
                    return False

            def custom_len(value: Any) -> Any:
                if value is selected:
                    events.append('len')
                    return Compared()
                return original_len(value)

            def replacement(context: Any) -> Any:
                yield 'CUSTOM'

            async def async_replacement(context: Any) -> Any:
                yield 'CUSTOM'

            @jinja2.pass_context
            def install(context: Any) -> str:
                selected.append(async_replacement if asynchronous else replacement)
                context.blocks['body'] = selected
                builtins.len = custom_len  # ty: ignore[invalid-assignment]
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{{ install() }}{% block body required %}{% endblock %}'
            with patch.object(builtins, 'len', original_len):
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'install': install}
                    ).render('case', source)
                else:
                    output = environment.from_string(source).render(install=install)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('CUSTOM', ['len', 'compare:1'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)
