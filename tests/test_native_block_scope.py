"""Differential block-scope checks for the native Jinja executor."""

from __future__ import annotations

import asyncio
import io
import unittest
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import nodes
from jinja2.ext import Extension
from paritylab.compare import compare_docx
from paritylab.protocol_base import template


class GeneratedBlock(Extension):
    tags: set[str] = {'generated_scoped', 'generated_plain'}  # noqa: RUF012 - Jinja requires class-level tags

    def parse(self, parser: Any) -> nodes.Block:
        token = next(parser.stream)
        body = [nodes.Output([nodes.Name('x', 'load')])]
        block = nodes.Block('generated', body, token.value == 'generated_scoped', False)
        block.set_lineno(token.lineno)
        return block


class BlockScopeTests(unittest.TestCase):
    def test_block_inside_macro_respects_scope(self) -> None:
        for asynchronous in (False, True):
            for scoped in (False, True):
                with self.subTest(asynchronous=asynchronous, scoped=scoped):
                    flag = ' scoped' if scoped else ''
                    source = ('{% macro m(x) %}{% block body' + flag +
                              ' %}{{x|default("missing")}}{% endblock %}{% endmacro %}{{m(3)}}')
                    reference = jinja2.Environment(enable_async=asynchronous).from_string(source).render()
                    native = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), {}
                    ).render('case', source)
                    self.assertEqual(reference, '3' if scoped else 'missing')
                    self.assertEqual(native, reference)

    def test_block_inside_imported_macro_uses_defining_template(self) -> None:
        for asynchronous in (False, True):
            for scoped in (False, True):
                with self.subTest(asynchronous=asynchronous, scoped=scoped):
                    flag = ' scoped' if scoped else ''
                    templates = {'library': '{% macro m(x) %}{% block body' + flag +
                                 ' %}library:{{x|default("missing")}}{% endblock %}{% endmacro %}'}
                    source = ('{% from "library" import m %}{{m(3)}}'
                              '{% block body %}caller{% endblock %}')
                    reference_env = jinja2.Environment(loader=jinja2.DictLoader(templates), enable_async=asynchronous)
                    reference = reference_env.from_string(source).render()
                    native_env = jinja2.Environment(loader=jinja2.DictLoader(templates), enable_async=asynchronous)
                    native = docxtpl_native._extension().PythonRenderSession(BridgeConfig(native_env, False), {}).render('case', source)
                    self.assertEqual(reference, ('library:3' if scoped else 'library:missing') + 'caller')
                    self.assertEqual(native, reference)

    def test_block_inside_macro_inherits_late_parent_and_super(self) -> None:
        templates = {'base': '{% block outer %}{% endblock %}|{% block body %}BASE{% endblock %}'}
        source = (
            '{% macro m(x) %}{% block body scoped %}child{{super()}}'
            '{{x|default("M")}}{% endblock %}{% endmacro %}{% extends "base" %}'
            '{% block outer %}{{m(3)}}{% endblock %}'
        )
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference_env = jinja2.Environment(loader=jinja2.DictLoader(templates), enable_async=asynchronous)
                native_env = jinja2.Environment(loader=jinja2.DictLoader(templates), enable_async=asynchronous)
                reference = reference_env.from_string(source).render()
                self.assertEqual(reference, 'childBASE3|childBASEM')
                native = docxtpl_native._extension().PythonRenderSession(BridgeConfig(native_env, False), {}).render('case', source)
                self.assertEqual(native, reference)

    def test_imported_inherited_macro_keeps_own_blocks_after_include(self) -> None:
        templates = {
            'base': '{% block outer %}{% endblock %}|{% block body %}BASE{% endblock %}',
            'library': (
                '{% macro m(x) %}{% block body scoped %}lib{{super()}}'
                '{{x|default("M")}}{% endblock %}{% endmacro %}{% extends "base" %}'
                '{% block outer %}{{m(3)}}{% endblock %}'
            ),
            'other': '{% block body %}OTHER{% endblock %}',
        }
        source = ('{% from "library" import m %}{% include "other" %}{{m(4)}}'
                  '{% block body %}CALLER{% endblock %}')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference_env = jinja2.Environment(loader=jinja2.DictLoader(templates), enable_async=asynchronous)
                native_env = jinja2.Environment(loader=jinja2.DictLoader(templates), enable_async=asynchronous)
                reference = reference_env.from_string(source).render()
                self.assertEqual(reference, 'OTHERlibBASE4CALLER')
                native = docxtpl_native._extension().PythonRenderSession(BridgeConfig(native_env, False), {}).render('case', source)
                self.assertEqual(native, reference)

    def test_macro_created_during_super_starts_block_at_most_derived(self) -> None:
        base = ('{% block body %}{% macro m() %}{{self.body()}}{% endmacro %}'
                '{% if go() %}B{{m()}}{% else %}END{% endif %}{% endblock %}')
        source = '{% extends "base" %}{% block body %}C{{super()}}{% endblock %}'

        def run(native: bool, asynchronous: bool) -> str:
            calls = iter((True, False))
            environment = jinja2.Environment(loader=jinja2.DictLoader({'base': base}), enable_async=asynchronous)
            values = {'go': lambda: next(calls)}
            if native:
                return docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values).render('case', source)
            return environment.from_string(source).render(values)

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference = run(False, asynchronous)
                self.assertEqual(reference, 'CBCEND')
                self.assertEqual(run(True, asynchronous), reference)

    def test_block_in_macro_complete_document(self) -> None:
        source = template('{% macro m(x) %}{% block body scoped %}{{x}}{% endblock %}{% endmacro %}{{m(3)}}')
        results: list[bytes] = []
        for module in (docxtpl, docxtpl_native):
            document: Any = module.DocxTemplate(io.BytesIO(source))
            document.render({})
            output = io.BytesIO()
            document.save(output)
            results.append(output.getvalue())
        self.assertTrue(compare_docx(*results)['equivalent'])

    def test_block_in_call_body_uses_own_template(self) -> None:
        templates = {'library': '{% macro wrap() %}[{{caller()}}]{% endmacro %}'}
        source = ('{% from "library" import wrap %}{% call wrap() %}'
                  '{% block body %}caller{% endblock %}{% endcall %}')
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                reference_env = jinja2.Environment(loader=jinja2.DictLoader(templates), enable_async=asynchronous)
                native_env = jinja2.Environment(loader=jinja2.DictLoader(templates), enable_async=asynchronous)
                reference = reference_env.from_string(source).render()
                self.assertEqual(reference, '[caller]')
                native = docxtpl_native._extension().PythonRenderSession(BridgeConfig(native_env, False), {}).render('case', source)
                self.assertEqual(native, reference)

    def test_extension_generated_scoped_block(self) -> None:
        for asynchronous in (False, True):
            for tag in ('generated_scoped', 'generated_plain'):
                with self.subTest(asynchronous=asynchronous, tag=tag):
                    source = '{% for x in [1,2] %}{% ' + tag + ' %};{% endfor %}'
                    reference_env = jinja2.Environment(extensions=[GeneratedBlock], enable_async=asynchronous)
                    reference = reference_env.from_string(source).render()
                    native_env = jinja2.Environment(extensions=[GeneratedBlock], enable_async=asynchronous)
                    native = docxtpl_native._extension().PythonRenderSession(BridgeConfig(native_env, False), {}).render('case', source)
                    self.assertEqual(reference, '1;2;' if tag == 'generated_scoped' else ';;')
                    self.assertEqual(native, reference)

    def test_scoped_and_unscoped_blocks_preserve_visible_bindings(self) -> None:
        cases = (
            ('{% for x in [1,2] %}{% block body %}{{x|default("missing")}}{% endblock %};{% endfor %}',
             'missing;missing;'),
            ('{% for x in [1,2] %}{% block body scoped %}{{x}}{% endblock %};{% endfor %}', '1;2;'),
            ('{% set x="root" %}{% for x in [1] %}{% block body %}{{x}}{% endblock %}{% endfor %}', 'root'),
            ('{% with x="local" %}{% block body %}{{x|default("missing")}}{% endblock %}{% endwith %}', 'missing'),
            ('{% with x="local" %}{% block body scoped %}{{x}}{% endblock %}{% endwith %}', 'local'),
        )
        for asynchronous in (False, True):
            for source, expected in cases:
                with self.subTest(asynchronous=asynchronous, source=source):
                    environment = jinja2.Environment(enable_async=asynchronous)
                    self.assertEqual(environment.from_string(source).render(), expected)
                    candidate = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), {}
                    ).render('case', source)
                    self.assertEqual(candidate, expected)

    def test_block_override_inherits_call_site_scope(self) -> None:
        for asynchronous in (False, True):
            for scoped in (False, True):
                with self.subTest(asynchronous=asynchronous, scoped=scoped):
                    flag = ' scoped' if scoped else ''
                    loader = {'base': '{% for x in [1,2] %}{% block body' + flag +
                              ' %}base{% endblock %};{% endfor %}'}
                    source = '{% extends "base" %}{% block body %}{{x|default("missing")}}{% endblock %}'
                    expected = '1;2;' if scoped else 'missing;missing;'
                    environment = jinja2.Environment(loader=jinja2.DictLoader(loader), enable_async=asynchronous)
                    self.assertEqual(environment.from_string(source).render(), expected)
                    native_environment = jinja2.Environment(loader=jinja2.DictLoader(loader), enable_async=asynchronous)
                    candidate = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(native_environment, False), {}
                    ).render('case', source)
                    self.assertEqual(candidate, expected)

    def test_block_callback_context_observes_its_scope(self) -> None:
        @jinja2.pass_context
        def inspect(context: jinja2.runtime.Context) -> str:
            return str(context.get('x', 'missing'))

        for asynchronous in (False, True):
            for scoped in (False, True):
                with self.subTest(asynchronous=asynchronous, scoped=scoped):
                    flag = ' scoped' if scoped else ''
                    source = ('{% for x in [1,2] %}{% block body' + flag +
                              ' %}{{inspect()}}{% endblock %};{% endfor %}')
                    context = {'inspect': inspect}
                    reference = jinja2.Environment(enable_async=asynchronous).from_string(source).render(context)
                    native = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), context
                    ).render('case', source)
                    self.assertEqual(reference, '1;2;' if scoped else 'missing;missing;')
                    self.assertEqual(native, reference)

    def test_block_inside_include_uses_the_included_context(self) -> None:
        for asynchronous in (False, True):
            for with_context in (False, True):
                with self.subTest(asynchronous=asynchronous, with_context=with_context):
                    suffix = '' if with_context else ' without context'
                    source = '{% for x in [1,2] %}{% include "part"' + suffix + ' %};{% endfor %}'
                    loader = {'part': '{% block body %}{{x|default("missing")}}{% endblock %}'}
                    reference = jinja2.Environment(loader=jinja2.DictLoader(loader), enable_async=asynchronous).from_string(source).render()
                    environment = jinja2.Environment(loader=jinja2.DictLoader(loader), enable_async=asynchronous)
                    native = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {}).render('case', source)
                    self.assertEqual(reference, '1;2;' if with_context else 'missing;missing;')
                    self.assertEqual(native, reference)

    def test_self_block_call_uses_its_context(self) -> None:
        cases = (
            (('{% for x in [1] %}{{self.body()}};{% endfor %}'
              '{% block body %}{{x|default("missing")}}{% endblock %}'), 'missing;missing'),
            (('{% for x in [1] %}{% block outer scoped %}{{self.inner()}}{% endblock %}{% endfor %}'
              '{% block inner %}{{x|default("missing")}}{% endblock %}'), '1missing'),
            (('{% for x in [1] %}{% block outer scoped %}{% with y=2 %}{{self.inner()}}{% endwith %}'
              '{% endblock %}{% endfor %}{% block inner %}{{x|default("missing")}}/'
              '{{y|default("missing")}}{% endblock %}'), '1/missingmissing/missing'),
            (('{% block outer %}{% set x=1 %}{% block inner %}'
              '{{x|default("missing")}}{% endblock %}{% endblock %}'), 'missing'),
        )
        for asynchronous in (False, True):
            for source, expected in cases:
                with self.subTest(asynchronous=asynchronous, source=source):
                    reference = jinja2.Environment(enable_async=asynchronous).from_string(source).render()
                    native = docxtpl_native._extension().PythonRenderSession(
                        BridgeConfig(jinja2.Environment(enable_async=asynchronous), False), {}
                    ).render('case', source)
                    self.assertEqual(reference, expected)
                    self.assertEqual(native, reference)

    def test_unscoped_block_in_complete_document(self) -> None:
        source = template('{% for x in [1] %}{% block body %}{{x|default("missing")}}{% endblock %}{% endfor %}')
        results: list[bytes] = []
        for module in (docxtpl, docxtpl_native):
            document: Any = module.DocxTemplate(io.BytesIO(source))
            document.render({})
            output = io.BytesIO()
            document.save(output)
            results.append(output.getvalue())
        self.assertTrue(compare_docx(*results)['equivalent'])

    def test_error_in_unscoped_block_does_not_corrupt_session_reuse(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                failure = ValueError('block failed')

                def fail(error: ValueError = failure) -> str:
                    raise error

                environment = jinja2.Environment(enable_async=asynchronous)
                session = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'fail': fail}
                )
                with self.assertRaises(ValueError) as caught:
                    session.render('first', '{% for x in [1] %}{% block body %}{{fail()}}{% endblock %}{% endfor %}')
                self.assertIs(caught.exception, failure)
                self.assertEqual(session.render('second', '{% for x in [1,2] %}{{x}}{% endfor %}'), '12')

    def test_unscoped_block_restores_loop_after_async_suspension(self) -> None:
        source = ('{% for x in [1,2] %}{% block body %}'
                  '{{pause()}}{{x|default("missing")}}{% endblock %}:{{x}};{% endfor %}')

        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            async def pause() -> str:
                events.append('before')
                await asyncio.sleep(0)
                events.append('after')
                return ''

            environment = jinja2.Environment(enable_async=True)
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'pause': pause}
                ).render('case', source)
            else:
                output = environment.from_string(source).render(pause=pause)
            return output, events

        expected = ('missing:1;missing:2;', ['before', 'after', 'before', 'after'])
        self.assertEqual(observe(False), expected)
        self.assertEqual(observe(True), expected)

    def test_unscoped_block_strict_undefined_matches_reference(self) -> None:
        source = '{% for x in [1] %}{% block body %}{{x}}{% endblock %}{% endfor %}'

        def observe(native: bool, asynchronous: bool) -> tuple[str, str]:
            environment = jinja2.Environment(enable_async=asynchronous, undefined=jinja2.StrictUndefined)
            try:
                if native:
                    docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {}).render('case', source)
                else:
                    environment.from_string(source).render()
            except Exception as error:  # noqa: BLE001 - compare the reference exception boundary
                return type(error).__name__, str(error)
            raise AssertionError('expected an undefined error')

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                expected = observe(False, asynchronous)
                self.assertEqual(expected, ('UndefinedError', "'x' is undefined"))
                self.assertEqual(observe(True, asynchronous), expected)

    def test_unscoped_block_preserves_context_lookup_timing(self) -> None:
        source = ('{% for x in items %}{% block body %}'
                  '{{ x|default("missing") }}{{ after() }}{% endblock %}{% endfor %}')

        def observe(native: bool, asynchronous: bool, items: list[int]) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def resolve_or_missing(self, key: str) -> Any:
                    events.append('resolve:' + key)
                    return super().resolve_or_missing(key)

            def after() -> str:
                events.append('after')
                return ''

            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = Context
            values = {'items': items, 'after': after}
            if native:
                output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values).render('case', source)
            else:
                output = environment.from_string(source).render(values)
            return output, events

        for asynchronous in (False, True):
            for items in ([], [1, 2]):
                with self.subTest(asynchronous=asynchronous, items=items):
                    self.assertEqual(observe(True, asynchronous, items), observe(False, asynchronous, items))


if __name__ == '__main__':
    unittest.main()
