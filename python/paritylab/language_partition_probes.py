"""Differential probes for the partitions of the Jinja language contracts.

The catalog's language (`jinja.language.*`), iteration (`jinja.iteration.*`),
lookup, callback, sandbox, compilation, extension, async, undefined and
finalize contracts name partitions such as generator laziness, exception
timing, scope unwinding, import caches or extension mutations. Each probe
renders one template of that partition in a document body, with callbacks
that record what they see, and observes the rendered text, the trace and the
exception (type and message).

Expected observations in ``language_partition_expected.json`` were recorded
from the reference (docxtpl 0.20.2 with Jinja2 3.1.6) by
``scripts/record_reference_observations.py``.
"""
from __future__ import annotations

import io
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import jinja2
import jinja2.ext
import jinja2.sandbox
from docx import Document
from markupsafe import Markup

from .protocol_base import Probe, Result

Trace = list[Any]
Scenario = Callable[[Any], dict[str, Any]]
SCENARIOS: dict[str, dict[str, Scenario]] = {}


class ProbeError(Exception):
    """An error a callback raises; probes check it reaches the caller as is."""


def error(exception: BaseException) -> dict[str, Any]:
    return {'type': type(exception).__module__ + '.' + type(exception).__qualname__,
            'message': re.sub(r' at 0x[0-9a-fA-F]+', '', str(exception))}


def render(engine: Any, source: str, context: dict[str, Any] | None = None, *,
           environment: jinja2.Environment | None = None, autoescape: bool = False) -> dict[str, Any]:
    """Render `source` in a document body and return its text or the error."""
    document = Document()
    document.add_paragraph(source)
    buffer = io.BytesIO()
    document.save(buffer)
    template = engine.DocxTemplate(io.BytesIO(buffer.getvalue()))
    try:
        template.render(context or {}, jinja_env=environment, autoescape=autoescape)
        output = io.BytesIO()
        template.save(output)
    except Exception as exception:  # noqa: BLE001 - the exception is the observation
        return {'error': error(exception)}
    return {'text': '\n'.join(paragraph.text for paragraph in Document(io.BytesIO(output.getvalue())).paragraphs)}


def environment(templates: dict[str, str] | None = None, *, extensions: tuple[Any, ...] = (),
                sandbox: bool = False, **options: Any) -> jinja2.Environment:
    cls = jinja2.sandbox.SandboxedEnvironment if sandbox else jinja2.Environment
    return cls(loader=jinja2.DictLoader(templates) if templates is not None else None, extensions=list(extensions),
               **options)


def namespace(env: jinja2.Environment, kind: str) -> dict[str, Any]:
    """An environment's globals, filters or tests, as a plain mapping."""
    return cast(dict[str, Any], getattr(env, kind))


def scenario(source: str, context: Callable[[Trace], dict[str, Any]] | None = None, *,
             templates: dict[str, str] | None = None, extensions: tuple[Any, ...] = (), sandbox: bool = False,
             autoescape: bool = False, setup: Callable[[jinja2.Environment, Trace], object] | None = None,
             **options: Any) -> Scenario:
    def run(engine: Any) -> dict[str, Any]:
        trace: Trace = []
        env = environment(templates, extensions=extensions, sandbox=sandbox, **options)
        if setup is not None:
            setup(env, trace)
        result = render(engine, source, context(trace) if context else {}, environment=env, autoescape=autoescape)
        return result | {'trace': trace}
    return run


_scenario = scenario


def register(contract: str, partitions: dict[str, Scenario]) -> None:
    SCENARIOS.setdefault(contract, {}).update(partitions)


# Callback helpers -----------------------------------------------------------

def generator(trace: Trace, items: list[Any], fail: int | None = None, name: str = 'g') -> Iterator[Any]:
    for index, item in enumerate(items):
        if index == fail:
            trace.append([name, 'raise'])
            raise ProbeError(f'{name} fails at {index}')
        trace.append([name, item])
        yield item
    trace.append([name, 'end'])


def predicate(trace: Trace, fail: Any = None, name: str = 'p') -> Callable[[Any], bool]:
    def check(value: Any) -> bool:
        trace.append([name, value])
        if value == fail:
            raise ProbeError(f'{name} fails on {value}')
        return value % 2 == 1 if isinstance(value, int) else bool(value)
    return check


def recorder(trace: Trace, name: str, result: Any = '') -> Callable[..., Any]:
    def call(*args: Any, **kwargs: Any) -> Any:
        trace.append([name, list(args), sorted(kwargs)])
        return result
    return call


def failing(trace: Trace, name: str = 'fail') -> Callable[..., Any]:
    def call(*args: Any) -> Any:
        trace.append([name, list(args)])
        raise ProbeError(name + ' raised')
    return call


TREE = [{'name': 'a', 'nums': [1, 2, 3], 'children': [{'name': 'b', 'nums': [4, 5], 'children': []}]},
        {'name': 'c', 'nums': [], 'children': []}]


# Loops: for/else, filters, metadata, recursion, assignment scope -------------

def _loops(head: str, tail: str = '') -> dict[str, Scenario]:
    """The loop family's partitions with `head` as the loop's opening clause."""
    def lazy(trace: Trace) -> dict[str, Any]:
        return {'items': generator(trace, [1, 2, 3, 4]), 'p': predicate(trace), 'mark': recorder(trace, 'body')}
    metadata = ('{{ loop.index }}{{ "L" if loop.last }}{{ loop.revindex }}/{{ loop.length }}'
                '<{{ loop.nextitem }}>')
    return {
        'single-pass generator laziness and side effects': scenario(
            head + '{{ x }}{{ mark(x) }}' + tail + '|{% for y in items %}{{ y }}{% endfor %}', lazy),
        'loop.last/nextitem/revindex/length over filtered generators': scenario(
            head + metadata + tail, lambda trace: {'items': generator(trace, [1, 2, 3, 5, 6]), 'p': predicate(trace)}),
        'predicate/iterator exception timing': scenario(
            head + '{{ x }}' + tail, lambda trace: {'items': generator(trace, [1, 2, 3, 4], fail=3),
                                                    'p': predicate(trace, fail=2)}),
        'recursive filtered loops and scoped outer loop references': scenario(
            '{% for node in tree recursive %}{% set outer = loop %}{{ node.name }}('
            + head.replace('items', 'node.nums') + '{{ x }}{{ outer.index }}' + tail
            + '){% if node.children %}[{{ loop(node.children) }}]{% endif %}{% endfor %}',
            lambda trace: {'tree': TREE, 'p': predicate(trace)}),
    }


for _contract, _head, _tail in (
        ('jinja.language.for_else_empty', '{% for x in items %}', '{% else %}E{% endfor %}'),
        ('jinja.language.for_filter', '{% for x in items if p(x) %}', '{% endfor %}'),
        ('jinja.language.for_filter_empty', '{% for x in items if p(x) %}', '{% else %}E{% endfor %}'),
        ('jinja.language.for_filter_metadata', '{% for x in items if p(x) %}{{ loop.index0 }}', '{% endfor %}'),
        ('jinja.language.recursive_loop', '{% for x in items if p(x) recursive %}', '{% endfor %}'),
        ('jinja.language.loop_assignment_scope', '{% for x in items if p(x) %}{% set last = x %}',
         '{% endfor %}{{ last|default("U") }}')):
    register(_contract, _loops(_head, _tail))


