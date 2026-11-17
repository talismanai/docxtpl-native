import tempfile
import unittest
from pathlib import Path
from paritylab.upstream_adapters import execute

class AdapterTests(unittest.TestCase):
    def test_changed_upstream_source_cannot_use_the_assertion_adapter(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'get_undeclared_variables.py'
            source.write_text('def test_before_render():return True\n')
            with self.assertRaisesRegex(AssertionError,'adapter source changed'):
                execute(source,'docxtpl',Path(temporary))
