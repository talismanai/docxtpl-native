"""Mutation checks for the embedded-payload provenance probe."""
import io
import datetime
import unittest
import warnings
import zipfile

from paritylab.embedded_probes import OTHER, TARGET, UNCHANGED, generated_input, generated_zip_properties, verify_payloads


def container(target, other=UNCHANGED, duplicate=False):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:
        archive.writestr(TARGET,target);archive.writestr(OTHER,other)
        if duplicate:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning)
                archive.writestr(TARGET,target)
    return out.getvalue()


class EmbeddedPayloadProbeTests(unittest.TestCase):
    def test_detects_embedded_and_unrelated_binary_mutations(self):
        replacement=generated_input()
        self.assertEqual(verify_payloads(container(replacement),replacement),{'embedded_exact':True,'other_exact':True})
        self.assertFalse(verify_payloads(container(replacement+b'changed'),replacement)['embedded_exact'])
        self.assertFalse(verify_payloads(container(replacement,other=b'changed'),replacement)['other_exact'])
        self.assertFalse(verify_payloads(container(replacement,duplicate=True),replacement)['embedded_exact'])

    def test_invalid_timestamp_oracle_rejects_wrong_exception(self):
        from paritylab.protocols import execute
        class WrongTemplate:
            def __init__(self, source): pass
            def render(self, context): pass
            def save(self, output): raise ValueError('incorrect error')
        class WrongEngine:
            DocxTemplate = WrongTemplate
        with self.assertRaisesRegex(AssertionError, 'reference contract changed'):
            execute('generated_zip_invalid_timestamp', WrongEngine, validate_reference=True)

    def test_zip_property_checks_detect_mutations(self):
        now=datetime.datetime(2024,1,1)
        def archive(*,timestamp=(2024,1,1,0,0,0),permissions=0o600,level=6):
            entry=zipfile.ZipInfo('word/document.xml',timestamp)
            entry.external_attr=permissions<<16
            entry.compress_type=zipfile.ZIP_DEFLATED
            entry._compresslevel=level
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w') as writer:
                writer.writestr(entry,b'<xml>'+b'x'*10000+b'</xml>')
            return generated_zip_properties(output.getvalue(),now)
        self.assertEqual(archive(),{'timestamp_current':True,'default_permissions':True,'reference_deflate':True})
        self.assertFalse(archive(timestamp=(1980,1,1,0,0,0))['timestamp_current'])
        self.assertFalse(archive(permissions=0o100644)['default_permissions'])
        self.assertFalse(archive(level=1)['reference_deflate'])


if __name__=='__main__':unittest.main()
