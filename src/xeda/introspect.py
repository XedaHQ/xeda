"""Machine-readable introspection of Xeda's flows, settings, results and design schema.

The CLI output of `--json` and `--format`, the generated documentation and the reference files
of the agent skill all read from this module. Every function returns plain JSON-serializable
data and never touches the console, so other code can use it too:

    >>> from xeda.introspect import flow_info, settings_info
    >>> flow_info("vivado_synth")["category"]
    'fpga_synthesis'
"""

from ._internal.introspect import (
    boards_info,
    design_schema,
    flow_info,
    flows_info,
    platforms_info,
    results_info,
    settings_info,
)

__all__ = [
    "flows_info",
    "flow_info",
    "settings_info",
    "results_info",
    "design_schema",
    "boards_info",
    "platforms_info",
]
