"""Audit: in sync renders first, list, sum and groupby do not run Jinja's Python bodies."""

from __future__ import annotations

import unittest

import docxtpl_native
import jinja2
import jinja2.filters as filters
from docxtpl_native.bridge import BridgeConfig
from native_audit import CodeCalls


class SequenceFilterAuditTests(unittest.TestCase):
    def test_sync_bodies_do_not_run(self) -> None:
        values = {'numbers': [3, 1], 'records': [{'v': 2}, {'v': 1}]}
        source = ('{{ numbers|first }}{{ numbers|list }}{{ numbers|sum }}{{ records|sum(attribute="v") }}'
                  '{{ records|groupby("v") }}{{ records|groupby("w", default=0, case_sensitive=true) }}')
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(jinja2.Environment(), False), values)
        with CodeCalls(filters.sync_do_first, filters.sync_do_list, filters.sync_do_sum, filters.sync_do_groupby,
                       filters.make_attrgetter, filters.ignore_case, filters.do_first) as calls:
            session.render('case', source)
        self.assertEqual(calls.calls, [])


if __name__ == '__main__':
    unittest.main()
