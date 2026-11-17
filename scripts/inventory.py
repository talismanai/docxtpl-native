"""Inventory reference APIs; discovery is not a declaration of compatibility."""
import json
from pathlib import Path
from paritylab.inventory import inventory

path=Path('artifacts/reference-api.json')
path.parent.mkdir(exist_ok=True)
result=inventory()
path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(path,len(result['symbols']),'symbols;',len(result['errors']),'import errors')
if result['errors']:raise SystemExit(1)

