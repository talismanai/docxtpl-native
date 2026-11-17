"""Verify binary preservation when replacing a generated embedded DOCX."""
import io
import _struct
from contextlib import ExitStack
import datetime
from pathlib import Path
import struct
import tempfile
import time
import zipfile
import zlib
from unittest.mock import patch

from docx import Document
from docx.opc.packuri import PackURI
from docx.opc.part import Part

from .protocol_base import Probe, Result

TARGET='word/embeddings/payload.docx'
OTHER='word/embeddings/other.bin'
ORIGINAL=b'original embedded payload\x00\xff'
UNCHANGED=b'unrelated binary\x00\xfe'


def generated_input():
    document=Document();document.add_paragraph('inner embedded content')
    raw=io.BytesIO();document.save(raw)
    return fixed_archive(raw.getvalue(),zipfile.ZIP_STORED)


def fixed_archive(raw, compression):
    result=io.BytesIO()
    # Fixed per-entry metadata makes the input bytes identical in both workers.
    with zipfile.ZipFile(io.BytesIO(raw)) as source,zipfile.ZipFile(result,'w') as target:
        for name in sorted(source.namelist()):
            entry=zipfile.ZipInfo(name,(1980,1,1,0,0,0));entry.compress_type=compression
            target.writestr(entry,source.read(name))
    return result.getvalue()


def verify_payloads(document, replacement):
    with zipfile.ZipFile(io.BytesIO(document)) as archive:
        names=archive.namelist()
        if names.count(TARGET)!=1 or names.count(OTHER)!=1:
            return {'embedded_exact':False,'other_exact':False}
        return {'embedded_exact':archive.read(TARGET)==replacement,
                'other_exact':archive.read(OTHER)==UNCHANGED}


def embedded_payload_binary(engine):
    document=Document();document.add_paragraph('outer')
    for name,blob in [(TARGET,ORIGINAL),(OTHER,UNCHANGED)]:
        part=Part(PackURI('/'+name),'application/vnd.openxmlformats-officedocument.oleObject',blob,document.part.package)
        document.part.relate_to(part,'http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject')
    source=io.BytesIO();document.save(source);replacement=generated_input()
    with tempfile.TemporaryDirectory(prefix='embedded-probe-') as folder:
        original=Path(folder)/'original.bin';target=Path(folder)/'replacement.docx'
        original.write_bytes(ORIGINAL);target.write_bytes(replacement)
        template=engine.DocxTemplate(io.BytesIO(source.getvalue()))
        template.replace_embedded(str(original),str(target));template.render({})
        output=io.BytesIO();template.save(output);blob=output.getvalue()
    return Result(verify_payloads(blob,replacement),{'document.docx':blob})


