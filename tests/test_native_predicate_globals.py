"""Runtime predicate globals remain observable without Python predicate bodies."""
from unittest.mock import patch
import unittest
import docxtpl
import docxtpl_native
import jinja2.tests as predicates
from paritylab.compare import compare_docx
from paritylab.predicate_global_probes import PROBES, NAMES, CLASSES
from test_native_predicate_keywords import run, native_predicates_only


class PredicateGlobalTests(unittest.TestCase):
    def test_live_globals_return_identity_and_short_circuit(self):
        probe = PROBES['native_predicate_globals']
        reference = probe.execute(docxtpl)
        self.assertEqual(reference.observation, probe.expected)
        with native_predicates_only(): native = probe.execute(docxtpl_native)
        self.assertEqual(native.observation, probe.expected)
        for filename, blob in native.outputs.items():
            self.assertTrue(compare_docx(reference.outputs[filename], blob)['equivalent'])

    def test_original_check_and_truth_errors_keep_identity(self):
        for name in NAMES:
            for phase in ('call', 'truth'):
                def execute(engine):
                    trace = []
                    failure = ValueError('predicate failure')
                    class Verdict:
                        def __bool__(self):
                            trace.append('truth')
                            raise failure
                        def __str__(self): return 'verdict'
                    def check(value, kind):
                        trace.append('check')
                        if phase == 'call': raise failure
                        return Verdict()
                    with patch.object(predicates, 'isinstance', check, create=True):
                        return run(engine, '{{ value is '+name+' }}', {'value': 41}, trace, failure=failure)
                with self.subTest(name=name, phase=phase):
                    expected, reference = execute(docxtpl)
                    actual, native = execute(docxtpl_native)
                    self.assertEqual(actual, expected)
                    if phase == 'call' or name in ('defined', 'integer'):
                        self.assertEqual(expected['error'], ['ValueError', 'predicate failure', True])
                    else:
                        self.assertIsNone(expected['error'])
                        self.assertTrue(compare_docx(reference, native)['equivalent'])

    def test_default_isinstance_uses_live_type_alias(self):
        for name in NAMES:
            def execute(engine):
                with patch.object(predicates, CLASSES[name], str, create=True):
                    return run(engine, '{{ value is '+name+' }}', {'value': 'text'}, [])
            with self.subTest(name=name):
                expected, reference = execute(docxtpl)
                actual, native = execute(docxtpl_native)
                self.assertEqual(actual, expected)
                self.assertTrue(compare_docx(reference, native)['equivalent'])

    def test_keyword_error_precedes_the_changed_global(self):
        def execute(engine):
            trace = []
            original = predicates.__dict__.get('isinstance')
            present = 'isinstance' in predicates.__dict__
            def check(value, kind):
                trace.append('new check')
                return True
            class Key(str):
                __hash__ = str.__hash__
                def __eq__(self, other):
                    trace.append(['equal', other])
                    predicates.isinstance = check
                    return str.__eq__(self, other)
            # The first positional value is compulsory; bind by equality to an
            # unknown name and fail first, proving no predicate body executes.
            try:
                return run(engine, '{{ value is float(**kwargs) }}',
                           {'value': 41, 'kwargs': {Key('unknown'): 42}}, trace)
            finally:
                if present: predicates.isinstance = original
                else: predicates.__dict__.pop('isinstance', None)
        expected, _ = execute(docxtpl)
        actual, _ = execute(docxtpl_native)
        self.assertEqual(actual, expected)
        self.assertEqual(expected['trace'], [['equal', 'value']])


if __name__ == '__main__': unittest.main()
