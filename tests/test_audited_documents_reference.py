"""Complete documents of the native audits compared with the pinned docxtpl.

The audit modules run each document with standard Python algorithms forbidden
and check it against fixed expectations, without the reference. This module
compares the same documents, built by the same functions, with docxtpl. The
candidate runs here without audit guards: execution and output equivalence
are separate evidence.
"""
from __future__ import annotations

import unittest
from collections.abc import Callable, Mapping
from typing import Any

import docxtpl
import docxtpl_native
from paritylab import (
    finalizer_probes,
    include_scope_probes,
    language_spec_probes,
    native_predicate_probes,
    runtime_error_probes,
)
from paritylab.compare import compare_docx
from paritylab.protocol_base import Probe, Result
from test_native_call_algorithms import dispatch_document
from test_native_concat_algorithms import concat_document
from test_native_context_builders import context_document
from test_native_format_algorithms import format_document
from test_native_lookup import lookup_document
from test_native_sandbox_call import sandbox_document
from test_native_sandbox_operators import operators_document


@unittest.skipUnless(docxtpl_native.capabilities()['native_available'],
                     'native extension not built; this is not parity evidence')
class AuditedDocumentReferenceTests(unittest.TestCase):
    def assert_documents(self, reference: Mapping[str, bytes], native: Mapping[str, bytes]) -> None:
        self.assertEqual(set(native), set(reference))
        for name in reference:
            comparison = compare_docx(reference[name], native[name])
            self.assertTrue(comparison['equivalent'], (name, comparison))

    def assert_probes(self, probes: Mapping[str, Probe], single_output: bool = False) -> None:
        for name, probe in probes.items():
            with self.subTest(case=name):
                reference: Result = probe.execute(docxtpl)
                native: Result = probe.execute(docxtpl_native)
                self.assertEqual(reference.observation, probe.expected)
                self.assertEqual(native.observation, reference.observation)
                if single_output:
                    self.assertEqual(len(reference.outputs), 1)
                self.assert_documents(reference.outputs, native.outputs)

    def assert_blobs(self, build: Callable[..., Any], *arguments: Any) -> None:
        reference, native = build(docxtpl, *arguments), build(docxtpl_native, *arguments)
        if isinstance(reference, tuple):
            (reference, reference_trace), (native, native_trace) = reference, native
            self.assertEqual(native_trace, reference_trace)
        self.assert_documents({'document.docx': reference}, {'document.docx': native})

    def test_include_scope_documents(self) -> None:
        for asynchronous in (False, True):
            for name, (source, expected) in include_scope_probes.CASES.items():
                with self.subTest(case=name, asynchronous=asynchronous):
                    reference = include_scope_probes.execute(docxtpl, source=source, asynchronous=asynchronous)
                    native = include_scope_probes.execute(docxtpl_native, source=source, asynchronous=asynchronous)
                    self.assertEqual(reference.observation, {'text': expected})
                    self.assertEqual(native.observation, reference.observation)
                    self.assertTrue(reference.outputs)
                    self.assert_documents(reference.outputs, native.outputs)

    def test_language_documents(self) -> None:
        self.assert_probes(language_spec_probes.PROBES)

    def test_predicate_documents(self) -> None:
        self.assert_probes(native_predicate_probes.PROBES)

    def test_runtime_error_documents(self) -> None:
        self.assert_probes(runtime_error_probes.PROBES, single_output=True)

    def test_finalizer_documents(self) -> None:
        self.assert_probes(finalizer_probes.PROBES)

    def test_lookup_sandbox_dispatch_and_context_documents(self) -> None:
        for asynchronous in (False, True):
            with self.subTest(document='lookup', asynchronous=asynchronous):
                self.assert_blobs(lookup_document, asynchronous)
            with self.subTest(document='sandbox call', asynchronous=asynchronous):
                self.assert_blobs(sandbox_document, asynchronous)
            with self.subTest(document='sandbox operators', asynchronous=asynchronous):
                self.assert_blobs(operators_document, asynchronous)
        with self.subTest(document='dispatch'):
            self.assert_blobs(dispatch_document)
        with self.subTest(document='context'):
            self.assert_blobs(context_document)

    def test_concat_and_format_documents(self) -> None:
        for autoescape in (False, True):
            with self.subTest(document='concat', autoescape=autoescape):
                self.assert_blobs(concat_document, autoescape)
            with self.subTest(document='format', autoescape=autoescape):
                self.assert_blobs(format_document, autoescape)


if __name__ == '__main__':
    unittest.main()
