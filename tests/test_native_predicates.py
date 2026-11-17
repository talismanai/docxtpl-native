"""Boundary parity plus proof that default runtime predicates avoid Python helpers."""
import sys
import unittest
import docxtpl_native
from jinja2.defaults import DEFAULT_TESTS
from native_audit import forbid
from paritylab.native_predicate_probes import PROBES, NAMES


@unittest.skipUnless(docxtpl_native.capabilities()['native_available'], 'native build required; not parity evidence')
class NativePredicateTests(unittest.TestCase):
    def test_boundaries_errors_overrides_and_complete_packages(self):
        # The reference oracles and document equivalence: test_audited_documents_reference.
        for name, probe in PROBES.items():
            with self.subTest(case=name):
                with forbid('jinja2.Template.render', message='Python executor'):
                    native = probe.execute(docxtpl_native)
                self.assertEqual(native.observation, probe.expected)

    def test_runtime_predicates_do_not_execute_reference_implementations(self):
        codes = {DEFAULT_TESTS[name].__code__ for name in NAMES}
        calls = []
        def profile(frame, event, arg):
            if event == 'call' and frame.f_code in codes:
                calls.append(frame.f_code.co_name)
        previous = sys.getprofile()
        try:
            sys.setprofile(profile)
            for name in ('native_predicate_types', 'native_predicate_identity', 'native_predicate_undefined'):
                result = PROBES[name].execute(docxtpl_native)
                self.assertEqual(result.observation, PROBES[name].expected)
        finally:
            sys.setprofile(previous)
        self.assertEqual(calls, [], 'builtin Python predicate executed')
