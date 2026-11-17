import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class WorkerTests(unittest.TestCase):
    def test_upstream_output_must_be_generated_by_this_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);corpus=root/'corpus';corpus.mkdir()
            (corpus/'empty.py').write_text('pass\n')
            (corpus/'output').mkdir()
            (corpus/'output'/'stale.docx').write_bytes(b'preexisting output')
            output=root/'result';output.mkdir()
            request={'backend':'docxtpl','kind':'upstream','upstream_tests':str(corpus),
                     'script':'empty.py','output':str(output),'repeat':1,'warmup':0}
            path=root/'request.json';path.write_text(json.dumps(request))
            subprocess.run([sys.executable,'-m','paritylab.worker',str(path)],check=True)
            report=json.loads((output/'worker.json').read_text())
            self.assertEqual(report['status'],'error')
            self.assertIn('produced no DOCX',report['exception']['message'])
            self.assertEqual(report['outputs'],[])
            self.assertFalse((output/'work').exists())
