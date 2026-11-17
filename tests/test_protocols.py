import unittest
import docxtpl
from paritylab.protocols import PROBES,execute


class ReferenceContractTests(unittest.TestCase):
    def test_protocol_oracles_hold_for_reference(self):
        for name in PROBES:
            with self.subTest(protocol=name):
                execute(name,docxtpl,validate_reference=True)
