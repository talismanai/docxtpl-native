"""Bounded checks for the opt-in await protocol diagnostic."""

import asyncio
import sys
import unittest
from types import FrameType
from typing import Any

import docxtpl_native
import jinja2
import jinja2.async_utils
from docxtpl_native import bridge


class AwaitAdapterProbeTests(unittest.TestCase):
    def test_adapter_does_not_call_jinja_auto_await_in_direct_render(self) -> None:
        async def value() -> str:
            await asyncio.sleep(0)
            return 'ok'

        environment = jinja2.Environment(enable_async=True)
        config = bridge.BridgeConfig(environment, False)
        config.package_values_live = True
        session = docxtpl_native._extension().PythonRenderSession(config, {'value': value})
        session._enable_await_adapter_probe()
        seen: list[object] = []
        target_codes = (bridge._await_native_value.__code__, jinja2.async_utils.auto_await.__code__)

        def profile(frame: FrameType, event: str, arg: Any) -> None:
            if event == 'call' and frame.f_code in target_codes:
                seen.append(frame.f_code)

        previous = sys.getprofile()
        try:
            sys.setprofile(profile)
            self.assertEqual(session.render('source', '{{ value() }}'), 'ok')
        finally:
            sys.setprofile(previous)
        self.assertIn(target_codes[0], seen)
        self.assertNotIn(target_codes[1], seen)
