import unittest
from paritylab.spec_test_coverage import assess_tests


class SpecTestCoverageTests(unittest.TestCase):
    def entry(self, **changes):
        return {'id': 'test', 'cases': ['protocol/example'],
                'partition_cases': {'normal': ['protocol/example']},
                'partitions': {'covered': ['normal'], 'pending': []}, **changes}

    def assess(self, entry):
        return assess_tests({'contracts': [entry]}, {'protocol/example'})

    def test_missing_test_is_a_failure_even_with_review_flag(self):
        result = self.assess(self.entry(cases=[], reviewed=True))
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing_cases'], ['test'])

    def test_dangling_test_cannot_make_coverage_complete(self):
        result = self.assess(self.entry(cases=['protocol/nonexistent']))
        self.assertFalse(result['complete'])
        self.assertEqual(result['dangling_cases'], [{'contract': 'test', 'case': 'protocol/nonexistent'}])

    def test_normal_case_does_not_cover_pending_errors(self):
        self.assertFalse(self.assess(self.entry(partitions={'covered': ['normal'], 'pending': ['errors']}))['complete'])

    def test_empty_partition_decomposition_does_not_pass(self):
        self.assertFalse(self.assess(self.entry(partitions={'covered': [], 'pending': []}))['complete'])

    def test_claimed_partition_requires_its_own_executable_binding(self):
        result = self.assess(self.entry(partition_cases={'normal': ['protocol/nonexistent']}))
        self.assertFalse(result['complete'])
        self.assertEqual(result['unbound_covered_partitions'], [{'contract': 'test', 'partition': 'normal'}])

    def test_out_of_scope_partition_needs_a_recorded_decision(self):
        partitions = {'covered': ['normal'], 'pending': [], 'out_of_scope': ['internal']}
        result = self.assess(self.entry(partitions=partitions))
        self.assertFalse(result['complete'])
        self.assertEqual(result['undecided_out_of_scope_partitions'], [{'contract': 'test', 'partition': 'internal'}])
        result = self.assess(self.entry(partitions=partitions, partition_decisions={'internal': 'owner decision'}))
        self.assertTrue(result['complete'])
        self.assertEqual(result['out_of_scope_partitions'], [{'contract': 'test', 'partition': 'internal'}])

    def test_closed_enumerated_scope_is_distinct_from_global_claim(self):
        result = self.assess(self.entry())
        self.assertTrue(result['complete'])
        self.assertIn('enumerated catalog only', result['scope'])

    def test_manual_evidence_is_pending_without_failing_the_check(self):
        manual = {'id': 'word', 'cases': [], 'evidence': 'manual'}
        result = assess_tests({'contracts': [self.entry(), manual]}, {'protocol/example'})
        self.assertTrue(result['complete'])
        self.assertEqual(result['manual_evidence'], ['word'])
        self.assertEqual(result['missing_cases'], [])
        self.assertEqual(result['bound_contracts'], 1)

    def test_manual_evidence_binds_no_case(self):
        with self.assertRaises(ValueError):
            self.assess(self.entry(evidence='manual'))
