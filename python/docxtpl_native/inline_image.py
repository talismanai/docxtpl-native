"""The image value. Insertion runs in the native extension.

`OxmlElement`, `qn` and `str` are looked up in this module's globals when an
image is built, so replacing them here changes the picture XML the way
replacing them in python-docx-based code would.
"""
from __future__ import annotations

from typing import Any

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from . import _extension

__all__ = ["InlineImage"]
# Read by the extension through this module's globals.
_XML_HELPERS = (OxmlElement, qn)


class InlineImage:
    """A picture inserted in the run that contains its template tag."""

    tpl: Any = None
    image_descriptor: Any = None
    width: Any = None
    height: Any = None
    anchor: Any = None
    title: str | None = None
    descr: str | None = None

    def __init__(self, tpl: Any, image_descriptor: Any, width: Any = None, height: Any = None,
                 anchor: Any = None, title: Any = None, descr: Any = None) -> None:
        _extension().image_init(self, tpl, image_descriptor, width, height, anchor, title, descr)

    def _set_doc_properties(self, run: Any) -> Any:
        return _extension().image_set_properties(self, run)

    def _add_hyperlink(self, run: Any, url: Any, part: Any) -> Any:
        return _extension().image_add_hyperlink(run, url, part)

    def _insert_image(self) -> str:
        return _extension().image_insert(self)

    def __unicode__(self) -> str: return self._insert_image()
    def __str__(self) -> str: return self._insert_image()
    def __html__(self) -> str: return self._insert_image()