# Loop controls ---------------------------------------------------------------

class OnlyBreak(jinja2.ext.LoopControlExtension):
    tags = {'break'}  # noqa: RUF012 - Jinja reads the class attribute


def _controls(body: str, else_branch: str = '') -> dict[str, Scenario]:
    """break/continue partitions; `body` uses the control statement."""
    loop = '{% for x in items %}' + body + else_branch + '{% endfor %}'
    controls = (jinja2.ext.loopcontrols,)
    return {
        'mixed completed and interrupted iterations': scenario(
            loop + '|' + loop, lambda trace: {'items': [1, 2, 3, 4]}, extensions=controls),
        'nested with/macro/recursive-loop frame unwinding': scenario(
            '{% macro m(xs) %}' + loop.replace('items', 'xs') + '{% endmacro %}{% with items = [1, 2, 3] %}'
            '{{ m(items) }};{% for n in tree recursive %}{% with items = n.nums %}' + loop + '{% endwith %}'
            '{% if n.children %}[{{ loop(n.children) }}]{% endif %}{% endfor %}{% endwith %}',
            lambda trace: {'tree': TREE}, extensions=controls),
        'empty and filtered-loop else semantics': scenario(
            loop + '|' + loop.replace('items', 'empty') + '|' + loop.replace(' in items', ' in items if x > 1'),
            lambda trace: {'items': [1, 2, 3], 'empty': []}, extensions=controls),
        'disabled/mutated extension errors': lambda engine: {
            'disabled': scenario(loop, lambda trace: {'items': [1, 2]})(engine),
            'mutated': scenario(loop, lambda trace: {'items': [1, 2]}, extensions=(OnlyBreak,))(engine)},
    }


register('jinja.language.break', _controls('{{ x }}{% if x == 2 %}{% break %}{% endif %}'))
register('jinja.language.continue', _controls('{% if x == 2 %}{% continue %}{% endif %}{{ x }}'))
register('jinja.language.break_else', _controls('{{ x }}{% if x == 2 %}{% break %}{% endif %}', '{% else %}E'))
register('jinja.language.continue_else', _controls('{% if x == 2 %}{% continue %}{% endif %}{{ x }}', '{% else %}E'))
register('jinja.language.nested_break_else', _controls(
    '{{ x }}:{% for y in items %}{{ y }}{% if y >= x %}{% break %}{% endif %}{% else %}e{% endfor %};', '{% else %}E'))
register('jinja.language.continue_after_completed_iteration', _controls(
    '{{ x }}{% if x > 1 %}{% continue %}{% endif %}!', '{% else %}E'))
register('jinja.language.break_after_completed_iteration', _controls(
    '{{ x }}{% if x > 2 %}{% break %}{% endif %}!', '{% else %}E'))


# Assignments: tuples, captures, filtered blocks --------------------------------

class Pair:
    def __iter__(self) -> Iterator[str]:
        return iter(['first', 'second'])


def _assignments(assign: str) -> dict[str, Scenario]:
    """`assign` assigns `value` to `target` (a capture wraps it in a block)."""
    return {
        'destructuring arity errors': lambda engine: {
            name: scenario('{% set a, b = value %}{{ a }}{{ b }}|' + assign + '{{ target }}',
                           lambda trace, value=value: {'value': value})(engine)
            for name, value in (('short', [1]), ('long', [1, 2, 3]), ('exact', 'ab'), ('scalar', 5))},
        'undefined and arbitrary Python assignment values': scenario(
            assign.replace('value', 'missing') + '[{{ target }}]{% set a, b = pair %}{{ a }}-{{ b }}|' + assign
            + '{{ target }}', lambda trace: {'value': {'k': (1, 2)}, 'pair': Pair()}),
        'nested assignment scopes and capture safety': scenario(
            '{% set target = "outer" %}{% for i in [1] %}' + assign + '{{ target }}{% endfor %}{{ target }}'
            '{% macro m() %}' + assign + '{{ target }}{% endmacro %}{{ m() }}{{ target }}',
            lambda trace: {'value': 'V'}, autoescape=True),
        'filter arguments, callbacks and capture errors': lambda engine: {
            'callback': scenario(assign + '{{ target|f("arg") }}', lambda trace: {
                'value': 'V'}, setup=lambda env, trace: namespace(env, 'filters').update(f=recorder(trace, 'f', 'F')))(engine),
            'raising': scenario(assign.replace('value', 'fail()') + '{{ target }}', lambda trace: {
                'fail': failing(trace)})(engine)},
    }


register('jinja.language.tuple_assignment', _assignments('{% set target, other = (value, 2) %}'))
register('jinja.language.capture', _assignments('{% set target %}{{ value }}{% endset %}'))
register('jinja.language.block_assignment_filter', _assignments(
    '{% set target | replace("V", "W") | upper %}{{ value }}{% endset %}'))


# with ------------------------------------------------------------------------

register('jinja.language.with_scope', {
    'multiple assignments evaluate in outer scope': scenario(
        '{% set a = "outer" %}{% with a = "inner", b = a %}{{ a }}/{{ b }}{% endwith %}{{ a }}'),
    'undefined and callback values': scenario(
        '{% with a = missing, b = f(1) %}[{{ a }}]{{ b }}{% endwith %}', lambda trace: {'f': recorder(trace, 'f', 'F')}),
    'nested with/loop/macro scopes': scenario(
        '{% macro m(v) %}{% with v = v ~ "m" %}{{ v }}{% endwith %}{{ v }}{% endmacro %}{% with v = "w" %}'
        '{% for i in [1, 2] %}{% with v = v ~ i %}{{ m(v) }}{% endwith %}{% endfor %}{{ v }}{% endwith %}'),
    'failure and mutation order': scenario(
        '{% with a = add("a"), b = fail(), c = add("c") %}{{ a }}{% endwith %}',
        lambda trace: {'add': recorder(trace, 'add', 'A'), 'fail': failing(trace)}),
})


# filter blocks -----------------------------------------------------------------

@jinja2.pass_context
def _context_filter(context: Any, value: str) -> str:
    return value + ':' + str(context.get('name'))


@jinja2.pass_eval_context
def _eval_filter(eval_context: Any, value: str) -> str:
    return value + (':auto' if eval_context.autoescape else ':plain')


@jinja2.pass_environment
def _environment_filter(env: Any, value: str) -> str:
    return value + ':' + type(env).__name__


register('jinja.language.filter_block', {
    'autoescape and Markup preservation': scenario(
        '{% filter upper %}{{ value }}{{ safe }}{% endfilter %}|{% filter e %}{{ safe }}{% endfilter %}',
        # an entity upper() keeps valid: an invalid one (&AMP;) would take the
        # deliberate difference in recovery (docxtpl.difference.xml_recovery)
        lambda trace: {'value': 'a"b', 'safe': Markup('&#39;s')}, autoescape=True),
    'context/eval-context/environment callbacks': scenario(
        '{% filter c %}x{% endfilter %}|{% filter ev %}y{% endfilter %}|{% filter en %}z{% endfilter %}',
        lambda trace: {'name': 'N'},
        setup=lambda env, trace: namespace(env, 'filters').update(c=_context_filter, ev=_eval_filter, en=_environment_filter)),
    'nested capture/whitespace control': scenario(
        '{% filter upper -%}  a {% filter replace("B", "c") %} b {%- endfilter %}{% set x %}d{% endset %}{{ x }}'
        '  {%- endfilter %}!'),
})


