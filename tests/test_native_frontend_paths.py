"""Native frontend for data-only attribute and item expressions."""

import io
import random
import string
import subprocess
import sys
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from docxtpl_native.simple_frontend import accepts
from jinja2.lexer import Lexer
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template


@contextmanager
def no_reference_parser() -> Generator[None, None, None]:
    codes = {jinja2.Environment.parse.__code__, jinja2.Environment._parse.__code__,
             Lexer.tokeniter.__code__}
    calls: list[str] = []
    previous = sys.getprofile()

    def profile(frame: FrameType, event: str, argument: object) -> None:
        if event == 'call' and frame.f_code in codes:
            calls.append(frame.f_code.co_qualname)

    try:
        sys.setprofile(profile)
        yield
    finally:
        sys.setprofile(previous)
    if calls:
        raise AssertionError('reference parser or lexer executed: ' + repr(calls))


def parsed_sources(run: Any) -> list[str]:
    """The sources Jinja's parser received while `run` ran (observed with a
    profiler, so the parser itself stays the standard one)."""
    parsed: list[str] = []
    previous = sys.getprofile()

    def profile(frame: FrameType, event: str, argument: object) -> None:
        if event == 'call' and frame.f_code is jinja2.Environment.parse.__code__:
            parsed.append(frame.f_locals['source'])

    try:
        sys.setprofile(profile)
        run()
    finally:
        sys.setprofile(previous)
    return parsed