def save_replacement_phases(engine):
    name = 'word/embeddings/replaced.bin'
    document = Document(); document.add_paragraph('Hello {{ name }}')
    part = Part(PackURI('/' + name), 'application/octet-stream', b'original', document.part.package)
    document.part.relate_to(part, 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject')
    source = io.BytesIO(); document.save(source)
    initial = []; saved_flags = []; results = []; clocks = []; positions = []; timestamps = []
    with tempfile.TemporaryDirectory(prefix='save-phases-') as folder:
        original = Path(folder) / 'original'; original.write_bytes(b'original')
        replacement = Path(folder) / 'replacement'; replacement.write_bytes(b'first')
        template = engine.DocxTemplate(io.BytesIO(source.getvalue()))
        template.replace_embedded(str(original), str(replacement)); template.render({'name': 'Ada'})
        postprocess = template.post_processing
        def observed_postprocess(target):
            with zipfile.ZipFile(io.BytesIO(target.getvalue())) as archive:
                initial.append(archive.read(name).decode())
            saved_flags.append(template.is_saved)
            postprocess(target)
        template.post_processing = observed_postprocess
        for payload in [b'first', b'second']:
            replacement.write_bytes(payload); template.replace_embedded(str(original), str(replacement))
            output = io.BytesIO()
            with patch('zipfile.time.time', return_value=1700000000) as clock:
                template.save(output)
            blob = output.getvalue(); positions.append(output.tell())
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                results.append(archive.read(name).decode())
                clocks.append(clock.call_count == 2 * len(archive.infolist()))
                timestamps.append(all(info.date_time == time.localtime(1700000000)[:6] for info in archive.infolist()))
        retained = next(rel.target_part.blob for rel in template.docx.part.rels.values()
                        if not rel.is_external and str(rel.target_part.partname) == '/' + name)
    return Result({'before_postprocessing': initial, 'saved_flags': saved_flags, 'replacements': results,
                   'two_save_clocks': clocks, 'stream_positions': positions, 'timestamps_equal': timestamps,
                   'retained_original': retained == b'original'}, {'document.docx': blob})


def generated_zip_properties(blob, now):
    """Inspect one entry's metadata and exact raw deflate stream."""
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        entry=archive.getinfo('word/document.xml')
        timestamp=datetime.datetime(*entry.date_time)
        near_now=abs((timestamp-now).total_seconds())<300
        # python-docx saves via ZipFile.writestr using ZipInfo's default mode.
        permissions=entry.external_attr >> 16
        header=entry.header_offset
        name_len,extra_len=struct.unpack_from('<HH',blob,header+26)
        start=header+30+name_len+extra_len
        compressed=blob[start:start+entry.compress_size]
        compressor=zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION,zlib.DEFLATED,-15)
        expected=compressor.compress(archive.read(entry))+compressor.flush()
        return {'timestamp_current':near_now,'default_permissions':permissions==0o600,
                'reference_deflate':entry.compress_type==zipfile.ZIP_DEFLATED and compressed==expected}


def save_clock_failure_cursor(engine):
    source = generated_input()
    with zipfile.ZipFile(io.BytesIO(source)) as archive: count = len(archive.infolist())
    identities = []; clocks = []; positions = []; saved = []; output_blob = None
    for direct in [False, True]:
        template = engine.DocxTemplate(io.BytesIO(source)); template.render({})
        template.zipname_to_replace['unused'] = b'x'
        target = io.BytesIO(source if direct else b''); failure = ValueError('clock failed'); calls = 0
        fail_at = 2 if direct else count + 2
        def clock():
            nonlocal calls
            calls += 1
            if calls == fail_at: raise failure
            return 1700000000
        with patch('zipfile.time.time', side_effect=clock):
            try: (template.post_processing if direct else template.save)(target)
            except ValueError as error: identities.append(error is failure)
            else: raise AssertionError('clock failure was not raised')
        blob = target.getvalue(); position = target.tell()
        # Independent physical-loader oracle, applied to the unchanged output.
        loaded = io.BytesIO(blob); Document(loaded)
        positions.append(position == loaded.tell()); clocks.append(calls == fail_at); saved.append(template.is_saved)
        if not direct: output_blob = blob
        failure.__traceback__ = None
    return Result({'same_exception': identities, 'clock_counts_match': clocks,
                   'load_cursors_match': positions, 'saved_flags': saved}, {'document.docx': output_blob})


def generated_zip_observation(engine, field):
    # An old input timestamp distinguishes preserving the input metadata from
    # the reference's fresh timestamp at save time.
    document=Document();document.add_paragraph('archive {{ value }}')
    source=io.BytesIO();document.save(source)
    template=engine.DocxTemplate(io.BytesIO(fixed_archive(source.getvalue(),zipfile.ZIP_DEFLATED)))
    template.render({'value':'metadata'})
    output=io.BytesIO();template.save(output)
    return Result({field:generated_zip_properties(output.getvalue(),datetime.datetime.now())[field]}, {})


