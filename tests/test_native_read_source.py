"""Source-loading behavior for paths, bytes, streams and callbacks."""
import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import docxtpl_native
from docxtpl_native import _read


class NativeReadSourceTests(unittest.TestCase):
    def test_bytes_identity_and_pathlike(self):
        class BytesSubclass(bytes):pass
        data=BytesSubclass(b'data');self.assertIs(_read(data),data)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.bin';path.write_bytes(b'file bytes');trace=[]
            class Source(os.PathLike):
                def __fspath__(self):trace.append('fspath');return str(path)
            self.assertEqual(_read(str(path)),b'file bytes')
            self.assertEqual(_read(path),b'file bytes')
            self.assertEqual(_read(Source()),b'file bytes');self.assertEqual(trace,['fspath'])
            class PathSubclass(type(path)):
                def __fspath__(self):trace.append('subclass fspath');return super().__fspath__()
            self.assertEqual(_read(PathSubclass(path)),b'file bytes');self.assertEqual(trace,['fspath'])

    @unittest.skipUnless(os.name=='posix','surrogateescaped names require POSIX')
    def test_surrogateescaped_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw=os.fsencode(directory)+b'/name-\xff'
            try:
                fd=os.open(raw,os.O_CREAT|os.O_WRONLY,0o600)
            except OSError as error:
                if sys.platform == 'darwin' and error.errno in (errno.EILSEQ, errno.EPERM):
                    self.skipTest('temporary filesystem rejects non-UTF-8 filenames; successful reads remain unverified here')
                raise
            try:os.write(fd,b'data')
            finally:os.close(fd)
            self.assertEqual(_read(os.fsdecode(raw)),b'data')

    @unittest.skipUnless(os.name == 'posix', 'surrogateescaped names require POSIX')
    def test_surrogateescaped_path_error_preserves_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            filename = os.fsdecode(os.fsencode(directory) + b'/missing-\xff')
            with self.assertRaises(OSError) as reference:
                Path(filename).read_bytes()
            with self.assertRaises(OSError) as native:
                _read(filename)
            expected, actual = reference.exception, native.exception
            self.assertEqual((type(actual), actual.errno, actual.filename, actual.strerror),
                             (type(expected), expected.errno, expected.filename, expected.strerror))

    def test_path_errors_match_pathlib(self):
        def observe(function,value):
            try:return ('ok',function(value))
            except Exception as exc:return type(exc).__name__,exc.args,getattr(exc,'filename',None)
        with tempfile.TemporaryDirectory() as directory:
            cases=[directory,str(Path(directory)/'missing'/'..'/'file'),str(Path(directory)/'nul\x00name')]
            for source in cases:
                with self.subTest(source=repr(source)):
                    self.assertEqual(observe(_read,source),observe(lambda value:Path(value).read_bytes(),source))

    def test_stream_order_and_result_identity(self):
        trace=[];position=object();result=object()
        class Stream:
            def __getattribute__(self,name):
                if name in ('read','seek','tell'):trace.append(('lookup',name))
                return object.__getattribute__(self,name)
            def tell(self):trace.append('tell');return position
            def seek(self,value):trace.append(('seek',0 if value==0 else value is position))
            def read(self):trace.append('read');return result
        self.assertIs(_read(Stream()),result)
        self.assertEqual(trace,[('lookup','read'),('lookup','seek'),('lookup','tell'),'tell',('lookup','seek'),('seek',0),('lookup','read'),'read',('lookup','seek'),('seek',True)])

    def test_stream_failure_and_finally_context(self):
        import sys
        trace=[];original=RuntimeError('read failed');cleanup=RuntimeError('restore failed')
        class Stream:
            def tell(self):trace.append('tell');return 7
            def seek(self,value):
                trace.append(('seek',value))
                if value==7:
                    trace.append(('handled',sys.exception() is original));raise cleanup
            def read(self):trace.append('read');raise original
        with self.assertRaises(RuntimeError) as caught:_read(Stream())
        self.assertIs(caught.exception,cleanup);self.assertIs(caught.exception.__context__,original)
        self.assertEqual(trace,['tell',('seek',0),'read',('seek',7),('handled',True)])

    def test_successful_stream_preserves_enclosing_handled_exception(self):
        import sys
        marker=RuntimeError('outer');seen=[]
        class Stream:
            def tell(self):return 7
            def seek(self,value):
                if value==7:seen.append(sys.exception() is marker)
            def read(self):return b'ok'
        try:raise marker
        except RuntimeError:self.assertEqual(_read(Stream()),b'ok')
        self.assertEqual(seen,[True])

    def test_initial_seek_failure_still_restores_position(self):
        import sys
        marker=RuntimeError('initial seek failed');trace=[]
        class Stream:
            def tell(self):trace.append('tell');return 7
            def seek(self,value):
                trace.append(('seek',value))
                if value==0:raise marker
                trace.append(('handled',sys.exception() is marker))
            def read(self):raise AssertionError('read must not run')
        with self.assertRaises(RuntimeError) as caught:_read(Stream())
        self.assertIs(caught.exception,marker)
        self.assertEqual(trace,['tell',('seek',0),('seek',7),('handled',True)])

    def test_path_override_and_nonattribute_error(self):
        trace=[];token=object();marker=RuntimeError('attribute lookup')
        class CustomPath:
            def __init__(self,source):trace.append(('path',source))
            def read_bytes(self):trace.append('read_bytes');return token
        with patch.object(docxtpl_native,'Path',CustomPath):self.assertIs(_read('sample'),token)
        self.assertEqual(trace,[('path','sample'),'read_bytes'])
        class Bad:
            def __getattribute__(self,name):
                if name=='read':raise marker
                return object.__getattribute__(self,name)
        with self.assertRaises(RuntimeError) as caught:_read(Bad())
        self.assertIs(caught.exception,marker)

    def test_path_read_bytes_override_is_called(self):
        token=object();seen=[]
        def overridden(path):seen.append(str(path));return token
        with patch.object(Path,'read_bytes',overridden):self.assertIs(_read('folder/./file'),token)
        self.assertEqual(seen,['folder/file'])

    def test_io_open_override_receives_normalized_path_and_context_protocol(self):
        import io
        trace=[];token=object()
        class Stream:
            def read(self):trace.append('read');return token
        class Manager:
            def __enter__(self):trace.append('enter');return Stream()
            def __exit__(self,kind,value,tb):trace.append(('exit',kind,value,tb));return False
        def opened(path,mode,buffering,encoding,errors,newline):
            trace.append(('open',type(path).__name__,str(path),mode,buffering,encoding,errors,newline));return Manager()
        with patch.object(io,'open',opened):self.assertIs(_read('folder/./file'),token)
        self.assertEqual(trace,[('open',type(Path()).__name__,'folder/file','rb',-1,None,None,None),'enter','read',('exit',None,None,None)])

    def test_standard_file_read_does_not_execute_pathlib_read_bytes(self):
        import sys
        seen=[]; previous=sys.getprofile(); code=Path.read_bytes.__code__
        def profile(frame,event,arg):
            if event=='call' and frame.f_code is code:seen.append(True)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'file';path.write_bytes(b'data')
            try:
                sys.setprofile(profile)
                self.assertEqual(_read(path),b'data')
            finally:sys.setprofile(previous)
        self.assertEqual(seen,[])

    def test_overridden_path_open_can_suppress_read_failure(self):
        import sys
        marker=RuntimeError('read failed');trace=[]
        class Manager:
            def __enter__(self):return self
            def read(self):raise marker
            def __exit__(self,kind,value,tb):
                trace.append((kind,value,sys.exception() is marker))
                return True
        def opened(path,*,mode):
            trace.append((str(path),mode));return Manager()
        with patch.object(Path,'open',opened):self.assertIsNone(_read('folder/./file'))
        self.assertEqual(trace,[('folder/file','rb'),(RuntimeError,marker,True)])

    @unittest.skipUnless(os.name=='posix','audit flags are POSIX-specific')
    def test_audit_hook_can_reject_open(self):
        import subprocess,sys,textwrap
        code=textwrap.dedent('''
            import sys
            from docxtpl_native import _read
            marker=RuntimeError('blocked')
            def hook(event,args):
                if event=='open':raise marker
            sys.addaudithook(hook)
            try:_read('does-not-matter')
            except RuntimeError as error:assert error is marker
            else:raise AssertionError('audit hook did not block open')
        ''')
        subprocess.run([sys.executable,'-c',code],check=True,capture_output=True,text=True)

    @unittest.skipUnless(os.name=='posix','audit flags are POSIX-specific')
    def test_path_read_emits_open_audit_event(self):
        import subprocess,sys,textwrap
        code=textwrap.dedent('''
            import os,sys,tempfile
            from pathlib import Path
            from docxtpl_native import _read
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'file';path.write_bytes(b'data');events=[]
                def hook(event,args):
                    if event=='open':events.append(args)
                sys.addaudithook(hook)
                assert _read(str(path))==b'data'
                assert events==[(str(path),'r',os.O_CLOEXEC)],events
        ''')
        subprocess.run([sys.executable,'-c',code],check=True,capture_output=True,text=True)


if __name__=='__main__':unittest.main()
