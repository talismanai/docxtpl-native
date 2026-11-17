"""The striptags, xmlattr and urlize filters match Jinja."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from jinja2 import filters
from markupsafe import Markup


class Html:
    def __html__(self) -> str:
        return '<i>x &amp; y</i>'


def values() -> dict[str, Any]:
    return {
        'html': '<p>Hello &amp;\t<b>World</b><!-- c --> x\x1cy z</p>', 'unclosed': 'a < b <!-- c', 'object': Html(),
        'markup': Markup('<b>a</b>'), 'number': 5, 'attributes': {'class': 'a"b', 'id': None, 'x': 1},
        'spaced': {'a b': 1}, 'numeric': {1: 2}, 'nested': {'a': Markup('<b>')},
        'text': ('see http://ex.com/a?b=1. and (www.foo.org) or <https://x.io/p> mail a@b.co, mailto:c@d.ef'
                 ' ftp://z.q @x a@b:c'),
        'long': 'https://example.com/very/long/path here', 'parens': 'http://a.com/(x) (http://b.com/(y)))',
        'unicode': 'héllo http://ü.com/é é@ü.com', 'lines': 'x\n http://a.com.\n',
    }


def outcome(native: bool, source: str, environment: jinja2.Environment) -> tuple[str, str]:
    try:
        if native:
            output = docxtpl_native._extension().PythonRenderSession(
                BridgeConfig(environment, False), values()
            ).render('case', source)
        else:
            output = environment.from_string(source).render(values())
        return ('output', str(output))
    except Exception as error:  # noqa: BLE001 - compared between renderers
        return (type(error).__name__, str(error))


CASES = (
    '{{ html|striptags }}', '{{ unclosed|striptags }}', '{{ object|striptags }}', '{{ markup|striptags }}',
    '{{ number|striptags }}', '{{ "&lt;x&gt; &#39;"|striptags }}',
    '{{ attributes|xmlattr }}', '{{ attributes|xmlattr(false) }}', '{{ {}|xmlattr }}', '{{ spaced|xmlattr }}',
    '{{ numeric|xmlattr }}', '{{ number|xmlattr }}', '{{ nested|xmlattr }}',
    '{{ text|urlize }}', '{{ long|urlize(10) }}', '{{ long|urlize("a") }}',
    '{{ text|urlize(nofollow=true, target="_blank", rel="me x") }}', '{{ text|urlize(extra_schemes=["ftp:"]) }}',
    '{{ text|urlize(extra_schemes=["bad scheme"]) }}', '{{ parens|urlize }}', '{{ "&lt;http://a.com&gt;"|urlize }}',
    '{{ markup|urlize }}', '{{ number|urlize }}', '{{ lines|urlize }}', '{{ unicode|urlize(5) }}',
)


class MarkupTextFilterTests(unittest.TestCase):
    def test_outputs_and_errors_match_reference(self) -> None:
        for autoescape in (False, True):
            for asynchronous in (False, True):
                environment = jinja2.Environment(autoescape=autoescape, enable_async=asynchronous)
                for source in CASES:
                    with self.subTest(autoescape=autoescape, asynchronous=asynchronous, source=source):
                        self.assertEqual(outcome(True, source, environment), outcome(False, source, environment))

    def test_urlize_policies_are_read(self) -> None:
        environment = jinja2.Environment()
        environment.policies.update({'urlize.rel': 'policy', 'urlize.target': 'frame', 'urlize.extra_schemes': ['ftp:']})
        for source in ('{{ text|urlize }}', '{{ text|urlize(rel="") }}', '{{ text|urlize(target="t") }}'):
            with self.subTest(source=source):
                self.assertEqual(outcome(True, source, environment), outcome(False, source, environment))

    def test_replacements_are_used(self) -> None:
        def urlize(text: Any, **kwargs: Any) -> str:
            return f'urlize:{text}'

        def striptags(self: Markup) -> str:
            return 'stripped'

        environment = jinja2.Environment()
        with patch.object(filters, 'urlize', urlize):
            self.assertEqual(outcome(True, '{{ text|urlize }}', environment),
                             outcome(False, '{{ text|urlize }}', environment))
        with patch.object(Markup, 'striptags', striptags):
            self.assertEqual(outcome(True, '{{ html|striptags }}', environment), ('output', 'stripped'))


if __name__ == '__main__':
    unittest.main()
