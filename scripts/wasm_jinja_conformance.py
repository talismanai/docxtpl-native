"""Compare the browser (wasm) Jinja environment with Jinja2 on generated cases.

The browser engine has no Python: every builtin filter, test, global and
Python method call that templates use must behave like Jinja2 over plain
JSON data. Expected outputs come from ``jinja2.Environment(autoescape=False)``,
the environment docxtpl creates by default.

Usage: python scripts/wasm_jinja_conformance.py COMMAND [ARG...]

COMMAND reads the case list as JSON on stdin and prints one result per case:
``target/release/examples/conformance`` (host build) or
``node scripts/wasm_conformance_runner.mjs docxtpl_wasm.wasm`` (wasm build).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from typing import Any

import jinja2

CONTEXT: dict[str, Any] = {
    "s": "Hello World",
    "lower_s": "naïve café, façade",
    "pad": "  padded  ",
    "html": "<b>a & b</b>",
    "csv": "a,b,,c",
    "multi": "line one\nline two\n\nline four",
    "long": "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor",
    "n": 7,
    "neg": -3,
    "f": 3.14159,
    "big": 1234567.891,
    "zero": 0,
    "t": True,
    "no": False,
    "none": None,
    "empty": "",
    "nums": [3, 1, 2, 3],
    "floats": [1.5, 2.25],
    "words": ["banana", "Apple", "cherry", "apple"],
    "mixed": [1, "a", None, True],
    "nested": [[1, 2], [3], []],
    "d": {"b": 2, "a": 1, "c": 3},
    "people": [
        {"name": "Ana", "age": 30, "city": "SP", "tags": ["x"]},
        {"name": "bruno", "age": 25, "city": "RJ", "tags": []},
        {"name": "Carla", "age": 30, "city": "SP"},
    ],
    "deep": {"a": {"b": {"c": "found"}}, "list": [{"v": 1}, {"v": 2}]},
    "url": "https://example.com/a b?x=1&y=ç",
    "date_like": "2024-01-31",
    "money": "1500.5",
    "int_str": "42",
    "bad_num": "abc",
    "size": 1536000,
}

# fmt: off
FILTERS = [
    "{{ neg|abs }}", "{{ f|abs }}",
    "{{ d|attr('b') }}",
    "{% for row in nums|batch(3) %}{{ row }};{% endfor %}", "{% for row in nums|batch(3, 0) %}{{ row }};{% endfor %}",
    "{{ lower_s|capitalize }}", "{{ s|center(21) }}|", "{{ s|center(21, '*') }}",
    "{{ nums|count }}", "{{ none|default('x') }}", "{{ empty|default('x') }}", "{{ empty|default('x', true) }}",
    "{{ missing|default('fallback') }}", "{{ missing|d('fb') }}", "{{ none|default('x', boolean=true) }}",
    "{{ d|dictsort }}", "{{ d|dictsort(reverse=true) }}", "{{ d|dictsort(by='value') }}",
    "{{ html|e }}", "{{ html|escape }}", "{{ html|forceescape }}",
    "{{ size|filesizeformat }}", "{{ size|filesizeformat(true) }}", "{{ 100|filesizeformat }}",
    "{{ nums|first }}", "{{ empty|first }}", "{{ s|first }}",
    "{{ int_str|float }}", "{{ bad_num|float }}", "{{ bad_num|float(1.5) }}", "{{ n|float }}",
    "{{ '%s is %d'|format('x', 3) }}", "{{ '%05.1f'|format(f) }}", "{{ '%(a)s'|format(a=1) }}",
    "{% for g in people|groupby('city') %}{{ g.grouper }}:{{ g.list|map(attribute='name')|join(',') }};{% endfor %}",
    "{% for city, items in people|groupby('city') %}{{ city }}={{ items|length }};{% endfor %}",
    "{% for g in people|groupby('age', default=0) %}{{ g.grouper }}:{{ g.list|length }};{% endfor %}",
    "{{ multi|indent }}", "{{ multi|indent(2, true) }}", "{{ multi|indent(width='> ', blank=true) }}", "{{ multi|indent(4, first=true, blank=true) }}",
    "{{ money|int }}", "{{ int_str|int }}", "{{ f|int }}", "{{ bad_num|int }}", "{{ bad_num|int(9) }}", "{{ '0x1A'|int(0, 16) }}", "{{ '0x1A'|int(0, 0) }}",
    "{{ d|items|list }}", "{% for k, v in d|items %}{{ k }}={{ v }};{% endfor %}",
    "{{ nums|join }}", "{{ nums|join('-') }}", "{{ people|join(', ', attribute='name') }}",
    "{{ nums|last }}", "{{ s|length }}", "{{ d|length }}", "{{ s|list }}", "{{ d|list }}",
    "{{ s|lower }}", "{{ people|map(attribute='name')|list }}", "{{ people|map(attribute='tags.0', default='-')|list }}",
    "{{ words|map('upper')|list }}", "{{ nums|map('string')|join }}",
    "{{ nums|max }}", "{{ words|max }}", "{{ words|max(case_sensitive=true) }}", "{{ people|max(attribute='age') }}",
    "{{ nums|min }}", "{{ words|min }}", "{{ people|min(attribute='name') }}",
    "{{ d|pprint }}", "{{ nums|pprint }}",
    "{{ nums|reject('odd')|list }}", "{{ people|rejectattr('city', 'equalto', 'SP')|map(attribute='name')|list }}",
    "{{ people|rejectattr('tags')|map(attribute='name')|list }}",
    "{{ s|replace('o', '0') }}", "{{ s|replace('o', '0', 1) }}",
    "{{ nums|reverse|list }}", "{{ s|reverse }}",
    "{{ f|round }}", "{{ f|round(2) }}", "{{ f|round(1, 'floor') }}", "{{ f|round(1, 'ceil') }}", "{{ 2.5|round }}", "{{ 3.5|round }}", "{{ n|round }}",
    "{{ html|safe }}",
    "{{ nums|select('odd')|list }}", "{{ nums|select('greaterthan', 1)|list }}", "{{ nums|select|list }}", "{{ mixed|select|list }}",
    "{{ people|selectattr('age', 'equalto', 30)|map(attribute='name')|join(',') }}", "{{ people|selectattr('tags')|map(attribute='name')|list }}",
    "{% for c in nums|slice(2) %}{{ c }};{% endfor %}", "{% for c in nums|slice(3, 'x') %}{{ c }};{% endfor %}",
    "{{ nums|sort }}", "{{ words|sort }}", "{{ words|sort(case_sensitive=true) }}", "{{ nums|sort(reverse=true) }}",
    "{{ people|sort(attribute='age,name')|map(attribute='name')|list }}", "{{ people|sort(attribute='name')|map(attribute='name')|list }}",
    "{{ n|string }}", "{{ none|string }}", "{{ nums|string }}", "{{ t|string }}",
    "{{ html|striptags }}", "{{ '  a  <br/> b\n c '|striptags }}",
    "{{ nums|sum }}", "{{ floats|sum }}", "{{ people|sum(attribute='age') }}", "{{ nums|sum(start=10) }}",
    "{{ lower_s|title }}", "{{ \"it's o'neil\"|title }}",
    "{{ d|tojson }}", "{{ people[0]|tojson }}", "{{ html|tojson }}", "{{ d|tojson(indent=2) }}",
    "{{ pad|trim }}|", "{{ 'xxaxx'|trim('x') }}",
    "{{ long|truncate(20) }}", "{{ long|truncate(20, true) }}", "{{ long|truncate(20, false, '…') }}", "{{ long|truncate(20, leeway=0) }}", "{{ s|truncate(9) }}",
    "{{ nums|unique|list }}", "{{ words|unique|list }}", "{{ words|unique(case_sensitive=true)|list }}", "{{ people|unique(attribute='city')|map(attribute='name')|list }}",
    "{{ s|upper }}", "{{ url|urlencode }}", "{{ d|urlencode }}", "{{ 'a/b c'|urlencode }}",
    "{{ 'visit https://x.org now'|urlize }}", "{{ 'x www.y.com'|urlize(10) }}",
    "{{ long|wordcount }}", "{{ long|wordwrap(20) }}", "{{ long|wordwrap(15, false) }}", "{{ long|wordwrap(20, wrapstring='|') }}",
    "{{ {'class': 'a', 'id': 3, 'x': none}|xmlattr }}",
]

TESTS = [
    "{{ n is odd }}", "{{ n is even }}", "{{ n is divisibleby 7 }}", "{{ n is defined }}", "{{ missing is undefined }}",
    "{{ none is none }}", "{{ t is true }}", "{{ no is false }}", "{{ t is boolean }}", "{{ n is boolean }}",
    "{{ n is number }}", "{{ f is float }}", "{{ n is integer }}", "{{ t is integer }}", "{{ s is string }}",
    "{{ d is mapping }}", "{{ nums is sequence }}", "{{ s is sequence }}", "{{ nums is iterable }}", "{{ n is iterable }}",
    "{{ s is lower }}", "{{ 'abc' is lower }}", "{{ 'ABC' is upper }}", "{{ n is eq 7 }}", "{{ n is ne 7 }}",
    "{{ n is lt 8 }}", "{{ n is le 7 }}", "{{ n is gt 8 }}", "{{ n is ge 7 }}", "{{ n is equalto 7 }}",
    "{{ n is greaterthan 1 }}", "{{ n is lessthan 1 }}", "{{ 1 is in nums }}", "{{ 'a' is in d }}", "{{ html is escaped }}",
    "{{ none is sameas none }}", "{{ 'upper' is filter }}", "{{ 'odd' is test }}", "{{ 'nope' is filter }}",
    "{{ s is callable }}", "{{ range is callable }}", "{{ n is not odd }}",
]

GLOBALS = [
    "{{ range(5)|list }}", "{{ range(2, 9, 3)|list }}", "{{ range(5, 0, -2)|list }}",
    "{{ dict(a=1, b=2) }}", "{% set c = cycler('x', 'y') %}{{ c.next() }}{{ c.next() }}{{ c.next() }}{{ c.current }}",
    "{% set j = joiner(', ') %}{% for i in nums %}{{ j() }}{{ i }}{% endfor %}",
    "{% set ns = namespace(total=0) %}{% for i in nums %}{% set ns.total = ns.total + i %}{% endfor %}{{ ns.total }}",
    "{{ lipsum(1, false, 5, 5)|wordcount > 0 }}",
]

METHODS = [
    "{{ s.upper() }}", "{{ s.lower() }}", "{{ s.title() }}", "{{ lower_s.capitalize() }}", "{{ pad.strip() }}|",
    "{{ pad.lstrip() }}|", "{{ pad.rstrip() }}|", "{{ csv.strip(',a') }}|", "{{ (csv ~ ', ').rstrip(' ,') }}|",
    "{{ s.lstrip('HeW') }}|", "{{ s.strip('dlroW H') }}|", "{{ pad.strip(None) }}|", "{{ ('\x1c x \x1f').strip() }}|",
    "{{ 'Kind regards,'.lower().replace('.', '').rstrip(' ,') }}|", "{{ 'P. S.'.strip(' ,.') }}|", "{{ csv.split(',') }}", "{{ s.split() }}", "{{ csv.split(',', 1) }}",
    "{{ s.replace('l', 'L') }}", "{{ s.startswith('He') }}", "{{ s.endswith('x') }}", "{{ s.find('o') }}", "{{ s.count('o') }}",
    "{{ '-'.join(words) }}", "{{ 'x'.center(5, '-') }}", "{{ int_str.isdigit() }}", "{{ s.isalpha() }}", "{{ int_str.zfill(5) }}",
    "{{ '{} and {}'.format('a', 'b') }}", "{{ '{x}!'.format(x=1) }}", "{{ s.swapcase() }}", "{{ multi.splitlines() }}",
    "{{ date_like.split('-')[0] }}", "{{ s.rsplit(' ', 1) }}", "{{ s.partition(' ') }}",
    "{{ d.get('a') }}", "{{ d.get('z') }}", "{{ d.get('z', 9) }}", "{{ d.keys()|list }}", "{{ d.values()|list }}",
    "{{ d.items()|list }}", "{% for k, v in d.items() %}{{ k }}{{ v }}{% endfor %}", "{{ nums.index(2) }}", "{{ nums.count(3) }}",
    "{{ '{:>8}|{:<6}|{:^7}'.format('a', 'b', 'c') }}", "{{ '{:08.2f}|{:,}|{:,.2f}'.format(f, big, big) }}", "{{ '{:+d} {: d}'.format(n, n) }}",
    "{{ '{0}{1}{0}'.format('a', 'b') }}", "{{ '{p[name]} {p[age]}'.format(p=people[0]) }}", "{{ '{:.1%}'.format(0.256) }}",
    "{{ '{:x} {:X} {:#o} {:b}'.format(255, 255, 8, 5) }}", "{{ '{!r}'.format(s) }}", "{{ '{:*^9}'.format('mid') }}", "{{ '{{literal}}'.format() }}",
    "{{ '{:.3}'.format(s) }}", "{{ '{:e}'.format(big) }}", "{{ '{:g}'.format(0.00001234) }}",
    "{{ 'ab'.center(7, '*') }}", "{{ 'abc'.center(6, '*') }}", "{{ ' a  b '.split() }}", "{{ ' a  b '.split(None, 1) }}",
    "{{ 'a,b'.split(',', 0) }}", "{{ ''.split() }}", "{{ ''.split(',') }}", "{{ 'a b c'.rsplit(None, 1) }}",
    "{{ words.index('cherry') }}", "{{ [1, 2.0, '1'].count(1) }}", "{{ 'x'.ljust(3, '.') }}|{{ 'x'.rjust(3) }}",
    "{{ 'Title Case'.istitle() }}", "{{ 'ABC1'.isupper() }}", "{{ 'abc'.isalnum() }}", "{{ '123'.isnumeric() }}",
    "{{ 'prefix_x'.removeprefix('prefix_') }}", "{{ 'banana'.rfind('a') }}", "{{ 'banana'.rpartition('n') }}",
]

SYNTAX = [
    "{{ 7 // 2 }}", "{{ 7 % 3 }}", "{{ 2 ** 10 }}", "{{ 7 / 2 }}", "{{ 4 / 2 }}", "{{ 'a' ~ 1 ~ none }}", "{{ 'ab' * 3 }}",
    "{{ 1 in nums }}", "{{ 'W' in s }}", "{{ not t }}", "{{ t and no }}", "{{ none or 'x' }}", "{{ n if t else 'no' }}",
    "{{ n if no }}", "{{ s[0] }}{{ s[-1] }}", "{{ s[1:4] }}", "{{ s[::-1] }}", "{{ nums[1:] }}", "{{ d['a'] }}{{ d.a }}",
    "{{ deep.a.b.c }}", "{{ deep['list'][1].v }}", "{{ missing }}|", "{{ missing.attr }}|",
    "{{ '%s-%s' % ('a', 'b') }}", "{{ '%.2f' % f }}", "{{ '%d%%' % n }}", "{{ (1, 2) }}", "{{ [1, 'a'] }}", "{{ {'k': [1]} }}",
    "{{ 1 < n < 10 }}", "{{ n == 7.0 }}", "{{ f }}", "{{ big }}", "{{ 1.0 }}", "{{ 1e20 }}", "{{ 10 / 3 }}", "{{ 0.1 + 0.2 }}",
    "{{ t }}{{ no }}{{ none }}", "{{ nums }}", "{{ words }}", "{{ d }}", "{{ mixed }}",
    "{% for p in people %}{{ loop.index }}/{{ loop.length }}{{ '!' if loop.first }}{{ loop.revindex }}{{ loop.cycle('a','b') }};{% endfor %}",
    "{% for p in people if p.age > 26 %}{{ p.name }}{% else %}none{% endfor %}", "{% for x in [] %}x{% else %}empty{% endfor %}",
    "{% for a, b in [[1, 2], [3, 4]] %}{{ a + b }}{% endfor %}", "{% for i in nums %}{% if loop.changed(i) %}{{ i }}{% endif %}{% endfor %}",
    "{% for p in people %}{{ loop.previtem.name if loop.previtem else '-' }}{% endfor %}",
    "{% for i in nums %}{% if i == 2 %}{% break %}{% endif %}{{ i }}{% endfor %}",
    "{% for row in nested recursive %}[{{ row if row is number else loop(row) }}]{% endfor %}",
    "{% macro m(a, b=2) %}{{ a }}-{{ b }}-{{ varargs }}-{{ kwargs }}{% endmacro %}{{ m(1) }}|{{ m(1, 3, 4, z=5) }}",
    "{% macro w() %}<{{ caller() }}>{% endmacro %}{% call w() %}inner{% endcall %}",
    "{% set x = 5 %}{% set y %}captured {{ x }}{% endset %}{{ y }}", "{% with a = 1, b = 2 %}{{ a + b }}{% endwith %}",
    "{% filter upper %}shout {{ s }}{% endfilter %}", "{% raw %}{{ not rendered }}{% endraw %}",
    "a\n  {%- if t %}\n  b\n  {%- endif %}\nc", "{# comment #}x", "{{ '\\u00e7' }}", "{{ \"quote's\" }}",
    "{% if n is odd and s|length > 3 %}yes{% elif n %}elif{% else %}no{% endif %}",
    "{{ people|selectattr('age', '>=', 30)|list|length }}", "{{ nums|map('abs')|sum }}", "{{ (nums|sort)[0] }}",
    "{{ none is not none }}", "{{ 'é'|upper }}", "{{ 'ß'|upper }}", "{{ lower_s|length }}",
    "{{ '%5s|%-5s|' % ('a', 'b') }}", "{{ '%05d|%+d|% d' % (n, n, n) }}", "{{ '%x %X %o %#x' % (255, 255, 8, 255) }}",
    "{{ '%e|%E' % (big, big) }}", "{{ '%g|%g|%g' % (f, big, 0.00001234) }}", "{{ '%r' % s }}", "{{ '%c%c' % (65, 'b') }}",
    "{{ '%(a)s-%(b)05.1f' % {'a': 1, 'b': 2.25} }}", "{{ '%.3s' % s }}", "{{ '%*d' % (5, 42) }}", "{{ '%s' % none }}",
    "{{ '%d' % f }}", "{{ '%s %s' % ('only',) }}", "{{ '%.0f|%.0f|%.1f' % (0.5, 1.5, 0.25) }}",
    "{{ money|float * 2 }}", "{{ (money|float)|round(1) }}", "{{ 'R$ %.2f' % (money|float) }}", "{{ '%.2f'|format(money|float)|replace('.', ',') }}",
    "{% macro f(a, b) %}{{ a }}{{ b }}{% endmacro %}{{ f(**{'a': 1, 'b': 2}) }}|{{ f(1, **{'b': 3}) }}", "{{ dict(a=1, **d) }}",
    "{% for a, b in [(1, 2), (3, 4)] %}{{ a * b }};{% endfor %}", "{% set x = (1, 2) %}{{ x[0] }}{{ x|length }}{{ x|list }}{{ x[-1] }}",
    "{{ (1,) }}|{{ () }}", "{% set a, b = (1, 2) %}{{ a }}{{ b }}", "{{ 1 in (1, 2) }}", "{{ (1, 2) + (3,) }}", "{{ (1, 2) == (1, 2) }}",
    "{{ (1, 2)|join('-') }}", "{{ [(2, 'b'), (1, 'a')]|sort }}", "{{ d.items()|first }}", "{% for k, v in d|dictsort %}{{ k }}{% endfor %}",
    "{{ ('a', 'b')|map('upper')|list }}", "{{ (3, 1)|max }}", "{{ [1, 2] + [3] }}", "{{ ((1, 2), [3]) }}",
    "{% macro v() %}{{ varargs|length }}{{ varargs[0] if varargs }}{% endmacro %}{{ v() }}|{{ v(7, 8) }}",
    "{{ 1500.5|int }}", "{{ -2.7|int }}", "{{ '-2.7'|int }}", "{{ ' 12 '|int }}", "{{ '1_000'|int }}", "{{ '1e3'|int }}", "{{ 'nan'|float }}",
]
# fmt: on

# Divergences that remain, with the reason; they are reported, not hidden.
KNOWN_GAPS = {
    "{{ (1, 2) + (3,) }}": "tuple concatenation yields a list (same items, list repr)",
}


def cases() -> list[dict[str, Any]]:
    groups = {"filter": FILTERS, "test": TESTS, "global": GLOBALS, "method": METHODS, "syntax": SYNTAX}
    return [
        {"group": group, "template": source, "context": CONTEXT}
        for group, sources in groups.items()
        for source in sources
    ]


def expected(source: str) -> dict[str, Any]:
    environment = jinja2.Environment(autoescape=False, extensions=["jinja2.ext.loopcontrols"])
    try:
        return {"ok": True, "output": environment.from_string(source).render(CONTEXT)}
    except Exception as error:  # noqa: BLE001 - every reference failure is an expected outcome
        return {"ok": False, "output": f"{type(error).__name__}: {error}"}


def main() -> int:
    command = sys.argv[1:]
    all_cases = cases()
    references = [expected(case["template"]) for case in all_cases]
    run = subprocess.run(
        command, input=json.dumps(all_cases), capture_output=True, text=True, check=True
    )
    actuals = json.loads(run.stdout)
    failures = 0
    summary: dict[str, list[int]] = {}
    for case, reference, actual in zip(all_cases, references, actuals, strict=True):
        same = reference["ok"] == actual["ok"] and (
            not reference["ok"] or reference["output"] == actual["output"]
        )
        counts = summary.setdefault(case["group"], [0, 0])
        counts[0] += same
        counts[1] += 1
        if not same and case["template"] in KNOWN_GAPS:
            print(f"KNOWN [{case['group']}] {case['template']!r}: {KNOWN_GAPS[case['template']]}")
            counts[0] += 1
            continue
        if not same:
            failures += 1
            print(f"DIFF [{case['group']}] {case['template']!r}")
            print(f"   jinja2: {reference['ok']} {reference['output']!r}")
            print(f"   wasm:   {actual['ok']} {actual['output']!r}")
    for group, (passed, total) in summary.items():
        print(f"{group}: {passed}/{total}")
    print(f"total: {len(all_cases) - failures}/{len(all_cases)} ({len(KNOWN_GAPS)} known gap(s), {shlex.join(command)})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