# macros, caller ----------------------------------------------------------------

def _macros(definition: str, call: str) -> dict[str, Scenario]:
    library = {'lib': '{% macro shout(v) %}{{ v|upper }}{% endmacro %}'}
    return {
        'recursive context and undefined values': lambda engine: {
            'value': scenario(definition + call, lambda trace: {'value': 3})(engine),
            'undefined': scenario(definition + '{{ m(missing) }}')(engine)},
        'nested closures and imported macro lifetimes': scenario(
            "{% import 'lib' as lib %}" + definition + '{% macro outer(v) %}{% macro inner() %}{{ lib.shout(v) }}'
            '{% endmacro %}{{ inner() }}{{ m(v) }}{% endmacro %}{{ outer(2) }}', lambda trace: {}, templates=library),
        'safe output and autoescape transitions': scenario(
            definition + '{{ m(value) }}|{% autoescape false %}{{ m(value) }}{% endautoescape %}',
            lambda trace: {'value': 2}, autoescape=True),
    }


_recursion = '{% macro m(n, sep="\'") %}{{ n }}{{ sep }}{% if n is number and n > 0 %}{{ m(n - 1) }}{% endif %}{% endmacro %}'
register('jinja.language.macro_recursion', _macros(_recursion, '{{ m(value) }}') | {
    'argument binding errors and identity': lambda engine: {
        name: scenario(_recursion + source, lambda trace: {'value': [1]})(engine)
        for name, source in (('extra', '{{ m(1, 2, 3) }}'), ('keyword', '{{ m(n=1, x=2) }}'),
                             ('missing', '{{ m() }}'), ('duplicate', '{{ m(1, n=2) }}'),
                             ('identity', '{{ m.name }} {{ m.arguments }} {{ m.catch_kwargs }}'))}})
register('jinja.language.macro_defaults', _macros(
    '{% macro m(n, d=n * 2, e=missing) %}{{ n }}/{{ d }}/{{ e is defined }}/{{ "\'" }}{% endmacro %}',
    '{{ m(value) }}{{ m(value, e=1) }}'))

_caller_macro = '{% macro m(n) %}[{{ caller(n, k="K") }}]{% endmacro %}'
register('jinja.language.caller', {
    'caller arguments and keyword binding': lambda engine: {
        'declared': scenario(_caller_macro + '{% call(a, k) m(1) %}{{ a }}{{ k }}{% endcall %}')(engine),
        'undeclared_keyword': scenario(_caller_macro + '{% call(a) m(2) %}{{ a }}{% endcall %}')(engine),
        'varargs': scenario('{% macro m() %}{{ caller(1, 2, k=3) }}{% endmacro %}'
                            '{% call m() %}{{ varargs }}{{ kwargs }}{% endcall %}')(engine)},
    'caller defaults, recursion and identity': scenario(
        '{% macro m(n) %}{{ n }}{% if n %}({{ caller(n) }}){% endif %}{% endmacro %}'
        '{% call(x, y="d") m(2) %}{{ x }}{{ y }}{{ caller is defined }}{% endcall %}'),
    'callback exceptions and captured scope': scenario(
        '{% set v = "outer" %}' + _caller_macro + '{% call(a, k) m(1) %}{{ v }}{{ fail(a) }}{% endcall %}',
        lambda trace: {'fail': failing(trace)}),
    'autoescape and Markup output': scenario(
        _caller_macro + '{% call(a, k) m(value) %}{{ a }}{{ safe }}{% endcall %}',
        lambda trace: {'value': 'a"b', 'safe': Markup('&amp;s')}, autoescape=True),
})


# imports -----------------------------------------------------------------------

def _imports(statement: str, use: str) -> dict[str, Scenario]:
    """`statement` imports the library `lib`; `use` calls its `greet`."""
    library = '{% set private_count = 1 %}{% macro greet() %}{{ value|default("no context") }}{% endmacro %}' \
              '{% macro _hidden() %}H{% endmacro %}{% set exported = side() %}'

    def counted(trace: Trace) -> dict[str, Any]:
        return {'value': 'ctx'}

    def side(env: jinja2.Environment, trace: Trace) -> None:
        # a global: imported templates without context see it too
        namespace(env, 'globals').update(side=recorder(trace, 'side', 'S'))
    return {
        'default without-context semantics': scenario(statement + use, counted, templates={'lib': library}, setup=side),
        'with/without-context cache and side effects': scenario(
            statement + use + statement + use + (statement.replace(' with context %}', ' without context %}')
                                                 if 'with context' in statement
                                                 else statement.replace(' %}', ' with context %}')) + use,
            counted, templates={'lib': library}, setup=side),
        'private/missing exports and errors': lambda engine: {
            name: scenario(source, counted, templates={'lib': library}, setup=side)(engine)
            for name, source in (('private', "{% from 'lib' import _hidden %}{{ _hidden() }}"),
                                 ('missing_name', "{% from 'lib' import nothing %}{{ nothing is defined }}"),
                                 ('missing_template', "{% import 'absent' as x %}"),
                                 ('attribute', statement + '{{ ' + use.strip('{} ').split('(')[0].split('.')[0]
                                  + '._hidden is defined }}'))},
        'environment global mutations and macro lifetime': scenario(
            statement + '{% set held = ' + use.strip('{} ').split('(')[0] + ' %}' + use + '{{ mutate() }}' + use,
            counted, templates={'lib': library},
            setup=lambda env, trace: (side(env, trace), namespace(env, 'globals').update(
                value='global', mutate=lambda: namespace(env, 'globals').update(value='changed') or ''))),
    }


register('jinja.language.import', _imports("{% import 'lib' as lib %}", '{{ lib.greet() }}'))
register('jinja.language.from_import', _imports("{% from 'lib' import greet %}", '{{ greet() }}'))
register('jinja.language.import_context', _imports("{% import 'lib' as lib with context %}", '{{ lib.greet() }}'))


# inheritance -------------------------------------------------------------------

_bases = {'base': 'A{% block body %}B{% endblock %}{% block tail scoped %}{{ item }}{% endblock %}C',
          'middle': "{% extends 'base' %}{% block body %}M{{ super() }}{% endblock %}",
          'required': '{% block must required %}{% endblock %}R'}


def _inheritance(child_body: str) -> dict[str, Scenario]:
    return {
        'dynamic and multi-level inheritance': scenario(
            '{% extends layout %}{% block body %}' + child_body + '{% endblock %}', lambda trace: {'layout': 'middle'},
            templates=_bases),
        'scoped/required blocks and self references': lambda engine: {
            'scoped': scenario("{% extends 'base' %}{% block body %}" + child_body + '{{ self.tail() }}{% endblock %}',
                               lambda trace: {'item': 'I'}, templates=_bases)(engine),
            'required': scenario("{% extends 'required' %}", templates=_bases)(engine),
            'filled': scenario("{% extends 'required' %}{% block must %}F{% endblock %}", templates=_bases)(engine)},
        'multiple extends and duplicate block errors': lambda engine: {
            'twice': scenario("{% extends 'base' %}{% extends 'middle' %}", templates=_bases)(engine),
            'duplicate': scenario('{% block a %}{% endblock %}{% block a %}{% endblock %}', templates=_bases)(engine),
            'conditional': scenario("{% if flag %}{% extends 'base' %}{% endif %}X", lambda trace: {'flag': False},
                                    templates=_bases)(engine)},
        'super.super, recursive calls and loaders': scenario(
            "{% extends 'middle' %}{% block body %}" + child_body + '{% endblock %}',
            lambda trace: {}, templates=_bases),
    }


