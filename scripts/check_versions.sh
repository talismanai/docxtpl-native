#!/usr/bin/env bash
# The project has one version: `[workspace.package] version` in Cargo.toml.
# pyproject.toml and python/docxtpl_native/__init__.py repeat it for Python
# tooling. This prints the version, or fails naming the place that disagrees.
set -euo pipefail
cd "$(dirname "$0")/.."

workspace="$(sed -n '/^\[workspace\.package\]/,/^\[/s/^version = "\(.*\)"$/\1/p' Cargo.toml)"
project="$(sed -n '/^\[project\]/,/^\[/s/^version = "\(.*\)"$/\1/p' pyproject.toml)"
package="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' python/docxtpl_native/__init__.py)"

if [ -z "$workspace" ]; then
    echo "versions: no [workspace.package] version in Cargo.toml" >&2
    exit 1
fi
failed=0
if [ "$project" != "$workspace" ]; then
    echo "versions: pyproject.toml has '$project', Cargo.toml has '$workspace'" >&2
    failed=1
fi
if [ "$package" != "$workspace" ]; then
    echo "versions: python/docxtpl_native/__init__.py has '$package', Cargo.toml has '$workspace'" >&2
    failed=1
fi
# Every crate of the project takes the workspace version.
for manifest in crates/*/Cargo.toml bindings/python/Cargo.toml; do
    if ! grep -q '^version\.workspace = true$' "$manifest"; then
        echo "versions: $manifest does not use version.workspace = true" >&2
        failed=1
    fi
done
[ "$failed" -eq 0 ] || exit 1
echo "$workspace"
