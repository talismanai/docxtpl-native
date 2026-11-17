"""Offline synthetic differential for the native root symbol planner.

Build the Rust example with the unstable_machinery, loop_controls and unicode features,
then pass its executable path. A parser rejection counts as a mismatch.
"""

import argparse
import re
import subprocess
from typing import Any

import jinja2

CASES = [
    '{% for x in xs %}{{self}}{% endfor %}', '{% macro m() %}{{self}}{% endmacro %}',
    '{% block content %}{{self}}{% endblock %}', '{% with x=xs %}{{self}}{% endwith %}',
    '{% autoescape flag %}{{self}}{% endautoescape %}',
    '', 'plain text', '{{ a }} {{ b }} {{ a }}', '{{ a }}{{self}}{{b}}',
    '{% set foo = bar %}{{foo}}', '{% set x = x + y %}{{x}}',
    '{% set self = x %}{{self}}', '{% set loop = x %}{{loop}}', '{% set super = x %}{{super}}',
    '{% set caller = x %}{{caller}}', '{% set ns.x = value %}',
    '{{ café }} {{ usuário }}',
    '{% if flag %}{% set a = value %}{% else %}{{other}}{% endif %}{{a}}',
    '{% if flag %}{{a}}{% elif flag2 %}{{b}}{% else %}{{c}}{% endif %}',
    '{% if flag %}{% set x=1 %}{% else %}{% set y=2 %}{% endif %}',
    '{% if flag %}{{x}}{% else %}{% set x=2 %}{% endif %}{{x}}',
    '{% for x in values(offset + threshold) if x > threshold %}{{x}}{{body}}{% endfor %}{{after}}',
    '{% for x in outer %}{% for y in inner %}{{y}}{% endfor %}{% else %}{{missing}}{% endfor %}',
    '{% with x=left, y=right %}{{inside}}{% endwith %}{{after}}',
    '{% autoescape mode %}{{inside}}{% endautoescape %}{{after}}',
    '{% filter replace(first, second) %}{{inside}}{% endfilter %}{{after}}',
    '{% set captured %}{{inside}}{% endset %}{{captured}}',
    '{% include template_name %}{{after}}', '{% extends parent %}{% block content %}{{inside}}{% endblock %}',
    '{% import module_name as alias %}{{alias}}', '{% from module_name import exported as alias %}{{alias}}',
    '{% macro greet(param=default) %}{{inside}}{% endmacro %}{{greet}}',
    '{% call widget(arg) %}{{inside}}{% endcall %}{{after}}',
    '{% do action(arg, *items, keyword=named, **extras) %}{{after}}',
    '{{ values(*[offset], key=threshold) }}', '{{ [alpha, beta, alpha] }}',
    '{{ {alpha: beta, gamma: delta} }}', '{{ array[start:stop:step] }}',
    '{{ yes if condition else no }}', '{{ value|replace(old, new) }}', '{{ value is sameas(other) }}',
    '{% set a,b = pair %}{{a}}{{b}}', '{% set a=1 %}{% if cond %}{% set a=2 %}{% endif %}{{a}}',
    '{% if a %}{% set z=1 %}{% elif b %}{{y}}{% else %}{% set q=2 %}{% endif %}{{z}}{{q}}',
    '{{ foo.bar[baz] }}', '{{ f(a, b=kw, *more, **extra) }}',
]
PIECES = [
    '{{ a }}', '{{ b }}', '{% set a = b %}', '{% set b = a %}',
    '{% if cond %}{% set a = x %}{% else %}{{ y }}{% endif %}',
    '{% if cond %}{{ a }}{% else %}{% set a = x %}{% endif %}',
    '{% for item in seq %}{{ body }}{% endfor %}',
    '{% with item = source %}{{ body }}{% endwith %}',
    '{% set capture %}{{ body }}{% endset %}',
    '{% include partial %}', '{{ self }}', '{% autoescape flag %}{{ body }}{% endautoescape %}',
]
CASES.extend(left + right for left in PIECES for right in PIECES)
BRANCHES = ['{{ a }}', '{{ b }}', '{% set a = x %}', '{% set b = y %}', '{{self}}', '{% for i in seq %}{{self}}{{ i }}{% endfor %}']
CASES.extend(prefix + '{% if cond %}' + yes + '{% else %}' + no + '{% endif %}' + suffix
             for prefix in ('', '{% set a=1 %}', '{{ b }}') for yes in BRANCHES for no in BRANCHES
             for suffix in ('', '{{a}}{{b}}'))

