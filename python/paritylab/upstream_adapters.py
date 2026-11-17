"""Explicit adapters for fixed examples without reliable in-process outputs."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

ADAPTERS={
    'get_undeclared_variables.py':'1e6a9291f027d5b5e673a687993e4cd2f4798471',
    'module_execute.py':'86c4ab9b9d3f6ca5effd26c81be4657de95b30b1',
}

def execute(script,backend,output):
    data=Path(script).read_bytes()
    digest=hashlib.sha1(f'blob {len(data)}\0'.encode()+data).hexdigest()
    if digest!=ADAPTERS[Path(script).name]:raise AssertionError('upstream adapter source changed')
    if Path(script).name=='get_undeclared_variables.py':
        namespace=runpy.run_path(str(script),run_name='__parity_adapter__')
        names=['test_before_render','test_after_incomplete_render','test_after_complete_render','test_with_custom_jinja_env']
        results={name:namespace[name]() for name in names}
        if not all(value is True for value in results.values()):raise AssertionError(results)
        return results
    record=output/'cli-provenance.json'
    process=subprocess.run([sys.executable,'-m','paritylab.cli_child',backend,str(record),
        'templates/module_execute_tpl.docx','templates/module_execute.json','output/module_execute.docx','-o','-q'],
        capture_output=True,text=True,check=True)
    (output/'cli-stdout.txt').write_text(process.stdout);(output/'cli-stderr.txt').write_text(process.stderr)
    provenance=json.loads(record.read_text())
    if provenance['backend']!=backend or provenance['saved'] is not True:raise AssertionError(provenance)
    if not Path('output/module_execute.docx').is_file():raise AssertionError('CLI produced no output')
    return {'saved':True,'stdout':process.stdout,'stderr':process.stderr}
