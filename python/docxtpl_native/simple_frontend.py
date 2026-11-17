"""A closed source grammar for the default, data-only package path.

Literal text and ASCII context names with conservative attribute, integer-index
and quoted-ASCII-key paths are accepted, including nested if/elif/else branches
whose conditions combine those paths with not/and/or. Other statements,
comments, unsupported literals, missing/reserved roots and arbitrary Python
objects go through the complete original-language validation adapter. Parser
overrides also use that adapter. No filters or other compile-time callbacks can
occur in the accepted grammar.
"""


def context_names(context: object, *, compare_metaclasses: bool = True) -> frozenset[str] | None:
    from . import _extension
    if compare_metaclasses:
        return _extension().simple_context_names(context)
    return _extension().simple_context_names(context, False)


_STANDARD_CONTEXT_NAMES = context_names
_STANDARD_CONTEXT_NAMES_CODE = context_names.__code__


def accepts(source,names):
    from . import _extension
    return _extension().simple_source_accepts(source,names)
