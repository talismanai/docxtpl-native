"""Differential checks for already-mutated lxml C API registry at load time."""

import subprocess
import sys
import unittest


class XmlProviderInitializationTests(unittest.TestCase):
    def test_preexisting_registry_mutation_does_not_change_recovered_body(self) -> None:
        script = r'''
import io
import json
import sys
import zipfile
import docxtpl
import docxtpl_native
import jinja2
from lxml import etree
from docxtpl_native.bridge import BridgeConfig
from paritylab.compare import compare_docx
from paritylab.protocol_base import template

source = template('{{ value }}')
value = '<x a="A\ufffeB">Z</x>'
payload = json.dumps({'specials': [], 'replacements': {
    'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
}})
mode = sys.argv[1]
if mode == 'replace':
    etree.__pyx_capi__['rootNodeOrRaise'] = None
elif mode == 'delete':
    del etree.__pyx_capi__['rootNodeOrRaise']
elif mode == 'registry':
    etree.__pyx_capi__ = {}
else:
    etree.__pyx_capi__ = object()
reference = docxtpl.DocxTemplate(io.BytesIO(source))
reference.render({'value': value}, jinja_env=jinja2.Environment())
output = io.BytesIO()
reference.save(output)
candidate, _ = docxtpl_native._extension().process_python_docx(
    source, {'value': value}, BridgeConfig(jinja2.Environment(), False), payload, False,
)
comparison = compare_docx(output.getvalue(), bytes(candidate))
assert not comparison['equivalent']
assert comparison['reference_errors'] and comparison['candidate_errors']
with zipfile.ZipFile(output) as left, zipfile.ZipFile(io.BytesIO(candidate)) as right:
    assert left.namelist() == right.namelist()
    for name in left.namelist():
        assert left.read(name) == right.read(name), name
'''
        for mode in ('replace', 'delete', 'registry', 'nonmapping'):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, '-c', script, mode], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_unverified_replacement_type_is_not_used_as_provider(self) -> None:
        script = r'''
import io
import json
import docxtpl_native
import jinja2
from lxml import etree
from docxtpl_native.bridge import BridgeConfig
from paritylab.protocol_base import template

source = template('{{ value }}')
calls = []
class _Element:
    def __new__(cls):
        calls.append('constructed')
        return super().__new__(cls)
_Element.__module__ = 'lxml.etree'
etree._Element = _Element
etree.__pyx_capi__['rootNodeOrRaise'] = None
payload = json.dumps({'specials': [], 'replacements': {
    'media': {}, 'embedded': {}, 'zipname': {}, 'pictures': {},
}})
try:
    docxtpl_native._extension().process_python_docx(
        source, {'value': '<x a="A\ufffeB">Z</x>'},
        BridgeConfig(jinja2.Environment(), False), payload, False,
    )
except ValueError as error:
    assert 'PyCapsule' in str(error)
else:
    raise AssertionError('unverified provider was accepted')
assert calls == [], calls
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
