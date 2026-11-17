"""Install a local wheel into a fresh venv and smoke-test the native extension.

Dependencies are supplied separately from an existing site-packages directory;
this checks the local wheel, not dependency resolution or portable wheel repair.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

WORKER = r'''
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import platform
import sys

site, environment = (Path(value).resolve() for value in sys.argv[1:])
sys.path.append(str(site))
from docx import Document
import docxtpl_native
from paritylab import reference_fingerprint
from paritylab.compare import compare_docx

module = Path(docxtpl_native.__file__).resolve()
extension = Path(docxtpl_native._extension().__file__).resolve()
laboratory = Path(reference_fingerprint.__file__).resolve()
if not all(path.is_relative_to(environment) for path in (module, extension, laboratory)):
    raise AssertionError('wheel imports escaped the fresh environment')
reference = reference_fingerprint.installed_reference()
if not reference['matched']:
    raise AssertionError('installed reference does not match the pinned source')
import docxtpl
reference_module = Path(docxtpl.__file__).resolve()
if not reference_module.is_relative_to(site.resolve()):
    raise AssertionError('reference import escaped the dependency site')
capabilities = docxtpl_native.capabilities()
if not capabilities['native_available'] or capabilities['reference_fallback']:
    raise AssertionError('native extension unavailable or reference fallback enabled')
source = io.BytesIO()
document = Document()
document.add_paragraph('Café {{ name }}')
document.save(source)
reference_template = docxtpl.DocxTemplate(io.BytesIO(source.getvalue()))
reference_template.render({'name': 'Ada'})
reference_output = io.BytesIO()
reference_template.save(reference_output)
template = docxtpl_native.DocxTemplate(io.BytesIO(source.getvalue()))
template.render({'name': 'Ada'})
output = io.BytesIO()
template.save(output)
if Document(io.BytesIO(output.getvalue())).paragraphs[0].text != 'Café Ada':
    raise AssertionError('unexpected rendered text')
comparison = compare_docx(reference_output.getvalue(), output.getvalue())
if not comparison['equivalent']:
    raise AssertionError('complete DOCX comparison failed: ' + json.dumps(comparison))
dependencies = {name: importlib.metadata.version(name) for name in
                ('docxtpl', 'Jinja2', 'python-docx', 'lxml', 'docxcompose')}
print(json.dumps({'python': sys.version.split()[0], 'platform': sys.platform, 'machine': platform.machine(),
                  'environment': str(environment.resolve()),
                  'package_version': importlib.metadata.version('docxtpl-native'),
                  'module': str(module), 'extension': str(extension),
                  'laboratory': str(laboratory), 'dependencies': dependencies,
                  'reference_module': str(reference_module),
                  'extension_sha256': hashlib.sha256(extension.read_bytes()).hexdigest(),
                  'reference_commit': reference['commit'], 'reference_source_matches': reference['matched'],
                  'reference_source': reference,
                  'native_available': capabilities['native_available'],
                  'reference_fallback': capabilities['reference_fallback'], 'rendered_text': 'Café Ada',
                  'docx_equivalent': comparison['equivalent'],
                  'archive_bytes_equal': comparison['archive_bytes_equal'],
                  'reference_docx_sha256': comparison['reference_sha256'],
                  'candidate_docx_sha256': comparison['candidate_sha256']}))
'''


def verify_sources(observed, dependencies, environment):
    """Check wheel and reference paths and hashes independently of the wheel."""
    environment, dependencies = Path(environment).resolve(), Path(dependencies).resolve()
    if not all(Path(observed[name]).resolve().is_relative_to(environment)
               for name in ('module', 'extension', 'laboratory')):
        raise AssertionError('wheel imports escaped the fresh environment')
    if hashlib.sha256(Path(observed['extension']).read_bytes()).hexdigest() != observed['extension_sha256']:
        raise AssertionError('extension hash differs from independent file check')
    helper = Path(__file__).with_name('reference_fingerprint.py')
    helper_sha = hashlib.sha256(helper.read_bytes()).hexdigest()
    if hashlib.sha256(Path(observed['laboratory']).read_bytes()).hexdigest() != helper_sha:
        raise AssertionError('wheel reference fingerprint helper differs from the checker')
    spec = importlib.util.spec_from_file_location('_trusted_reference_fingerprint', helper)
    if spec is None or spec.loader is None:
        raise RuntimeError('reference fingerprint helper cannot be loaded')
    trusted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trusted)
    reference_module = Path(observed['reference_module']).resolve()
    if not reference_module.is_relative_to(dependencies):
        raise AssertionError('reference import escaped the dependency site')
    if trusted.fingerprint(reference_module.parent) != observed['reference_source'] or not observed['reference_source']['matched']:
        raise AssertionError('reference fingerprint did not match independent source check')
    return helper_sha


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--dependency-site', type=Path, required=True)
    parser.add_argument('--revision', help='source revision used to build the wheel; defaults to GITHUB_SHA or git HEAD')
    args = parser.parse_args()
    wheel, dependencies = args.wheel.resolve(), args.dependency_site.resolve()
    if not wheel.is_file() or not dependencies.is_dir():
        parser.error('wheel and dependency site must exist')
    with tempfile.TemporaryDirectory(prefix='docxtpl-wheel-smoke-') as temporary:
        environment = Path(temporary) / 'venv'
        subprocess.run(['uv', 'venv', '--no-project', '--no-python-downloads',
                        '--python', sys.executable, str(environment)], check=True)
        python = environment / 'bin/python'
        # Target the fresh environment explicitly so an existing distribution
        # in the caller's PYTHONPATH cannot satisfy the installation.
        subprocess.run(['uv', 'pip', 'install', '--python', str(python), '--no-deps', '--no-index', str(wheel)],
                       check=True, stdout=subprocess.DEVNULL)
        result = subprocess.run([str(python), '-I', '-c', WORKER, str(dependencies), str(environment)],
                                check=False, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(f'wheel smoke failed ({result.returncode}):\n{result.stderr}')
        observed = json.loads(result.stdout)
        # Verify the wheel's fingerprint helper and independently hash the
        # installed reference from the source checkout, outside the wheel.
        observed['fingerprint_helper_sha256'] = verify_sources(observed, dependencies, environment)
    revision = args.revision or os.environ.get('GITHUB_SHA')
    if revision is None:
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    python_target = '.'.join(observed['python'].split('.')[:2])
    cases = {'3.11': 'standalone/wheel_python311', '3.12': 'standalone/wheel_python312'}
    if python_target not in cases:
        raise RuntimeError('unsupported Python target for this test: ' + python_target)
    observed.update(case=cases[python_target], mode='local_wheel_install', status='pass',
                    source_revision=revision,
                    checker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    wheel=wheel.name, wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    dependency_site=str(dependencies),
                    scope='Fresh Python venv; local wheel; preinstalled dependencies; no portable wheel repair.')
    print(json.dumps(observed, indent=2))


if __name__ == '__main__':
    main()
