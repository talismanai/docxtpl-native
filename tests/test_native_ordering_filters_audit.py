"""Audit: sort, dictsort, min and max do not run Jinja's Python bodies."""

from __future__ import annotations

import unittest

import docxtpl_native
import jinja2
import jinja2.filters as filters
from docxtpl_native.bridge import BridgeConfig
from native_audit import CodeCalls


class OrderingFilterAuditTests(unittest.TestCase):
    def test_python_bodies_do_not_run(self) -> None:
        values = {'words': ['b', 'A'], 'mapping': {'b': 1, 'a': 2}, 'records': [{'v': 2}, {'v': 1}]}
        source = ('{{ words|sort }}{{ mapping|dictsort }}{{ words|min }}{{ words|max }}'
                  '{{ records|sort(attribute="v") }}{{ records|max(attribute="v") }}')
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(jinja2.Environment(), False), values)
        with CodeCalls(filters.do_sort, filters.do_dictsort, filters.do_min, filters.do_max, filters.ignore_case,
                       filters.make_attrgetter, filters.make_multi_attrgetter, filters._min_or_max,
                       filters._prepare_attribute_parts) as calls:
            session.render('case', source)
        self.assertEqual(calls.calls, [])


if __name__ == '__main__':
    unittest.main()
