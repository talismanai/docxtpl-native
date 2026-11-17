import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from docxtpl_native import capabilities
from paritylab.compare import compare_docx
from paritylab.protocol_base import template
from paritylab import cli as parity_cli
from paritylab.upstream_manifest import UPSTREAM_EXAMPLE_SHA256


class CaseClockTests(unittest.TestCase):
    def test_a_case_that_embeds_its_own_output_never_runs_on_the_live_clock(self):
        own, epoch = parity_cli.FIXED_CLOCK_EPOCH, 1.0
        for case_id in parity_cli.FIXED_CLOCK_CASES:
            with self.subTest(case_id):
                self.assertEqual(parity_cli._case_clock(case_id, None, set()), own)
                self.assertEqual(parity_cli._case_clock(case_id, epoch, set()), epoch)
                self.assertEqual(parity_cli._case_clock(case_id, epoch, {'synthetic/plain'}), own)
                self.assertEqual(parity_cli._case_clock(case_id, epoch, {case_id}), epoch)

    def test_other_cases_keep_the_live_clock_unless_frozen(self):
        self.assertIsNone(parity_cli._case_clock('synthetic/plain', None, set()))
        self.assertEqual(parity_cli._case_clock('synthetic/plain', 1.0, set()), 1.0)
        self.assertIsNone(parity_cli._case_clock('synthetic/plain', 1.0, {'synthetic/unicode'}))

    def test_fixed_clock_cases_name_pinned_upstream_examples(self):
        examples = {'upstream/' + Path(name).stem for name in UPSTREAM_EXAMPLE_SHA256}
        self.assertLessEqual(parity_cli.FIXED_CLOCK_CASES, examples)


@unittest.skipUnless(capabilities()['native_available'],'native extension not built; not a parity pass')
class CliTests(unittest.TestCase):
    def test_isolated_cli_generates_the_same_document_with_native_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'source.docx').write_bytes(template('Hello {{ name }}'))
            (root/'data.json').write_text(json.dumps({'name':'CLI'}))
            for backend in ['docxtpl','docxtpl_native']:
                subprocess.run([sys.executable,'-m','paritylab.cli_child',backend,str(root/(backend+'.json')),
                    str(root/'source.docx'),str(root/'data.json'),str(root/(backend+'.docx')),'-o','-q'],check=True)
            self.assertTrue(compare_docx((root/'docxtpl.docx').read_bytes(),(root/'docxtpl_native.docx').read_bytes())['equivalent'])
            self.assertEqual(json.loads((root/'docxtpl_native.json').read_text())['execution']['renderer'],'native')

    def test_fixed_worker_clock_is_recorded_and_preserves_strict_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);epoch=1704067200
            subprocess.run([sys.executable,'-m','paritylab.cli','--candidate','reference','--suite','synthetic',
                '--case','plain','--repeat','1','--warmup','0','--visual','off','--freeze-time',str(epoch),
                '--out',str(root)],check=True,capture_output=True,text=True)
            report=json.loads((root/'report.json').read_text())
            self.assertEqual(report['mode'],'self_check')
            self.assertEqual(report['clock'],{'mode':'fixed_wall_time','epoch_seconds':epoch})
            self.assertEqual(report['counts']['passed'],1)
            for role in ('reference','candidate'):
                self.assertTrue(report['cases'][0][role]['reference_source']['matched'])
            folder=root/'cases/synthetic/plain'
            first=(folder/'reference/documents/document.docx').read_bytes()
            second=(folder/'candidate/documents/document.docx').read_bytes()
            self.assertEqual(first,second)
            with zipfile.ZipFile(folder/'reference/documents/document.docx') as archive:
                self.assertTrue(all(item.date_time==time.localtime(epoch)[:6] for item in archive.infolist()))

    def test_fixed_clock_can_be_scoped_without_changing_other_workers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);epoch=1704067200
            command=[sys.executable,'-m','paritylab.cli','--candidate','reference','--suite','synthetic',
                '--case','plain','--case','unicode','--repeat','1','--warmup','0','--visual','off',
                '--freeze-time',str(epoch),'--freeze-time-case','synthetic/plain','--out',str(root)]
            subprocess.run(command,check=True,capture_output=True,text=True)
            report=json.loads((root/'report.json').read_text())
            self.assertEqual(report['mode'],'self_check')
            self.assertEqual(report['clock'],{'mode':'fixed_wall_time','epoch_seconds':epoch,'cases':['synthetic/plain']})
            self.assertEqual(report['counts']['passed'],2)
            for case in ('plain','unicode'):
                for role in ('reference','candidate'):
                    folder=root/'cases/synthetic'/case/role
                    request=json.loads((folder/'request.json').read_text())
                    self.assertEqual(request.get('freeze_time'),epoch if case=='plain' else None)
                    with zipfile.ZipFile(folder/'documents/document.docx') as archive:
                        self.assertEqual(all(info.date_time==time.localtime(epoch)[:6] for info in archive.infolist()),case=='plain')
            command[command.index('synthetic/plain')]='synthetic/typo'
            command[command.index('--out') + 1]=str(root/'invalid-run')
            failed=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(failed.returncode,2)
            self.assertIn('unknown frozen cases: synthetic/typo',failed.stderr)


