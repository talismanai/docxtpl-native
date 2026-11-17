"""Lazy filtered loops on the native document path."""
import unittest

import docxtpl
import docxtpl_native
from paritylab.iteration_spec_probes import PROBES


class FilteredIterationNativeTests(unittest.TestCase):
    def test_filter_order_and_break_match_reference(self):
        for name in ("iteration_filter_lazy_order", "iteration_filter_break_consumption"):
            with self.subTest(name=name):
                probe = PROBES[name]
                reference = probe.execute(docxtpl).observation
                candidate = probe.execute(docxtpl_native).observation
                self.assertEqual(reference, probe.expected)
                self.assertEqual(candidate, reference)

    def test_existing_else_and_length_behavior_is_preserved(self):
        for name in ("iteration_filter_length_consumption", "iteration_filter_empty_else"):
            with self.subTest(name=name):
                probe = PROBES[name]
                self.assertEqual(probe.execute(docxtpl_native).observation, probe.expected)


    def test_generator_exception_keeps_body_and_error_order(self):
        probe = PROBES["iteration_filter_generator_error"]
        self.assertEqual(probe.execute(docxtpl).observation, probe.expected)
        self.assertEqual(probe.execute(docxtpl_native).observation, probe.expected)


if __name__ == "__main__":
    unittest.main()
