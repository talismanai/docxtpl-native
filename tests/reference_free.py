"""Run code with every import of docxtpl blocked and recorded.

`run(workload)` executes `workload` in a fresh interpreter. The run fails when
docxtpl is already loaded, when any import of docxtpl or one of its submodules
is attempted (even if the caller catches the ImportError), or when the
workload itself fails.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "REFERENCE-FREE-OK"

PRELUDE = '''
import importlib.abc
import sys

_attempts = []

def _is_reference(name):
    return name == "docxtpl" or name.startswith("docxtpl.")

class _BlockReference(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if _is_reference(name):
            _attempts.append(name)
            raise ImportError("docxtpl is not available in this run: " + name)
        return None

_loaded = sorted(name for name in sys.modules if _is_reference(name))
if _loaded:
    raise SystemExit("docxtpl was loaded before the workload: " + ", ".join(_loaded))
sys.meta_path.insert(0, _BlockReference())
'''

EPILOGUE = '''
_loaded = sorted(name for name in sys.modules if _is_reference(name))
if _attempts or _loaded:
    raise SystemExit("docxtpl was used: attempts={} loaded={}".format(_attempts, _loaded))
print("MARKER")
'''


def script(workload: str) -> str:
    return PRELUDE + textwrap.dedent(workload) + EPILOGUE.replace("MARKER", MARKER)


def run(workload: str, *, cwd: Path | None = None, python: str = sys.executable,
        source_tree: bool = True) -> subprocess.CompletedProcess[str]:
    """Run in a new interpreter; `source_tree=False` uses only the installed packages."""
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    if source_tree:
        environment["PYTHONPATH"] = str(ROOT / "python")
    return subprocess.run([python, "-c", script(workload)], cwd=cwd, env=environment,
                          capture_output=True, text=True, check=False)


def passed(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == 0 and result.stdout.rstrip().endswith(MARKER)


# A tiny valid PNG (1x1, RGB), so the workload needs no imaging library.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
       b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")

# Everything a user of the minimal installation does. Runs in a temporary
# working directory.
WORKLOAD = """
import io, json, runpy, sys
from pathlib import Path
from docx import Document
from docx.shared import Mm
from jinja2 import Environment
import docxtpl_native as d

PNG = %r

def template(*lines):
    source = Document()
    for line in lines:
        source.add_paragraph(line)
    data = io.BytesIO()
    source.save(data)
    return data.getvalue()

def text(data):
    return [paragraph.text for paragraph in Document(io.BytesIO(data)).paragraphs]

def save(tpl):
    output = io.BytesIO()
    tpl.save(output)
    return output.getvalue()

# Plain data, rich values, listing, image and hyperlink on the package path.
tpl = d.DocxTemplate(io.BytesIO(template(
    "Hello {{ name }}", "{{r rich }}", "{{p para }}", "{{ listing }}", "{{ image }}", "{{r link }}")))
rich = d.RichText("bold", bold=True)
rich.add(d.R(" and italic", italic=True))
para = d.RichTextParagraph()
para.add("styled", parastyle="Normal")
link = d.RichText("site", url_id=tpl.build_url_id("https://example.test"))
tpl.render({"name": "Ada", "rich": rich, "para": para, "listing": d.Listing("a<b"),
            "image": d.InlineImage(tpl, io.BytesIO(PNG), width=Mm(5)), "link": link})
rendered = save(tpl)
lines = text(rendered)
assert lines[0] == "Hello Ada", lines
assert lines[1] == "bold and italic", lines
assert "styled" in lines, lines
assert "a<b" in lines, lines
assert b"word/media/" in rendered

# Python callbacks and a supplied environment select the python-docx model path.
tpl = d.DocxTemplate(io.BytesIO(template("{{ shout(name) }}", "{{ name|twice }}")))
environment = Environment()
environment.filters["twice"] = lambda value: value * 2
tpl.render({"name": "ada", "shout": str.upper}, jinja_env=environment)
assert text(save(tpl))[:2] == ["ADA", "adaada"]

# Programmatic and file subdocuments.
tpl = d.DocxTemplate(io.BytesIO(template("{{p programmatic }}")))
sub = tpl.new_subdoc()
sub.add_paragraph("from a subdocument")
tpl.render({"programmatic": sub})
assert "from a subdocument" in text(save(tpl))
Path("part.docx").write_bytes(template("from a file"))
tpl = d.DocxTemplate(io.BytesIO(template("{{p included }}")))
tpl.render({"included": tpl.new_subdoc("part.docx")})
assert "from a file" in text(save(tpl))

# Analysis, document access, replacements, composition and files on disk.
Path("template.docx").write_bytes(template("{{ first }} {{ second }}"))
tpl = d.DocxTemplate("template.docx")
assert tpl.get_undeclared_template_variables() == {"first", "second"}
assert tpl.get_docx().paragraphs[0].text == "{{ first }} {{ second }}"
tpl.render({"first": 1, "second": 2})
tpl.save("output.docx")
assert text(Path("output.docx").read_bytes())[0] == "1 2"
assert [line for line in text(d.compose([template("one"), template("two")])) if line] == ["one", "two"]
assert d.capabilities()["native_available"] and d.capabilities()["reference_fallback"] is False

# The command-line interface.
Path("data.json").write_text(json.dumps({"first": "a", "second": "b"}))
sys.argv = ["docxtpl_native", "template.docx", "data.json", "cli.docx", "-o", "-q"]
runpy.run_module("docxtpl_native", run_name="__main__")
assert text(Path("cli.docx").read_bytes())[0] == "a b"
""" % (PNG,)
