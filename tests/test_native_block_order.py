"""Compare the native compiler's block inventory with Jinja's template."""

import unittest
from typing import Any, ClassVar

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import nodes
from jinja2.ext import Extension


class NativeBlockOrderTests(unittest.TestCase):
    def test_block_inventory_uses_adapted_syntax_tree(self) -> None:
        class GeneratedBlock(Extension):
            tags: ClassVar[set[str]] = {'makeblock'}  # ty: ignore[invalid-attribute-override]

            def parse(self, parser: Any) -> nodes.Block:
                token = next(parser.stream)
                body = [nodes.Output([nodes.TemplateData('generated')])]
                block = nodes.Block('injected', body, False, False)
                block.set_lineno(token.lineno)
                return block

        source = '{% block before %}{% endblock %}{% makeblock %}{% block after %}{% endblock %}'
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                environment = jinja2.Environment(enable_async=asynchronous, extensions=[GeneratedBlock])
                expected = list(environment.from_string(source).blocks)
                self.assertEqual(expected, ['before', 'injected', 'after'])
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                self.assertEqual(session._native_block_names(source), expected)

    def test_block_names_follow_reference_order(self) -> None:
        sources = (
            'plain text',
            '{% block zebra %}{% endblock %}{% block alpha %}{% endblock %}',
            '{% block outer %}{% block inside %}{% endblock %}{% endblock %}{% block last %}{% endblock %}',
            '{% if false %}{% block b %}{% endblock %}{% else %}{% block a %}{% endblock %}{% endif %}',
            '{% for x in xs %}{% block z %}{% endblock %}{% else %}{% block y %}{% endblock %}{% endfor %}',
            '{% macro m() %}{% block nested %}{% endblock %}{% endmacro %}{% block root %}{% endblock %}',
            '{% for x in xs recursive %}{% block first %}{% endblock %}{% else %}{% block second %}{% endblock %}{% endfor %}',
            '{% call invoke() %}{% block callbody %}{% endblock %}{% endcall %}{% block following %}{% endblock %}',
            '{% with x=1 %}{% block withbody %}{% endblock %}{% endwith %}{% autoescape true %}{% block escaped %}{% endblock %}{% endautoescape %}',
        )
        for asynchronous in (False, True):
            for source in sources:
                with self.subTest(asynchronous=asynchronous, source=source):
                    environment = jinja2.Environment(enable_async=asynchronous)
                    expected = list(environment.from_string(source).blocks)
                    session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {})
                    self.assertEqual(session._native_block_names(source), expected)