register('jinja.language.inheritance', _inheritance('child'))
register('jinja.language.super', _inheritance('[{{ super() }}|{{ super.super() if false else super() }}]'))


# do --------------------------------------------------------------------------

class OnlyLoops(jinja2.ext.ExprStmtExtension):
    tags = {'evaluate'}  # noqa: RUF012 - Jinja reads the class attribute


register('jinja.language.do', {
    'ignored return values and arbitrary mutations': scenario(
        '{% do items.append(4) %}{% do mapping.update(k=1) %}{% do f() %}{{ items }}{{ mapping }}',
        lambda trace: {'items': [1], 'mapping': {}, 'f': recorder(trace, 'f', 'ignored')},
        extensions=(jinja2.ext.do,)),
    'exception type/order and reentrant callbacks': scenario(
        '{% do first() %}{% do fail() %}{% do last() %}',
        lambda trace: {'first': recorder(trace, 'first'), 'fail': failing(trace), 'last': recorder(trace, 'last')},
        extensions=(jinja2.ext.do,)),
    'disabled/mutated extension errors': lambda engine: {
        'disabled': scenario('{% do x %}')(engine),
        'mutated': scenario('{% evaluate items.append(1) %}{{ items }}', lambda trace: {'items': []},
                            extensions=(OnlyLoops,))(engine)},
    'nested scopes and autoescape': scenario(
        '{% for i in [1, 2] %}{% do items.append(i ~ "<") %}{% endfor %}{% macro m() %}{% do items.append(9) %}'
        '{% endmacro %}{{ m() }}{{ items }}', lambda trace: {'items': []}, extensions=(jinja2.ext.do,),
        autoescape=True),
})


# namespaces ------------------------------------------------------------------

register('jinja.language.namespace_assignment', {
    'non-Namespace assignment errors': lambda engine: {
        name: scenario(source, lambda trace: {'d': {'a': 1}})(engine)
        for name, source in (('dict', '{% set d.a = 2 %}{{ d.a }}'), ('undefined', '{% set nothing.a = 1 %}'),
                             ('string', '{% set s = "x" %}{% set s.a = 1 %}'))},
    'attribute mutation identity and callback behavior': scenario(
        '{% set ns = namespace(items=[]) %}{% set ns.items = ns.items + [1] %}{% set ns.f = f %}{{ ns.f(ns.items) }}'
        '{{ ns.items }}', lambda trace: {'f': recorder(trace, 'f', 'F')}),
    'multiple assignment targets and nested namespaces': scenario(
        '{% set ns = namespace(a=1, inner=namespace(b=2)) %}{% set ns.a, x = 3, 4 %}{% for i in [1] %}'
        '{% set ns.inner = namespace(b=ns.inner.b + i) %}{% endfor %}{{ ns.a }}{{ x }}{{ ns.inner.b }}'),
    'undefined namespace values': scenario(
        '{% set ns = namespace() %}[{{ ns.missing }}]{% set ns.v = missing %}[{{ ns.v }}]{{ ns.v is defined }}'),
})


# includes ----------------------------------------------------------------------

@jinja2.pass_context
def _seen(context: Any, label: str) -> str:
    return label + ':' + str(context.get('value', 'none')) + ':' + str(context.get('local', 'none'))


def _candidates(name: str) -> str:
    """A template name as the include_candidates contract writes it."""
    return name if name.startswith('[') or name == 'names' else f"['absent', {name}]"


def _includes(statement: str, target: Callable[[str], str] = lambda name: name) -> dict[str, Scenario]:
    """`statement` includes `part` (with `{name}` replaced by `target` of the template name)."""
    parts = {'part': '{{ seen("p") }}{{ value|default("U") }}', 'nested': "{% include 'part' %}N",
             'empty': ''}

    def include(name: str) -> str:
        return statement.format(name=target(name))
    included = include("'part'")
    return {
        'callback context and environment-global isolation': scenario(
            '{% set local = "L" %}' + included + '{% for value in ["loop"] %}' + included + '{% endfor %}',
            lambda trace: {'value': 'V'}, templates=parts,
            setup=lambda env, trace: namespace(env, 'globals').update(seen=_seen)),
        'missing or empty candidate lists and exact exceptions': lambda engine: {
            name: scenario(include(name_), lambda trace: {'names': []}, templates=parts,
                           setup=lambda env, trace: namespace(env, 'globals').update(seen=_seen))(engine)
            for name, name_ in (('missing', "'absent'"), ('empty_list', '[]'), ('list', "['absent', 'other']"),
                                ('variable', 'names'))},
        'foreign Template objects and string subclasses': lambda engine: {
            'template': scenario(include('tpl'), lambda trace: {
                'tpl': jinja2.Environment().from_string('foreign {{ value|default("U") }}'), 'value': 'V'},
                templates=parts)(engine),
            'subclass': scenario(include('name'), lambda trace: {
                'name': type('Name', (str,), {})('part'), 'value': 'V'}, templates=parts,
                setup=lambda env, trace: namespace(env, 'globals').update(seen=_seen))(engine)},
    }


def _include_cache(statement: str, target: Callable[[str], str] = lambda name: name) -> dict[str, Scenario]:
    parts = {'part': '{{ seen("p") }}{{ value|default("U") }}', 'nested': "{% include 'part' %}N"}
    return {'loader cache, nested scopes and relative names': scenario(
        '{% for value in [1, 2] %}' + statement.format(name=target("'nested'")) + '{% endfor %}'
        + statement.format(name=target("'part'")), lambda trace: {'value': 'V'}, templates=parts,
        setup=lambda env, trace: namespace(env, 'globals').update(seen=_seen))}


register('jinja.language.include_context', _includes('{{% include {name} %}}'))
for _contract, _statement in (('jinja.language.include_without_context', '{{% include {name} without context %}}'),
                              ('jinja.language.include_ignore_missing', '{{% include {name} ignore missing %}}')):
    register(_contract, _includes(_statement) | _include_cache(_statement))
register('jinja.language.include_candidates', _includes('{{% include {name} %}}', _candidates)
         | _include_cache('{{% include {name} %}}', _candidates))


def _foreign(templates: dict[str, str]) -> jinja2.Environment:
    foreign = jinja2.Environment(loader=jinja2.DictLoader(templates))
    namespace(foreign, 'globals').update(seen=_seen)
    return foreign


class RecordingContext(jinja2.runtime.Context):
    pass


