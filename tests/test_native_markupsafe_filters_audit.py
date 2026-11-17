"""Audit: the ``string`` and ``escape`` filters do not run MarkupSafe's Python bodies."""

from __future__ import annotations

import unittest

import docxtpl_native
import jinja2
import markupsafe
from docxtpl_native.bridge import BridgeConfig
from native_audit import CodeCalls


class Html:
    def __html__(self) -> str:
        return '<b>safe</b>'


class MarkupSafeFilterAuditTests(unittest.TestCase):
    def test_markupsafe_bodies_do_not_run(self) -> None:
        values = {'plain': 'a<b', 'number': 7, 'html': Html(), 'markup': markupsafe.Markup('<i>x</i>')}
        source = '{{ plain|escape }}{{ number|string }}{{ html|e }}{{ markup|string }}{{ number|e }}'
        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                session = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(jinja2.Environment(autoescape=autoescape), False), values
                )
                with CodeCalls(markupsafe.escape, markupsafe.soft_str) as calls:
                    session.render('case', source)
                self.assertEqual(calls.calls, [])


if __name__ == '__main__':
    unittest.main()
