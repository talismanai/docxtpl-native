"""ZIP failure state before and after the failed call's traceback is released."""
import gc
import io
import time
from unittest.mock import patch
import zipfile
from .protocol_base import Probe, Result, template


def zip_failure_finalization(engine):
    source = template('Hello {{ name }}')
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        names = archive.namelist()
    observations = []
    for fail_at in (1, 2, len(names)):
        document = engine.DocxTemplate(io.BytesIO(source))
        document.render({'name': 'Ada'})
        target = io.BytesIO()
        failure = ValueError('clock failed')
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls == fail_at:
                raise failure
            return 1700000000
        original = False
        with patch('zipfile.time.time', side_effect=clock):
            try:
                document.save(target)
            except ValueError as error:
                original = error is failure
        before = target.getvalue()
        traceback = failure.__traceback__
        failure.__traceback__ = None
        gc.collect()
        deferred = target.getvalue() == before
        del traceback
        gc.collect()
        after = target.getvalue()
        try:
            with zipfile.ZipFile(io.BytesIO(after)) as archive:
                valid_prefix = archive.namelist() == names[:fail_at - 1] and archive.testzip() is None
                metadata = all(info.date_time == time.localtime(1700000000)[:6]
                               and info.external_attr == 0o600 << 16 for info in archive.infolist())
        except zipfile.BadZipFile:
            valid_prefix = metadata = False
        observations.append({'original_error': original, 'clock_count': calls == fail_at,
                             'deferred': deferred, 'completed_on_release': len(after) > len(before),
                             'valid_prefix': valid_prefix, 'metadata': metadata,
                             'cursor_at_end': target.tell() == len(after), 'saved': document.is_saved})
    return Result({'observations': observations}, {})


PROBES = {
    'generated_zip_failure_finalization': Probe(zip_failure_finalization, {
        'observations': [{'original_error': True, 'clock_count': True, 'deferred': True,
                          'completed_on_release': True, 'valid_prefix': True, 'metadata': True,
                          'cursor_at_end': True, 'saved': False} for _ in range(3)]
    }, assertion_only=True),
}
