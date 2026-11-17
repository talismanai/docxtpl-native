"""Independent reference checks for async callback and iteration contracts."""
import unittest
import docxtpl
from paritylab.async_spec_probes import PROBES


class AsyncReferenceTests(unittest.TestCase):
    def test_async_partitions_match_reference(self):
        for name, probe in PROBES.items():
            with self.subTest(case=name):
                self.assertEqual(probe.execute(docxtpl).observation, probe.expected)


if __name__ == '__main__':
    unittest.main()
