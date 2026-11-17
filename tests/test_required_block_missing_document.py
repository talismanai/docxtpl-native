"""A context callback may remove a required block during document rendering."""

from __future__ import annotations

import unittest
from typing import Any

import docxtpl
import docxtpl_native
import jinja2
from jinja2 import runtime

from paritylab.protocol_base import generate, template


class MissingRequiredBlockDocumentTests(unittest.TestCase):
    def test_removed_required_block_keeps_key_error(self) -> None:
        source = template('{% block body required %}{% endblock %}')

        class Context(runtime.Context):
            def __init__(self, environment: Any, parent: Any, name: Any, blocks: Any,
                         globals: Any = None) -> None:
                super().__init__(environment, parent, name, blocks, globals=globals)
                self.blocks.pop('body', None)

        for asynchronous in (False, True):
            for engine in (docxtpl, docxtpl_native):
                with self.subTest(asynchronous=asynchronous, engine=engine.__name__):
                    environment = jinja2.Environment(enable_async=asynchronous)
                    environment.context_class = Context
                    with self.assertRaises(KeyError) as raised:
                        generate(engine, '', {}, environment, source=source)
                    self.assertEqual(raised.exception.args, ('body',))


if __name__ == '__main__':
    unittest.main()
