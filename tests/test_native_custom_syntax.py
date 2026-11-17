"""Custom Jinja2 delimiters are compiled by the native engine."""

import unittest
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from paritylab.compare import compare_docx
from paritylab.protocol_base import generate, template

DELIMITERS: list[dict[str, str]] = [
    {'block_start_string': '<%', 'block_end_string': '%>', 'variable_start_string': '<<',
     'variable_end_string': '>>', 'comment_start_string': '<#', 'comment_end_string': '#>'},
    {'block_start_string': '[%', 'block_end_string': '%]', 'variable_start_string': '[[',
     'variable_end_string': ']]', 'comment_start_string': '[#', 'comment_end_string': '#]'},
    {'variable_start_string': '${', 'variable_end_string': '}'},
]


def convert(source: str, delimiters: dict[str, str]) -> str:
    """`source`, written with the default delimiters, in `delimiters`."""
    for default, key in (('{%', 'block_start_string'), ('%}', 'block_end_string'),
                         ('{{', 'variable_start_string'), ('}}', 'variable_end_string'),
                         ('{#', 'comment_start_string'), ('#}', 'comment_end_string')):
        source = source.replace(default, delimiters.get(key, default))
    return source


def outcome(render: Any) -> tuple[str, str]:
    try:
        return ('output', render())
    except Exception as error:  # noqa: BLE001 - compared between renderers
        return (type(error).__name__, str(error))


class CustomSyntaxTests(unittest.TestCase):
    def test_delimiters_match_reference(self) -> None:
        sources = ['  {%- for x in items -%}\n  {{- x|upper -}} ,\n{% endfor %}\n  {# c #}\nend\n',
                   '{% if items %}\n  yes {{ items|length }}\n{% else %}no{% endif %}\n',
                   '{% raw %}{{ x }}{% endraw %}', '{% set y = {"a": 1} %}{{ y.a }} {{ 1 + 2 }}']
        context = {'items': ['a', 'b']}
        for delimiters in DELIMITERS:
            for options in ({}, {'trim_blocks': True, 'lstrip_blocks': True}, {'keep_trailing_newline': True}):
                for source in sources:
                    text = convert(source, delimiters)
                    with self.subTest(delimiters=delimiters, options=options, source=text):
                        expected = outcome(lambda: jinja2.Environment(**delimiters, **options)
                                           .from_string(text).render(context))
                        session = docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(jinja2.Environment(**delimiters, **options), False), context)
                        self.assertEqual(outcome(lambda: session.render('case', text)), expected)

    def test_syntax_error_matches_reference(self) -> None:
        delimiters = DELIMITERS[0]
        text = convert('{% for x in items %}{{ x }}', delimiters)
        expected = outcome(lambda: jinja2.Environment(**delimiters).from_string(text).render(items=[]))
        session = docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(jinja2.Environment(**delimiters), False), {'items': []})
        self.assertEqual(expected[0], 'TemplateSyntaxError')
        self.assertEqual(outcome(lambda: session.render('case', text)), expected)

    def test_document_with_delimiters_matches_docxtpl(self) -> None:
        delimiters = DELIMITERS[0]
        source = convert('{% for user in users %}{{ user.name }};{% endfor %}', delimiters)
        values = {'users': [{'name': 'Ada'}, {'name': 'Grace'}]}
        input_doc = template(source)
        reference = generate(docxtpl, source, values, jinja2.Environment(**delimiters), source=input_doc)
        candidate = generate(docxtpl_native, source, values, jinja2.Environment(**delimiters), source=input_doc)
        self.assertTrue(compare_docx(reference, candidate)['equivalent'])

    def test_line_prefixes_stay_unsupported(self) -> None:
        for options in ({'line_statement_prefix': '#'}, {'line_comment_prefix': '##'}):
            with self.subTest(options=options), self.assertRaises(docxtpl_native.UnsupportedFeature):
                BridgeConfig(jinja2.Environment(**options), False)


if __name__ == '__main__':
    unittest.main()
