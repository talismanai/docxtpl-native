"""Differential checks for imports compiled from extension-produced ASTs."""

import sys
import subprocess
import unittest
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2.ext import Extension
from jinja2.sandbox import SandboxedEnvironment


class Passthrough(Extension):
    """Select the extension AST path without changing the template."""


def render(native: bool, source: str, *, extension: bool = True, asynchronous: bool = False) -> str:
    environment = jinja2.Environment(
        extensions=[Passthrough] if extension else [],
        enable_async=asynchronous,
        loader=jinja2.DictLoader({
            'library': '{% set visible = value|default("missing") %}'
                       '{% macro show() %}{{ value|default("missing") }}{% endmacro %}'
                       '{% macro other() %}other{% endmacro %}',
        }),
    )
    environment.globals['value'] = 'global'
    context: dict[str, Any] = {'value': 'caller'}
    if native:
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
        return str(session.render('case', source))
    return environment.from_string(source).render(context)


class AstImportTests(unittest.TestCase):
    def test_get_exported_can_rename_a_macro_without_losing_its_body(self) -> None:
        def observe(native: bool, asynchronous: bool) -> str:
            class Context(jinja2.runtime.Context):
                def get_exported(self) -> dict[str, Any]:
                    exported = super().get_exported()
                    exported['renamed'] = exported.pop('body')
                    return exported

            environment = jinja2.Environment(enable_async=asynchronous, loader=jinja2.DictLoader({
                'module': '{% macro body() %}M{% endmacro %}',
            }))
            environment.context_class = Context
            source = '{% import "module" as m %}{{ m.renamed() }}'
            if native:
                return docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {},
                ).render('case', source)
            return environment.from_string(source).render()

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(False, asynchronous), 'M')
                self.assertEqual(observe(True, asynchronous), 'M')

    def test_unexported_values_are_released_before_caller_resumes(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Value:
                def __del__(self) -> None:
                    events.append('drop')

            def after() -> str:
                events.append('after')
                return 'done'

            environment = jinja2.Environment(loader=jinja2.DictLoader({
                'module': '{% set _hidden=make() %}',
            }))
            functions: dict[str, Any] = {'make': Value, 'after': after}
            environment.globals.update(functions)
            source = '{% import "module" as m %}{{after()}}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {},
                ).render('case', source)
            else:
                output = environment.from_string(source).render()
            return output, events.copy()

        expected = observe(False)
        self.assertEqual(expected, ('done', ['drop', 'after']))
        self.assertEqual(observe(True), expected)

    def test_module_get_exported_override_before_native_import(self) -> None:
        script = '''
import jinja2
import jinja2.runtime
events = []
original = jinja2.runtime.Context.get_exported
def get_exported(context):
    events.append('get_exported')
    values = original(context)
    values['added'] = 'from callback'
    return values
jinja2.runtime.Context.get_exported = get_exported
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig
environment = jinja2.Environment(loader=jinja2.DictLoader({'library': ''}))
source = '{% import "library" as module %}{{ module.added|default("absent") }}'
expected = environment.from_string(source).render()
reference_events = events[:]
events.clear()
session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
actual = session.render('case', source)
assert (actual, events) == (expected, reference_events), ((actual, events), (expected, reference_events))
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_module_exported_name_missing_from_context_vars_raises(self) -> None:
        def observe(native: bool) -> tuple[str, str]:
            @jinja2.pass_context
            def expose_missing(context: jinja2.runtime.Context) -> str:
                context.exported_vars.add('missing')
                return ''

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'library': '{{ expose_missing() }}'}),
            )
            environment.globals['expose_missing'] = expose_missing
            source = '{% import "library" as module %}done'
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                    session.render('case', source)
                else:
                    environment.from_string(source).render()
            except Exception as error:  # noqa: BLE001 - compare exception boundary
                return type(error).__name__, str(error)
            return 'no exception', ''

        self.assertEqual(observe(True), observe(False))

    def test_module_default_get_exported_does_not_call_reference_body(self) -> None:
        calls: list[str] = []
        code = jinja2.runtime.Context.get_exported.__code__
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: object) -> None:
            if event == 'call' and frame.f_code is code:
                calls.append('Context.get_exported')

        environment = jinja2.Environment(loader=jinja2.DictLoader({'library': '{% set item="value" %}'}))
        source = '{% import "library" as module %}{{ module.item }}'
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
        try:
            sys.setprofile(profile)
            self.assertEqual(environment.from_string(source).render(), 'value')
            self.assertEqual(calls, ['Context.get_exported'])
            calls.clear()
            self.assertEqual(session.render('case', source), 'value')
        finally:
            sys.setprofile(previous)
        self.assertEqual(calls, [])

    def test_module_context_custom_get_exported_is_called(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Context(jinja2.runtime.Context):
                def get_exported(self) -> dict[str, Any]:
                    events.append('get_exported')
                    values = super().get_exported()
                    values['added'] = 'from callback'
                    return values

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'library': ''}), enable_async=asynchronous,
            )
            environment.context_class = Context
            source = '{% import "library" as module %}{{ module.added|default("absent") }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_module_context_callback_controls_public_exports(self) -> None:
        def observe(native: bool, action: str, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            @jinja2.pass_context
            def modify(context: jinja2.runtime.Context) -> str:
                events.append(action)
                if action == 'remove':
                    context.exported_vars.discard('item')
                else:
                    context.vars['item'] = 'changed'
                return ''

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'library': '{% set item="original" %}{{ modify() }}'}),
                enable_async=asynchronous,
            )
            environment.globals['modify'] = modify
            source = '{% import "library" as module %}{{ module.item|default("absent") }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        for action in ('remove', 'change'):
            for asynchronous in (False, True):
                with self.subTest(action=action, asynchronous=asynchronous):
                    self.assertEqual(observe(True, action, asynchronous), observe(False, action, asynchronous))

    def test_module_context_callback_can_export_private_name(self) -> None:
        def observe(native: bool, asynchronous: bool, extension: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            @jinja2.pass_context
            def expose(context: jinja2.runtime.Context) -> str:
                events.append('expose')
                context.exported_vars.add('_hidden')
                return ''

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'library': '{% set _hidden="secret" %}{{ expose() }}'}),
                enable_async=asynchronous, extensions=[Passthrough] if extension else [],
            )
            environment.globals['expose'] = expose
            source = '{% import "library" as module %}{{ module._hidden|default("absent") }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        for asynchronous in (False, True):
            for extension in (False, True):
                with self.subTest(asynchronous=asynchronous, extension=extension):
                    self.assertEqual(observe(True, asynchronous, extension),
                                     observe(False, asynchronous, extension))

    def test_imported_module_constructor_override_before_native_import(self) -> None:
        script = '''
