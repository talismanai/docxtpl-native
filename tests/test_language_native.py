"""Fast regression gate; full visual qualification remains in the differential suite."""
import unittest
import docxtpl_native
from native_audit import forbid
from paritylab.language_spec_probes import PROBES


@unittest.skipUnless(docxtpl_native.capabilities()['native_available'],
                     'native extension not built; this is not parity evidence')
class NativeLanguageTests(unittest.TestCase):
    def test_grammar_oracles_and_complete_packages_without_python_renderer(self):
        # Document equivalence with docxtpl: test_audited_documents_reference.
        for name, probe in PROBES.items():
            with self.subTest(case=name):
                with forbid('jinja2.Template.render', message='Python template executor called'):
                    native=probe.execute(docxtpl_native)
                self.assertEqual(native.observation,probe.expected)
                self.assertTrue(native.outputs)
