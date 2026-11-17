#!/usr/bin/env bash
# The engine is Rust only. This fails when an engine crate
#   - gains a Python dependency (PyO3, libpython) on any target,
#   - enables a C deflate backend (it would replace the audited zlib-rs fork),
#   - links any C library in the browser path (wasm32),
#   - gets a build script, or spawns a process, in its own sources.
# It needs cargo only: no Python, no network beyond what `--locked` resolves.
set -euo pipefail
cd "$(dirname "$0")/.."

# Every crate a Rust or WebAssembly consumer builds.
engine=(docxtpl docxtpl-core docxtpl-markers docxtpl-pdf docxtpl-pycompat docxtpl-wasm docxtplrs minijinja zlib-rs)
# The subset that must also build for the browser, with no C at all.
browser=(docxtpl-markers docxtpl-pdf docxtpl-pycompat docxtpl-wasm)
# The facade, with every layer a browser pipeline uses, is held to the same bar.
browser_features="browser"

python='^(pyo3|pyo3-[a-z-]+|python3-sys|cpython|rustpython[a-z-]*|inline-python) v'
c_deflate='^(libz-sys|libz-ng-sys|libz-rs-sys|cloudflare-zlib-sys) v'
c_library='^[a-z0-9_-]+-sys v|^(cc|bindgen|cmake|pkg-config|libloading) v'

failed=0
report() { echo "engine boundary: $1" >&2; failed=1; }

tree() { cargo tree --locked --edges normal,build --prefix none "$@"; }

for crate in "${engine[@]}"; do
    graph="$(tree -p "$crate" --target all)"
    if hit="$(grep -E "$python" <<<"$graph" | sort -u)" && [ -n "$hit" ]; then
        report "$crate depends on Python: $(tr '\n' ' ' <<<"$hit")"
    fi
    if hit="$(grep -E "$c_deflate" <<<"$graph" | sort -u)" && [ -n "$hit" ]; then
        report "$crate enables a C deflate backend: $(tr '\n' ' ' <<<"$hit")"
    fi
done

# A plain root build must not select the opt-in binding either.
graph="$(tree --target all)"
if hit="$(grep -E "$python" <<<"$graph" | sort -u)" && [ -n "$hit" ]; then
    report "default workspace members depend on Python: $(tr '\n' ' ' <<<"$hit")"
fi

for crate in "${browser[@]}"; do
    graph="$(tree -p "$crate" --target wasm32-unknown-unknown)"
    if hit="$(grep -E "$python|$c_library" <<<"$graph" | sort -u)" && [ -n "$hit" ]; then
        report "$crate (wasm32) depends on Python or C: $(tr '\n' ' ' <<<"$hit")"
    fi
done

# No build scripts and no process spawning in the crates this project owns.
for directory in crates/docxtpl crates/docxtpl-core crates/docxtpl-markers crates/docxtpl-pdf \
                 crates/docxtpl-pycompat crates/docxtpl-wasm; do
    if [ -e "$directory/build.rs" ]; then
        report "$directory has a build script"
    fi
    if hit="$(grep -rnE 'std::process::Command|process::Command|Command::new|pyo3::' "$directory/src" || true)" && [ -n "$hit" ]; then
        report "$directory spawns a process or uses PyO3: $hit"
    fi
done

if [ "$failed" -ne 0 ]; then
    echo "See AGENTS.md, \"Engine boundary\"." >&2
    exit 1
fi
echo "engine boundary: ${engine[*]} are free of Python; ${browser[*]} are free of C on wasm32"
