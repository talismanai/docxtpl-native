#!/bin/bash
# Deployment evidence for one target of `deployment.python_platform_matrix`.
#
#   scripts/qualify_deployment_target.sh 3.12 reports/linux-x86_64-cp312.json
#
# Run it in a clean checkout of the revision under test, on the platform and
# architecture of the target itself (a container of that architecture is
# enough). It builds the wheel, installs the pinned reference beside it and
# writes the installation report the compatibility matrix accepts with
# `--deployment-report`. The report names the revision of the checkout, so a
# dirty tree or another commit qualifies nothing.
set -eu
PYTHON_TARGET=${1:?usage: qualify_deployment_target.sh <python-version> <report-path>}
REPORT=${2:?usage: qualify_deployment_target.sh <python-version> <report-path>}
test -f pyproject.toml || { echo 'run this from the repository root' >&2; exit 2; }
mkdir -p "$(dirname "$REPORT")"
uv venv --python "$PYTHON_TARGET" .venv
uv pip install --python .venv/bin/python 'maturin==1.16.0' 'python-docx==1.2.0' 'Jinja2==3.1.6' \
  'lxml==6.1.1' 'docxcompose==1.4.0' 'setuptools==80.9.0' 'Babel==2.18.0'
# The reference comes from its pinned checkout, never from the package index.
.venv/bin/python scripts/fetch_upstream.py
uv pip install --python .venv/bin/python --no-deps .upstream/docxtpl
# Build for this venv's interpreter, into a directory of its own: a wheel
# left over from another target would be picked up by the loop below.
tag=cp${PYTHON_TARGET/./}
output=dist/$tag
rm -rf "$output"
.venv/bin/maturin build --release --locked --compatibility linux \
  --interpreter .venv/bin/python --out "$output"
.venv/bin/python scripts/check_wheel.py "$output"
site=$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
for wheel in "$output"/*.whl; do
  case $(basename "$wheel") in
    *-$tag-$tag-*) ;;
    *) echo "the wheel is not for $PYTHON_TARGET: $wheel" >&2; exit 3 ;;
  esac
  .venv/bin/python python/paritylab/check_wheel_install.py --wheel "$wheel" --dependency-site "$site" > "$REPORT.tmp"
  mv "$REPORT.tmp" "$REPORT"
done
echo "deployment report: $REPORT"
