"""Whether python-docx's save chain is the one the native exposed save ports.

The classes and module globals that ``Document.save`` reaches are captured
when this module is imported. A save runs natively only while every captured
class keeps the same members (and functions the same code) and every global
still names the captured object; otherwise python-docx saves as usual.
"""

from __future__ import annotations

import posixpath
import zipfile
from types import FunctionType
from typing import Any

import docx.document
import docx.opc.oxml as opc_oxml
import docx.opc.package as opc_package
import docx.opc.rel as opc_rel
import docx.opc.shared as opc_shared
import docx.opc.spec as opc_spec
import docx.package
import docx.parts.document
from docx.opc import packuri, phys_pkg, pkgwriter

DOCUMENT = docx.document.Document
DOCUMENT_PART = docx.parts.document.DocumentPart
PACKAGES = (opc_package.OpcPackage, docx.package.Package)
RELATIONSHIPS = opc_rel.Relationships
PACKURI = packuri.PackURI

_CLASSES = (DOCUMENT, DOCUMENT_PART, *PACKAGES, pkgwriter.PackageWriter, pkgwriter._ContentTypesItem,
            phys_pkg.PhysPkgWriter, phys_pkg._ZipPkgWriter, RELATIONSHIPS, PACKURI, opc_oxml.CT_Relationships,
            opc_oxml.CT_Relationship, opc_oxml.CT_Types, opc_oxml.CT_Default, opc_oxml.CT_Override,
            opc_shared.CaseInsensitiveDict)

_GLOBALS = (
    (opc_package, 'PackageWriter', pkgwriter.PackageWriter),
    (pkgwriter, 'PhysPkgWriter', phys_pkg.PhysPkgWriter),
    (pkgwriter, 'CT_Types', opc_oxml.CT_Types),
    (pkgwriter, 'serialize_part_xml', opc_oxml.serialize_part_xml),
    (pkgwriter, 'CaseInsensitiveDict', opc_shared.CaseInsensitiveDict),
    (pkgwriter, 'default_content_types', opc_spec.default_content_types),
    (pkgwriter, 'CONTENT_TYPES_URI', packuri.CONTENT_TYPES_URI),
    (pkgwriter, 'PACKAGE_URI', packuri.PACKAGE_URI),
    (phys_pkg, 'ZipFile', zipfile.ZipFile),
    (phys_pkg, 'ZIP_DEFLATED', zipfile.ZIP_DEFLATED),
    (opc_rel, 'CT_Relationships', opc_oxml.CT_Relationships),
    (opc_oxml, 'serialize_part_xml', opc_oxml.serialize_part_xml),
    (packuri, 'posixpath', posixpath),
)


def _functions(member: Any) -> tuple[Any, ...]:
    if type(member) is property:
        return (member.fget, member.fset, member.fdel)
    if type(member) in (staticmethod, classmethod):
        return (member.__func__,)
    return (member,)


def _snapshot(cls: type) -> tuple[dict[str, Any], tuple[tuple[Any, Any], ...]]:
    members = dict(vars(cls))
    codes = tuple((function, function.__code__) for member in members.values()
                  for function in _functions(member) if type(function) is FunctionType)
    return members, codes


_SNAPSHOTS = tuple((cls, *_snapshot(cls)) for cls in _CLASSES)
_FUNCTIONS = ((opc_oxml.serialize_part_xml, opc_oxml.serialize_part_xml.__code__),)


def save_chain_is_standard(document: object) -> bool:
    """Whether saving `document` would run python-docx's captured save chain."""
    if type(document) is not DOCUMENT:
        return False
    for module, name, value in _GLOBALS:
        if module.__dict__.get(name) is not value:
            return False
    for function, code in _FUNCTIONS:
        if function.__code__ is not code:
            return False
    for cls, members, codes in _SNAPSHOTS:
        current = vars(cls)
        if len(current) != len(members) or any(current.get(name) is not member for name, member in members.items()):
            return False
        if any(function.__code__ is not code for function, code in codes):
            return False
    return True
