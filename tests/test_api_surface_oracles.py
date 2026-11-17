import unittest
import docxtpl
from paritylab.api_surface_probes import PROBES


class ApiSurfaceReferenceTests(unittest.TestCase):
    def test_direct_api_oracles(self):
        for name, probe in PROBES.items():
            with self.subTest(case=name):
                self.assertEqual(probe.execute(docxtpl).observation, probe.expected)
