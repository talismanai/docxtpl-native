"""Shared protocol data and document helpers; no engine selection here."""
from dataclasses import dataclass
import io
from typing import Callable
from docx import Document

@dataclass
class Result:
    observation: dict
    outputs: dict[str, bytes]

@dataclass(frozen=True)
class Probe:
    execute: Callable
    expected: dict
    assertion_only: bool = False

def template(text, header=None):
    document=Document();document.add_paragraph(text)
    if header is not None:document.sections[0].header.paragraphs[0].text=header
    source=io.BytesIO();document.save(source)
    return source.getvalue()

def generate(engine, text, context, environment=None, source=None):
    document=engine.DocxTemplate(io.BytesIO(source if source is not None else template(text)))
    document.render(context,jinja_env=environment)
    output=io.BytesIO();document.save(output)
    return output.getvalue()

def observed(blob, trace=None, **checks):
    document=Document(io.BytesIO(blob))
    observation={'text':document.paragraphs[0].text,**checks}
    if trace is not None:observation['trace']=trace
    return Result(observation,{'document.docx':blob})
