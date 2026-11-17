"""Audit: in sync renders items, batch, slice and unique do not run Jinja's Python bodies."""

from __future__ import annotations

import unittest

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import filters
from native_audit import CodeCalls


class GeneratorFilterAuditTests(unittest.TestCase):
    def test_sync_bodies_do_not_run(self) -> None:
        values = {'mapping': {'a': 1}, 'numbers': [3, 1, 3, 2], 'records': [{'v': 'a'}, {'v': 'A'}]}
        source = ('{{ mapping|items|list }}{{ numbers|batch(3, 0)|list }}{{ numbers|slice(3, 0)|list }}'
                  '{{ numbers|unique|list }}{{ records|unique(attribute="v")|list }}')
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(jinja2.Environment(), False), values)
        with CodeCalls(filters.do_items, filters.do_batch, filters.sync_do_slice, filters.sync_do_unique,
                       filters.make_attrgetter, filters.ignore_case) as calls:
            output = session.render('case', source)
        self.assertEqual(calls.calls, [])
        self.assertEqual(output, jinja2.Environment().from_string(source).render(values))


if __name__ == '__main__':
    unittest.main()