def _include_callbacks(statement: str) -> dict[str, Scenario]:
    parts = {'part': '{{ seen("inc") }}', 'macros': '{% macro m() %}{{ seen("macro") }}{% endmacro %}',
             'caller': "{% include 'part' %}"}

    def with_seen(env: jinja2.Environment, trace: Trace) -> None:
        namespace(env, 'globals').update(seen=_seen)
    return {
        'nested and imported macro callbacks': scenario(
            "{% set local = 'L' %}{% import 'macros' as lib %}{{ lib.m() }}" + statement.format(name="'caller'"),
            lambda trace: {'value': 'V'}, templates=parts, setup=with_seen),
        'custom Context types and mutation': scenario(
            "{% set local = 'L' %}" + statement.format(name="'part'"), lambda trace: {'value': 'V'},
            templates=parts, setup=lambda env, trace: (with_seen(env, trace),
                                                       setattr(env, 'context_class', RecordingContext))),
        'Template objects and string subclasses': scenario(
            statement.format(name='tpl'), lambda trace: {
                'tpl': _foreign(parts).get_template('part'), 'value': 'V'},
            templates=parts, setup=lambda env, trace: (
                with_seen(env, trace), namespace(env, 'globals').update(seen=_seen))),
        'loader cache and exception timing': scenario(
            "{% set local = 'L' %}" + statement.format(name="'part'") + '{{ fail() }}' + statement.format(name="'part'"),
            lambda trace: {'value': 'V', 'fail': failing(trace)}, templates=parts, setup=with_seen),
    }


register('jinja.language.include_callback_with', _include_callbacks('{{% include {name} with context %}}'))
register('jinja.language.include_callback_without', _include_callbacks('{{% include {name} without context %}}'))


# conditional expressions ---------------------------------------------------------

class Truthy:
    def __init__(self, trace: Trace, value: bool) -> None:
        self.trace, self.value = trace, value

    def __bool__(self) -> bool:
        self.trace.append(['bool', self.value])
        return self.value

    def __str__(self) -> str:
        return 'T' if self.value else 'F'


register('jinja.language.conditional_expression', {
    'short-circuit branch effects and exceptions': scenario(
        '{{ a() if flag else fail() }}{{ fail() if not flag else b() }}{{ fail() if missing else "x" }}',
        lambda trace: {'flag': True, 'a': recorder(trace, 'a', 'A'), 'b': recorder(trace, 'b', 'B'),
                       'fail': failing(trace)}),
    'expression without else and Undefined policies': lambda engine: {
        name: scenario('[{{ "x" if flag }}][{{ ("x" if flag)|default("d") }}]', lambda trace: {'flag': False},
                       undefined=undefined)(engine)
        for name, undefined in (('default', jinja2.Undefined), ('strict', jinja2.StrictUndefined),
                                ('chainable', jinja2.ChainableUndefined))},
    'custom truthiness and return identity': scenario(
        '{{ t if t else "no" }}{{ f if f else "no" }}{{ (t if t else f) is sameas t }}',
        lambda trace: {'t': Truthy(trace, True), 'f': Truthy(trace, False)}),
    'autoescape, filters and nested conditionals': scenario(
        '{{ (a if flag else b)|upper }}{{ a if flag else (b if other else safe) }}{{ "<" if flag }}',
        lambda trace: {'flag': True, 'other': False, 'a': 'a"', 'b': 'b', 'safe': Markup('&amp;')}, autoescape=True),
})


# Python operators ------------------------------------------------------------------

class Number:
    def __init__(self, trace: Trace, value: int) -> None:
        self.trace, self.value = trace, value

    def __add__(self, other: Any) -> Any:
        self.trace.append(['add', self.value])
        if isinstance(other, ReflectedNumber) and not isinstance(self, ReflectedNumber):
            return NotImplemented
        return self.value + (other.value if isinstance(other, Number) else int(other))

    def __radd__(self, other: Any) -> Any:
        self.trace.append(['radd', self.value])
        return int(other) + self.value

    def __str__(self) -> str:
        return 'N' + str(self.value)


class ReflectedNumber(Number):
    def __radd__(self, other: Any) -> Any:
        self.trace.append(['reflected', self.value])
        return 'R'


class Growing(list):
    def __init__(self, trace: Trace) -> None:
        super().__init__([1, 2])
        self.trace = trace

    def __contains__(self, item: object) -> bool:
        self.trace.append(['contains', item])
        self.append(len(self) + 1)
        return super().__contains__(item)


register('jinja.language.python_operators', {
    'all_numeric_and_container_types': scenario(
        '{{ 7 // 2 }} {{ -7 // 2 }} {{ 7 % -3 }} {{ 2 ** 100 }} {{ 1 / 4 }} {{ d * 3 }} {{ f + 1 }} {{ [1] + [2] }} '
        '{{ (1, 2) * 2 }} {{ "ab" * 2 }} {{ s[1:3] }} {{ s[::-1] }} {{ 2 in r }} {{ "k" in m }} {{ 1 < 2 < 3 }} '
        '{{ fr }} {{ b | list }}', lambda trace: {
            'd': __import__('decimal').Decimal('1.1'), 'f': 0.1, 's': 'hello', 'r': range(3), 'm': {'k': 1},
            'fr': __import__('fractions').Fraction(1, 3) + 1, 'b': b'ab'}),
    'reflected_subclass_precedence': scenario(
        '{{ n + r }} {{ 1 + n }} {{ n + 1 }} {{ r + n }}',
        lambda trace: {'n': Number(trace, 1), 'r': ReflectedNumber(trace, 2)}),
    'mutation_and_reentrancy': scenario(
        '{{ 1 in g }} {{ 3 in g }} {{ g|length }} {{ items + items }}{% set items = items + [9] %}{{ items }}',
        lambda trace: {'g': Growing(trace), 'items': [1]}),
})


# Filtered-loop iteration ----------------------------------------------------------

def _filtered(body: str, else_branch: str = '') -> dict[str, Scenario]:
    loop = '{% for x in items if p(x) %}' + body + else_branch + '{% endfor %}'
    extensions = (jinja2.ext.loopcontrols,)

    def scenario(source: str, context: Callable[[Trace], dict[str, Any]] | None = None) -> Scenario:
        return _scenario(source, context, extensions=extensions)

    def traced(trace: Trace, fail: int | None = None, pfail: Any = None) -> dict[str, Any]:
        return {'items': generator(trace, [1, 2, 3, 4, 5], fail=fail), 'p': predicate(trace, fail=pfail),
                'mark': recorder(trace, 'body')}
    return {
        'loop metadata and lookahead': scenario(
            loop.replace(body, body + '{{ loop.last }}{{ loop.nextitem }}{{ loop.length }}'), traced),
        'else branch with observable predicate effects': scenario(
            '{% for x in items if p(x) %}{{ x }}{% else %}E{{ mark() }}{% endfor %}',
            lambda trace: {'items': generator(trace, [2, 4]), 'p': predicate(trace), 'mark': recorder(trace, 'else')}),
        'recursive filtered loops': scenario(
            '{% for node in tree if node.name != "c" recursive %}{{ node.name }}{{ loop.depth }}'
            '{% if node.children %}[{{ loop(node.children) }}]{% endif %}{% endfor %}', lambda trace: {'tree': TREE}),
        'destructuring targets': scenario(
            '{% for a, b in pairs if p(a) %}{{ a }}{{ b }}{{ loop.length }};{% endfor %}',
            lambda trace: {'pairs': generator(trace, [(1, 'a'), (2, 'b'), (3, 'c')]), 'p': predicate(trace)}),
        'source containing loop text': scenario(
            '{% for x in items if p(x) %}{{ "{% for %}" }}{{ "loop.index" }}{{ x }}{% endfor %}',
            lambda trace: {'items': [1, 2, 3], 'p': predicate(trace)}),
        'predicate exceptions and nested interactions': lambda engine: {
            'predicate': scenario(loop, lambda trace: traced(trace, pfail=3))(engine),
            'iterator': scenario(loop, lambda trace: traced(trace, fail=3))(engine),
            'nested': scenario('{% for y in [1, 2] %}' + loop + ';{% endfor %}',
                               lambda trace: {'items': [1, 2, 3], 'p': predicate(trace), 'mark': recorder(trace, 'b')})(
                engine)},
    }


