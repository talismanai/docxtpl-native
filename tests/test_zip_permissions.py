"""ZIP attributes of DOCX outputs match the Python package writer."""
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from docx import Document
import docxtpl
import docxtpl_native


class ZipPermissionTests(unittest.TestCase):
    def run_case(self, engine, mode):
        document=Document();document.add_paragraph('Café {{ name }}')
        source=io.BytesIO();document.save(source)
        template=engine.DocxTemplate(io.BytesIO(source.getvalue()))
        if mode=='render':template.render({'name':'Ada'})
        if mode=='live':
            template.get_docx()
            template.render({'name':'Ada'})
        output=io.BytesIO();template.save(output)
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
            attributes={entry.filename:entry.external_attr for entry in archive.infolist()}
            self.assertTrue(attributes)
            return attributes

    def test_attributes_match_for_render_and_save_paths(self):
        for mode in ('render','live','save'):
            with self.subTest(mode=mode):self.assertEqual(self.run_case(docxtpl_native,mode),self.run_case(docxtpl,mode))

    def test_postprocessing_attributes_match_for_stream_and_file(self):
        source=io.BytesIO();Document().save(source)
        def run(engine, mode, directory):
            template=engine.DocxTemplate(io.BytesIO(source.getvalue()));template.render({})
            target=io.BytesIO();template.save(target)
            # A path rewrite preserves preexisting metadata, while a stream
            # first rebuilds the OPC package with the reference defaults.
            custom=io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(target.getvalue())) as original,zipfile.ZipFile(custom,'w') as output:
                for entry in original.infolist():
                    content=original.read(entry)
                    entry.external_attr=0o100755<<16
                    entry.create_system=0
                    entry.create_version=30
                    output.writestr(entry,content)
            replacement=Path(directory)/'replacement.bin';replacement.write_bytes(b'new')
            template.replace_zipname('docProps/app.xml',str(replacement))
            if mode=='file':
                target=Path(directory)/(engine.__name__+'.docx');target.write_bytes(custom.getvalue())
            else:target=io.BytesIO(custom.getvalue())
            template.post_processing(target)
            blob=target.read_bytes() if mode=='file' else target.getvalue()
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                self.assertEqual(archive.read('docProps/app.xml'),b'new')
                return {entry.filename:(entry.external_attr,entry.create_system,entry.create_version)
                        for entry in archive.infolist()}
        with tempfile.TemporaryDirectory() as directory:
            for mode in ('stream','file'):
                with self.subTest(mode=mode):self.assertEqual(run(docxtpl_native,mode,directory),run(docxtpl,mode,directory))

    def test_partial_postprocessing_keeps_prior_entry_attributes(self):
        source=io.BytesIO();Document().save(source)
        def run(engine, mode, directory):
            failure=ValueError('selection failed')
            class FailingMap(dict):
                calls=0
                def __contains__(self, key):
                    self.calls+=1
                    if self.calls==3:raise failure
                    return False
            template=engine.DocxTemplate(io.BytesIO(source.getvalue()));template.render({})
            output=io.BytesIO();template.save(output)
            template.zipname_to_replace=FailingMap({'activate':b'x'})
            if mode=='file':
                target=Path(directory)/(engine.__name__+'.docx');target.write_bytes(output.getvalue())
            else:target=io.BytesIO(output.getvalue())
            with self.assertRaises(ValueError) as caught:template.post_processing(target)
            self.assertIs(caught.exception,failure)
            blob=target.read_bytes() if mode=='file' else target.getvalue()
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                return [(item.filename,item.external_attr,archive.read(item)) for item in archive.infolist()]
        with tempfile.TemporaryDirectory() as directory:
            for mode in ('stream','file'):
                with self.subTest(mode=mode):self.assertEqual(run(docxtpl_native,mode,directory),run(docxtpl,mode,directory))


if __name__=='__main__':unittest.main()
