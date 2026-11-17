#!/usr/bin/env bash
# Build a portable wheel (manylinux) of the Python binding.
#
# The extension links the host's libxml2 (and the lzma it pulls in), so a wheel
# built on an ordinary distribution is only valid there and is tagged `linux`.
# Built inside a manylinux image and repaired with auditwheel, those libraries
# are copied into the wheel and the tag becomes manylinux_2_28: it installs on
# any distribution of that baseline, with no -dev packages.
#
# Run it INSIDE a manylinux_2_28 image, from the repository root:
#
#   docker run --rm -v "$PWD:/work" -w /work quay.io/pypa/manylinux_2_28_$(uname -m) \
#     bash scripts/build_portable_wheel.sh 3.12
#
# The repaired wheel lands in dist/. Check it afterwards on a system without
# libxml2 (`python:3.12-slim` has none):
#
#   docker run --rm -v "$PWD/dist:/dist:ro" python:3.12-slim \
#     bash -c 'pip install -q /dist/*manylinux*.whl python-docx Jinja2 docxcompose &&
#              python -c "import docxtpl_native; print(docxtpl_native.capabilities())"'
set -euo pipefail

version="${1:-3.12}"
tag="cp${version/./}"
python="/opt/python/$tag-$tag/bin"
test -x "$python/python" || { echo "no $python/python: run this inside a manylinux image" >&2; exit 2; }
export PATH="$python:${CARGO_HOME:-/opt/rust/cargo}/bin:$PATH"

if ! command -v cargo >/dev/null; then
    curl -fsS https://sh.rustup.rs -o /tmp/rustup.sh
    CARGO_HOME="${CARGO_HOME:-/opt/rust/cargo}" RUSTUP_HOME="${RUSTUP_HOME:-/opt/rust/rustup}" \
        sh /tmp/rustup.sh -y --profile minimal --default-toolchain 1.93.0
fi
# libclang is needed for the libxml2 bindings; both come from the image's
# package manager (manylinux_2_28 is AlmaLinux 8).
if ! test -f /usr/include/libxml2/libxml/parser.h; then
    yum install -y libxml2-devel clang-devel >/dev/null
fi
python -m pip install --quiet 'maturin==1.16.0' 'auditwheel==6.8.2'

cargo_home="${CARGO_HOME:-/opt/rust/cargo}"
export RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=$cargo_home/registry/src=/cargo/registry --remap-path-prefix=$cargo_home/git/checkouts=/cargo/git --remap-path-prefix=$PWD=."
rm -rf dist/portable-build
maturin build --release --locked --compatibility linux --out dist/portable-build
auditwheel show dist/portable-build/*.whl
# The wheel's own tag says `linux`; the repair bundles what it links and
# retags it (the libraries the manylinux policy already allows stay external).
auditwheel repair dist/portable-build/*.whl -w dist
rm -rf dist/portable-build
ls -l dist/*manylinux*.whl
