"""Mutation checks for the offline root-symbol differential comparator."""

import subprocess
import unittest
from unittest.mock import patch

from scripts import check_root_symbol_plan as checker


class RootSymbolComparatorMutationTests(unittest.TestCase):
    def test_detects_reordering_wrong_kind_and_missing_binding(self) -> None:
        source = '{{ alpha }}{{ beta }}'
        expected = checker.reference(source)
        self.assertEqual(expected, ['resolve:alpha', 'resolve:beta'])
        with patch.object(checker, 'candidate', return_value=expected):
            self.assertIsNone(checker.compare_case('unused', source))
        mutants = [
            ['resolve:beta', 'resolve:alpha'],
            ['resolve:alpha', 'missing:beta'],
            ['resolve:alpha'],
        ]
        for mutant in mutants:
            with self.subTest(mutant=mutant), patch.object(checker, 'candidate', return_value=mutant):
                self.assertEqual(checker.compare_case('unused', source), (expected, mutant))

    def test_candidate_parser_failure_is_a_mismatch(self) -> None:
        failure = subprocess.CalledProcessError(2, 'probe', stderr='syntax rejected')
        with patch.object(checker, 'candidate', side_effect=failure):
            self.assertEqual(checker.compare_case('unused', '{{ alpha }}'),
                             (['resolve:alpha'], 'candidate parser error: syntax rejected'))

    def test_unavailable_candidate_is_a_mismatch(self) -> None:
        with patch.object(checker, 'candidate', side_effect=OSError('probe unavailable')):
            self.assertEqual(checker.compare_case('unused', '{{ alpha }}'),
                             (['resolve:alpha'], 'candidate execution error: probe unavailable'))

    def test_unknown_reference_initialization_is_not_silently_ignored(self) -> None:
        compiled = '    if 0: yield None\n    l_0_alpha = unfamiliar()\n    pass\n'
        with (patch.object(checker.ENV, 'compile', return_value=compiled),
              self.assertRaisesRegex(ValueError, 'unrecognized root initialization')):
            checker.reference('{{ alpha }}')

    def test_reference_target_resolving_another_name_is_not_normalized(self) -> None:
        compiled = "    if 0: yield None\n    l_0_alpha = resolve('beta')\n    pass\n"
        with (patch.object(checker.ENV, 'compile', return_value=compiled),
              self.assertRaisesRegex(ValueError, 'resolves a different name')):
            checker.reference('{{ alpha }}')


if __name__ == '__main__':
    unittest.main()
