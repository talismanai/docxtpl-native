"""Included template assignments must not escape into the calling frame."""
from functools import partial
import jinja2
from .protocol_base import Probe, generate, observed


FRAGMENT = "{% set x='inner' %}{{ x }}"
CASES = {
    'include_assignment_scope': (
        "{% include 'fragment' %}|{{ x }}", 'inner|outer'),
    'include_loop_assignment_scope': (
        "{% for x in [1,2] %}{% include 'fragment' %}|{{ x }};{% endfor %}|{{ x }}",
        'inner|1;inner|2;|outer'),
    'include_macro_assignment_scope': (
        "{% macro m() %}{% include 'fragment' %}|{{ x }}{% endmacro %}{{ m() }}|{{ x }}",
        'inner|outer|outer'),
}


def execute(engine, *, source, asynchronous=False):
    environment = jinja2.Environment(
        loader=jinja2.DictLoader({'fragment': FRAGMENT}), enable_async=asynchronous)
    return observed(generate(engine, source, {'x': 'outer'}, environment))


PROBES = {name: Probe(partial(execute, source=source), {'text': expected})
          for name, (source, expected) in CASES.items()}
