"""Differential checks for the block registry visible to Python contexts."""

import asyncio
import inspect
import sys
import unittest
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import runtime
from jinja2.sandbox import SandboxedEnvironment
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


class BlockRegistryTests(unittest.TestCase):
    def test_replaced_block_can_invoke_macro_after_suspension(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            @jinja2.pass_context
            def install(context: Any, macro: Any) -> str:
                def replacement(current: Any) -> Any:
                    events.append('block')
                    yield macro()

                async def replacement_async(current: Any) -> Any:
                    await asyncio.sleep(0)
                    events.append('block')
                    yield await macro()

                context.blocks['body'] = [replacement_async if asynchronous else replacement]
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% macro m() %}M{% endmacro %}{{install(m)}}{% block body %}ORIGINAL{% endblock %}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'install': install}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(install=install)
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('M', ['block'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_block_lookup_can_call_native_macro(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            @jinja2.pass_context
            def install(context: Any, macro: Any) -> str:
                def replacement(current: Any) -> Any:
                    events.append('block')
                    yield 'REPLACED'

                class Blocks(dict[str, Any]):
                    def __getitem__(self, key: str) -> Any:
                        events.append('macro:' + macro())
                        return super().__getitem__(key)

                context.blocks = Blocks(body=[replacement])
                return ''

            environment = jinja2.Environment()
            source = '{% macro m() %}M{% endmacro %}{{install(m)}}{% block body %}ORIGINAL{% endblock %}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'install': install}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(install=install)
            return output, events

        expected = ('REPLACED', ['macro:M', 'block'])
        self.assertEqual(observe(False), expected)
        self.assertEqual(observe(True), expected)

    def test_required_stack_length_can_call_native_macro(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            @jinja2.pass_context
            def install(context: Any, macro: Any) -> str:
                def replacement(current: Any) -> Any:
                    events.append('block')
                    yield 'REPLACED'

                class Stack(list[Any]):
                    def __len__(self) -> int:
                        events.append('macro:' + macro())
                        return 2

                context.blocks['body'] = Stack([replacement])
                return ''

            environment = jinja2.Environment()
            source = ('{% macro m() %}M{% endmacro %}{{install(m)}}'
                      '{% block body required %}{% endblock %}')
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'install': install}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(install=install)
            return output, events

        expected = ('REPLACED', ['macro:M', 'block'])
        self.assertEqual(observe(False), expected)
        self.assertEqual(observe(True), expected)

    def test_sandbox_required_check_precedes_replaced_block(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    events: list[str] = []

                    class Sandbox(SandboxedEnvironment):
                        def __init__(self, record: list[str], **kwargs: Any) -> None:
                            super().__init__(**kwargs)
                            self.record = record

                        def is_safe_callable(self, obj: Any) -> bool:
                            self.record.append('policy')
                            return True

                    class Context(runtime.Context):
                        def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                                     globals: Any = None, record: list[str] = events,
                                     async_mode: bool = asynchronous) -> None:
                            def replacement(context: Any) -> Any:
                                record.append('block')
                                yield 'REPLACED'

                            async def replacement_async(context: Any) -> Any:
                                record.append('block')
                                yield 'REPLACED'

                            blocks['body'] = replacement_async if async_mode else replacement
                            super().__init__(environment, parent, name, blocks, globals=globals)

                    environment = Sandbox(events, enable_async=asynchronous)
                    environment.context_class = Context
                    source = '{% block body required %}{% endblock %}'
                    with self.assertRaises(jinja2.TemplateRuntimeError) as raised:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render()
                    self.assertEqual(str(raised.exception), "Required block 'body' not found")
                    self.assertEqual(events, [])

    def test_removed_required_block_keeps_missing_key_error(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    class Context(runtime.Context):
                        def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                                     globals: Any = None) -> None:
                            super().__init__(environment, parent, name, blocks, globals=globals)
                            self.blocks.pop('body', None)

                    environment = jinja2.Environment(enable_async=asynchronous)
                    environment.context_class = Context
                    source = '{% block body required %}{% endblock %}'
                    with self.assertRaises(KeyError) as raised:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render()
                    self.assertEqual(raised.exception.args, ('body',))

    def test_required_stack_length_error_precedes_replacement(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    original = ValueError('length failed')
                    class Stack(list[Any]):
                        def __init__(self, error: ValueError) -> None:
                            super().__init__([None])
                            self.error = error

                        def __len__(self) -> int:
                            raise self.error

                    class Context(runtime.Context):
                        def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                                     globals: Any = None, error: ValueError = original) -> None:
                            super().__init__(environment, parent, name, blocks, globals=globals)
                            self.blocks['body'] = Stack(error)

                    environment = jinja2.Environment(enable_async=asynchronous)
                    environment.context_class = Context
                    source = '{% block body required %}{% endblock %}'
                    with self.assertRaises(ValueError) as raised:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render()
                    self.assertIs(raised.exception, original)

    def test_inherited_required_block_renders_override(self) -> None:
        templates = {'base': 'A{% block body required %}{% endblock %}Z'}
        source = '{% extends "base" %}{% block body %}CHILD{% endblock %}'
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                environment = jinja2.Environment(enable_async=asynchronous, loader=jinja2.DictLoader(templates))
                reference = environment.from_string(source).render()
                native_env = jinja2.Environment(enable_async=asynchronous, loader=jinja2.DictLoader(templates))
                native = docxtpl_native._extension().PythonRenderSession(BridgeConfig(native_env, False), {}).render('case', source)
                self.assertEqual(reference, 'ACHILDZ')
                self.assertEqual(native, reference)

    def test_hidden_required_self_in_complete_document(self) -> None:
        source = ('{% if false %}{% block body required %}{% endblock %}{% endif %}'
                  '[{{self.body()}}]')
        document = template(source)
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference = generate(docxtpl, source, {}, jinja2.Environment(enable_async=asynchronous), source=document)
                native = generate(docxtpl_native, source, {}, jinja2.Environment(enable_async=asynchronous), source=document)
                self.assertTrue(compare_docx(reference, native)['equivalent'])

    def test_required_stack_length_mutation_precedes_dispatch(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def replacement(context: Any) -> Any:
                events.append('replacement')
                yield 'REPLACED'

            async def replacement_async(context: Any) -> Any:
                events.append('replacement')
                await asyncio.sleep(0)
                yield 'REPLACED'

            selected = replacement_async if asynchronous else replacement

            class Stack(list[Any]):
                def __len__(self) -> int:
                    events.append('len')
                    self[0] = selected
                    return 2

            class Context(runtime.Context):
                def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                             globals: Any = None) -> None:
                    blocks['body'] = selected
                    super().__init__(environment, parent, name, blocks, globals=globals)
                    self.blocks['body'] = Stack([selected])

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% block body required %}{% endblock %}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {}
                ).render('case', source)
            else:
                output = environment.from_string(source).render()
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('REPLACED', ['len', 'replacement'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_unimplemented_required_block_raises_reference_exception(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    environment = jinja2.Environment(enable_async=asynchronous)
                    source = '{% block body required %}{% endblock %}'
                    with self.assertRaises(jinja2.TemplateRuntimeError) as raised:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render()
                    self.assertEqual(str(raised.exception), "Required block 'body' not found")

    def test_required_block_stack_with_two_entries_allows_replacement(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool, async_mode: bool = asynchronous) -> tuple[str, list[str]]:
                    events: list[str] = []

                    class Context(runtime.Context):
                        def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                                     globals: Any = None) -> None:
                            def replacement(context: Any) -> Any:
                                events.append('replacement')
                                yield 'REPLACED'

                            async def replacement_async(context: Any) -> Any:
                                events.append('replacement')
                                await asyncio.sleep(0)
                                yield 'REPLACED'

                            selected = replacement_async if async_mode else replacement
                            blocks['body'] = selected
                            super().__init__(environment, parent, name, blocks, globals=globals)
                            self.blocks['body'].append(selected)

                    environment = jinja2.Environment(enable_async=async_mode)
                    environment.context_class = Context
                    source = '{% block body required %}{% endblock %}'
                    if native:
                        result = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {}
                        ).render('case', source)
                    else:
                        result = environment.from_string(source).render()
                    return result, events

                expected = ('REPLACED', ['replacement'])
                self.assertEqual(observe(False), expected)
                self.assertEqual(observe(True), expected)

    def test_hidden_required_block_can_be_called_through_self(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                source = ('{% if false %}{% block body required %}{% endblock %}{% endif %}'
                          '[{{self.body()}}]')
                environment = jinja2.Environment(enable_async=asynchronous)
                expected = environment.from_string(source).render()
                self.assertEqual(expected, '[]')
                actual = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), {}
                ).render('case', source)
                self.assertEqual(actual, expected)

    def test_replacing_required_block_does_not_bypass_required_check(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    events: list[str] = []

                    class Context(runtime.Context):
                        def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                                     globals: Any = None, record: list[str] = events,
                                     async_mode: bool = asynchronous) -> None:
                            def replacement(context: Any) -> Any:
                                record.append('replacement')
                                yield 'REPLACED'

                            async def replacement_async(context: Any) -> Any:
                                record.append('replacement')
                                yield 'REPLACED'

                            blocks['body'] = replacement_async if async_mode else replacement
                            super().__init__(environment, parent, name, blocks, globals=globals)

                    environment = jinja2.Environment(enable_async=asynchronous)
                    environment.context_class = Context
                    source = '{% block body required %}{% endblock %}'
                    with self.assertRaises(jinja2.TemplateRuntimeError) as raised:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render()
                    self.assertEqual(str(raised.exception), "Required block 'body' not found")
                    self.assertEqual(events, [])

    def test_context_constructor_can_remove_block_before_render(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    class Context(runtime.Context):
                        def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                                     globals: Any = None) -> None:
                            super().__init__(environment, parent, name, blocks, globals=globals)
                            self.blocks.pop('body', None)

                    environment = jinja2.Environment(enable_async=asynchronous)
                    environment.context_class = Context
                    source = '{% block body %}ORIGINAL{% endblock %}'
                    with self.assertRaises(KeyError) as raised:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render()
                    self.assertEqual(raised.exception.args, ('body',))

    def test_replaced_block_retains_non_string_fragment_until_join(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Fragment:
                def __str__(self) -> str:
                    events.append('str')
                    return 'converted'

            class Context(runtime.Context):
                def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                             globals: Any = None) -> None:
                    def replacement(context: Any) -> Any:
                        events.append('yield')
                        yield Fragment()

                    async def replacement_async(context: Any) -> Any:
                        events.append('yield')
                        yield Fragment()

                    blocks['body'] = replacement_async if asynchronous else replacement
                    super().__init__(environment, parent, name, blocks, globals=globals)

            def after() -> str:
                events.append('after')
                return 'after'

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            source = '{% block body %}ORIGINAL{% endblock %}{{after()}}'
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {'after': after}
                    ).render('case', source)
                else:
                    environment.from_string(source).render(after=after)
            except TypeError as error:
                return str(error), events
            raise AssertionError('expected a join failure')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_default_block_body_stays_in_native_vm(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            calls: list[str] = []
            environment = jinja2.Environment(enable_async=asynchronous)
            source = '{% block body %}NATIVE{% endblock %}'
            reference = None if native else environment.from_string(source)

            def profile(frame: Any, event: str, arg: Any) -> None:
                if event == 'call' and frame.f_code.co_name == 'block_body':
                    calls.append(frame.f_code.co_name)

            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                if native:
                    output = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(environment, False), {}
                    ).render('case', source)
                else:
                    assert reference is not None
                    output = reference.render()
            finally:
                sys.setprofile(previous)
            return output, calls

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference, reference_calls = observe(False, asynchronous)
                native, native_calls = observe(True, asynchronous)
                self.assertEqual(reference, 'NATIVE')
                self.assertEqual(native, reference)
                self.assertTrue(reference_calls)
                self.assertEqual(native_calls, [])

    def test_replacement_in_complete_document_matches_reference(self) -> None:
        source = 'before{% block body %}ORIGINAL{% endblock %}after'
        document = template(source)
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def make_environment(async_mode: bool = asynchronous) -> jinja2.Environment:
                    class Context(runtime.Context):
                        def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                                     globals: Any = None) -> None:
                            def replacement(context: Any) -> Any:
                                yield 'REPLACED'

                            async def replacement_async(context: Any) -> Any:
                                await asyncio.sleep(0)
                                yield 'REPLACED'

                            blocks['body'] = replacement_async if async_mode else replacement
                            super().__init__(environment, parent, name, blocks, globals=globals)

                    environment = jinja2.Environment(enable_async=async_mode)
                    environment.context_class = Context
                    return environment

                expected = generate(docxtpl, source, {}, make_environment(), source=document)
                actual = generate(docxtpl_native, source, {}, make_environment(), source=document)
                self.assertTrue(compare_docx(expected, actual)['equivalent'])

    def test_replaced_block_mapping_error_is_not_suppressed(self) -> None:
        for asynchronous in (False, True):
            for native in (False, True):
                with self.subTest(asynchronous=asynchronous, native=native):
                    original = KeyError('registry failed')

                    class Blocks(dict[str, Any]):
                        def __init__(self, values: Any, error: KeyError) -> None:
                            super().__init__(values)
                            self.error = error

                        def __getitem__(self, key: str) -> Any:
                            raise self.error

                    class Context(runtime.Context):
                        def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                                     globals: Any = None, error: KeyError = original) -> None:
                            super().__init__(environment, parent, name, blocks, globals=globals)
                            self.blocks = Blocks(self.blocks, error)

                    environment = jinja2.Environment(enable_async=asynchronous)
                    environment.context_class = Context
                    source = '{% block body %}ORIGINAL{% endblock %}'
                    with self.assertRaises(KeyError) as raised:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render()
                    self.assertIs(raised.exception, original)

    def test_callback_can_replace_block_before_it_executes(self) -> None:
        def observe(native: bool, asynchronous: bool, self_call: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            @jinja2.pass_context
            def change(context: Any) -> str:
                events.append('change')

                def replacement(current: Any) -> Any:
                    events.append('same:' + str(current is context))
                    yield 'REPLACED'

                async def replacement_async(current: Any) -> Any:
                    events.append('same:' + str(current is context))
                    await asyncio.sleep(0)
                    yield 'REPLACED'

                context.blocks['body'] = [replacement_async if asynchronous else replacement]
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            block = '{% block body %}ORIGINAL{% endblock %}'
            source = '{{change()}}' + ('{% if false %}' + block + '{% endif %}{{self.body()}}'
                                      if self_call else block)
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'change': change}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(change=change)
            return output, events

        for asynchronous in (False, True):
            for self_call in (False, True):
                with self.subTest(asynchronous=asynchronous, self_call=self_call):
                    self.assertEqual(observe(True, asynchronous, self_call), observe(False, asynchronous, self_call))

    def test_replaced_block_yields_fragments_and_preserves_context(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            instances: list[Any] = []

            class Context(runtime.Context):
                def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                             globals: Any = None) -> None:
                    def replacement(context: Any) -> Any:
                        events.append('same:' + str(context is instances[0]))
                        yield 'α'
                        events.append('resumed')
                        yield context.resolve('suffix')

                    async def replacement_async(context: Any) -> Any:
                        events.append('same:' + str(context is instances[0]))
                        yield 'α'
                        await asyncio.sleep(0)
                        events.append('resumed')
                        yield context.resolve('suffix')

                    blocks['body'] = replacement_async if asynchronous else replacement
                    super().__init__(environment, parent, name, blocks, globals=globals)
                    instances.append(self)

            environment = jinja2.Environment(enable_async=asynchronous, autoescape=True)
            environment.context_class = Context
            source = 'before{% block body %}ORIGINAL{% endblock %}after'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'suffix': '<&'}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(suffix='<&')
            return output, events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = ('beforeα<&after', ['same:True', 'resumed'])
                self.assertEqual(observe(False, asynchronous), expected)
                self.assertEqual(observe(True, asynchronous), expected)

    def test_replaced_block_preserves_exception_identity(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool, async_mode: bool = asynchronous) -> bool:
                    original = ValueError('replacement failed')

                    class Context(runtime.Context):
                        def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                                     globals: Any = None) -> None:
                            def replacement(context: Any) -> Any:
                                yield 'first'
                                raise original

                            async def replacement_async(context: Any) -> Any:
                                yield 'first'
                                await asyncio.sleep(0)
                                raise original

                            blocks['body'] = replacement_async if async_mode else replacement
                            super().__init__(environment, parent, name, blocks, globals=globals)

                    environment = jinja2.Environment(enable_async=async_mode)
                    environment.context_class = Context
                    source = '{% block body %}ORIGINAL{% endblock %}'
                    with self.assertRaises(ValueError) as raised:
                        if native:
                            docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), {}
                            ).render('case', source)
                        else:
                            environment.from_string(source).render()
                    return raised.exception is original

                self.assertTrue(observe(False))
                self.assertTrue(observe(True))

    def test_context_constructor_can_replace_block_before_render(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                def observe(native: bool, async_mode: bool = asynchronous) -> str:
                    class Context(runtime.Context):
                        def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                                     globals: Any = None) -> None:
                            def replacement(context: Any) -> Any:
                                yield 'REPLACED'

                            async def replacement_async(context: Any) -> Any:
                                yield 'REPLACED'

                            blocks['body'] = replacement_async if async_mode else replacement
                            super().__init__(environment, parent, name, blocks, globals=globals)

                    environment = jinja2.Environment(enable_async=async_mode)
                    environment.context_class = Context
                    source = '{% block body %}ORIGINAL{% endblock %}'
                    if native:
                        return docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), {}
                        ).render('case', source)
                    return environment.from_string(source).render()

                self.assertEqual(observe(False), 'REPLACED')
                self.assertEqual(observe(True), 'REPLACED')

    def test_context_constructor_and_callbacks_observe_real_block_stacks(self) -> None:
        templates = {
            'base': '{{probe()}}{% block body %}BASE{% endblock %}',
            'included': '{{probe()}}{% block part %}PART{% endblock %}',
        }
        sources = {
            'root': '{{probe()}}{% block body %}BODY{% endblock %}',
            'inheritance': '{% extends "base" %}{% block body %}CHILD{% endblock %}',
            'include': '{% include "included" %}',
        }

        def observe(native: bool, asynchronous: bool, source: str) -> tuple[str, list[Any]]:
            events: list[Any] = []

            def describe(blocks: Any) -> tuple[tuple[str, tuple[tuple[str, bool], ...]], ...]:
                return tuple(sorted((name, tuple((function.__name__,
                                                   inspect.isasyncgenfunction(function) if asynchronous
                                                   else inspect.isgeneratorfunction(function))
                                                  for function in functions))
                                    for name, functions in blocks.items()))

            class Context(runtime.Context):
                def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                             globals: Any = None) -> None:
                    events.append(('construct', name, describe({key: [value] for key, value in blocks.items()})))
                    super().__init__(environment, parent, name, blocks, globals=globals)

            @jinja2.pass_context
            def probe(context: Any) -> str:
                events.append(('probe', context.name, describe(context.blocks)))
                return ''

            environment = jinja2.Environment(enable_async=asynchronous, loader=jinja2.DictLoader(templates))
            environment.context_class = Context
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'probe': probe}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(probe=probe)
            return output, events

        for asynchronous in (False, True):
            for case, source in sources.items():
                with self.subTest(asynchronous=asynchronous, case=case):
                    self.assertEqual(observe(True, asynchronous, source), observe(False, asynchronous, source))
