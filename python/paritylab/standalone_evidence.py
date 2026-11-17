"""Traceability for non-differential installation checks.

These cases cover one local wheel installation, not distribution portability,
dependency resolution or the whole Python API.
"""
import re
import hashlib
from pathlib import Path

from .reference_fingerprint import EXPECTED, REFERENCE_COMMIT


CASES = {
    'standalone/wheel_python311': {'python': '3.11', 'runner': 'python/paritylab/check_wheel_install.py'},
    'standalone/wheel_python312': {'python': '3.12', 'runner': 'python/paritylab/check_wheel_install.py'},
}
REFERENCE = {'commit': REFERENCE_COMMIT, 'expected_files': len(EXPECTED), 'matched': True, 'files': EXPECTED}
REQUIRED_DEPENDENCIES = {'docxtpl', 'Jinja2', 'python-docx', 'lxml', 'docxcompose'}
REFERENCE_HELPER_SHA256 = hashlib.sha256(Path(__file__).with_name('reference_fingerprint.py').read_bytes()).hexdigest()
CHECKER_SHA256 = hashlib.sha256(Path(__file__).with_name('check_wheel_install.py').read_bytes()).hexdigest()


def valid_wheel_evidence(case, evidence, revision, expected_versions=None):
    """Validate one execution independently of differential corpus results."""
    definition = CASES.get(case)
    if definition is None or not isinstance(evidence, dict):
        return False
    python = evidence.get('python')
    if (evidence.get('case') != case or evidence.get('status') != 'pass'
            or evidence.get('mode') != 'local_wheel_install'
            or evidence.get('source_revision') != revision
            or not isinstance(python, str) or python.split('.')[:2] != definition['python'].split('.')
            or evidence.get('platform') != 'linux'
            or evidence.get('reference_source') != REFERENCE
            or evidence.get('fingerprint_helper_sha256') != REFERENCE_HELPER_SHA256
            or evidence.get('checker_sha256') != CHECKER_SHA256
            or evidence.get('native_available') is not True
            or evidence.get('reference_fallback') is not False
            or evidence.get('rendered_text') != 'Café Ada'):
        return False
    dependencies = evidence.get('dependencies')
    if (not isinstance(expected_versions, dict) or not REQUIRED_DEPENDENCIES.issubset(expected_versions)
            or not isinstance(dependencies, dict)
            or any(dependencies.get(name) != version for name, version in expected_versions.items())):
        return False
    for name in ('wheel_sha256', 'extension_sha256'):
        if not isinstance(evidence.get(name), str) or not re.fullmatch(r'[0-9a-f]{64}', evidence[name]):
            return False
    wheel = evidence.get('wheel')
    if not isinstance(wheel, str) or Path(wheel).name != wheel or not wheel.endswith('.whl'):
        return False
    pieces = wheel[:-4].rsplit('-', 3)
    if len(pieces) != 4:
        return False
    target = 'cp' + definition['python'].replace('.', '')
    machine = evidence.get('machine')
    if not isinstance(machine, str) or not re.fullmatch(r'[A-Za-z0-9_]+', machine):
        return False
    platforms = pieces[3].split('.')
    if (target not in pieces[1].split('.') or target not in pieces[2].split('.')
            or not any(re.fullmatch(r'(?:linux|manylinux[0-9_]*|musllinux[0-9_]*)_' + re.escape(machine), tag)
                       for tag in platforms)):
        return False
    root = evidence.get('environment')
    if not isinstance(root, str) or not Path(root).is_absolute():
        return False
    root = Path(root)
    if root.name != 'venv' or not root.parent.name.startswith('docxtpl-wheel-smoke-') or '..' in root.parts:
        return False
    for name in ('module', 'extension', 'laboratory'):
        value = evidence.get(name)
        if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_relative_to(root):
            return False
        if '..' in Path(value).parts:
            return False
    extension_name = Path(evidence['extension']).name
    if (Path(evidence['module']).name != '__init__.py'
            or not extension_name.startswith('_native.cpython-' + definition['python'].replace('.', ''))
            or not extension_name.endswith('.so')
            or Path(evidence['laboratory']).name != 'reference_fingerprint.py'):
        return False
    dependency_site, reference_module = evidence.get('dependency_site'), evidence.get('reference_module')
    if not isinstance(dependency_site, str) or not isinstance(reference_module, str):
        return False
    dependency_site, reference_module = Path(dependency_site), Path(reference_module)
    if (not dependency_site.is_absolute() or not reference_module.is_absolute()
            or '..' in dependency_site.parts or '..' in reference_module.parts
            or not reference_module.is_relative_to(dependency_site)
            or reference_module.name != '__init__.py' or reference_module.parent.name != 'docxtpl'):
        return False
    return True
