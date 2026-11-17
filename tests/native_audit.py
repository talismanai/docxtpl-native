"""Helpers for native execution audits.

An audit runs the candidate with standard Python algorithms replaced by
failures, so the test fails when one of them runs. Test modules that import
this helper and do not reach docxtpl form the audit tier of
`scripts/run_product_tests.py`, which runs them with every docxtpl import
blocked. docxtpl is therefore never a target here: the product does not import
it, and that runner fails when anything does.

An audit proves which code ran, not that the output is right. Comparing the
output with the reference is a differential test and lives in another module.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch


def forbid(*targets: str, message: str = "Python algorithm called") -> ExitStack:
    """Patch each dotted target to raise AssertionError; close the result to restore them."""
    stack = ExitStack()
    try:
        for target in targets:
            stack.enter_context(patch(target, side_effect=AssertionError(f"{message}: {target}")))
    except BaseException:
        stack.close()
        raise
    return stack


class CodeCalls:
    """Record calls of the given functions' code objects while active.

    Use it where a function cannot be patched because the candidate holds the
    original object (a filter registered on an environment, a method of a
    class): the profiler sees every frame that runs that code.
    """

    def __init__(self, *functions: object) -> None:
        self.codes = {getattr(function, '__code__'): getattr(function, '__qualname__', repr(function))
                      for function in functions}
        self.calls: list[str] = []
        self._previous: object = None

    def __enter__(self) -> CodeCalls:
        import sys

        def profile(frame: object, event: str, argument: object) -> None:
            code = getattr(frame, 'f_code', None)
            if event == 'call' and code in self.codes:
                self.calls.append(self.codes[code])

        self._previous = sys.getprofile()
        sys.setprofile(profile)
        return self

    def __exit__(self, *exc_info: object) -> None:
        import sys
        sys.setprofile(self._previous)  # type: ignore[arg-type]