class SourceRevisionTests(unittest.TestCase):
    """The report's revision: CI names it, a clean checkout names it, a dirty one does not."""

    def label(self, environment: dict[str, str] | None = None) -> dict[str, object]:
        import os
        from paritylab.cli import source_revision
        previous = os.environ.get('GITHUB_SHA')
        try:
            if environment and 'GITHUB_SHA' in environment:
                os.environ['GITHUB_SHA'] = environment['GITHUB_SHA']
            else:
                os.environ.pop('GITHUB_SHA', None)
            return source_revision()
        finally:
            if previous is None:
                os.environ.pop('GITHUB_SHA', None)
            else:
                os.environ['GITHUB_SHA'] = previous

    def test_ci_sha_is_used_as_given(self) -> None:
        self.assertEqual(self.label({'GITHUB_SHA': 'abc123'}),
                         {'source_revision': 'abc123', 'source_tree': 'github_sha'})

    def test_checkout_names_the_revision_only_while_it_is_clean(self) -> None:
        import subprocess
        root = Path(__file__).resolve().parents[1]
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, text=True,
                              check=True).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=root, capture_output=True, text=True,
                               check=True).stdout.strip()
        result = self.label()
        if dirty:
            self.assertEqual(result, {'source_revision': None, 'source_tree': 'dirty'})
        else:
            self.assertEqual(result, {'source_revision': head, 'source_tree': 'clean_checkout'})


class ConcurrentCasesTests(unittest.TestCase):
    """--jobs changes how long a run takes, never what it concludes."""

    def run_suite(self, directory: str, *extra: str) -> dict:
        root = Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable, '-m', 'paritylab.cli', '--suite', 'synthetic', '--case', 'plain',
                        '--visual', 'off', '--repeat', '1', '--warmup', '0', '--out', directory, *extra],
                       check=True, capture_output=True, text=True,
                       env={**os.environ, 'PYTHONPATH': os.pathsep.join([str(root / 'tests'), str(root / 'python')])})
        return json.loads((Path(directory) / 'report.json').read_text())

    def test_concurrent_run_reaches_the_same_verdict_without_timings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            serial = self.run_suite(str(Path(directory) / 'serial'))
            concurrent = self.run_suite(str(Path(directory) / 'concurrent'), '--jobs', '4')
        self.assertEqual([case['id'] for case in serial['cases']], [case['id'] for case in concurrent['cases']])
        self.assertEqual({case['id']: case['status'] for case in serial['cases']},
                         {case['id']: case['status'] for case in concurrent['cases']})
        self.assertEqual(serial['counts'], concurrent['counts'])
        self.assertTrue(any('speedup' in case for case in serial['cases']))
        self.assertFalse(any('speedup' in case for case in concurrent['cases']),
                         'a loaded host must not report timings')

    def test_timing_options_refuse_to_run_concurrently(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, '-m', 'paritylab.cli', '--suite', 'synthetic', '--case', 'plain', '--visual', 'off',
                 '--repeat', '3', '--jobs', '4', '--out', directory],
                capture_output=True, text=True,
                env={**os.environ, 'PYTHONPATH': os.pathsep.join([str(root / 'tests'), str(root / 'python')])})
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('does not measure time', completed.stderr)


class RerunSelectionTests(unittest.TestCase):
    """--rerun-from narrows a development run and never qualifies a revision."""

    def cli(self, *arguments: str) -> subprocess.CompletedProcess:
        root = Path(__file__).resolve().parents[1]
        return subprocess.run([sys.executable, '-m', 'paritylab.cli', *arguments], capture_output=True, text=True,
                              env={**os.environ, 'PYTHONPATH': os.pathsep.join([str(root / 'tests'),
                                                                                str(root / 'python')])})

    def test_only_the_cases_that_did_not_pass_run_again(self) -> None:
        # A healthy corpus has nothing failing, so the earlier report is
        # written by hand: the selection is what is under test.
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'earlier.json'
            report.write_text(json.dumps({'cases': [{'id': 'synthetic/plain', 'status': 'pass'},
                                                    {'id': 'synthetic/strict_undefined', 'status': 'gap'}]}))
            out = Path(directory) / 'again'
            completed = self.cli('--suite', 'synthetic', '--case', 'plain', '--case', 'strict_undefined',
                                 '--visual', 'off', '--repeat', '1', '--warmup', '0',
                                 '--rerun-from', str(report), '--out', str(out))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            again = json.loads((out / 'report.json').read_text())
        self.assertEqual([case['id'] for case in again['cases']], ['synthetic/strict_undefined'])
        self.assertEqual(again['counts']['total'], 1)
        self.assertFalse(again['selection']['complete'])
        self.assertEqual(again['selection']['rerun_from'], str(report))

    def test_a_rerun_cannot_invent_cases_the_suite_does_not_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'earlier.json'
            report.write_text(json.dumps({'cases': [{'id': 'protocol/callback_identity', 'status': 'gap'}]}))
            completed = self.cli('--suite', 'synthetic', '--case', 'plain', '--visual', 'off', '--repeat', '1',
                                 '--warmup', '0', '--rerun-from', str(report),
                                 '--out', str(Path(directory) / 'again'))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('protocol/callback_identity', completed.stderr)

    def test_a_complete_run_cannot_be_narrowed_without_saying_so(self) -> None:
        # The flag marks the report partial even when the suite is the whole
        # corpus: the matrix must not take it for a qualification run.
        from paritylab.cli import selection
        arguments = argparse.Namespace(suite='all', case=None, protocol_case=None, real_manifest=None,
                                       rerun_from='artifacts/parity/report.json')
        self.assertFalse(selection(arguments)['complete'])
        arguments.rerun_from = None
        self.assertTrue(selection(arguments)['complete'])

    def test_rerunning_a_report_with_nothing_left_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / 'first'
            self.cli('--suite', 'synthetic', '--case', 'plain', '--visual', 'off', '--repeat', '1',
                     '--warmup', '0', '--out', str(first))
            completed = self.cli('--suite', 'synthetic', '--case', 'plain', '--visual', 'off', '--repeat', '1',
                                 '--warmup', '0', '--rerun-from', str(first / 'report.json'),
                                 '--out', str(Path(directory) / 'second'))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('nothing to rerun', completed.stderr)



