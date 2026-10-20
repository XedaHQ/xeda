"""Every flow names the programs it starts, and `flow_executables` is the one list of them.

`xeda remote check` looks for these programs on a remote host. A flow that starts a program and
does not name it would be missing from that report.
"""

import importlib
import pkgutil

import xeda.flows
from xeda.introspect import all_flow_classes, flow_executables
from xeda.tool import Tool

#: Programs that only some designs need (`nextpnr` runs `ecppll` for an ECP5 PLL setting).
HELPERS = {"ecppll"}


def _tool_classes() -> list[type[Tool]]:
    for module in pkgutil.walk_packages(xeda.flows.__path__, "xeda.flows."):
        importlib.import_module(module.name)
    found: list[type[Tool]] = []
    pending = [Tool]
    while pending:
        for subclass in pending.pop().__subclasses__():
            pending.append(subclass)
            if subclass.__module__.startswith("xeda.flows."):
                found.append(subclass)
    return found


def test_every_flow_names_at_least_one_program():
    # Other tests register flows of their own; only the shipped flows count here.
    shipped = [cls for cls in all_flow_classes() if cls.__module__.startswith("xeda.flows.")]

    assert shipped
    assert [cls.name for cls in shipped if not cls.executables] == []


def test_the_default_program_of_every_tool_class_of_the_flows_is_named():
    named = set(flow_executables())
    tools = {
        cls.model_fields["executable"].default
        for cls in _tool_classes()
        if isinstance(cls.model_fields["executable"].default, str)
    }
    assert tools, "the sweep found no tool class"
    assert tools - HELPERS <= named, sorted(tools - HELPERS - named)


def test_the_list_has_no_repeats_and_holds_the_programs_of_the_main_tools():
    names = flow_executables()

    assert len(names) == len(set(names))
    assert {"vivado", "yosys", "nextpnr-ecp5", "nextpnr-himbaechel", "ghdl", "openroad"} <= set(
        names
    )
