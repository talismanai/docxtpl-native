"""Audit: striptags, xmlattr and urlize do not run Jinja's or MarkupSafe's Python bodies."""

from __future__ import annotations

import unittest

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import filters, utils
from markupsafe import Markup
from native_audit import CodeCalls


class MarkupTextFilterAuditTests(unittest.TestCase):
    def test_bodies_do_not_run(self) -> None:
        values = {'html': '<b>a &amp; b</b>', 'attributes': {'id': 'x', 'n': None}, 'text': 'see www.a.com, a@b.co'}
        source = '{{ html|striptags }}{{ attributes|xmlattr }}{{ text|urlize(10, true) }}'
        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                environment = jinja2.Environment(autoescape=autoescape)
                session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values)
                with CodeCalls(filters.do_striptags, filters.do_xmlattr, filters.do_urlize, utils.urlize,
                               Markup.striptags, Markup.unescape) as calls:
                    output = session.render('case', source)
                self.assertEqual(calls.calls, [])
                self.assertEqual(output, environment.from_string(source).render(values))


if __name__ == '__main__':
    unittest.main()
