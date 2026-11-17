"""Every relative link in the repository's Markdown resolves.

A link to a file that moved, or to a section that was renamed, sends a reader
nowhere without any build failing. This checks every tracked Markdown file:
a relative link must name an existing file, and a `#section` must be one of
that file's headings.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\]\(([^)\s]+)\)")


def heading_slugs(path: Path) -> set[str]:
    """The anchors GitHub gives the headings of a Markdown file."""
    slugs: set[str] = set()
    fenced = False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        heading = re.match(r"#{1,6}\s+(.*)", line)
        if heading and not fenced:
            slugs.add(re.sub(r"[^\w\- ]", "", heading.group(1).strip().lower()).replace(" ", "-"))
    return slugs


def broken_links(document: Path) -> list[str]:
    """The relative links of `document` whose file or section does not exist."""
    broken: list[str] = []
    fenced = False
    for line in document.read_text().splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        for target in LINK.findall(line):
            if re.match(r"[a-z]+:", target):
                continue
            path, _, section = target.partition("#")
            resolved = (document.parent / path).resolve() if path else document
            if not resolved.exists():
                broken.append(target)
            elif section and resolved.suffix == ".md" and section not in heading_slugs(resolved):
                broken.append(target)
    return broken


def tracked_markdown() -> list[Path]:
    listed = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / name for name in listed.stdout.split() if not name.startswith("vendor/")]


class DocumentationLinkTests(unittest.TestCase):
    def test_every_relative_link_resolves(self) -> None:
        documents = tracked_markdown()
        self.assertTrue(documents)
        broken = {str(path.relative_to(ROOT)): links for path in documents if (links := broken_links(path))}
        self.assertEqual(broken, {})

    def test_a_missing_file_or_section_is_reported(self) -> None:
        # The check above must catch both ways a link breaks.
        with tempfile.NamedTemporaryFile("w", suffix=".md", dir=ROOT / "docs", delete=False) as sample:
            sample.write("[kept](STATUS.md#accepted-debts) [gone](NO_SUCH_FILE.md) [moved](STATUS.md#no-such-section)\n")
        try:
            self.assertEqual(broken_links(Path(sample.name)), ["NO_SUCH_FILE.md", "STATUS.md#no-such-section"])
        finally:
            Path(sample.name).unlink()


if __name__ == "__main__":
    unittest.main()
