"""Check that synthetic image fixtures can be decoded, including their CRCs."""

import ast
import base64
import io
import unittest
from pathlib import Path

from PIL import Image


class SyntheticImageIntegrityTests(unittest.TestCase):
    def test_wheel_and_consumer_png_fixtures_decode(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name in ('check_clean_wheel_case.py', 'check_clean_wheel_specials.py', 'check_rust_consumer_case.py'):
            with self.subTest(script=name):
                tree = ast.parse((root / 'scripts' / name).read_text())
                literals = [node.args[0].value for node in ast.walk(tree)
                            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name) and node.func.value.id == 'base64'
                            and node.func.attr == 'b64decode' and len(node.args) == 1
                            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)]
                self.assertEqual(len(literals), 1)
                data = base64.b64decode(literals[0], validate=True)
                with Image.open(io.BytesIO(data)) as image:
                    self.assertEqual(image.format, 'PNG')
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    self.assertEqual(image.size, (1, 1))


if __name__ == '__main__':
    unittest.main()
