"""The literals of the opt-in real-tool tests are valid today, checked without any launch.

`test_optin_planning.py` plans each opt-in test. This file reads the same test modules (and the
helpers of `tool_utils.py`) as text and checks what a plan cannot reach:

- the settings mapping that a call of `run_flow`, `launch_flow` or `<Flow>.Settings.from_input`
  gets as a literal (a `{...}`, or a module constant such as `IMPL_SETTINGS`, with `**` spreads
  of them) is valid for `<Flow>.Settings`, as far as its values are literal: a bare number for a quantity, a removed
  or renamed setting and a wrong letter case fail here, in a helper that only a tool runs too;
- every VHDL or SystemVerilog testbench text that a test writes ends the simulation with a
  statement (`std.env.finish`, `$finish`, `$stop`, `$fatal` and so on), since a simulation that
  ends by an empty event queue fails by design.

Not covered: a mapping or a text that the test builds from a parameter, a variable or a call (the
check skips such a value, and a testbench text with such a part); values a test reads after its
launch (metrics, artifact keys).
"""

import ast
import inspect
import re
from pathlib import Path
from typing import Any

import pytest

from xeda._internal import flows as builtin_flows
from xeda._internal.flow.flow import Flow, FlowSettingsError

from .optin_plan_probe import OPT_IN_REQUIREMENTS

pytestmark = pytest.mark.python_compat

TESTS = Path(__file__).parent
LAUNCH_CALLS = {"run_flow", "launch_flow"}
#: The statements that end a simulation. VHDL is not case-sensitive; SystemVerilog is.
VHDL_END = re.compile(r"\bstd\.env\.(finish|stop)\b|\b(finish|stop)\s*(\(|;)", re.IGNORECASE)
VERILOG_END = re.compile(r"\$(finish|stop|fatal)\b")
#: Comments and string literals, which hold no statement: VHDL and SystemVerilog.
VHDL_NOT_CODE = re.compile(r'--[^\n]*|"(?:[^"\n]|"")*"')
VERILOG_NOT_CODE = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:[^"\\\n]|\\.)*"', re.DOTALL)
#: A VHDL process without a sensitivity list runs from the start of the simulation.
END_PROCESS = re.compile(r"\bend\s+process\b", re.IGNORECASE)
FREE_PROCESS = re.compile(r"\bprocess\b(?!\s*\()", re.IGNORECASE)


def opt_in_modules() -> list[Path]:
    """The test modules that reach a `require_*` check of an opt-in layer, and `tool_utils.py`."""
    modules = [TESTS / "tool_utils.py"]
    for path in sorted(TESTS.glob("test_*.py")):
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, (ast.Name, ast.Attribute))
        } & set(OPT_IN_REQUIREMENTS)
        if names and path.name != Path(__file__).name:
            modules.append(path)
    return modules


def module_constants(tree: ast.Module) -> dict[str, ast.expr]:
    """The module-level `NAME = <expression>` assignments, by name."""
    return {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }


def literal_mapping(node: ast.expr, constants: dict[str, ast.expr]) -> dict[str, Any] | None:
    """The part of a dict expression that is literal, or `None` if `node` is no dict expression.

    A key or a value that is not literal is left out; a `**` of a module constant is read."""
    if isinstance(node, ast.Name) and node.id in constants:
        return literal_mapping(constants[node.id], constants)
    if not isinstance(node, ast.Dict):
        return None
    found: dict[str, Any] = {}
    for key, value in zip(node.keys, node.values):
        if key is None:
            found.update(literal_mapping(value, constants) or {})
            continue
        try:
            found[ast.literal_eval(key)] = ast.literal_eval(value)
        except ValueError:
            continue
    return found


