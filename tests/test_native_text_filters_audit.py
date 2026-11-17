"""Audit: urlencode, wordwrap, pprint, random and attr do not run Jinja's Python bodies."""

from __future__ import annotations

import unittest

import docxtpl_native
import jinja2
import jinja2.filters as filters
import jinja2.utils as utils
from docxtpl_native.bridge import BridgeConfig
from native_audit import CodeCalls


class Thing:
    value = 1


class TextFilterAuditTests(unittest.TestCase):
    def test_python_bodies_do_not_run(self) -> None:
        values = {'text': 'a b', 'pairs': [('k', 'v')], 'thing': Thing()}
        source = ('{{ text|urlencode }}{{ pairs|urlencode }}{{ text|wordwrap(2) }}{{ pairs|pprint }}'
                  '{{ [1]|random }}{{ thing|attr("value") }}')
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(jinja2.Environment(), False), values)
        with CodeCalls(filters.do_urlencode, utils.url_quote, filters.do_wordwrap, filters.do_pprint,
                       utils.pformat, filters.do_random, filters.do_attr, jinja2.Environment.getattr) as calls:
            session.render('case', source)
        self.assertEqual(calls.calls, [])


if __name__ == '__main__':
    unittest.main()