SCOPES = [
    '{% for item in seq if item > limit %}{{self}}{{ inner }}{% else %}{{other}}{% endfor %}',
    '{% macro callme(p=default) %}{{self}}{{p}}{{local}}{% endmacro %}',
    '{% block content %}{{self}}{{inner}}{% endblock %}',
    '{% with inner=outside %}{{self}}{{inner}}{% endwith %}',
    '{% autoescape mode %}{{self}}{{inside}}{% endautoescape %}',
    '{% filter replace(old, new) %}{{self}}{{body}}{% endfilter %}',
    '{% set result %}{{self}}{{body}}{% endset %}',
    '{% if test %}{% set v=source %}{% elif other %}{{self}}{{v}}{% else %}{{fallback}}{% endif %}',
]
CASES.extend(left + scope + right for left in ('', '{{first}}', '{% set v=base %}')
             for scope in SCOPES for right in ('', '{{self}}{{v}}', '{{after}}'))
ENV = jinja2.Environment(extensions=['jinja2.ext.do', 'jinja2.ext.loopcontrols'])
PATTERN = re.compile(r"^\s*l_0_([\w]+) = (?:resolve\('([^']+)'\)|(missing)|TemplateReference\(context\))$")


def reference(source: str) -> list[str]:
    compiled = ENV.compile(source, raw=True)
    prologue = compiled.split('    if 0: yield None\n', 1)[1].split('    pass\n', 1)[0]
    result: list[str] = []
    for line in prologue.splitlines():
        if re.fullmatch(r'\s*(?:l_0_\w+ = )+missing', line):
            result.extend(f'missing:{name}' for name in re.findall(r'l_0_(\w+) = ', line))
            continue
        match = PATTERN.match(line)
        if match:
            name, resolved, missing = match.groups()
            if resolved is not None and resolved != name:
                raise ValueError(f'root target {name!r} resolves a different name {resolved!r}')
            result.append(f'{"resolve" if resolved is not None else "missing" if missing is not None else "self"}:{name}')
        elif line.strip().startswith('l_0_'):
            raise ValueError(f'unrecognized root initialization: {line.strip()}')
    return result


def candidate(probe: str, source: str) -> list[str]:
    run = subprocess.run([probe, source],
                         text=True, capture_output=True, check=True)
    return run.stdout.splitlines()


def compare_case(probe: str, source: str) -> tuple[list[str], list[str] | str] | None:
    expected = reference(source)
    try:
        actual: list[str] | str = candidate(probe, source)
    except subprocess.CalledProcessError as error:
        actual = f'candidate parser error: {error.stderr.strip()}'
    except OSError as error:
        actual = f'candidate execution error: {error}'
    return None if expected == actual else (expected, actual)


def main() -> None:
    parser = argparse.ArgumentParser(description='Compare native root symbol plans with Jinja compiled prologues.')
    parser.add_argument('probe', help='path to the compiled root_symbol_probe example')
    args = parser.parse_args()
    mismatches: list[tuple[str, Any, Any]] = []
    for source in CASES:
        try:
            difference = compare_case(args.probe, source)
        except Exception as error:  # noqa: BLE001 - record reference setup errors
            mismatches.append((source, f'reference error: {type(error).__name__}', str(error)))
            continue
        if difference is not None:
            mismatches.append((source, *difference))
    print(f'cases={len(CASES)} mismatches={len(mismatches)}')
    for source, expected, actual in mismatches:
        print('SOURCE:', source)
        print('REFERENCE:', expected)
        print('CANDIDATE:', actual)
    if mismatches:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