class RealManifestTests(unittest.TestCase):
    """The private-corpus path, exercised with documents that are not private.

    `--real-manifest` is how the owner's own documents enter the corpus. The
    manifest, the context file and the `real/<id>` case have to work before
    documents arrive, so this drives the whole path with a template built
    here: the case still compares the pinned reference with the engine.
    """

    def test_a_manifest_case_is_compared_like_any_other(self) -> None:
        from docx import Document
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            document = Document()
            document.add_paragraph('Client: {{ client }}')
            document.add_paragraph('{% for item in items %}{{ item.name }}: {{ item.value }}; {% endfor %}')
            document.add_paragraph('{{ note }}')
            document.save(folder / 'template.docx')
            (folder / 'context.json').write_text(json.dumps(
                {'client': 'Ada & Co.', 'items': [{'name': 'report', 'value': 3}, {'name': 'review', 'value': 7}],
                 'note': 'naïve façade, <markup> and emoji 🧪'}, ensure_ascii=False))
            manifest = folder / 'manifest.json'
            manifest.write_text(json.dumps({'cases': [{'id': 'generated-template', 'template': 'template.docx',
                                                       'context': 'context.json', 'autoescape': True}]}))
            out = folder / 'out'
            completed = subprocess.run(
                [sys.executable, '-m', 'paritylab.cli', '--suite', 'synthetic', '--case', 'plain',
                 '--real-manifest', str(manifest), '--visual', 'off', '--repeat', '1', '--warmup', '0',
                 '--out', str(out)],
                capture_output=True, text=True,
                env={**os.environ, 'PYTHONPATH': os.pathsep.join([str(root / 'tests'), str(root / 'python')])})
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            report = json.loads((out / 'report.json').read_text())
        case = [item for item in report['cases'] if item['id'] == 'real/generated-template']
        self.assertEqual(len(case), 1)
        self.assertEqual(case[0]['status'], 'pass')
        # The comparison is the usual one: same text, same XML, same parts.
        self.assertTrue(all(document['content_equal'] and document['structure_and_format_equal']
                            for document in case[0]['documents']))
        # A manifest run is extra evidence, never a complete corpus.
        self.assertTrue(report['selection']['real_manifest'])
        self.assertFalse(report['selection']['complete'])

    def test_a_manifest_that_points_nowhere_is_an_infrastructure_failure(self) -> None:
        # A document the laboratory cannot even open is never a parity
        # verdict: the reference fails its own setup, so the case is a
        # `reference_error` and the run does not pass.
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'manifest.json'
            manifest.write_text(json.dumps({'cases': [{'id': 'ausente', 'template': 'nao-existe.docx',
                                                       'context': 'nao-existe.json'}]}))
            out = Path(directory) / 'out'
            completed = subprocess.run(
                [sys.executable, '-m', 'paritylab.cli', '--suite', 'synthetic', '--case', 'plain',
                 '--real-manifest', str(manifest), '--visual', 'off', '--repeat', '1', '--warmup', '0',
                 '--out', str(out)],
                capture_output=True, text=True,
                env={**os.environ, 'PYTHONPATH': os.pathsep.join([str(root / 'tests'), str(root / 'python')])})
            report = json.loads((out / 'report.json').read_text())
        self.assertNotEqual(completed.returncode, 0)
        case = [item for item in report['cases'] if item['id'] == 'real/ausente'][0]
        self.assertEqual(case['status'], 'reference_error')
        self.assertEqual(case['reference']['stage'], 'setup')
