"""Audit: in async renders the async_variant filters do not run Jinja's Python bodies."""

from __future__ import annotations

import unittest

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import async_utils, filters
from native_audit import CodeCalls


def async_body(name: str) -> object:
    for cell in getattr(filters, name).__closure__:
        if getattr(cell.cell_contents, '__name__', None) == name:
            return cell.cell_contents
    raise AssertionError(name)


class AsyncVariantFilterAuditTests(unittest.TestCase):
    def test_async_bodies_do_not_run(self) -> None:
        values = {'numbers': [3, 1, 3], 'records': [{'v': 'a'}, {'v': 'A'}]}
        source = ('{{ numbers|first }}{{ numbers|list }}{{ numbers|sum }}{{ records|groupby("v") }}'
                  '{{ numbers|slice(2)|list }}{{ numbers|unique|list }}'
                  # The select family and map give async generators; a filter
                  # applied to one keeps Jinja's body (an async iterable).
                  '{% for x in numbers|select("odd") %}{{ x }}{% endfor %}{% for x in numbers|reject %}{{ x }}{% endfor %}'
                  '{% for x in records|selectattr("v") %}{{ x }}{% endfor %}'
                  '{% for x in records|rejectattr("v", "lower") %}{{ x }}{% endfor %}'
                  '{% for x in records|map(attribute="v") %}{{ x }}{% endfor %}'
                  '{% for x in numbers|map("string") %}{{ x }}{% endfor %}')
        environment = jinja2.Environment(enable_async=True)
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values)
        bodies = [async_body(name) for name in ('do_first', 'do_list', 'do_sum', 'do_groupby', 'do_slice', 'do_unique',
                                                'do_select', 'do_reject', 'do_selectattr', 'do_rejectattr', 'do_map')]
        with CodeCalls(*bodies, filters.sync_do_slice, filters.sync_do_unique, filters.sync_do_groupby,
                       filters.async_select_or_reject, filters.prepare_select_or_reject, filters.prepare_map,
                       async_utils.auto_aiter, async_utils.auto_to_list, filters.make_attrgetter) as calls:
            output = session.render('case', source)
        self.assertEqual(calls.calls, [])
        self.assertEqual(output, environment.from_string(source).render(values))


if __name__ == '__main__':
    unittest.main()
