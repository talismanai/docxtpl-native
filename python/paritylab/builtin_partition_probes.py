"""Differential probes for the generic partitions of Jinja's builtins.

Each builtin filter, test and global of the catalog has the same partitions:
empty and invalid input, argument variants, undefined input, exceptions
raised by the input's protocols, the order of protocol calls on the input,
and interactions with autoescape and other filters. One probe per builtin
and partition renders the scenarios in a document and records the output or
the exception, with the trace of protocol calls.

The expected observations in ``builtin_partition_expected.json`` were
recorded from the reference (docxtpl 0.20.2 with Jinja2 3.1.6) by
``scripts/record_reference_observations.py``: they pin the reference's current
behaviour; the differential run compares the native engine with it.
"""
from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jinja2
from docx import Document
from markupsafe import Markup, escape

from .builtin_expansion_probes import FILTERS as BASE_FILTERS
from .protocol_base import Probe, Result, generate

FILTERS: dict[str, tuple[str, Any]] = {name: (expression, value) for name, (expression, value, _) in BASE_FILTERS.items()}
FILTERS.update({
    'first': ('value|first', ['a', 'b']), 'last': ('value|last', ['a', 'b']),
    'join': ("value|join(',')", ['a', 'b']), 'length': ('value|length', [1, 2]),
    'lower': ('value|lower', 'AbC'), 'upper': ('value|upper', 'AbC'),
    'replace': ("value|replace('a','b')", 'aXa'), 'sum': ('value|sum', [1, 2]),
})
TESTS: dict[str, tuple[Any, Any]] = {
    'odd': (3, None), 'even': (2, None), 'divisibleby': (6, 3), 'defined': ('X', None), 'undefined': ('X', None),
    'none': (None, None), 'boolean': (True, None), 'false': (False, None), 'true': (True, None),
    'integer': (1, None), 'float': (1.5, None), 'lower': ('abc', None), 'upper': ('ABC', None),
    'string': ('X', None), 'mapping': ({}, None), 'number': (1, None), 'sequence': ([], None),
    'iterable': ([], None), 'callable': (str, None), 'in': ('a', ['a', 'b']), 'sameas': ('X', 'X'),
    'escaped': ('X', None), 'filter': ('upper', None), 'test': ('odd', None),
    'eq': (2, 2), 'equalto': (2, 2), 'ne': (3, 2), 'gt': (3, 2), 'greaterthan': (3, 2), 'ge': (2, 2),
    'lt': (1, 2), 'lessthan': (1, 2), 'le': (2, 2),
    '==': (2, 2), '!=': (3, 2), '>': (3, 2), '>=': (2, 2), '<': (1, 2), '<=': (2, 2),
}
GLOBALS: dict[str, str] = {
    'range': "{{ range(value)|join(',') }}", 'dict': '{{ dict(a=value).a }}',
    'lipsum': '{{ lipsum(value, false)|length > 0 }}', 'cycler': '{% set c = cycler(value) %}{{ c.next() }}',
    'joiner': '{% set j = joiner(value) %}{{ j() }}A{{ j() }}B', 'namespace': '{% set n = namespace(x=value) %}{{ n.x }}',
}
GLOBAL_VALUES: dict[str, Any] = {'range': 3, 'dict': 'X', 'lipsum': 1, 'cycler': 'A', 'joiner': '|', 'namespace': 'X'}

PROTOCOL_METHODS = ('__str__', '__repr__', '__iter__', '__len__', '__getitem__', '__contains__', '__lt__',
                    '__gt__', '__abs__', '__round__', '__float__', '__int__', '__index__', '__format__',
                    'keys', 'items', 'values', 'upper', 'lower', 'strip', 'title', 'capitalize', 'replace',
                    'split', 'center')


def _logged(value: Any, trace: list[str]) -> Any:
    """`value` as a subclass of its type that records protocol calls."""
    base = type(value)
    if base in (bool, type(None)) or not isinstance(value, (str, int, float, list, dict)):
        return value

    def wrap(name: str) -> Callable[..., Any]:
        original = getattr(base, name)

        def method(self: Any, *args: Any, **kwargs: Any) -> Any:
            trace.append(name)
            return original(self, *args, **kwargs)
        return method
    members = {name: wrap(name) for name in PROTOCOL_METHODS if hasattr(base, name)}
    return type('Logged' + base.__name__.capitalize(), (base,), members)(value)


