"""Jinja behaviours found by the language partition probes, against Jinja2 itself."""
import io
import unittest
from typing import Any

import docxtpl_native
import jinja2
from docx import Document
from markupsafe import Markup


def render(source: str, context: dict[str, Any] | None = None, environment: jinja2.Environment | None = None,
           autoescape: bool = False) -> str:
    document = Document()
    document.add_paragraph(source)
    buffer = io.BytesIO()
    document.save(buffer)
    template = docxtpl_native.DocxTemplate(io.BytesIO(buffer.getvalue()))
    template.render(context or {}, jinja_env=environment, autoescape=autoescape)
    output = io.BytesIO()
    template.save(output)
    return Document(io.BytesIO(output.getvalue())).paragraphs[0].text


def loader(**templates: str) -> jinja2.Environment:
    return jinja2.Environment(loader=jinja2.DictLoader(templates))


class NativeJinjaPartitionTests(unittest.TestCase):
    def test_missing_templates_raise_jinjas_exceptions(self) -> None:
        environment = loader(part='P')
        with self.assertRaises(jinja2.TemplateNotFound) as raised:
            render("{% include 'absent' %}", environment=environment)
        self.assertEqual((type(raised.exception), raised.exception.name), (jinja2.TemplateNotFound, 'absent'))
        with self.assertRaisesRegex(jinja2.TemplatesNotFound, 'Tried to select from an empty list of templates.'):
            render('{% include names %}', {'names': []}, environment=environment)
        with self.assertRaisesRegex(jinja2.TemplatesNotFound, 'none of the templates given were found: a, b'):
            render("{% include ['a', 'b'] %}", environment=environment)
        with self.assertRaises(jinja2.TemplateNotFound):
            render("{% import 'absent' as x %}", environment=environment)
        self.assertEqual(render("A{% include [] ignore missing %}B", environment=environment), 'AB')

    def test_template_objects_are_included_with_or_without_context(self) -> None:
        foreign = jinja2.Environment().from_string('foreign {{ value|default("U") }}')
        source = '{% include tpl %}|{% include tpl without context %}|{% include ["absent", tpl] %}'
        self.assertEqual(render(source, {'tpl': foreign, 'value': 'V'}, environment=loader()),
                         'foreign V|foreign U|foreign V')

    def test_inheritance_errors_have_jinjas_types(self) -> None:
        environment = loader(base='A{% block body %}{% endblock %}')
        with self.assertRaisesRegex(jinja2.TemplateRuntimeError, '^extended multiple times$'):
            render("{% extends 'base' %}{% extends 'base' %}", environment=environment)
        with self.assertRaises(jinja2.TemplateAssertionError) as raised:
            render('{% block a %}{% endblock %}{% block a %}{% endblock %}', environment=environment)
        self.assertEqual(str(raised.exception), "block 'a' defined twice")

    def test_if_without_else_is_a_plain_undefined(self) -> None:
        strict = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self.assertEqual(render('[{{ "x" if flag }}][{{ ("x" if flag)|upper }}][{{ ("x" if flag) ~ "a" }}]',
                                {'flag': False}, environment=strict), '[][][a]')
        seen = render('{{ f("x" if flag) }}', {'flag': False, 'f': lambda value: type(value).__name__},
                      environment=strict)
        self.assertEqual(seen, 'Undefined')
        with self.assertRaisesRegex(jinja2.UndefinedError, 'the inline if-expression on line 2 evaluated to false'):
            render('{{ ("x" if flag).attr }}', {'flag': False})

    def test_call_block_macro_is_anonymous(self) -> None:
        self.assertEqual(render('{% macro m() %}[{{ caller.name }}]{% endmacro %}{% call m() %}x{% endcall %}'),
                         '[None]')
        with self.assertRaisesRegex(TypeError, "^macro None takes no keyword argument 'k'$"):
            render('{% macro m() %}{{ caller(k=1) }}{% endmacro %}{% call m() %}x{% endcall %}')

    def test_with_values_use_the_enclosing_scope(self) -> None:
        self.assertEqual(render('{% set a = "outer" %}{% with a = "inner", b = a %}{{ a }}/{{ b }}{% endwith %}{{ a }}'),
                         'inner/outerouter')

    def test_macro_results_follow_the_callers_autoescape(self) -> None:
        source = ('{% macro m(n, sep="\'") %}{{ n }}{{ sep }}{% if n > 0 %}{{ m(n - 1) }}{% endif %}{% endmacro %}'
                  '{{ m(2) }}|{% autoescape false %}{{ m(2) }}{% endautoescape %}')
        self.assertEqual(render(source, autoescape=True), "2'1'0'|2'1&#39;0&amp;#39;")
        self.assertEqual(render('{% filter upper %}{{ safe }}{% endfilter %}', {'safe': Markup('&#39;s')},
                                autoescape=True), "'S")

    def test_recursive_filtered_loop_is_lazy(self) -> None:
        trace: list[str] = []

        def predicate(value: int) -> bool:
            trace.append(f'p{value}')
            return value % 2 == 1

        def mark(value: int) -> str:
            trace.append(f'b{value}')
            return ''

        def items() -> Any:
            yield from (1, 2, 3)
        text = render('{% for x in items if p(x) recursive %}{{ x }}{{ mark(x) }}{% endfor %}',
                      {'items': items(), 'p': predicate, 'mark': mark})
        self.assertEqual((text, trace), ('13', ['p1', 'b1', 'p2', 'p3', 'b3']))


if __name__ == '__main__':
    unittest.main()
