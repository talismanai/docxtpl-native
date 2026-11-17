"""Independent reference checks for extension-generated syntax and nodes."""
import unittest
import docxtpl
from paritylab.extension_spec_probes import PROBES


class ExtensionReferenceTests(unittest.TestCase):
    def test_extension_partitions_match_reference(self):
        for name, probe in PROBES.items():
            with self.subTest(case=name):
                self.assertEqual(probe.execute(docxtpl).observation, probe.expected)


if __name__ == '__main__':
    unittest.main()