class Failing:
    """An input whose protocols fail."""
    def _fail(self, *args: Any, **kwargs: Any) -> Any:
        raise ValueError('probe input failure')
    __str__ = __iter__ = __len__ = __getitem__ = __abs__ = __float__ = __int__ = __index__ = __round__ = _fail
    __lt__ = __gt__ = __format__ = _fail


class Opaque:
    """An input with no protocols beyond object's."""
    __slots__ = ()

    def __repr__(self) -> str:
        return '<opaque>'


def _empty(value: Any) -> Any:
    if isinstance(value, bool):
        return False
    return type(value)() if isinstance(value, (str, int, float, list, dict)) else None


Scenario = tuple[str, Callable[[list[str]], dict[str, Any]], Callable[[], jinja2.Environment | None]]


def _outcome(engine: Any, scenario: Scenario) -> dict[str, Any]:
    source, context, environment = scenario
    trace: list[str] = []
    try:
        blob = generate(engine, source, context(trace), environment())
    except Exception as error:  # noqa: BLE001 - the exception is the observation
        message = re.sub(r' at 0x[0-9a-fA-F]+', '', str(error))
        return {'type': type(error).__module__ + '.' + type(error).__qualname__, 'message': message, 'trace': trace}
    return {'text': Document(io.BytesIO(blob)).paragraphs[0].text, 'trace': trace}


def _probe(name: str, scenarios: list[Scenario]) -> Callable[[Any], Result]:
    def execute(engine: Any) -> Result:
        return Result({'scenarios': [_outcome(engine, scenario) for scenario in scenarios]}, {})
    execute.__name__ = name
    return execute


def _default() -> None:
    return None


def _strict() -> jinja2.Environment:
    return jinja2.Environment(undefined=jinja2.StrictUndefined)


def _autoescape() -> jinja2.Environment:
    return jinja2.Environment(autoescape=True)


def _value(value: Any) -> Callable[[list[str]], dict[str, Any]]:
    return lambda trace: {'value': value}


def _logged_value(value: Any) -> Callable[[list[str]], dict[str, Any]]:
    return lambda trace: {'value': _logged(value, trace)}


def _with_keyword(expression: str, name: str) -> str:
    """`expression` with an unknown keyword argument given to filter `name`."""
    head = 'value|' + name
    rest = expression[len(head):]
    if not rest.startswith('('):
        return head + '(bogus=1)' + rest
    depth = 0
    for index, character in enumerate(rest):
        depth += {'(': 1, ')': -1}.get(character, 0)
        if depth == 0:
            return head + rest[:index] + ', bogus=1' + rest[index:]
    raise ValueError(expression)


def filter_scenarios(name: str) -> dict[str, list[Scenario]]:
    expression, value = FILTERS[name]
    source = '{{ ' + expression + ' }}'
    keyword = _with_keyword(expression, name)
    # Escaped first: raw `<` in a document part is XML recovery, where the
    # package keeps text on purpose (a fixed docxtpl bug, not this partition).
    markup = Markup(escape(value)) if isinstance(value, str) else value
    return {
        'empty': [(source, _value(_empty(value)), _default)],
        'invalid_type': [(source, _value(Opaque()), _default)],
        'argument_variants': [('{{ ' + keyword + ' }}', _value(value), _default)],
        'undefined': [(source, lambda trace: {}, _default), (source, lambda trace: {}, _strict)],
        'exceptions': [(source, _value(Failing()), _default)],
        'mutation_order': [(source, _logged_value(value), _default)],
        'interactions': [(source, _value(markup), _autoescape),
                         ('{{ (' + expression + ')|string|length }}', _value(value), _default)],
    }


def _test_source(name: str, argument: Any, call: str = 'value') -> str:
    if not name.isidentifier():
        return '{{ check(' + call + ') }}'
    return '{{ ' + call + ' is ' + name + ('(arg)' if argument is not None else '') + ' }}'


