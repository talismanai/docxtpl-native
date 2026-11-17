"""Audit: in sync renders select, reject, selectattr, rejectattr and map do not run Jinja's Python bodies."""

from __future__ import annotations

import unittest

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import environment, filters
from native_audit import CodeCalls


class SelectMapFilterAuditTests(unittest.TestCase):
    def test_sync_bodies_do_not_run(self) -> None:
        values = {'numbers': [1, 2, 3, 0], 'records': [{'v': 'a', 'on': True}, {'v': 'B', 'on': False}]}
        source = ('{{ numbers|select|list }}{{ numbers|reject("odd")|list }}{{ records|selectattr("on")|list }}'
                  '{{ records|rejectattr("v", "equalto", "a")|list }}{{ records|map(attribute="v")|list }}'
                  '{{ records|map(attribute="w", default=1)|list }}{{ records|map(attribute="v")|map("upper")|list }}')
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(jinja2.Environment(), False), values)
        with CodeCalls(filters.sync_do_select, filters.sync_do_reject, filters.sync_do_selectattr,
                       filters.sync_do_rejectattr, filters.sync_do_map, filters.select_or_reject,
                       filters.prepare_select_or_reject, filters.prepare_map, filters.make_attrgetter,
                       filters.do_upper, environment.Environment.call_filter, environment.Environment.call_test,
                       environment.Environment._filter_test_common) as calls:
            output = session.render('case', source)
        self.assertEqual(calls.calls, [])
        self.assertEqual(output, jinja2.Environment().from_string(source).render(values))


if __name__ == '__main__':
    unittest.main()
