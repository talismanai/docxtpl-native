"""Reference-only evidence for the live-loop execution boundary.

These checks do not exercise the native renderer or establish parity.
"""

from __future__ import annotations

import sys
import unittest
from collections.abc import Iterator
from types import FrameType

from jinja2 import Environment
from jinja2.runtime import LoopContext


class LoopReferenceExecutionBoundaryTests(unittest.TestCase):
    def test_callback_can_invoke_reference_loop_protocols_directly(self) -> None:
        observed_calls: list[str] = []
        callback_results: list[tuple[bool, int, int]] = []
        index_getter = LoopContext.index.fget
        assert index_getter is not None
        tracked = {
            LoopContext.__next__.__code__: "next",
            index_getter.__code__: "index",
        }

        def callback(loop: LoopContext) -> str:
            previous = sys.getprofile()

            def profile(frame: FrameType, event: str, arg: object) -> None:
                del arg
                if event == "call" and frame.f_code in tracked:
                    observed_calls.append(tracked[frame.f_code])

            try:
                sys.setprofile(profile)
                index = loop.index
                item, returned_loop = next(loop)
            finally:
                sys.setprofile(previous)
            callback_results.append((type(loop) is LoopContext, index, item))
            self.assertIs(returned_loop, loop)
            return ""

        def items() -> Iterator[int]:
            yield from (1, 2, 3)

        result = Environment().from_string(
            "{% for item in items %}{% if loop.first %}{{ callback(loop) }}{% endif %}{{ item }}{% endfor %}"
        )
        self.assertEqual(result.render(items=items(), callback=callback), "13")
        self.assertEqual(callback_results, [(True, 1, 2)])
        self.assertEqual(observed_calls, ["index", "next"])


if __name__ == "__main__":
    unittest.main()