def flow_settings_literals(path: Path) -> list[tuple[int, type[Flow], dict[str, Any]]]:
    """The settings literals of `path` for a built-in flow class: line, class, settings.

    A literal is the third argument of a call of `run_flow` or `launch_flow` with the class as
    its first, or the first argument of `<Class>.Settings.from_input`. A mapping with no literal
    entry is left out, and so is a call in a `with pytest.raises(...)` block: it expects a
    refusal."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = module_constants(tree)
    refused = {
        id(child)
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(ast.unparse(item.context_expr).startswith("pytest.raises") for item in node.items)
        for child in ast.walk(node)
    }
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in refused:
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
        if name in LAUNCH_CALLS and len(node.args) >= 3:
            class_node, settings_node = node.args[0], node.args[2]
        elif (
            name == "from_input"
            and node.args
            and isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Attribute)
            and function.value.attr == "Settings"
        ):
            class_node, settings_node = function.value.value, node.args[0]
        else:
            continue
        flow_class = getattr(builtin_flows, getattr(class_node, "id", ""), None)
        settings = literal_mapping(settings_node, constants)
        if inspect.isclass(flow_class) and issubclass(flow_class, Flow) and settings:
            found.append((node.lineno, flow_class, settings))
    return found


def settings_cases() -> list:
    return [
        pytest.param(cls, settings, id=f"{path.name}:{line}")
        for path in opt_in_modules()
        for line, cls, settings in flow_settings_literals(path)
    ]


@pytest.mark.parametrize(("flow_class", "settings"), settings_cases())
def test_the_settings_literal_of_an_opt_in_launch_is_valid(flow_class, settings) -> None:
    """The literal is valid, but for a file that the test writes first: it is not there to find."""
    try:
        flow_class.Settings.from_input(settings)
    except FlowSettingsError as error:
        other = [finding for finding in error.errors if "file not found" not in (finding[1] or "")]
        assert not other, str(error)


def test_the_scan_finds_the_launches_of_the_opt_in_layers() -> None:
    cases = settings_cases()
    assert len(cases) > 20
    names = {path.name for path in opt_in_modules()}
    assert {"tool_utils.py", "test_dockerized.py", "test_vivado_real.py"} <= names


def test_a_bare_number_for_a_quantity_setting_is_found() -> None:
    with pytest.raises(Exception, match="timeout"):
        builtin_flows.Modelsim.Settings.from_input({"docker": True, "timeout": 60})


def string_text(node: ast.expr, constants: dict[str, ast.expr]) -> str | None:
    """The text of a string expression: constants, `+`, f-strings and module constants. A part
    that is not literal makes the result `None`."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name) and node.id in constants:
        return string_text(constants[node.id], constants)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = string_text(node.left, constants), string_text(node.right, constants)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = [string_text(part, constants) for part in node.values]
        return None if None in parts else "".join(parts)  # type: ignore[arg-type]
    if isinstance(node, ast.FormattedValue) and node.conversion == -1 and node.format_spec is None:
        return string_text(node.value, constants)  # `{TEXT}` of a literal text; else `None`
    return None


def bench_texts(path: Path) -> list[tuple[int, str, str]]:
    """The VHDL and SystemVerilog testbench texts of `path` that are fully literal: line,
    language (`vhdl` or `verilog`), text.

    A VHDL text is a testbench if it has a process without a sensitivity list; a SystemVerilog
    text is one if it has an `initial` block. A string expression is read whole: a part of one
    that has a part that is not literal is not read."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = module_constants(tree)
    inside: set[int] = set()
    found = []
    for node in ast.walk(tree):
        if id(node) in inside or not isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr)):
            continue
        inside.update(id(child) for child in ast.walk(node))
        text = string_text(node, constants)
        if text is None:
            continue
        vhdl = "architecture" in text and FREE_PROCESS.search(END_PROCESS.sub("", text))
        verilog = re.search(r"\bmodule\b", text) and re.search(r"\binitial\b", text)
        if vhdl or verilog:
            found.append((node.lineno, "vhdl" if vhdl else "verilog", text))
    return found


def ends_the_simulation(language: str, text: str) -> bool:
    """Whether `text` has a statement that ends the simulation, outside comments and strings."""
    if language == "vhdl":
        return bool(VHDL_END.search(VHDL_NOT_CODE.sub(" ", text)))
    return bool(VERILOG_END.search(VERILOG_NOT_CODE.sub(" ", text)))


def bench_cases() -> list:
    return [
        pytest.param(language, text, id=f"{path.name}:{line}")
        for path in opt_in_modules()
        for line, language, text in bench_texts(path)
    ]


@pytest.mark.parametrize(("language", "text"), bench_cases())
def test_a_testbench_of_an_opt_in_test_ends_the_simulation_with_a_statement(
    language: str, text: str
) -> None:
    assert ends_the_simulation(language, text), text


def test_the_scan_finds_the_testbenches_of_the_opt_in_layers() -> None:
    assert len(bench_cases()) >= 5


def test_a_testbench_that_ends_by_an_empty_event_queue_is_found() -> None:
    text = 'entity tb is end; architecture a of tb is begin process begin report "x"; wait; end process; end;'
    assert not ends_the_simulation("vhdl", text)


@pytest.mark.parametrize(
    ("language", "text", "ends"),
    [
        ("vhdl", 'process begin report "std.env.finish"; wait; end process;', False),
        ("vhdl", "process begin -- std.env.finish;\n wait; end process;", False),
        ("vhdl", "process begin STD.ENV.FINISH; end process;", True),
        ("vhdl", "use std.env.all; process begin Finish; end process;", True),
        ("verilog", 'initial begin $display("$finish"); end', False),
        ("verilog", "initial begin // $finish;\n end", False),
        ("verilog", "initial begin /* $finish; */ end", False),
        ("verilog", "initial begin #1 $finish; end", True),
        ("verilog", "initial begin $FINISH; end", False),
    ],
)
def test_only_a_statement_outside_comments_and_strings_ends_a_simulation(
    language: str, text: str, ends: bool
) -> None:
    assert ends_the_simulation(language, text) is ends


def test_a_text_built_from_a_variable_is_not_read() -> None:
    tree = ast.parse('BODY = "$finish;"\nf"module tb; initial {body} endmodule"\nf"{BODY}"')
    constants = module_constants(tree)
    built = tree.body[1].value  # type: ignore[attr-defined]
    literal = tree.body[2].value  # type: ignore[attr-defined]
    assert string_text(built, constants) is None
    assert string_text(literal, constants) == "$finish;"