def _destructure(engine: Any, values: list[Any]) -> dict[str, Any]:
    return scenario('{% for a, b in values if a %}{{ a }}{{ b }}{% endfor %}', lambda trace: {'values': values})(engine)


class Opaque:
    pass


class LengthOnly:
    def __len__(self) -> int:
        return 2


register('jinja.iteration.filter.lazy_order', _filtered('{{ x }}{{ mark(x) }}') | {
    'destructure_noniterable_other_types': lambda engine: {
        name: _destructure(engine, [value]) for name, value in (('int', 5), ('none', None), ('float', 1.5))},
    'destructure_other_opaque_protocols': lambda engine: {
        name: _destructure(engine, [value]) for name, value in (('opaque', Opaque()), ('length', LengthOnly()),
                                                                ('string', 'abc'))}})
register('jinja.iteration.filter.break_consumption', _filtered(
    '{{ x }}{{ mark(x) }}{% if x >= 3 %}{% break %}{% endif %}'))
register('jinja.iteration.filter.generator_error', _filtered('{{ x }}{{ mark(x) }}'))
def _lookahead(body: str, else_branch: str = '') -> dict[str, Scenario]:
    source = '{% for x in items if p(x) %}' + body + else_branch + '{% endfor %}'
    return {
        'independent iterator and predicate effect trace': scenario(
            source, lambda trace: {'items': generator(trace, [1, 2, 3, 4, 5, 7]), 'p': predicate(trace),
                                   'mark': recorder(trace, 'body')}),
        'native filtered loop behavior': scenario(
            source + '|' + source.replace(' if p(x)', ' if x > 2'), lambda trace: {
                'items': [1, 2, 3, 4, 5], 'p': predicate(trace), 'mark': recorder(trace, 'body')}),
        'exceptions and nested interactions': lambda engine: {
            'predicate': scenario(source, lambda trace: {'items': generator(trace, [1, 2, 3, 5]),
                                                         'p': predicate(trace, fail=3),
                                                         'mark': recorder(trace, 'body')})(engine),
            'iterator': scenario(source, lambda trace: {'items': generator(trace, [1, 3, 5, 7], fail=2),
                                                        'p': predicate(trace), 'mark': recorder(trace, 'body')})(engine),
            'nested': scenario('{% for y in [1, 2] %}' + source.replace('items', 'rows[y]') + ';{% endfor %}',
                               lambda trace: {'rows': {1: [1, 3], 2: [2, 5]}, 'p': predicate(trace),
                                              'mark': recorder(trace, 'body')})(engine)},
    }


register('jinja.iteration.filter.last_lookahead', _lookahead('{{ x }}{{ mark(x) }}{{ "L" if loop.last }}'))
register('jinja.iteration.filter.nextitem_lookahead', _lookahead('{{ x }}{{ mark(x) }}>{{ loop.nextitem }}'))
register('jinja.iteration.filter.length_consumption', _lookahead('{{ mark(x) }}{{ x }}/{{ loop.length }}'))
register('jinja.iteration.filter.empty_else', _lookahead('{{ x }}{{ mark(x) }}', '{% else %}E'))


class Reentrant:
    """An iterable whose iterator renders a nested template when advanced."""
    def __init__(self, trace: Trace, nested: Callable[[], str]) -> None:
        self.trace, self.nested = trace, nested

    def __iter__(self) -> Iterator[str]:
        for value in ('a', 'b'):
            self.trace.append(['nested', self.nested()])
            yield value


def _growing_items() -> dict[str, Any]:
    items: list[Any] = [{}, {}]

    def grow() -> str:
        if len(items) < 4:
            items.append({})
        return ''
    return {'items': items, 'grow': grow}


register('jinja.iteration.lookahead', {
    'reentrant_iterators': scenario(
        '{% for x in items %}{{ x }}{{ loop.nextitem }}{{ loop.last }};{% endfor %}',
        lambda trace: {'items': Reentrant(trace, lambda: jinja2.Template('{{ 1 + 1 }}').render())}),
    'mutation_and_identity': scenario(
        '{% for x in items %}{{ x }}{{ loop.nextitem is sameas items[loop.index] if not loop.last }}'
        '{{ grow() }}{{ loop.length }};{% endfor %}',
        lambda trace: _growing_items()),
    'nested_loops_and_control_flow': scenario(
        '{% for x in outer %}{% for y in inner(x) %}{{ x }}{{ y }}{{ loop.revindex }}{{ loop.previtem }}'
        '{% if y > 1 %}{% break %}{% endif %}{% endfor %}{{ loop.nextitem }};{% endfor %}',
        lambda trace: {'outer': generator(trace, [1, 2], name='o'),
                       'inner': lambda x: generator(trace, [x, x + 1, x + 2], name='i')},
        extensions=(jinja2.ext.loopcontrols,)),
    'error_context_and_cleanup': scenario(
        '{% for x in items %}{{ x }}{{ loop.length }}{% endfor %}',
        lambda trace: {'items': generator(trace, [1, 2, 3], fail=2)}),
})


# Sandbox calls ---------------------------------------------------------------------

class Described:
    def __init__(self, trace: Trace) -> None:
        self.trace = trace

    @property
    def prop(self) -> str:
        self.trace.append('prop')
        return 'P'

    def method(self, *args: Any, **kwargs: Any) -> str:
        self.trace.append(['method', list(args), sorted(kwargs)])
        return 'M'

    def __bool__(self) -> bool:
        self.trace.append('bool')
        return False


class Policy(jinja2.sandbox.SandboxedEnvironment):
    def is_safe_callable(self, obj: Any) -> bool:
        return getattr(obj, '__name__', '') != 'method'

    def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
        return attr != 'secret' and super().is_safe_attribute(obj, attr, value)


def _policy(source: str, context: Callable[[Trace], dict[str, Any]]) -> Scenario:
    def run(engine: Any) -> dict[str, Any]:
        trace: Trace = []
        return render(engine, source, context(trace), environment=Policy()) | {'trace': trace}
    return run


register('jinja.sandbox.call', {
    'descriptor_and_truth_protocols': scenario(
        '{{ d.prop }}{{ "T" if d else "F" }}{{ d.method(1, k=2) }}{{ d.__class__ }}{{ d.mro is defined }}',
        lambda trace: {'d': Described(trace)}, sandbox=True),
    'overrides_code_and_global_mutation': lambda engine: {
        'policy': _policy('{{ d.prop }}{{ d.method() }}', lambda trace: {'d': Described(trace)})(engine),
        'secret': _policy('{{ obj.secret }}', lambda trace: {'obj': type('Holder', (), {'secret': 's'})()})(engine),
        'code': scenario('{{ f.__code__ }}{{ f.__globals__ }}', lambda trace: {'f': recorder(trace, 'f')},
                         sandbox=True)(engine)},
    'exception_identity_and_tracebacks': scenario(
        '{{ ok() }}{{ fail(1) }}', lambda trace: {'ok': recorder(trace, 'ok', 'O'), 'fail': failing(trace)},
        sandbox=True),
    'argument_context_and_reentrancy_interactions': scenario(
        '{{ c("arg") }}{{ f(*args, **kwargs) }}{{ nested() }}',
        lambda trace: {'c': jinja2.pass_context(lambda context, v: v + str(context.get('args'))),
                       'f': recorder(trace, 'f', 'F'), 'args': [1, 2], 'kwargs': {'k': 3},
                       'nested': lambda: jinja2.sandbox.SandboxedEnvironment().from_string('{{ 2 * 3 }}').render()},
        sandbox=True),
})