class NativeFrontendPathTests(unittest.TestCase):
    def test_call_in_body_without_reference_parser(self) -> None:
        values: dict[str, Any] = {'user': {'name': 'Ada', 'age': 7}}
        source = '{% for key, value in user.items() %}{{ key }}={{ value }};{% endfor %}'
        input_doc = template(source)
        reference = generate(docxtpl, source, values, source=input_doc)
        candidate: list[bytes] = []
        parsed = parsed_sources(lambda: candidate.append(generate(docxtpl_native, source, values, source=input_doc)))
        self.assertTrue(compare_docx(reference, candidate[0])['equivalent'])
        # The call is a callback: the body is native, later parts use Jinja's parser.
        self.assertFalse([text for text in parsed if 'user.items' in text])

    def test_conditional_frontend_parser_override_changes_result(self) -> None:
        source = '{% if user.active %}{{user.name}}{% endif %}'
        context = {'user': {'active': True, 'name': 'Ada', 'other': 'Grace'}}
        original = jinja2.Environment._parse

        def parse(environment: Any, source: str, name: str | None, filename: str | None) -> Any:
            return original(environment, source.replace('user.name', 'user.other'), name, filename)

        try:
            setattr(jinja2.Environment, '_parse', parse)
            expected = jinja2.Environment().from_string(source).render(context)
            config = BridgeConfig(None, False)
            config.enable_simple_sources(context)
            session = docxtpl_native._extension().PythonRenderSession(config, context)
            actual = session.render('conditional-override-result', source)
        finally:
            jinja2.Environment._parse = original
        self.assertEqual(expected, 'Grace')
        self.assertEqual(actual, expected)

    def test_simple_frontend_parser_override_changes_result(self) -> None:
        source = '{{user.name}}'
        context = {'user': {'name': 'Ada', 'other': 'Grace'}}
        config = BridgeConfig(None, False)
        config.enable_simple_sources(context)
        session = docxtpl_native._extension().PythonRenderSession(config, context)
        original = jinja2.Environment._parse

        def parse(environment: Any, source: str, name: str | None, filename: str | None) -> Any:
            return original(environment, source.replace('user.name', 'user.other'), name, filename)

        try:
            setattr(jinja2.Environment, '_parse', parse)
            expected = jinja2.Environment().from_string(source).render(context)
            actual = session.render('simple-override-result', source)
        finally:
            jinja2.Environment._parse = original
        self.assertEqual(expected, 'Grace')
        self.assertEqual(actual, expected)

    def test_simple_frontend_parser_override_before_import_changes_result(self) -> None:
        script = '''
import jinja2
original = jinja2.Environment._parse
def parse(environment, source, name, filename):
    return original(environment, source.replace('user.name', 'user.other'), name, filename)
jinja2.Environment._parse = parse
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig
source = '{{user.name}}'
context = {'user': {'name': 'Ada', 'other': 'Grace'}}
expected = jinja2.Environment().from_string(source).render(context)
config = BridgeConfig(None, False)
config.enable_simple_sources(context)
session = docxtpl_native._extension().PythonRenderSession(config, context)
actual = session.render('simple-override-result', source)
assert expected == 'Grace', expected
assert actual == expected, (actual, expected)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_simple_frontend_observes_parser_override_after_setup(self) -> None:
        source = '{{user.name}}'
        context = {'user': {'name': 'Ada'}}
        config = BridgeConfig(None, False)
        config.enable_simple_sources(context)
        session = docxtpl_native._extension().PythonRenderSession(config, context)
        original = jinja2.Environment._parse
        events: list[str] = []

        def parse(environment: Any, source: str, name: str | None, filename: str | None) -> Any:
            events.append('parse')
            return original(environment, source, name, filename)

        try:
            setattr(jinja2.Environment, '_parse', parse)
            self.assertEqual(session.render('simple-override', source), 'Ada')
        finally:
            jinja2.Environment._parse = original
        self.assertEqual(events, ['parse'])

    def test_data_conditionals_use_native_parser_and_match_reference(self) -> None:
        sources = (
            '{% if user.active %}Hello {{user.name}}{% endif %}',
            '{% if not user.active %}inactive{% else %}{{user.name}}{% endif %}',
            '{% if user.active and user.name %}{{user.name}}{% else %}empty{% endif %}',
            '{% if user.active %}{% if user.name %}{{user.name}}{% endif %}{% endif %}',
            '{% if user.active %}active{% elif user.name %}named{% else %}empty{% endif %}',
        )
        values = ({'user': {'active': True, 'name': 'Ada'}},
                  {'user': {'active': False, 'name': ''}},
                  {'user': {'active': False, 'name': 'Ada'}},
                  {'user': {'active': True}})
        for source in sources:
            for context in values:
                with self.subTest(source=source, context=context):
                    expected = jinja2.Environment().from_string(source).render(context)
                    config = BridgeConfig(None, False)
                    config.enable_simple_sources(context)
                    session = docxtpl_native._extension().PythonRenderSession(config, context)
                    with no_reference_parser():
                        actual = session.render('conditional', source)
                    self.assertEqual(actual, expected)
                    self.assertEqual(config.native_frontend_sources, 1)

    def test_data_conditional_complete_document_avoids_reference_parser(self) -> None:
        source = '{% if user.active %}Hello {{user.name}}{% else %}Inactive{% endif %}'
        input_doc = template(source)
        for active in (True, False):
            with self.subTest(active=active):
                context = {'user': {'active': active, 'name': 'Ada'}}
                reference = generate(docxtpl, source, context, source=input_doc)
                with no_reference_parser():
                    candidate = generate(docxtpl_native, source, context, source=input_doc)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_conditional_frontend_observes_parser_override_after_setup(self) -> None:
        source = '{% if user.active %}{{user.name}}{% endif %}'
        context = {'user': {'active': True, 'name': 'Ada'}}
        config = BridgeConfig(None, False)
        config.enable_simple_sources(context)
        session = docxtpl_native._extension().PythonRenderSession(config, context)
        original = jinja2.Environment._parse
        events: list[str] = []

        def parse(environment: Any, source: str, name: str | None, filename: str | None) -> Any:
            events.append('parse')
            return original(environment, source, name, filename)

        try:
            setattr(jinja2.Environment, '_parse', parse)
            self.assertEqual(session.render('conditional-override', source), 'Ada')
        finally:
            jinja2.Environment._parse = original
        self.assertEqual(events, ['parse'])

    def test_conditional_frontend_observes_parser_override_before_setup(self) -> None:
        source = '{% if user.active %}{{user.name}}{% endif %}'
        context = {'user': {'active': True, 'name': 'Ada'}}
        original = jinja2.Environment._parse
        events: list[str] = []

        def parse(environment: Any, source: str, name: str | None, filename: str | None) -> Any:
            events.append('parse')
            return original(environment, source, name, filename)

        try:
            setattr(jinja2.Environment, '_parse', parse)
            config = BridgeConfig(None, False)
            config.enable_simple_sources(context)
            session = docxtpl_native._extension().PythonRenderSession(config, context)
            self.assertEqual(session.render('conditional-override', source), 'Ada')
        finally:
            jinja2.Environment._parse = original
        self.assertEqual(events, ['parse'])

    def test_conditional_frontend_observes_parser_override_before_import(self) -> None:
        script = '''
