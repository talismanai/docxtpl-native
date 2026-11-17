"""Renderer diagnostics must not turn incomplete output into visual parity."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from paritylab import visual


class VisualRendererDiagnosticTests(unittest.TestCase):
    def compare_with_diagnostic(self, diagnostic: str) -> dict[str, object]:
        def render(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
            output = Path(command[command.index('--outdir') + 1])
            source = Path(command[-1])
            document = fitz.open()
            try:
                document.new_page().insert_text((72, 72), 'same pixels')
                document.save(output / (source.stem + '.pdf'))
            finally:
                document.close()
            return subprocess.CompletedProcess(command, 0, stdout='', stderr=diagnostic)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.docx'
            source.write_bytes(b'synthetic input')
            with patch.object(visual, 'run_renderer', render):
                return visual.compare_visual(source, source, root / 'output')

    def test_success_exit_with_png_error_is_not_visual_parity(self) -> None:
        with self.assertRaisesRegex(RuntimeError, 'libpng error: IDAT: CRC error'):
            self.compare_with_diagnostic('libpng error: IDAT: CRC error\n')

    def test_nonfatal_font_warning_does_not_reject_output(self) -> None:
        result = self.compare_with_diagnostic('Fontconfig warning: no cachedir elements found\n')
        self.assertTrue(result['equal'])

    def test_two_empty_pdf_results_are_not_visual_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.docx'
            with patch.object(visual, '_render', return_value=[]), \
                 self.assertRaisesRegex(RuntimeError, 'zero pages'):
                visual.compare_visual(source, source, root / 'output')


if __name__ == '__main__':
    unittest.main()
