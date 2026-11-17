"""Check the filtered-iteration observations against the pinned reference."""
import unittest

import docxtpl
from paritylab.iteration_spec_probes import PROBES


class FilteredIterationReferenceTests(unittest.TestCase):
    def test_metadata_and_empty_branch_observations(self):
        for name in (
            "iteration_filter_last_lookahead",
            "iteration_filter_last_generator_error",
            "iteration_filter_length_after_body",
            "iteration_filter_nextitem_lookahead",
            "iteration_filter_length_consumption",
            "iteration_filter_empty_else",
        ):
            with self.subTest(name=name):
                probe = PROBES[name]
                self.assertEqual(probe.execute(docxtpl).observation, probe.expected)


if __name__ == "__main__":
    unittest.main()
