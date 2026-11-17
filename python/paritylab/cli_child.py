"""Isolated CLI run with a checked native execution record and no fallback."""
import importlib
import json
from pathlib import Path
import sys
from unittest.mock import patch
from contextlib import ExitStack

def main():
    backend,record,*arguments=sys.argv[1:]
    library=importlib.import_module(backend)
    command=importlib.import_module(backend+'.__main__')
    created=[]
    def create(*args,**kwargs):
        document=library.DocxTemplate(*args,**kwargs);created.append(document);return document
    sys.argv=[backend,*arguments]
    with ExitStack() as guard:
        guard.enter_context(patch.object(command,'DocxTemplate',create))
        if backend=='docxtpl_native':
            guard.enter_context(patch('docxtpl.DocxTemplate.render',side_effect=AssertionError('reference renderer called')))
            guard.enter_context(patch('jinja2.Template.render',side_effect=AssertionError('Python template executor called')))
        command.main()
    if len(created)!=1:raise AssertionError('CLI did not construct exactly one document')
    observation={'backend':backend,'saved':created[0].is_saved}
    if backend=='docxtpl_native':
        observation['execution']=created[0].last_execution
        if observation['execution']['renderer']!='native':raise AssertionError('CLI did not use native rendering')
    Path(record).write_text(json.dumps(observation))

if __name__=='__main__':main()
