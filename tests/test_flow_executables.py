"""Every flow names the programs it may start, and `flow_executables` is the one list of them.

`xeda remote check` looks for these programs on a remote host. A program that the code starts and
that no flow names would be missing from that report. The test reads the code of the flows: the
first argument of `Tool(...)` and `.derive(...)` (a string), `executable=` of a call, and the
`executable` default of a `Tool` class.
"""

import ast
import importlib
import pkgutil
from pathlib import Path

import xeda.flows
from xeda.introspect import all_flow_classes, flow_executables

SOURCE = Path(xeda.__file__).parent

#: Programs that the code names and no flow starts, with the reason.
NOT_STARTED = {
    "ecppll": "`EcpPLL` is a model of the tool; no flow runs it",
}


def _is_tool_call(call: ast.Call) -> bool:
    function = call.func
    name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
    return name == "derive" or name.endswith("Tool") or name == "Tool"


def _literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _named_programs(path: Path) -> set[str]:
    """The programs that the code of one module names with a string."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_tool_call(node):
            if node.args and (name := _literal(node.args[0])):
                names.add(name)
            for keyword in node.keywords:
                if keyword.arg == "executable" and (name := _literal(keyword.value)):
                    names.add(name)
        elif isinstance(node, ast.ClassDef):
            bases = {ast.unparse(base) for base in node.bases}
            if "Tool" in bases or any(base.endswith("Tool") for base in bases):
                for statement in node.body:
                    if (
                        isinstance(statement, ast.AnnAssign)
                        and getattr(statement.target, "id", "") == "executable"
                        and statement.value is not None
                        and (name := _literal(statement.value))
                    ):
                        names.add(name)
    # A program that a run builds (`./simv`) is no program of the host.
    return {name for name in names if not name.startswith((".", "/"))}


def _shipped_flows() -> list[type]:
    for module in pkgutil.walk_packages(xeda.flows.__path__, "xeda.flows."):
        importlib.import_module(module.name)
    return [cls for cls in all_flow_classes() if cls.__module__.startswith("xeda.flows.")]


def test_every_flow_names_at_least_one_program():
    shipped = _shipped_flows()

    assert shipped
    assert [cls.name for cls in shipped if not cls.executables] == []


def test_the_scan_finds_the_programs_that_the_code_names():
    named = set().union(
        *(
            _named_programs(path)
            for directory in ("flows", "flow")
            for path in (SOURCE / directory).rglob("*.py")
        )
    )

    assert {"vivado", "yosys", "vlogan", "klayout", "c++", "g++", "sh"} <= named


def test_every_program_that_the_code_names_belongs_to_a_flow():
    declared = set(flow_executables())
    missing = {}
    for directory in ("flows", "flow"):
        for path in sorted((SOURCE / directory).rglob("*.py")):
            for name in _named_programs(path) - declared - set(NOT_STARTED):
                missing.setdefault(name, []).append(str(path.relative_to(SOURCE)))

    assert not missing, f"no flow declares these programs in `executables`: {missing}"


def test_the_list_has_no_repeats_and_holds_the_programs_of_the_main_tools():
    names = flow_executables()

    assert len(names) == len(set(names))
    assert {"vivado", "yosys", "nextpnr-ecp5", "nextpnr-himbaechel", "ghdl", "openroad"} <= set(
        names
    )
