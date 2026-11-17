"""Fetch the official example corpus at a fixed commit, retaining its license."""
import argparse
import subprocess
from pathlib import Path

REPO='https://github.com/elapouya/python-docx-template.git'
REV='1f143fbe86c19ecb28c3205d5f4b1547c7e2d7ad'

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',default='.upstream/docxtpl');a=p.parse_args()
    path=Path(a.out).resolve()
    if path.exists() and not (path/'.git').exists():raise SystemExit('refusing to overwrite a non-git directory')
    if not path.exists():
        path.mkdir(parents=True);subprocess.run(['git','init',str(path)],check=True)
        subprocess.run(['git','-C',str(path),'remote','add','origin',REPO],check=True)
    origin=subprocess.check_output(['git','-C',str(path),'remote','get-url','origin'],text=True).strip()
    if origin!=REPO:raise SystemExit('unexpected upstream remote')
    subprocess.run(['git','-C',str(path),'fetch','--depth=1','origin',REV],check=True)
    subprocess.run(['git','-C',str(path),'checkout','--detach','FETCH_HEAD'],check=True)
    actual=subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True).strip()
    if actual!=REV:raise SystemExit('upstream revision mismatch')
    scripts=[p for p in (path/'tests').glob('*.py') if p.name not in ('__init__.py','runtests.py')]
    print(f'{len(scripts)} upstream examples at {actual}')

if __name__=='__main__':main()