# Attribute and item lookup -----------------------------------------------------------

class ItemAndAttribute(dict):
    attr = 'class-attribute'

    @property
    def described(self) -> str:
        return 'descriptor'


class Faulty:
    def __getattr__(self, name: str) -> Any:
        raise ProbeError('attribute ' + name)

    def __getitem__(self, key: Any) -> Any:
        raise KeyError(key)


class LookupEnvironment(jinja2.Environment):
    def getattr(self, obj: Any, attribute: str) -> Any:
        return 'env-attr:' + attribute

    def getitem(self, obj: Any, argument: Any) -> Any:
        return 'env-item:' + str(argument)


def _nested_mapping() -> dict[str, Any]:
    value: dict[str, Any] = {'a': {'b': [{'c': 'first'}]}}

    def grow() -> str:
        value['a']['b'].append({'c': 'second'})
        return ''
    return {'v': value, 'grow': grow}


register('jinja.lookup.priority', {
    'subclasses_and_descriptors': scenario(
        '{{ v.attr }}|{{ v["attr"] }}|{{ v.described }}|{{ v.items }}|{{ v["items"] }}|{{ v.keys is defined }}',
        lambda trace: {'v': ItemAndAttribute(attr='item', items='item-items')}),
    'mapping_errors_and_identity': scenario(
        '[{{ f.anything }}]', lambda trace: {'f': Faulty()}),
    'environment_overrides': lambda engine: {
        'custom': render(engine, '{{ v.a }}|{{ v["b"] }}', {'v': {'a': 1}}, environment=LookupEnvironment()),
        'sandbox': render(engine, '{{ v.a }}|{{ v["b"] }}', {'v': {'a': 1, 'b': 2}},
                          environment=jinja2.sandbox.SandboxedEnvironment())},
    'undefined_and_sandbox': scenario(
        '[{{ v.missing }}][{{ v["missing"] }}][{{ v.__class__ }}]', lambda trace: {'v': {'a': 1}}, sandbox=True),
    'nested_lookup_and_mutation': scenario(
        '{{ v.a.b[0].c }}{{ grow() }}{{ v.a.b[1].c }}',
        lambda trace: _nested_mapping()),
})


# Callbacks ----------------------------------------------------------------------------

def _cyclic_containers() -> dict[str, Any]:
    cycle: dict[str, Any] = {}
    cycle['self'] = cycle
    return {'items': [1], 'cycle': cycle, 'same': lambda a, b: a is b, 'keep': lambda c: c['self'] is c}


register('jinja.callback.identity', {
    'mutable_containers_and_cycles': scenario(
        '{{ same(items, items) }}{{ items|length }}{{ cycle is mapping }}{{ keep(cycle) }}',
        lambda trace: _cyclic_containers()),
    'async_and_nested_calls': scenario(
        '{{ outer(inner) }}{{ identity(value) is sameas value }}',
        lambda trace: {'outer': lambda f: f(trace), 'inner': lambda t: len(t), 'identity': lambda v: v,
                       'value': object()}, enable_async=True),
    'mutation_and_lifetime': scenario(
        '{{ add(items) }}{{ items }}{{ add(items) }}{{ items|length }}',
        lambda trace: {'items': [], 'add': lambda items: items.append(len(items)) or ''}),
})


def _nested_failure(trace: Trace) -> dict[str, Any]:
    raised = ProbeError('inner render')

    def inner() -> str:
        try:
            jinja2.Environment().from_string('{{ fail() }}').render(fail=lambda: (_ for _ in ()).throw(raised))
        except ProbeError as exception:
            trace.append(['inner', exception is raised])
            raise
        return ''
    return {'inner': inner}


register('jinja.callback.exception', {'nested_rendering': scenario('{{ inner() }}', _nested_failure)})


# Constant arguments ---------------------------------------------------------------------

def _constant_identity() -> dict[str, Any]:
    seen: list[Any] = []

    def record(value: Any) -> str:
        seen.append(value)
        return str(len(seen)) + str(seen[0] is value)
    return {'f': record}


register('jinja.compile.constant_argument', {
    'constant_identity_and_reuse': scenario(
        '{{ f([1, 2]) }}{{ f([1, 2]) }}{{ f("s") }}{{ f((1,)) }}',
        lambda trace: _constant_identity()),
    'nested_filters_and_tests': scenario(
        '{{ "a"|f|g }}{{ ("x" is t)|string }}{{ f(g("b")) }}',
        lambda trace: {}, setup=lambda env, trace: (
            namespace(env, 'filters').update(f=recorder(trace, 'f', 'F'), g=recorder(trace, 'g', 'G')),
            namespace(env, 'globals').update(f=recorder(trace, 'gf', 'GF'), g=recorder(trace, 'gg', 'GG')),
            namespace(env, 'tests').update(t=lambda v: trace.append(['t', v]) or True))),
    'control_flow_and_extensions': scenario(
        '{% for i in [1, 2] %}{{ f("c") }}{% endfor %}{% if f("cond") %}Y{% endif %}{% do f("do") %}',
        lambda trace: {}, extensions=(jinja2.ext.do,),
        setup=lambda env, trace: namespace(env, 'globals').update(f=recorder(trace, 'f', 'F'))),
})


# Extension-generated AST -------------------------------------------------------------------

class Shout(jinja2.ext.Extension):
    tags = {'shout'}  # noqa: RUF012 - Jinja reads the class attribute

    def parse(self, parser: Any) -> Any:
        lineno = next(parser.stream).lineno
        value = parser.parse_expression()
        return jinja2.nodes.Output([jinja2.nodes.Filter(value, 'upper', [], [], None, None)], lineno=lineno)


class Broken(jinja2.ext.Extension):
    tags = {'broken'}  # noqa: RUF012 - Jinja reads the class attribute

    def parse(self, parser: Any) -> Any:
        token = next(parser.stream)
        parser.fail('broken tag here', token.lineno)


register('jinja.extensions.ast', {
    'registration_and_extension_identity': scenario(
        '{% shout value %}{{ ext }}', lambda trace: {'value': 'quiet'}, extensions=(Shout,),
        setup=lambda env, trace: namespace(env, 'globals').update(ext=sorted(env.extensions))),
    'token_locations_and_syntax_errors': lambda engine: {
        'broken': scenario('x\n{% broken %}', extensions=(Broken,))(engine),
        'unknown': scenario('{% shout %}', extensions=(Shout,))(engine)},
    'async_and_sandbox_interactions': lambda engine: {
        'async': scenario('{% shout value %}', lambda trace: {'value': 'a'}, extensions=(Shout,),
                          enable_async=True)(engine),
        'sandbox': scenario('{% shout value.__class__ %}', lambda trace: {'value': 'a'}, extensions=(Shout,),
                            sandbox=True)(engine)},
})


# Async callbacks ------------------------------------------------------------------------------