import jinja2
original = jinja2.Environment._parse
events = []
def parse(environment, source, name, filename):
    events.append('parse')
    return original(environment, source, name, filename)
jinja2.Environment._parse = parse
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig
context = {'user': {'active': True, 'name': 'Ada'}}
config = BridgeConfig(None, False)
config.enable_simple_sources(context)
session = docxtpl_native._extension().PythonRenderSession(config, context)
assert session.render('conditional-override', '{% if user.active %}{{user.name}}{% endif %}') == 'Ada'
assert events == ['parse'], events
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_accepted_and_rejected_syntax(self) -> None:
        names = frozenset({'user', 'items'})
        accepted = ['{{ user.name }}', '{{ user . name }}', "{{ user['first name'] }}",
                    '{{ user["name"] }}', '{{ items[0] }}', '{{ items[-1] }}',
                    "{{ user.members[1]['name'] }}"]
        rejected = ['{{ missing.name }}', '{{ user. }}', '{{ user.0 }}', '{{ user.true }}', '{{ user[] }}',
                    '{{ user[foo] }}', '{{ user[1:2] }}', '{{ user[0] ** 1 }}',
                    '{{ user[999999999999999999999999] }}', '{{ user[01] }}', '{{ user[-01] }}',
                    "{{ user['a\\nb'] }}",
                    '{{ user.name(**items) }}', '{{ user|upper(1) }}', '{{- user.name }}']
        for source in accepted:
            with self.subTest(source=source):
                self.assertTrue(accepts(source, names))
        for source in rejected:
            with self.subTest(source=source):
                self.assertFalse(accepts(source, names))

    def test_accepted_and_rejected_loops(self) -> None:
        names = frozenset({'user', 'items'})
        accepted = ['{% for item in items %}{{ item }}{% endfor %}',
                    '{% for item in user.members %}{{ item.name }}{{ loop.index }}{% endfor %}',
                    '{% for a, b in items %}{{ a }}{{ b }}{% else %}{{ user.name }}{% endfor %}',
                    '{% for item in items %}{% if loop.first and not loop.last %}{{ item }}{% endif %}{% endfor %}',
                    '{% for row in items %}{% for cell in row %}{{ cell }}{{ loop.length }}{% endfor %}{% endfor %}']
        rejected = ['{% for item in items if item %}{{ item }}{% endfor %}',
                    '{% for item in items recursive %}{{ item }}{% endfor %}',
                    '{% for loop in items %}{{ loop }}{% endfor %}',
                    '{% for item in items %}{{ loop.cycle }}{% endfor %}',
                    '{% for item in items %}{{ loop.cycle(1, 2) }}{% endfor %}',
                    '{{ loop.index }}',
                    '{% for item in items %}{% endfor %}{{ item }}',
                    '{% for item in items %}{% else %}{{ loop.index }}{% endfor %}',
                    '{% for item in missing %}{{ item }}{% endfor %}',
                    '{% for item in items|reverse %}{{ item }}{% endfor %}',
                    '{%- for item in items %}{{ item }}{% endfor %}']
        for source in accepted:
            with self.subTest(source=source):
                self.assertTrue(accepts(source, names))
        for source in rejected:
            with self.subTest(source=source):
                self.assertFalse(accepts(source, names))

    def test_accepted_and_rejected_conditions_and_filters(self) -> None:
        names = frozenset({'user', 'items'})
        accepted = ["{% if user.name == 'Ada' %}x{% endif %}", '{% if user.age > 3 %}x{% endif %}',
                    "{% if 'a' in items %}x{% endif %}", "{% if user.name not in items %}x{% endif %}",
                    '{% if user.name is defined and user.age is not none %}x{% endif %}',
                    '{% if user.flag == true %}x{% endif %}', '{{ user.name|upper }}',
                    "{{ user.name|default('none')|title }}", "{{ items|join(', ') }}",
                    '{% for item in items %}{{ item|trim }}{% endfor %}', '{{ items|length }}']
        rejected = ['{% if user.age > 3.5 %}x{% endif %}',
                    "{% if user.name == 'a\\nb' %}x{% endif %}", "{% if user.name == '<b>' %}x{% endif %}",
                    '{% if user.age > 007 %}x{% endif %}', '{% if user.name is divisibleby(3) %}x{% endif %}',
                    '{{ user.name|upper(1) }}', "{{ user.name|replace('a', 'b') }}",
                    "{{ user.name|default(user.other) }}", "{{ user.name|default(x='y') }}",
                    '{{ user.name == 1 }}', '{{ user.name is defined }}', '{{ user.name|nosuch }}']
        for source in accepted:
            with self.subTest(source=source):
                self.assertTrue(accepts(source, names))
        for source in rejected:
            with self.subTest(source=source):
                self.assertFalse(accepts(source, names))

    def test_condition_and_filter_documents_without_reference_parser(self) -> None:
        values: dict[str, Any] = {'user': {'name': ' ada ', 'age': 4, 'flag': True}, 'items': ['a', 'b']}
        sources = ["{% if user.name|trim == 'ada' %}{{ user.name|trim|title }}{% endif %}",
                   "{% if user.age >= 4 and 'a' in items %}{{ items|join(', ')|upper }}{% endif %}",
                   "{% if user.missing is undefined %}{{ user.missing|default('-') }}{% endif %}",
                   '{% for item in items %}{% if item != "b" %}{{ item|upper }}{% endif %}{% endfor %}',
                   '{{ items|length }} {{ items|first }} {{ items|last }}']
        for source in sources:
            with self.subTest(source=source):
                input_doc = template(source)
                reference = generate(docxtpl, source, values, source=input_doc)
                with no_reference_parser():
                    candidate = generate(docxtpl_native, source, values, source=input_doc)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_accepted_and_rejected_assignments(self) -> None:
        names = frozenset({'user', 'items'})
        accepted = ['{% set name = user.name %}{{ name }}', "{% set label = 'x' %}{{ label }}",
                    "{% set name = user.name|default('-') %}{% if name == '-' %}{{ name }}{% endif %}",
                    '{% if user %}{% set name = user.name %}{% endif %}{{ name }}',
                    '{% for item in items %}{% set last = item %}{{ last }}{% endfor %}']
        rejected = ['{{ name }}{% set name = user.name %}', '{% set a, b = items %}{{ a }}',
                    '{% set loop = items %}', '{% set name %}x{% endset %}{{ name }}',
                    '{% set ns.name = 1 %}', '{% set name = user.name ~ 1.5 %}',
                    '{% for item in items %}{% set last = item %}{% endfor %}{{ last }}']
        for source in accepted:
            with self.subTest(source=source):
                self.assertTrue(accepts(source, names))
        for source in rejected:
            with self.subTest(source=source):
                self.assertFalse(accepts(source, names))

    def test_assignment_documents_without_reference_parser(self) -> None:
        values: dict[str, Any] = {'user': {'name': 'Ada'}, 'items': ['a', 'b']}
        sources = ["{% set name = user.name|upper %}{{ name }} {% set label = 'x' %}{{ label }}",
                   '{% for item in items %}{% set copy = item %}{{ copy }}{% endfor %}',
                   "{% if user.name %}{% set shown = user.name %}{% endif %}{{ shown|default('-') }}"]
        for source in sources:
            with self.subTest(source=source):
                input_doc = template(source)
                reference = generate(docxtpl, source, values, source=input_doc)
                with no_reference_parser():
                    candidate = generate(docxtpl_native, source, values, source=input_doc)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_package_values_without_reference_parser(self) -> None:
        def values(engine: Any) -> dict[str, Any]:
            return {'name': 'Ada', 'title': engine.RichText('Bold', bold=True),
                    'lines': engine.Listing('one\ntwo'),
                    'rows': [engine.RichText('first', italic=True), engine.RichText('second')]}
        sources = ['Hi {{r title }} {{ name }}', '{{ lines }}',
                   '{% for row in rows %}{{r row }};{% endfor %}']
        for source in sources:
            with self.subTest(source=source):
                input_doc = template(source)
                reference = generate(docxtpl, source, values(docxtpl), source=input_doc)
                with no_reference_parser():
                    candidate = generate(docxtpl_native, source, values(docxtpl_native), source=input_doc)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_replaced_package_value_method_keeps_reference_parser(self) -> None:
        from docxtpl_native.simple_frontend import context_names
        value = docxtpl_native.RichText('x')
        self.assertIsNotNone(context_names({'value': value}))
        original = docxtpl_native.RichText.__str__
        try:
            docxtpl_native.RichText.__str__ = lambda self: 'replaced'  # type: ignore[method-assign]
            self.assertIsNone(context_names({'value': value}))
        finally:
            docxtpl_native.RichText.__str__ = original  # type: ignore[method-assign]
        value.extra = 1  # type: ignore[attr-defined]
        self.assertIsNone(context_names({'value': value}))

    def test_object_context_body_without_reference_parser(self) -> None:
        events: list[str] = []

        class User:
            def __init__(self, name: str) -> None:
                self.name = name

            @property
            def upper_name(self) -> str:
                events.append('property')
                return self.name.upper()

        values = {'user': User('Ada'), 'items': [User('a'), User('b')]}
        sources = ['Hi {{ user.name }} {{ user.upper_name }}',
                   '{% for item in items %}{{ item.name }}{% if not loop.last %},{% endif %}{% endfor %}']
        for source in sources:
            with self.subTest(source=source):
                input_doc = template(source)
                events.clear()
                reference = generate(docxtpl, source, values, source=input_doc)
                expected, events[:] = list(events), []
                rendered: list[bytes] = []
                parsed = parsed_sources(lambda: rendered.append(  # noqa: B023
                    generate(docxtpl_native, source, values, source=input_doc)))  # noqa: B023
                self.assertTrue(compare_docx(reference, rendered[0])['equivalent'])
                self.assertEqual(events, expected)
                # The body was compiled before any object ran user code;
                # the parts after it use the reference parser.
                self.assertFalse([text for text in parsed if 'item.name' in text or 'user.name' in text])

    def test_accepted_and_rejected_operations(self) -> None:
        names = frozenset({'user', 'items'})
        accepted = ["{{ user.name ~ '!' }}", '{{ user.age + 1 }}', '{{ items|length * 2 }}',
                    "{{ 'Dr. ' ~ user.name ~ ' ' ~ user.last }}", "{{ user.name if user.active else '-' }}",
                    "{{ 'yes' if user.age > 3 else 'no' }}", '{{ user.age // 2 }} {{ user.age % 2 }}',
                    '{{ items[0] + 1 }}', '{% set name = user.name ~ "x" %}{{ name }}',
                    "{{ user.greet('hi', user.name) }}", '{{ user.items()|length }}',
                    '{% for key, value in user.items() %}{{ key }}{% endfor %}']
        rejected = ['{{ user.age + 1 * 2 }}', '{{ (user.age + 1) * 2 }}',
                    "{{ user.name if user.active }}", '{{ user.age ** 2 }}', '{{ user.age + 1.5 }}',
                    "{{ user.name ~ user.age + 1 }}", '{{ -user.age }}', '{{ range(3) }}',
                    '{{ user.greet(name=1) }}', '{{ user.greet(*items) }}', '{{ user.greet()() }}',
                    '{{ caller() }}', '{{ loop.cycle(1) }}']
        for source in accepted:
            with self.subTest(source=source):
                self.assertTrue(accepts(source, names))
        for source in rejected:
            with self.subTest(source=source):
                self.assertFalse(accepts(source, names))

    def test_operation_documents_without_reference_parser(self) -> None:
        values: dict[str, Any] = {'user': {'name': 'Ada', 'last': 'L', 'age': 7, 'active': False},
                                  'items': ['a', 'b']}
        sources = ["{{ 'Dr. ' ~ user.name ~ ' ' ~ user.last }}!", '{{ user.age + 1 }}/{{ user.age // 2 }}/{{ user.age % 2 }}',
                   "{{ user.name if user.active else '-' }} {{ 'yes' if user.age > 3 else 'no' }}",
                   '{{ items|length * 2 }} {{ user.age / 2 }}']
        for source in sources:
            with self.subTest(source=source):
                input_doc = template(source)
                reference = generate(docxtpl, source, values, source=input_doc)
                with no_reference_parser():
                    candidate = generate(docxtpl_native, source, values, source=input_doc)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_accepted_and_rejected_macros(self) -> None:
        names = frozenset({'user', 'items'})
        accepted = ["{% macro greet(name, mark='!') %}Hi {{ name }}{{ mark }}{% endmacro %}{{ greet(user.name) }}",
                    ('{% macro row(item) %}{% for part in item %}{{ part }}{% endfor %}{% endmacro %}'
                     '{% for item in items %}{{ row(item) }}{% endfor %}'),
                    '{% macro twice(x) %}{% set y = x ~ x %}{{ y }}{% endmacro %}{{ twice(user.name) }}']
        rejected = ['{{ greet(user.name) }}{% macro greet(name) %}{{ name }}{% endmacro %}',
                    '{% macro m(a, a) %}{{ a }}{% endmacro %}', '{% macro m(varargs) %}{% endmacro %}',
                    '{% macro m() %}{{ varargs }}{% endmacro %}', '{% macro m() %}{{ caller() }}{% endmacro %}',
                    '{% macro m(a=user.name) %}{% endmacro %}', '{% macro m(x) %}{% set y = x %}{% endmacro %}{{ y }}',
                    '{% macro loop() %}{% endmacro %}', '{% macro m(a) %}{{ a }}{% endmacro %}{{ a }}']
        for source in accepted:
            with self.subTest(source=source):
                self.assertTrue(accepts(source, names))
        for source in rejected:
            with self.subTest(source=source):
                self.assertFalse(accepts(source, names))

    def test_macro_documents_match_docxtpl(self) -> None:
        values: dict[str, Any] = {'user': {'name': 'Ada'}, 'items': [['a', 'b'], ['c']]}
        sources = [("{% macro greet(name, mark='!') %}Hi {{ name }}{{ mark }}{% endmacro %}{{ greet(user.name) }} "
                    "{{ greet(user.name, '?') }}"),
                   ('{% macro row(item) %}{% for part in item %}{{ part }}{% endfor %}{% endmacro %}'
                    '{% for item in items %}{{ row(item) }};{% endfor %}')]
        for source in sources:
            with self.subTest(source=source):
                input_doc = template(source)
                reference = generate(docxtpl, source, values, source=input_doc)
                candidate: list[bytes] = []
                parsed = parsed_sources(lambda: candidate.append(  # noqa: B023
                    generate(docxtpl_native, source, values, source=input_doc)))  # noqa: B023
                self.assertTrue(compare_docx(reference, candidate[0])['equivalent'])
                self.assertFalse([text for text in parsed if 'endmacro' in text])

    def test_literal_expressions_without_reference_parser(self) -> None:
        names = frozenset({'user'})
        sources = ["{{ 'a' }}{{ 1 }}{{ true }}{{ none }}",
                   "{{ 'a' ~ 'b' }} {{ 1 + 2 }} {{ 7 // 2 }} {{ 7 % 3 }} {{ 1 / 2 }} {{ 999999999 * 999999999 }}",
                   "{{ 'ab'|upper }} {{ 'x'|default('y') }} {{ 'abc'|length }} {{ ' x '|trim }}",
                   "{% if 'a' == 'a' %}eq{% endif %}{% if 1 > 2 %}gt{% else %}le{% endif %}",
                   "{{ 'yes' if 1 else 'no' }} {{ 'x' * 3 }}"]
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(accepts(source, names))
                input_doc = template(source)
                reference = generate(docxtpl, source, {'user': 1}, source=input_doc)
                with no_reference_parser():
                    candidate = generate(docxtpl_native, source, {'user': 1}, source=input_doc)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])
        # A literal operation that fails folds to nothing in Jinja and fails
        # when rendered, as here.
        source = '{{ 1 / 0 }}'
        input_doc = template(source)
        for engine in (docxtpl, docxtpl_native):
            with self.subTest(engine=engine.__name__), self.assertRaises(ZeroDivisionError):
                generate(engine, source, {'user': 1}, source=input_doc)

    def test_loop_documents_without_reference_parser(self) -> None:
        values: dict[str, Any] = {'user': {'name': 'Ada', 'members': [{'name': 'first'}, {'name': 'second'}]},
                                  'items': [['a', 'b'], ['c', 'd']], 'empty': []}
        sources = ['{% for member in user.members %}{{ loop.index }}:{{ member.name }};{% endfor %}',
                   '{% for a, b in items %}{{ a }}{{ b }}{% if not loop.last %},{% endif %}{% endfor %}',
                   '{% for row in items %}{% for cell in row %}{{ loop.revindex }}{{ cell }}{% endfor %}{% endfor %}',
                   '{% for item in empty %}{{ item }}{% else %}none for {{ user.name }}{% endfor %}']
        for source in sources:
            with self.subTest(source=source):
                input_doc = template(source)
                reference = generate(docxtpl, source, values, source=input_doc)
                with no_reference_parser():
                    candidate = generate(docxtpl_native, source, values, source=input_doc)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_complete_documents_without_reference_parser(self) -> None:
        values: dict[str, Any] = {'user': {'name': 'Ada', 'first name': 'A',
                                           'members': [{'name': 'first'}, {'name': 'second'}]},
                                  'items': ['zero', 'one', 'two']}
        sources = ['{{ user.name }}', '{{ user . name }}', "{{ user['first name'] }}",
                   '{{ user["name"] }}', '{{ items[0] }}', '{{ items[-1] }}',
                   "{{ user.members[1]['name'] }}",
                   'Hi {{ user.name }} / {{ items[1] }}!']
        for source in sources:
            with self.subTest(source=source):
                input_doc = template(source)
                reference = generate(docxtpl, source, values, source=input_doc)
                with no_reference_parser():
                    candidate = generate(docxtpl_native, source, values, source=input_doc)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_plain_explicit_environment_avoids_reference_parser(self) -> None:
        values: dict[str, Any] = {'user': {'name': 'Ada', 'active': True}, 'items': ['zero', 'one']}
        source = '{% if user.active %}Hi {{ user.name }} / {{ items[1] }}{% endif %}'
        input_doc = template(source)
        for options in ({}, {'trim_blocks': True, 'lstrip_blocks': True}, {'autoescape': True}):
            with self.subTest(options=options):
                reference = generate(docxtpl, source, values, jinja2.Environment(**options), source=input_doc)
                with no_reference_parser():
                    candidate = generate(docxtpl_native, source, values, jinja2.Environment(**options),
                                         source=input_doc)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_explicit_environment_with_hooks_keeps_reference_parser(self) -> None:
        class Custom(jinja2.Environment):
            pass

        values: dict[str, Any] = {'user': {'name': 'Ada'}}
        source = 'Hi {{ user.name }}'
        input_doc = template(source)
        environments = {
            'finalize': lambda: jinja2.Environment(finalize=lambda value: f'<{value}>'),
            'extension': lambda: jinja2.Environment(extensions=['jinja2.ext.loopcontrols']),
            'subclass': lambda: Custom(),
        }
        for label, environment in environments.items():
            with self.subTest(label):
                reference = generate(docxtpl, source, values, environment(), source=input_doc)
                candidate: list[bytes] = []
                with self.assertRaisesRegex(AssertionError, 'reference parser'), no_reference_parser():
                    candidate.append(generate(docxtpl_native, source, values, environment(), source=input_doc))
                self.assertTrue(compare_docx(reference, candidate[0])['equivalent'])

    def test_missing_paths_keep_document_compatibility(self) -> None:
        values = {'user': {}, 'items': ['one']}
        for source in ('{{ user.missing }}', '{{ items[99] }}'):
            with self.subTest(source=source):
                input_doc = template(source)
                reference = generate(docxtpl, source, values, source=input_doc)
                candidate = generate(docxtpl_native, source, values, source=input_doc)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_invalid_paths_keep_reference_errors(self) -> None:
        values = {'user': {'name': 'Ada'}}
        for source in ('{{ user. }}', '{{ user[01] }}', '{{ user.name + }}'):
            with self.subTest(source=source):
                with self.assertRaises(jinja2.TemplateSyntaxError) as expected:
                    jinja2.Environment().from_string(source).render(values)
                config = BridgeConfig(None, False)
                config.enable_simple_sources(values)
                session = docxtpl_native._extension().PythonRenderSession(config, values)
                with self.assertRaises(jinja2.TemplateSyntaxError) as actual:
                    session.render('<string>', source)
                self.assertEqual((actual.exception.message, actual.exception.lineno),
                                 (expected.exception.message, expected.exception.lineno))
                self.assertEqual(config.native_frontend_sources, 0)

        source = '{{ user[] }}'
        expected_value = jinja2.Environment().from_string(source).render(values)
        config = BridgeConfig(None, False)
        config.enable_simple_sources(values)
        session = docxtpl_native._extension().PythonRenderSession(config, values)
        self.assertEqual(session.render('<string>', source), expected_value)
        self.assertEqual(config.native_frontend_sources, 0)

    def test_empty_subscript_passes_tuple_to_user_object(self) -> None:
        def run(engine: Any, source: str, input_doc: bytes) -> tuple[bytes, list[object]]:
            events: list[object] = []

            class Value:
                def __getitem__(self, key: object) -> dict[str, str]:
                    events.append((type(key), key))
                    return {'name': 'empty tuple' if key == () else 'other'}

            return generate(engine, source, {'value': Value()}, source=input_doc), events

        for source in ('{{ value[] }}', '{{ value[].name }}', "{{ value[]['name'] }}"):
            with self.subTest(source=source):
                input_doc = template(source)
                reference, expected_events = run(docxtpl, source, input_doc)
                candidate, actual_events = run(docxtpl_native, source, input_doc)
                self.assertEqual(expected_events, [(tuple, ())])
                self.assertEqual(actual_events, expected_events)
                self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_path_keys_and_index_boundaries_without_reference_parser(self) -> None:
        keys = ['', 'a', 'first name', 'a}}b', 'a{%b', 'a"b', 'hyphen-key', '0']
        values = {'user': {key: 'value' + str(i) for i, key in enumerate(keys)},
                  'items': ['first', 'middle', 'last']}
        sources = ["{{ user['" + key + "'] }}" for key in keys]
        sources.extend('{{ items[' + str(index) + '] }}' for index in (-3, -2, -1, 0, 1, 2))
        sources.extend(['{{ user . name }}', '{{ items[999999999] }}'])
        values['user']['name'] = 'Ada'
        for source in sources:
            with self.subTest(source=source):
                expected = jinja2.Environment().from_string(source).render(values)
                config = BridgeConfig(None, False)
                config.enable_simple_sources(values)
                session = docxtpl_native._extension().PythonRenderSession(config, values)
                with no_reference_parser():
                    actual = session.render('<string>', source)
                self.assertEqual(actual, expected)

    def test_generated_string_keys_without_reference_parser(self) -> None:
        randomizer = random.Random(1234)
        characters = ''.join(character for character in string.printable
                             if character not in "'\\<>&\t\r\n\x0b\x0c")
        for _ in range(500):
            key = ''.join(randomizer.choice(characters) for _ in range(randomizer.randrange(15)))
            source = "{{ user['" + key + "'] }}"
            values = {'user': {key: 'value'}}
            with self.subTest(source=source):
                self.assertTrue(accepts(source, frozenset(values)))
                expected = jinja2.Environment().from_string(source).render(values)
                config = BridgeConfig(None, False)
                config.enable_simple_sources(values)
                session = docxtpl_native._extension().PythonRenderSession(config, values)
                with no_reference_parser():
                    actual = session.render('<string>', source)
                self.assertEqual(actual, expected)

    def test_spoofed_scalar_callback_preserves_later_parser_mutation(self) -> None:
        input_doc = template('{{ value.name }}')

        def run(engine: Any) -> tuple[bytes, list[str]]:
            events: list[str] = []
            original = jinja2.Environment._parse

            class ScalarMeta(type):
                def __eq__(cls, other: object) -> bool:
                    return other is int

                __hash__ = type.__hash__

            class UserValue(metaclass=ScalarMeta):
                def __getattribute__(self, name: str) -> Any:
                    if name == 'name':
                        events.append('access')

                        def changed(self: jinja2.Environment, source: str,
                                    name: str | None, filename: str | None) -> Any:
                            events.append('later_parse')
                            return original(self, source, name, filename)

                        jinja2.Environment._parse = changed
                        return 'Ada'
                    return object.__getattribute__(self, name)

            try:
                document = engine.DocxTemplate(io.BytesIO(input_doc))
                document.render({'value': UserValue()})
                output = io.BytesIO()
                document.save(output)
                return output.getvalue(), events
            finally:
                jinja2.Environment._parse = original

        reference, expected_events = run(docxtpl)
        candidate, actual_events = run(docxtpl_native)
        self.assertGreater(expected_events.count('later_parse'), 0)
        self.assertEqual(actual_events, expected_events)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])
