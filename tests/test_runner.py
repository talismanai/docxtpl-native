import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from paritylab.cli import run
from test_compare import document

class RunnerTests(unittest.TestCase):
    def args(self,out,visual='off'):
        return argparse.Namespace(out=out,candidate='reference',suite='synthetic',case=['plain'],
          repeat=1,warmup=0,jobs=1,visual=visual,upstream='unused',real_manifest=None)
    def test_matching_setup_failures_are_not_parity(self):
        error={'status':'error','exception':{'type':'builtins.ImportError','message':'missing dependency'},'samples':[],'outputs':[]}
        with tempfile.TemporaryDirectory() as out,patch('paritylab.cli._worker',return_value=error):
            self.assertEqual(run(self.args(out)),1)
            report=json.loads((Path(out)/'report.json').read_text())
            self.assertEqual(report['cases'][0]['status'],'reference_error')
    def test_required_visual_cannot_be_silently_skipped(self):
        def worker(request,out):
            folder=out/'documents';folder.mkdir();(folder/'document.docx').write_bytes(document())
            return {'status':'ok','samples':[{'total_ms':1}], 'peak_process_rss_mib':1,'outputs':['document.docx']}
        # document() has static default core metadata, so both independent
        # fixture creations produce equivalent package contents.
        with tempfile.TemporaryDirectory() as out,patch('paritylab.cli._worker',side_effect=worker),patch('paritylab.cli.visual.available',return_value=False):
            self.assertEqual(run(self.args(out,'required')),1)
            report=json.loads((Path(out)/'report.json').read_text())
            self.assertEqual(report['cases'][0]['documents'][0]['visual']['status'],'unavailable')
    def test_native_unavailable_produces_blocked_report(self):
        with tempfile.TemporaryDirectory() as out,patch('docxtpl_native.capabilities',return_value={'native_available':False}):
            args=self.args(out);args.candidate='native'
            self.assertEqual(run(args),2)
            self.assertEqual(json.loads((Path(out)/'report.json').read_text())['status'],'blocked')
    def test_different_documents_cannot_earn_a_speedup_claim(self):
        def worker(request,out):
            folder=out/'documents';folder.mkdir()
            (folder/'document.docx').write_bytes(document(request['backend']))
            return {'status':'ok','samples':[{'total_ms':2 if request['backend']=='docxtpl' else 1}],
                    'peak_process_rss_mib':1,'outputs':['document.docx']}
        with tempfile.TemporaryDirectory() as out,patch('paritylab.cli._worker',side_effect=worker),patch('docxtpl_native.capabilities',return_value={'native_available':True}):
            args=self.args(out);args.candidate='native'
            self.assertEqual(run(args),1)
            row=json.loads((Path(out)/'report.json').read_text())['cases'][0]
            self.assertEqual(row['timing_ratio_reference_to_candidate'],2)
            self.assertNotIn('speedup',row)
    def test_matching_empty_outputs_are_not_parity(self):
        result={'status':'ok','samples':[],'outputs':[]}
        with tempfile.TemporaryDirectory() as out,patch('paritylab.cli._worker',return_value=result):
            self.assertEqual(run(self.args(out)),1)
            row=json.loads((Path(out)/'report.json').read_text())['cases'][0]
            self.assertEqual(row['status'],'gap')

    def test_identical_documents_cannot_hide_different_side_effects(self):
        def worker(request,out):
            folder=out/'documents';folder.mkdir();(folder/'document.docx').write_bytes(document())
            return {'status':'ok','samples':[],'outputs':['document.docx'],
                    'observation':{'trace':[str(out.name)]}}
        with tempfile.TemporaryDirectory() as out,patch('paritylab.cli._worker',side_effect=worker):
            args=self.args(out);args.suite='protocols';args.protocol_case=['callback_identity']
            self.assertEqual(run(args),1)
            row=json.loads((Path(out)/'report.json').read_text())['cases'][0]
            self.assertFalse(row['behavior']['equal'])
            self.assertTrue(row['documents'][0]['equivalent'])

