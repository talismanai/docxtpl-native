"""Independent oracles for output that cannot be represented in Rust str."""

import asyncio
from collections.abc import Callable
from types import ModuleType
from typing import Any

import jinja2
from docxtpl_native.bridge import BridgeConfig

from .protocol_base import Probe, Result


def execute(source: str, asynchronous: bool, value: str = 'a\ud800b') -> Callable[[ModuleType], Result]:
    def run(engine: ModuleType) -> Result:
        events: list[str] = []

        def later() -> str:
            events.append('later')
            return '!'

        context: dict[str, Any] = {'value': value, 'later': later}
        environment = jinja2.Environment(enable_async=asynchronous)
        if engine.__name__ not in ('docxtpl', 'docxtpl_native'):
            raise ValueError('unknown engine')
        try:
            if engine.__name__ == 'docxtpl_native':
                session = engine._extension().PythonRenderSession(BridgeConfig(environment, False), context)
                result = session.render('<string>', source)
            elif engine.__name__ == 'docxtpl':
                template = environment.from_string(source)
                result = asyncio.run(template.render_async(context)) if asynchronous else template.render(context)
            observation: dict[str, Any] = {'kind': 'result', 'value_ascii': ascii(result), 'events': events}
        except Exception as error:  # noqa: BLE001 - differential probe records all observable failures
            observation = {'kind': 'error', 'type': type(error).__name__, 'message': str(error), 'events': events}
        return Result(observation, {})

    return run


PROBES: dict[str, Probe] = {}
for label, asynchronous in (('sync', False), ('async', True)):
    PROBES['output_surrogate_direct_' + label] = Probe(
        execute('{{ value }}{{ later() }}', asynchronous),
        {'kind': 'result', 'value_ascii': "'a\\ud800b!'", 'events': ['later']}, assertion_only=True,
    )
    PROBES['output_surrogate_capture_' + label] = Probe(
        execute('{% set captured %}{{ value }}{% endset %}{{ captured|length }}', asynchronous),
        {'kind': 'result', 'value_ascii': "'3'", 'events': []}, assertion_only=True,
    )
    PROBES['output_surrogate_literal_' + label] = Probe(
        execute('{{ "a\\ud800b" }}', asynchronous),
        {'kind': 'result', 'value_ascii': "'a\\ud800b'", 'events': []}, assertion_only=True,
    )
    PROBES['output_surrogate_macro_' + label] = Probe(
        execute('{% macro m() %}{{ value }}{% endmacro %}{{ m()|length }}', asynchronous),
        {'kind': 'result', 'value_ascii': "'3'", 'events': []}, assertion_only=True,
    )
    PROBES['filter_block_nonstring_' + label] = Probe(
        execute('{% filter length %}hello{% endfilter %}{{ later() }}', asynchronous),
        {'kind': 'error', 'type': 'TypeError', 'message': 'sequence item 0: expected str instance, int found',
         'events': ['later']}, assertion_only=True,
    )
    PROBES['filter_block_error_index_' + label] = Probe(
        execute('A{{ value }}{% filter length %}abc{% endfilter %}{{ later() }}', asynchronous, 'x'),
        {'kind': 'error', 'type': 'TypeError', 'message': 'sequence item 2: expected str instance, int found',
         'events': ['later']}, assertion_only=True,
    )