def generated_zip_partial_clock_failure(engine):
    document=Document();document.add_paragraph('archive {{ value }}')
    source=io.BytesIO();document.save(source)
    template=engine.DocxTemplate(io.BytesIO(fixed_archive(source.getvalue(),zipfile.ZIP_DEFLATED)))
    template.render({'value':'metadata'});output=io.BytesIO();failure=ValueError('clock failure')
    calls=0
    def clock():
        nonlocal calls
        calls+=1
        if calls==3:raise failure
        return 1704067200
    with patch('zipfile.time.time',side_effect=clock):
        try:template.save(output)
        except ValueError as error:same=error is failure
        else:raise AssertionError('clock failure was not raised')
    blob=output.getvalue()
    try:
        with zipfile.ZipFile(io.BytesIO(blob)):valid=True
    except zipfile.BadZipFile:valid=False
    observation={'same_instance':same,'clock_calls':calls,'valid_zip':valid,
                 'has_partial_local_header':blob.startswith(b'PK\x03\x04')}
    # Observe the failure first, then release its writer while the destination
    # is still alive. Later GC probes must not finalize this unrelated save.
    failure.__traceback__=None
    return Result(observation,{})


def zip_timestamp_error_oracle():
    try:
        struct.pack('<H', 65536)
    except struct.error as error:
        out_of_range = str(error)
    else:
        raise AssertionError('struct unexpectedly accepted an out-of-range unsigned short')
    return {'errors': [
        ['builtins.ValueError', 'ZIP does not support timestamps before 1980'],
        ['struct.error', out_of_range], ['struct.error', out_of_range],
        ['struct.error', out_of_range],
    ]}


def generated_zip_invalid_timestamp(engine):
    errors=[]
    cases=[((1979,1,1,0,0,0),False),((2108,1,1,0,0,0),False),
           ((2024,1,1,0,0,-1),False),((2108,1,1,0,0,0),True)]
    for date,rebind in cases:
        template=engine.DocxTemplate(io.BytesIO(generated_input()))
        template.render({})
        with patch('zipfile.time.localtime',return_value=date), ExitStack() as stack:
            if rebind:
                stack.enter_context(patch.object(struct,'error',ValueError))
                stack.enter_context(patch.object(_struct,'error',ValueError))
            try:template.save(io.BytesIO())
            except Exception as error:errors.append([type(error).__module__+'.'+type(error).__qualname__,str(error)])
            else:raise AssertionError('invalid ZIP timestamp was accepted')
    return Result({'errors':errors},{})


PROBES={
    'save_clock_failure_cursor':Probe(save_clock_failure_cursor,{'same_exception':[True,True],
        'clock_counts_match':[True,True],'load_cursors_match':[True,True],'saved_flags':[False,False]}),
    'save_replacement_phases':Probe(save_replacement_phases,{
        'before_postprocessing':['original','original'],'saved_flags':[False,True],
        'replacements':['first','second'],'two_save_clocks':[True,True],
        'stream_positions':[0,0],'timestamps_equal':[True,True],'retained_original':True}),
    'embedded_payload_binary':Probe(embedded_payload_binary,{'embedded_exact':True,'other_exact':True}),
    'generated_zip_metadata':Probe(lambda engine:generated_zip_observation(engine,'timestamp_current'),{'timestamp_current':True},True),
    'generated_zip_permissions':Probe(lambda engine:generated_zip_observation(engine,'default_permissions'),{'default_permissions':True},True),
    'generated_zip_deflate':Probe(lambda engine:generated_zip_observation(engine,'reference_deflate'),{'reference_deflate':True},True),
    'generated_zip_invalid_timestamp':Probe(generated_zip_invalid_timestamp,zip_timestamp_error_oracle(),True),
    'generated_zip_partial_clock_failure':Probe(generated_zip_partial_clock_failure,
        {'same_instance':True,'clock_calls':3,'valid_zip':False,'has_partial_local_header':True},True),
}
