"""Fresh ZIP timestamps are observed when a document is saved."""
from contextlib import nullcontext
import io
import datetime
import struct
import unittest
from unittest.mock import patch
import zipfile

from docx import Document
import docxtpl
import docxtpl_native
from paritylab.embedded_probes import fixed_archive


class ZipTimestampTests(unittest.TestCase):
    def make_template(self, engine):
        document=Document();document.add_paragraph('Hello {{ name }}')
        source=io.BytesIO();document.save(source)
        template=engine.DocxTemplate(io.BytesIO(fixed_archive(source.getvalue(),zipfile.ZIP_DEFLATED)))
        template.render({'name':'Ada'})
        return template

    def test_save_refreshes_old_input_timestamps(self):
        def run(engine):
            template=self.make_template(engine);output=io.BytesIO()
            template.save(output)
            now=datetime.datetime.now()
            with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
                dates=[]
                for item in archive.infolist():
                    year,month,day,hour,minute,second=item.date_time
                    local_time,local_date=struct.unpack_from('<HH',output.getvalue(),item.header_offset+10)
                    self.assertEqual(local_time,(hour<<11)|(minute<<5)|(second//2))
                    self.assertEqual(local_date,((year-1980)<<9)|(month<<5)|day)
                    dates.append((item.filename,abs((now-datetime.datetime(*item.date_time)).total_seconds())<300))
            return dates
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_custom_clock_order_and_values_match(self):
        def run(engine):
            template=self.make_template(engine);output=io.BytesIO()
            values=iter(1704067200+2*i for i in range(1000))
            with patch('zipfile.time.time',side_effect=lambda:next(values)) as clock:
                template.save(output)
            with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
                dates=[(item.filename,item.date_time) for item in archive.infolist()]
            return dates,clock.call_count
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_clock_error_identity_is_preserved(self):
        failure=ValueError('clock failure')
        template=self.make_template(docxtpl_native)
        with patch('zipfile.time.time',side_effect=failure):
            with self.assertRaises(ValueError) as actual:template.save(io.BytesIO())
        self.assertIs(actual.exception,failure)

    def test_clock_failure_exposes_the_same_partial_stream_state(self):
        def run(engine):
            template=self.make_template(engine);output=io.BytesIO();failure=ValueError('clock failure');seen=[]
            def clock():
                seen.append(bool(output.getvalue()))
                if len(seen)==3:raise failure
                return 1704067200
            with patch('zipfile.time.time',side_effect=clock):
                with self.assertRaises(ValueError) as actual:template.save(output)
            self.assertIs(actual.exception,failure)
            return seen,output.getvalue().startswith(b'PK\x03\x04')
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_clock_callback_observes_previous_entries_during_success(self):
        def run(engine):
            template=self.make_template(engine);output=io.BytesIO();seen=[]
            def clock():seen.append(bool(output.getvalue()));return 1704067200
            with patch('zipfile.time.time',side_effect=clock):template.save(output)
            with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
                self.assertEqual(len(archive.infolist()),len(seen))
                self.assertIn('Hello Ada',archive.read('word/document.xml').decode())
            return seen
        self.assertEqual(run(docxtpl_native),run(docxtpl))

    def test_clock_installing_stream_method_is_explicitly_unsupported(self):
        template=self.make_template(docxtpl_native);output=io.BytesIO();calls=[]
        def clock():
            output.write=lambda data:calls.append(data)
            return 1704067200
        with patch('zipfile.time.time',side_effect=clock):
            with self.assertRaises(docxtpl_native.UnsupportedFeature):template.save(output)
        self.assertEqual(calls,[])

    def test_unrelated_stream_attributes_do_not_block_clock_callbacks(self):
        template=self.make_template(docxtpl_native);output=io.BytesIO()
        def clock():output.label='document';return 1704067200
        with patch('zipfile.time.time',side_effect=clock):template.save(output)
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
            self.assertIn('Hello Ada',archive.read('word/document.xml').decode())

    def test_out_of_range_clock_errors_match(self):
        def run(engine, date):
            template=self.make_template(engine)
            with patch('zipfile.time.localtime',return_value=date):
                try:template.save(io.BytesIO())
                except Exception as error:return type(error),str(error)
            self.fail('expected an invalid ZIP timestamp error')
        for date in ((1979,1,1,0,0,0),(2108,1,1,0,0,0),(2024,1,1,0,0,-1)):
            with self.subTest(date=date):self.assertEqual(run(docxtpl_native,date),run(docxtpl,date))

    def test_rebinding_struct_error_keeps_the_original_exception_type(self):
        import _struct
        import struct
        original_error = struct.error
        def run(engine, rebind_native):
            template = self.make_template(engine)
            with patch('zipfile.time.localtime', return_value=(2108,1,1,0,0,0)):
                with patch.object(struct, 'error', ValueError):
                    native = patch.object(_struct, 'error', ValueError) if rebind_native else nullcontext()
                    with native:
                        try:template.save(io.BytesIO())
                        except Exception as error:return type(error),str(error)
            self.fail('expected an invalid ZIP timestamp error')
        for rebind_native in (False, True):
            with self.subTest(rebind_native=rebind_native):
                expected = run(docxtpl, rebind_native)
                self.assertIs(expected[0], original_error)
                self.assertEqual(run(docxtpl_native, rebind_native), expected)

    def test_custom_struct_pack_is_explicitly_unsupported_on_timestamp_error(self):
        for target in ('zipfile.struct.pack', '_struct.pack'):
            with self.subTest(target=target):
                template = self.make_template(docxtpl_native)
                calls = []
                def custom_pack(*args):
                    calls.append(args)
                    raise AssertionError('custom pack must not run on unsupported path')
                with patch('zipfile.time.localtime', return_value=(2108,1,1,0,0,0)):
                    with patch(target, custom_pack):
                        with self.assertRaises(docxtpl_native.UnsupportedFeature):template.save(io.BytesIO())
                self.assertEqual(calls, [])

    def test_clock_integer_fields_follow_zip_bit_packing(self):
        def run(engine, date):
            template=self.make_template(engine);output=io.BytesIO()
            with patch('zipfile.time.localtime',return_value=date):template.save(output)
            with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
                return [entry.date_time for entry in archive.infolist()]
        for date in ((2024,0,0,0,0,0),(2024,True,1,0,0,0)):
            with self.subTest(date=date):self.assertEqual(run(docxtpl_native,date),run(docxtpl,date))


if __name__=='__main__':unittest.main()