def _test_context(name: str, argument: Any, value: Callable[[list[str]], Any]) -> Callable[[list[str]], dict[str, Any]]:
    def context(trace: list[str]) -> dict[str, Any]:
        values = {'value': value(trace), 'arg': argument}
        if not name.isidentifier():
            # Symbolic aliases are registry keys: called through call_test.
            env = jinja2.Environment()
            values['check'] = lambda item: env.call_test(name, item, [argument])
        return values
    return context


def test_scenarios(name: str) -> dict[str, list[Scenario]]:
    sample, argument = TESTS[name]
    source = _test_source(name, argument)

    def scenario(value: Callable[[list[str]], Any], environment: Callable[[], Any] = _default,
                 text: str = source) -> Scenario:
        return (text, _test_context(name, argument, value), environment)
    variants: list[Scenario]
    if name.isidentifier():
        extra = '{{ value is ' + name + ('(arg, arg)' if argument is not None else '(arg)') + ' }}'
        variants = [scenario(lambda trace: sample, text=extra)]
    else:
        variants = [scenario(lambda trace: sample, text='{{ check(value) }}{{ check(value) }}')]
    return {
        'argument_variants': variants,
        'undefined': [(source.replace('value', 'missing', 1) if name.isidentifier() else '{{ check(missing) }}',
                       _test_context(name, argument, lambda trace: sample), _default),
                      (source.replace('value', 'missing', 1) if name.isidentifier() else '{{ check(missing) }}',
                       _test_context(name, argument, lambda trace: sample), _strict)],
        'exceptions': [scenario(lambda trace: Failing())],
        'mutation_order': [scenario(lambda trace: _logged(sample, trace))],
        'interactions': [scenario(lambda trace: sample, _autoescape,
                                  '{% if ' + source[3:-3] + ' %}yes{% else %}no{% endif %}')
                         if name.isidentifier() else scenario(lambda trace: sample, _autoescape)],
    }


def global_scenarios(name: str) -> dict[str, list[Scenario]]:
    source = GLOBALS[name]
    value = GLOBAL_VALUES[name]
    return {
        'argument_variants': [(source.replace('value', 'value, bogus=1', 1) if name != 'dict' else '{{ dict(1).a }}',
                               _value(value), _default)],
        'undefined': [(source, lambda trace: {}, _default), (source, lambda trace: {}, _strict)],
        'exceptions': [(source, _value(Failing()), _default)],
        'mutation_order': [(source, _logged_value(value), _default)],
        'interactions': [(source, _value(Markup(escape(value)) if isinstance(value, str) else value), _autoescape)],
    }


def _scenarios() -> dict[str, dict[str, list[Scenario]]]:
    """Probe name -> partition -> scenarios, keyed by catalog contract."""
    table: dict[str, dict[str, list[Scenario]]] = {}
    for name in FILTERS:
        table['filter.' + name] = filter_scenarios(name)
    for name in TESTS:
        table['test.' + name] = test_scenarios(name)
    for name in GLOBALS:
        table['global.' + name] = global_scenarios(name)
    return table


SCENARIOS = _scenarios()


def probe_name(builtin: str, partition: str) -> str:
    kind, name = builtin.split('.', 1)
    if not name.isidentifier():
        name = 'alias_' + '_'.join(str(ord(character)) for character in name)
    return 'partition_' + kind + '_' + name + '_' + partition


# Probe name -> execution, for the recorder and the registry.
EXECUTIONS = {probe_name(builtin, partition): _probe(probe_name(builtin, partition), scenarios)
              for builtin, partitions in SCENARIOS.items() for partition, scenarios in partitions.items()}
EXPECTED_PATH = Path(__file__).with_name('builtin_partition_expected.json')
EXPECTED: dict[str, Any] = json.loads(EXPECTED_PATH.read_text()) if EXPECTED_PATH.exists() else {}

PROBES: dict[str, Probe] = {name: Probe(execute, EXPECTED[name], True)
                            for name, execute in EXECUTIONS.items() if name in EXPECTED}
