"""Identify the pinned reference by source bytes, not its version label."""
import hashlib
from pathlib import Path

REFERENCE_COMMIT = '1f143fbe86c19ecb28c3205d5f4b1547c7e2d7ad'
EXPECTED = {
    "__init__.py": "43d36b83ee320341d2afddbb6f7a6dc96ead440a4a215519686e8a0746753b26",
    "__main__.py": "6a898b831b0c7ff845a218c1ecd99d63ef433287678dccc7acdb9d821ca3724e",
    "inline_image.py": "7b0f24180bbccfeaec2cdcda3dba053acdcb9ad3e67b783f7ca9c8767da2f278",
    "listing.py": "328b6cdcf2caf68859a1a2ab903c0bc34d93a1d2397fa8768504be2c37d468c4",
    "richtext.py": "ae623f0e40cd9f176e10a6689ba791be218fde1883274d5786b98c0db3dd29bb",
    "subdoc.py": "a20872bb6a03f06110e3237b0b81378faa5fc26d202ac94e2a75e7984c223478",
    "template.py": "05c18c6f7856e833e978fa3548c9ec0ea8da8cbeeb52f38ef245a23d9f96a853"
}


def fingerprint(root, expected=EXPECTED):
    root = Path(root)
    actual = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in root.rglob('*.py') if path.is_file()}
    return {'commit': REFERENCE_COMMIT, 'expected_files': len(expected),
            'matched': actual == expected, 'files': actual}


def installed_reference():
    import docxtpl
    module = Path(docxtpl.__file__).resolve()
    result = fingerprint(module.parent)
    if not result['matched']:
        raise RuntimeError('installed docxtpl source does not match the pinned reference commit')
    return result