def _async_context(trace: Trace) -> dict[str, Any]:
    import contextvars
    variable: contextvars.ContextVar[str] = contextvars.ContextVar('probe', default='unset')

    async def set_value() -> str:
        variable.set('set')
        trace.append(['set', variable.get()])
        return 'S'

    async def get_value() -> str:
        trace.append(['get', variable.get()])
        return variable.get()
    return {'set_value': set_value, 'get_value': get_value}


async def _agen(trace: Trace) -> Any:
    try:
        for value in (1, 2, 3):
            trace.append(['yield', value])
            yield value
    finally:
        trace.append('closed')


def _nested_async_render(engine: Any) -> dict[str, Any]:
    trace: Trace = []

    def nested() -> str:
        trace.append(jinja2.Environment().from_string('{{ 4 }}').render())
        return 'N'
    return {'result': render(engine, '{{ nested() }}', {'nested': nested},
                             environment=environment(enable_async=True)), 'trace': trace}


register('jinja.async.callback', {
    'contextvars_and_task_lifecycle': scenario('{{ set_value() }}{{ get_value() }}', _async_context,
                                               enable_async=True),
    'loop_cleanup_and_cancellation': scenario(
        '{% for x in agen %}{{ x }}{% if x == 2 %}{% break %}{% endif %}{% endfor %}',
        lambda trace: {'agen': _agen(trace)}, enable_async=True, extensions=(jinja2.ext.loopcontrols,)),
    'async_generators_and_filters': scenario(
        '{{ agen|list }}{{ agen2|map("string")|join(",") }}',
        lambda trace: {'agen': _agen(trace), 'agen2': _agen(trace)}, enable_async=True),
    'extension_and_sandbox_interactions': scenario(
        '{% shout value %}{{ call() }}', lambda trace: {'value': 'v', 'call': recorder(trace, 'call', 'C')},
        enable_async=True, sandbox=True, extensions=(Shout,)),
    'nested_renders_and_part_boundaries': lambda engine: _nested_async_render(engine),
})


# Strict undefined ----------------------------------------------------------------------------

class LoggedUndefined(jinja2.StrictUndefined):
    pass


register('jinja.undefined.strict', {
    'custom_undefined_subclasses': scenario('{{ missing }}', undefined=LoggedUndefined),
    'attribute_item_and_iteration': lambda engine: {
        name: scenario(source, lambda trace: {'v': {}}, undefined=jinja2.StrictUndefined)(engine)
        for name, source in (('attribute', '{{ v.missing }}'), ('item', '{{ v["missing"] }}'),
                             ('iteration', '{% for x in missing %}{% endfor %}'), ('defined', '{{ missing is defined }}'),
                             ('default', '{{ missing|default("d") }}'), ('truth', '{% if missing %}{% endif %}'))},
    'exception_identity_and_traceback': scenario('{{ ok() }}{{ missing.attr }}',
                                                 lambda trace: {'ok': recorder(trace, 'ok', 'O')},
                                                 undefined=jinja2.StrictUndefined),
    'filters_tests_and_autoescape': lambda engine: {
        name: scenario(source, undefined=jinja2.StrictUndefined, autoescape=True)(engine)
        for name, source in (('upper', '{{ missing|upper }}'), ('length', '{{ missing|length }}'),
                             ('none', '{{ missing is none }}'), ('escape', '{{ missing|e }}'))},
    'async_and_sandbox': lambda engine: {
        'async': scenario('{{ missing }}', undefined=jinja2.StrictUndefined, enable_async=True)(engine),
        'sandbox': scenario('{{ missing }}', undefined=jinja2.StrictUndefined, sandbox=True)(engine)},
})


# Finalize --------------------------------------------------------------------------------------

def _finalize(trace: Trace) -> Callable[[Any], Any]:
    def finalize(value: Any) -> Any:
        trace.append(['finalize', str(value)])
        return '' if value is None else value
    return finalize


@jinja2.pass_context
def _context_finalize(context: Any, value: Any) -> Any:
    return str(value) + '@' + str(context.get('tag'))


@jinja2.pass_eval_context
def _eval_finalize(eval_context: Any, value: Any) -> Any:
    return str(value) + ('@auto' if eval_context.autoescape else '@plain')


def _failing_finalize(value: Any) -> Any:
    if value == 'bad':
        raise ProbeError('finalize fails')
    return value


def _finalize_escape(engine: Any) -> dict[str, Any]:
    trace: Trace = []
    return render(engine, '{{ "const" }}{{ v }}{{ none }}{{ safe }}', {'v': '<x>', 'none': None, 'safe': Markup('&amp;')},
                  environment=environment(finalize=_finalize(trace)), autoescape=True) | {'trace': trace}


def _finalize_nested(engine: Any) -> dict[str, Any]:
    trace: Trace = []

    def nested() -> str:
        return jinja2.Environment(finalize=_finalize(trace)).from_string('{{ 1 }}').render()
    return render(engine, '{{ v }}{{ nested() }}', {'v': None, 'nested': nested},
                  environment=environment(finalize=_finalize(trace))) | {'trace': trace}


def _finalize_async_sandbox(engine: Any) -> dict[str, Any]:
    trace: Trace = []
    return {'async': render(engine, '{{ v }}', {'v': None},
                            environment=environment(finalize=_finalize(trace), enable_async=True)),
            'sandbox': render(engine, '{{ v }}', {'v': 2},
                              environment=environment(finalize=_finalize(trace), sandbox=True)),
            'trace': trace}


register('jinja.finalize.capture', {
    'context_environment_and_eval_context': lambda engine: {
        'context': render(engine, '{{ v }}', {'v': 1, 'tag': 'T'},
                          environment=environment(finalize=_context_finalize)),
        'eval': render(engine, '{{ v }}', {'v': 1}, environment=environment(finalize=_eval_finalize),
                       autoescape=True)},
    'exceptions_and_identity': lambda engine: render(engine, '{{ "ok" }}{{ v }}', {'v': 'bad'},
                                                     environment=environment(finalize=_failing_finalize)),
    'compile_time_and_dynamic_escape': lambda engine: _finalize_escape(engine),
    'parts_and_nested_rendering': lambda engine: _finalize_nested(engine),
    'async_and_sandbox': lambda engine: _finalize_async_sandbox(engine),
})


# Probes -----------------------------------------------------------------------------------------

def probe_name(contract: str, name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    return 'language_partition_' + contract.replace('.', '_') + '_' + slug


def _execution(scenario_: Scenario) -> Callable[[Any], Result]:
    def execute(engine: Any) -> Result:
        try:
            observation = scenario_(engine)
        except Exception as exception:  # noqa: BLE001 - the exception is the observation
            observation = {'scenario_error': error(exception)}
        return Result(json.loads(json.dumps(observation, default=str)), {})
    return execute


EXECUTIONS = {probe_name(contract, name): _execution(scenario_)
              for contract, partitions in SCENARIOS.items() for name, scenario_ in partitions.items()}
EXPECTED_PATH = Path(__file__).with_name('language_partition_expected.json')
EXPECTED: dict[str, Any] = json.loads(EXPECTED_PATH.read_text()) if EXPECTED_PATH.exists() else {}
PROBES: dict[str, Probe] = {name: Probe(execute, EXPECTED[name], True)
                            for name, execute in EXECUTIONS.items() if name in EXPECTED}