import jinja2
import jinja2.environment
events = []
class CustomModule(jinja2.environment.TemplateModule):
    def __init__(self, *args, **kwargs):
        events.append('module_init')
        super().__init__(*args, **kwargs)
jinja2.environment.TemplateModule = CustomModule
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig
environment = jinja2.Environment(loader=jinja2.DictLoader({'library': 'body'}))
source = '{% import "library" as module %}{{ module }}'
expected = environment.from_string(source).render()
reference_events = events[:]
events.clear()
session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
actual = session.render('case', source)
assert (actual, events) == (expected, reference_events), ((actual, events), (expected, reference_events))
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_imported_module_uses_replaced_concat(self) -> None:
        def observe(native: bool, autoescape: bool) -> tuple[str, list[list[str]]]:
            events: list[list[str]] = []

            def concat(values: Any) -> str:
                events.append(list(values))
                return 'custom'

            environment = jinja2.Environment(loader=jinja2.DictLoader({'library': 'A{{ value }}B'}),
                                              autoescape=autoescape)
            environment.globals['value'] = 'X'
            source = '{% import "library" as module %}{{ module }}'
            with patch.object(jinja2.environment, 'concat', concat):
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                    return str(session.render('case', source)), events
                return environment.from_string(source).render(), events

        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                self.assertEqual(observe(True, autoescape), observe(False, autoescape))

    def test_imported_module_instance_html_override(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Variables(dict[str, Any]):
                def __setitem__(self, key: str, value: Any) -> None:
                    if key == 'module':
                        def html() -> str:
                            events.append('instance_html')
                            return '<changed>'
                        value.__html__ = html
                    super().__setitem__(key, value)

            class Context(jinja2.runtime.Context):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    super().__init__(*args, **kwargs)
                    self.vars = Variables()

            environment = jinja2.Environment(loader=jinja2.DictLoader({'library': 'body'}), autoescape=True)
            environment.context_class = Context
            source = '{% import "library" as module %}{{ module }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        self.assertEqual(observe(True), observe(False))

    def test_imported_module_method_override_before_native_import(self) -> None:
        script = '''
import sys
import jinja2
import jinja2.environment
method = sys.argv[1]
events = []
def changed(module):
    events.append(method)
    return 'changed'
setattr(jinja2.environment.TemplateModule, method, changed)
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig
environment = jinja2.Environment(loader=jinja2.DictLoader({'library': 'body'}), autoescape=method == '__html__')
source = '{% import "library" as module %}{{ module }}'
expected = environment.from_string(source).render()
reference_events = events[:]
events.clear()
session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
actual = session.render('case', source)
assert (actual, events) == (expected, reference_events), ((actual, events), (expected, reference_events))
'''
        for method in ('__str__', '__html__'):
            with self.subTest(method=method):
                result = subprocess.run([sys.executable, '-c', script, method], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_imported_module_custom_constructor_is_called(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            original = jinja2.environment.TemplateModule

            class CustomModule(original):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('module_init')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(loader=jinja2.DictLoader({'library': 'body'}))
            source = '{% import "library" as module %}{{ module }}'
            with patch.object(jinja2.environment, 'TemplateModule', CustomModule):
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                    return str(session.render('case', source)), events
                return environment.from_string(source).render(), events

        self.assertEqual(observe(True), observe(False))

    def test_imported_module_method_changed_during_render(self) -> None:
        def observe(native: bool, autoescape: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            module_class = jinja2.environment.TemplateModule
            method_name = '__html__' if autoescape else '__str__'

            def changed(module: Any) -> str:
                events.append('custom:' + method_name)
                return 'changed'

            class Variables(dict[str, Any]):
                def __setitem__(self, key: str, value: Any) -> None:
                    if key == 'module':
                        setattr(module_class, method_name, changed)
                    super().__setitem__(key, value)

            class Context(jinja2.runtime.Context):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    super().__init__(*args, **kwargs)
                    self.vars = Variables()

            environment = jinja2.Environment(loader=jinja2.DictLoader({'library': 'body'}), autoescape=autoescape)
            environment.context_class = Context
            source = '{% import "library" as module %}{{ module }}'
            original = getattr(module_class, method_name)
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                    return str(session.render('case', source)), events
                return environment.from_string(source).render(), events
            finally:
                setattr(module_class, method_name, original)

        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                self.assertEqual(observe(True, autoescape), observe(False, autoescape))

    def test_imported_module_html_does_not_call_reference_body(self) -> None:
        calls: list[str] = []
        code = jinja2.environment.TemplateModule.__html__.__code__
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: object) -> None:
            if event == 'call' and frame.f_code is code:
                calls.append('TemplateModule.__html__')

        environment = jinja2.Environment(loader=jinja2.DictLoader({'library': 'body'}), autoescape=True)
        source = '{% import "library" as module %}{{ module }}'
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
        try:
            sys.setprofile(profile)
            environment.from_string(source).render()
            self.assertEqual(calls, ['TemplateModule.__html__'])
            calls.clear()
            session.render('case', source)
        finally:
            sys.setprofile(previous)
        self.assertEqual(calls, [])

    def test_imported_module_string_does_not_call_reference_body(self) -> None:
        calls: list[str] = []
        code = jinja2.environment.TemplateModule.__str__.__code__
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: object) -> None:
            if event == 'call' and frame.f_code is code:
                calls.append('TemplateModule.__str__')

        environment = jinja2.Environment(loader=jinja2.DictLoader({'library': 'body'}))
        source = '{% import "library" as module %}{{ module }}'
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
        try:
            sys.setprofile(profile)
            environment.from_string(source).render()
            self.assertEqual(calls, ['TemplateModule.__str__'])
            calls.clear()
            session.render('case', source)
        finally:
            sys.setprofile(previous)
        self.assertEqual(calls, [])

    def test_imported_module_string_uses_native_algorithm(self) -> None:
        calls: list[str] = []
        code = jinja2.environment.TemplateModule.__str__.__code__
        previous = sys.getprofile()

        def profile(frame: Any, event: str, argument: object) -> None:
            if event == 'call' and frame.f_code is code:
                calls.append('TemplateModule.__str__')

        environment = jinja2.Environment(loader=jinja2.DictLoader({'library': 'body'}))
        source = '{% import "library" as module %}{{ module }}'
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
        try:
            sys.setprofile(profile)
            self.assertEqual(environment.from_string(source).render(), 'body')
            self.assertEqual(calls, ['TemplateModule.__str__'])
            calls.clear()
            actual = session.render('case', source)
        finally:
            sys.setprofile(previous)
        self.assertEqual(actual, 'body')
        self.assertEqual(calls, [])

    def test_import_context_callback_observes_same_module_and_fragments(self) -> None:
        def observe(native: bool) -> tuple[str, list[object]]:
            seen: list[Any] = []
            events: list[object] = []

            class Variables(dict[str, Any]):
                def __setitem__(self, key: str, value: Any) -> None:
                    if key == 'module':
                        seen.append(value)
                        events.append(list(value._body_stream))
                    super().__setitem__(key, value)

            class Context(jinja2.runtime.Context):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    super().__init__(*args, **kwargs)
                    self.vars = Variables()

            environment = jinja2.Environment(loader=jinja2.DictLoader({
                'library': 'A{{ value }}B{{ value }}',
            }))
            environment.context_class = Context
            environment.globals['value'] = 'X'

            def same(value: Any) -> str:
                events.append(value is seen[0])
                return str(value is seen[0])

            environment.globals['same'] = same
            source = '{% import "library" as module %}{{ same(module) }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        self.assertEqual(observe(True), observe(False))

    def test_import_context_callback_can_modify_module(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Variables(dict[str, Any]):
                def __setitem__(self, key: str, value: Any) -> None:
                    if key == 'module':
                        events.append('modify')
                        value.first = 'changed'
                    super().__setitem__(key, value)

            class Context(jinja2.runtime.Context):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    super().__init__(*args, **kwargs)
                    self.vars = Variables()

            environment = jinja2.Environment(loader=jinja2.DictLoader({
                'library': '{% set first="original" %}',
            }))
            environment.context_class = Context
            source = '{% import "library" as module %}{{ module.first }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        self.assertEqual(observe(True), observe(False))

    def test_imports_publish_context_vars_and_remove_exports(self) -> None:
        def observe(native: bool, source: str) -> tuple[str, list[str]]:
            events: list[str] = []

            class Variables(dict[str, Any]):
                def __setitem__(self, key: str, value: Any) -> None:
                    events.append('set:' + key)
                    if key == 'previous' and not isinstance(value, str):
                        events.append('module:' + str(isinstance(value, jinja2.environment.TemplateModule)))
                    super().__setitem__(key, value)

                def update(self, *args: Any, **kwargs: Any) -> None:
                    events.append('update')
                    super().update(*args, **kwargs)

            class Exports(set[str]):
                def add(self, value: str) -> None:
                    events.append('add:' + value)
                    super().add(value)

                def discard(self, value: str) -> None:
                    events.append('discard:' + value)
                    super().discard(value)

                def difference_update(self, *others: Any) -> None:
                    events.append('difference_update')
                    super().difference_update(*others)

            class Context(jinja2.runtime.Context):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    super().__init__(*args, **kwargs)
                    self.vars = Variables()
                    self.exported_vars = Exports()

            environment = jinja2.Environment(loader=jinja2.DictLoader({
                'module': '{% set first="one" %}{% set second="two" %}',
            }))
            environment.context_class = Context
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        sources = (
            '{% set previous="old" %}{% import "module" as previous %}{{ previous.first }}',
            '{% from "module" import first, second %}{{ first }}|{{ second }}',
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(observe(True, source), observe(False, source))

    def test_module_exports_exclude_private_names_and_import_bindings(self) -> None:
        def observe(native: bool, extension: bool, asynchronous: bool, module: str, source: str) -> str:
            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'module': module, 'dependency': '{% set item="dependency" %}'}),
                extensions=[Passthrough] if extension else [], enable_async=asynchronous,
            )
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source))
            return environment.from_string(source).render()

        cases = (
            ('{% set _hidden="private" %}', '{% import "module" as m %}{{ m._hidden is defined }}'),
            ('{% macro _hidden() %}private{% endmacro %}', '{% import "module" as m %}{{ m._hidden is defined }}'),
            ('{% import "dependency" as item %}', '{% import "module" as m %}{{ m.item is defined }}'),
            ('{% from "dependency" import item %}', '{% from "module" import item %}{{ item is defined }}'),
            ('{% set item="first" %}{% import "dependency" as item %}',
             '{% import "module" as m %}{{ m.item is defined }}'),
            ('{% import "dependency" as item %}{% set item="last" %}',
             '{% import "module" as m %}{{ m.item|default("absent") }}'),
        )
        for extension in (False, True):
            for asynchronous in (False, True):
                for module, source in cases:
                    with self.subTest(extension=extension, asynchronous=asynchronous, module=module):
                        self.assertEqual(observe(True, extension, asynchronous, module, source),
                                         observe(False, extension, asynchronous, module, source))

    def test_missing_import_undefined_constructor_iteration_error(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, str]:
            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    raise StopIteration('undefined stopped')

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'library': ''}), undefined=Undefined, enable_async=asynchronous,
            )
            source = '{% from "library" import missing %}unreached'
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                    session.render('case', source)
                else:
                    environment.from_string(source).render()
            except Exception as error:  # noqa: BLE001 - compare exception boundary
                return type(error).__name__, str(error)
            return 'no exception', ''

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_missing_import_uses_undefined_factory_return_value(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            environment = jinja2.Environment(loader=jinja2.DictLoader({'library': ''}))

            def factory(hint: str, *, name: str) -> str:
                events.append(name)
                return 'factory:' + name

            environment.undefined = factory  # type: ignore[assignment]
            source = '{% from "library" import missing %}{{ missing }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        self.assertEqual(observe(True), observe(False))

    def test_missing_import_undefined_constructor_can_reenter(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []
            session: Any = None

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('start')
                    if native:
                        assert session is not None
                        events.append(str(session.render('nested', 'nested')))
                    else:
                        events.append(environment.from_string('nested').render())
                    events.append('end')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(loader=jinja2.DictLoader({'library': ''}), undefined=Undefined)
            source = '{% from "library" import missing %}{{ missing is defined }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        self.assertEqual(observe(True), observe(False))

    def test_missing_import_hint_inside_imported_macro(self) -> None:
        def observe(native: bool) -> tuple[str, list[str | None]]:
            hints: list[str | None] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, hint: str | None = None, obj: Any = None, name: str | None = None,
                             exc: type[jinja2.TemplateRuntimeError] = jinja2.UndefinedError) -> None:
                    hints.append(hint)
                    super().__init__(hint=hint, obj=obj, name=name, exc=exc)

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({
                    'outer': '{% macro m() %}{% from "library" import missing %}{{ missing is defined }}{% endmacro %}',
                    'library': '',
                }), undefined=Undefined,
            )
            source = '{% import "outer" as outer %}{{ outer.m() }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), hints
            return environment.from_string(source).render(), hints

        self.assertEqual(observe(True), observe(False))

    def test_missing_import_under_sandbox(self) -> None:
        def observe(native: bool, asynchronous: bool) -> str:
            environment = SandboxedEnvironment(
                loader=jinja2.DictLoader({'library': ''}), enable_async=asynchronous,
            )
            environment.globals['missing'] = 'global'
            source = '{% from "library" import missing %}{{ missing|default("none") }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source))
            return environment.from_string(source).render()

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_loaded_template_retains_own_selected_undefined(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class First(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('first')
                    super().__init__(*args, **kwargs)

            class Second(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('second')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({
                    'outer': '{{ change() }}{% from "library" import missing %}{{ missing is defined }}',
                    'library': '',
                }), undefined=First, enable_async=asynchronous,
            )

            def change() -> str:
                events.append('change')
                environment.undefined = Second
                return ''

            environment.globals['change'] = change
            source = '{% include "outer" %}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_missing_import_retains_undefined_selected_before_module_execution(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class First(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('first')
                    super().__init__(*args, **kwargs)

            class Second(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('second')
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'library': '{{ change() }}'}), undefined=First,
            )

            def change() -> str:
                events.append('change')
                environment.undefined = Second
                return ''

            environment.globals['change'] = change
            source = '{% from "library" import missing %}{{ missing is defined }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        self.assertEqual(observe(True), observe(False))

    def test_missing_import_undefined_constructor_exception_identity(self) -> None:
        def observe(native: bool, asynchronous: bool, extension: bool) -> tuple[bool, list[str]]:
            events: list[str] = []
            failure = ValueError('undefined constructor')

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append('undefined:' + str(kwargs.get('name')))
                    raise failure

            environment = jinja2.Environment(
                extensions=[Passthrough] if extension else [], enable_async=asynchronous,
                loader=jinja2.DictLoader({'library': ''}), undefined=Undefined,
            )
            environment.globals['later'] = lambda: events.append('later')
            source = '{% from "library" import missing %}{{ later() }}'
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                    session.render('case', source)
                else:
                    environment.from_string(source).render()
            except ValueError as error:
                return error is failure, events
            return False, events

        for asynchronous in (False, True):
            for extension in (False, True):
                with self.subTest(asynchronous=asynchronous, extension=extension):
                    self.assertEqual(observe(True, asynchronous, extension),
                                     observe(False, asynchronous, extension))

    def test_missing_import_preserves_target_reference_count_in_callback(self) -> None:
        def observe(native: bool) -> tuple[str, list[int]]:
            events: list[int] = []

            class Name(str):
                pass

            target = Name('library')

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(sys.getrefcount(target))
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(loader=jinja2.DictLoader({'library': ''}), undefined=Undefined)
            source = '{% from target import missing %}{{ missing is defined }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'target': target},
                )
                return str(session.render('case', source)), events
            return environment.from_string(source).render({'target': target}), events

        self.assertEqual(observe(True), observe(False))

    def test_missing_import_multiple_callbacks_preserve_order(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(str(kwargs.get('name')))
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'library': '{% set available="yes" %}'}),
                undefined=Undefined, enable_async=asynchronous,
            )
            source = ('{% from "library" import first, available as value, second %}'
                      '{{ first is defined }}|{{ value }}|{{ second is defined }}')
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_missing_import_template_name_repr_effects(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Name(str):
                def __repr__(self) -> str:
                    events.append('repr')
                    return super().__repr__()

            environment = jinja2.Environment(loader=jinja2.DictLoader({'library': ''}))
            source = '{% from target import missing %}{{ missing is defined }}'
            context = {'target': Name('library')}
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                return str(session.render('case', source)), events
            return environment.from_string(source).render(context), events

        self.assertEqual(observe(True), observe(False))

    def test_missing_import_hint_in_loaded_template(self) -> None:
        def observe(native: bool, asynchronous: bool) -> tuple[str, list[tuple[str | None, str | None]]]:
            events: list[tuple[str | None, str | None]] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, hint: str | None = None, obj: Any = None, name: str | None = None,
                             exc: type[jinja2.TemplateRuntimeError] = jinja2.UndefinedError) -> None:
                    events.append((hint, name))
                    super().__init__(hint=hint, obj=obj, name=name, exc=exc)

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({
                    'outer name': '\n{% from "library" import missing as alias %}{{ alias is defined }}',
                    'library': '',
                }),
                undefined=Undefined,
                enable_async=asynchronous,
            )
            source = '{% include "outer name" %}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_missing_import_constructs_undefined_even_if_unused(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    events.append(str(kwargs.get('name')))
                    super().__init__(*args, **kwargs)

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'library': ''}), undefined=Undefined,
            )
            source = '{% from "library" import missing as alias %}constant'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        self.assertEqual(observe(True), observe(False))

    def test_missing_import_strict_undefined_error(self) -> None:
        def observe(native: bool) -> tuple[str, str]:
            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'library': ''}), undefined=jinja2.StrictUndefined,
            )
            environment.globals['missing'] = 'global'
            source = '{% from "library" import missing as alias %}{{ alias }}'
            try:
                if native:
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                    session.render('case', source)
                else:
                    environment.from_string(source).render()
            except Exception as error:  # noqa: BLE001 - compare exception and message
                return type(error).__name__, str(error)
            return 'no exception', ''

        self.assertEqual(observe(True), observe(False))

    def test_missing_import_constructs_custom_undefined(self) -> None:
        def observe(native: bool) -> tuple[str, list[tuple[str | None, str | None]]]:
            events: list[tuple[str | None, str | None]] = []

            class Undefined(jinja2.Undefined):
                def __init__(self, hint: str | None = None, obj: Any = None, name: str | None = None,
                             exc: type[jinja2.TemplateRuntimeError] = jinja2.UndefinedError) -> None:
                    events.append((hint, name))
                    super().__init__(hint=hint, obj=obj, name=name, exc=exc)

            environment = jinja2.Environment(
                loader=jinja2.DictLoader({'library': ''}), undefined=Undefined,
            )
            source = '{% from "library" import missing as alias %}{{ alias is defined }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        self.assertEqual(observe(True), observe(False))

    def test_missing_import_does_not_resolve_from_parent_or_globals(self) -> None:
        def observe(native: bool, extension: bool, with_context: bool) -> str:
            environment = jinja2.Environment(
                extensions=[Passthrough] if extension else [],
                loader=jinja2.DictLoader({'library': ''}),
            )
            environment.globals['missing'] = 'global'
            context = {'missing': 'caller'}
            suffix = ' with context' if with_context else ''
            source = ('{% from "library" import missing as alias' + suffix + ' %}'
                      '{{ alias is defined }}|{{ alias|default("none") }}')
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                return str(session.render('case', source))
            return environment.from_string(source).render(context)

        for extension in (False, True):
            for with_context in (False, True):
                with self.subTest(extension=extension, with_context=with_context):
                    self.assertEqual(observe(True, extension, with_context),
                                     observe(False, extension, with_context))

    def test_custom_template_cache_receives_lookup_and_store(self) -> None:
        def observe(native: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            class Cache(dict[Any, Any]):
                def get(self, key: Any, default: Any = None) -> Any:
                    events.append('get')
                    return super().get(key, default)

                def __setitem__(self, key: Any, value: Any) -> None:
                    events.append('set')
                    super().__setitem__(key, value)

            environment = jinja2.Environment(loader=jinja2.DictLoader({'library': 'ok'}))
            environment.cache = Cache()
            source = '{% include "library" %}{% include "library" %}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        self.assertEqual(observe(True), observe(False))

    def test_replacing_template_cache_discards_old_entries(self) -> None:
        def observe(native: bool) -> list[str]:
            mapping = {'library': '{% set result="old" %}'}
            environment = jinja2.Environment(loader=jinja2.DictLoader(mapping))
            source = '{% import "library" as lib %}{{ lib.result }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})

                def run() -> str:
                    return str(session.render('case', source))
            else:
                compiled = environment.from_string(source)

                def run() -> str:
                    return compiled.render()

            output = [run()]
            environment.cache = None
            mapping['library'] = '{% set result="new" %}'
            output.append(run())
            environment.cache = jinja2.utils.LRUCache(400)
            output.append(run())
            return output

        self.assertEqual(observe(True), observe(False))

    def test_cache_access_order(self) -> None:
        def observe(native: bool, cache_size: int) -> tuple[str, list[str]]:
            events: list[str] = []

            class Environment(jinja2.Environment):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'cache':
                        events.append('cache')
                    return super().__getattribute__(name)

            class Loader(jinja2.BaseLoader):
                def get_source(self, environment: jinja2.Environment, template: str) -> tuple[str, str, Any]:
                    events.append('load')
                    return '{% set result="ok" %}', template, lambda: True

            environment = Environment(loader=Loader(), cache_size=cache_size, auto_reload=False)
            source = '{% import "library" as lib %}{{ lib.result }}{% import "library" as lib %}{{ lib.result }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                events.clear()
                output = str(session.render('case', source))
            else:
                compiled = environment.from_string(source)
                events.clear()
                output = compiled.render()
            return output, events

        for cache_size in (0, 400):
            with self.subTest(cache_size=cache_size):
                self.assertEqual(observe(True, cache_size), observe(False, cache_size))

    def test_failed_import_can_be_retried_in_same_session(self) -> None:
        def observe(native: bool, extension: bool, with_context: bool) -> tuple[list[str], list[str], bool]:
            events: list[str] = []
            failure = ValueError('first import fails')

            def load_value() -> str:
                events.append('value')
                if len(events) == 1:
                    raise failure
                return 'ready'

            environment = jinja2.Environment(
                extensions=[Passthrough] if extension else [],
                loader=jinja2.DictLoader({'library': '{% set result=load_value() %}'}),
            )
            environment.globals['load_value'] = load_value
            suffix = ' with context' if with_context else ''
            source = '{% import "library" as lib' + suffix + ' %}{{ lib.result }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})

                def run() -> str:
                    return str(session.render('case', source))
            else:
                compiled = environment.from_string(source)

                def run() -> str:
                    return compiled.render()

            same_error = False
            try:
                run()
            except ValueError as error:
                same_error = error is failure
            output = [run()]
            return output, events, same_error

        for extension in (True, False):
            for with_context in (True, False):
                with self.subTest(extension=extension, with_context=with_context):
                    self.assertEqual(observe(True, extension, with_context),
                                     observe(False, extension, with_context))

    def test_import_loader_callbacks_and_auto_reload(self) -> None:
        def observe(native: bool, auto_reload: bool) -> tuple[list[str], list[str]]:
            events: list[str] = []
            data = {'source': '{% set result="old" %}'}

            class Loader(jinja2.BaseLoader):
                def get_source(self, environment: jinja2.Environment, template: str) -> tuple[str, str, Any]:
                    events.append('get_source:' + template)
                    original = data['source']

                    def uptodate() -> bool:
                        events.append('uptodate')
                        return data['source'] == original

                    return original, template, uptodate

            environment = jinja2.Environment(loader=Loader(), auto_reload=auto_reload)
            source = '{% import "library" as lib %}{{ lib.result }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})

                def run() -> str:
                    return str(session.render('case', source))
            else:
                compiled = environment.from_string(source)

                def run() -> str:
                    return compiled.render()

            output = [run(), run()]
            data['source'] = '{% set result="new" %}'
            output.append(run())
            return output, events

        for auto_reload in (False, True):
            with self.subTest(auto_reload=auto_reload):
                self.assertEqual(observe(True, auto_reload), observe(False, auto_reload))

    def test_import_cache_across_renders_and_loader_updates(self) -> None:
        def observe(native: bool, cache_size: int, asynchronous: bool) -> tuple[list[str], list[str]]:
            events: list[str] = []

            def tick() -> str:
                events.append('tick')
                return str(len(events))

            mapping = {'library': '{% set result="old" ~ tick() %}'}
            environment = jinja2.Environment(
                loader=jinja2.DictLoader(mapping), cache_size=cache_size, auto_reload=True,
                enable_async=asynchronous,
            )
            environment.globals['tick'] = tick
            source = '{% import "library" as lib %}{{ lib.result }}'
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})

                def run() -> str:
                    return str(session.render('case', source))
            else:
                compiled = environment.from_string(source)

                def run() -> str:
                    return compiled.render()

            output = [run(), run()]
            mapping['library'] = '{% set result="new" ~ tick() %}'
            output.append(run())
            return output, events

        for cache_size in (0, 400):
            for asynchronous in (False, True):
                with self.subTest(cache_size=cache_size, asynchronous=asynchronous):
                    expected = observe(False, cache_size, asynchronous)
                    expected_output = ['old1', 'old2', 'new3'] if cache_size == 0 else ['old1', 'old1', 'new2']
                    self.assertEqual(expected[0], expected_output)
                    self.assertEqual(observe(True, cache_size, asynchronous), expected)

    def test_import_module_execution_and_context(self) -> None:
        def observe(native: bool, extension: bool, with_context: bool, from_import: bool) -> tuple[str, list[str]]:
            events: list[str] = []

            def tick() -> str:
                events.append('tick')
                return str(len(events))

            environment = jinja2.Environment(
                extensions=[Passthrough] if extension else [],
                loader=jinja2.DictLoader({'library': '{% set n=tick() %}'}),
            )
            environment.globals['tick'] = tick
            suffix = ' with context' if with_context else ''
            if from_import:
                source = ('{% from "library" import n as first' + suffix + ' %}'
                          '{% from "library" import n as second' + suffix + ' %}'
                          '{{ first }}|{{ second }}')
            else:
                source = ('{% import "library" as first' + suffix + ' %}'
                          '{% import "library" as second' + suffix + ' %}'
                          '{{ first.n }}|{{ second.n }}')
            if native:
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                return str(session.render('case', source)), events
            return environment.from_string(source).render(), events

        for extension in (True, False):
            for with_context in (True, False):
                for from_import in (True, False):
                    with self.subTest(extension=extension, with_context=with_context, from_import=from_import):
                        self.assertEqual(observe(True, extension, with_context, from_import),
                                         observe(False, extension, with_context, from_import))

    def test_import_context_and_aliases(self) -> None:
        cases = (
            '{% import "library" as lib %}{{ lib.visible }}|{{ lib.show() }}',
            '{% import "library" as lib without context %}{{ lib.visible }}|{{ lib.show() }}',
            '{% import "library" as lib with context %}{{ lib.visible }}|{{ lib.show() }}',
            '{% from "library" import visible, show as display %}{{ visible }}|{{ display() }}',
            '{% from "library" import visible as v, show as display with context %}{{ v }}|{{ display() }}',
            '{% from "library" import show as display, other without context %}{{ display() }}|{{ other() }}',
        )
        for source in cases:
            for extension in (True, False):
                for asynchronous in (True, False):
                    with self.subTest(source=source, extension=extension, asynchronous=asynchronous):
                        self.assertEqual(render(True, source, extension=extension, asynchronous=asynchronous),
                                         render(False, source, extension=extension, asynchronous=asynchronous))


if __name__ == '__main__':
    unittest.main()
