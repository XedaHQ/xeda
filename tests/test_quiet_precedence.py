"""`quiet` gives way to `verbose` and `debug`: flows read `Flow.Settings.is_quiet`, never `quiet`.

`is_quiet` decides the precedence in one place, where the setting is read, so `quiet` holds what
was written. A flow that reads `quiet` itself silences a run the user asked to be verbose.
"""

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

import xeda.flows
from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys

FLOWS_DIR = Path(xeda.flows.__file__).parent

# In a template: `settings.quiet`, `ss.quiet`, `settings["quiet"]` -- but not `is_quiet`, and not
# a tool's own `-quiet` option.
TEMPLATE_QUIET = re.compile(r"(?<![\w-])\w+\s*(\.\s*quiet\b|\[\s*[\"']quiet[\"']\s*\])")


def _python_reads_of_quiet(path: Path) -> Iterator[tuple[int, str]]:
    """Find direct reads of quiet in Python flow code."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "quiet"
            and isinstance(node.ctx, ast.Load)
        ):
            yield node.lineno, ast.unparse(node)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "quiet"
        ):
            yield node.lineno, ast.unparse(node)


def _template_reads_of_quiet(path: Path) -> Iterator[tuple[int, str]]:
    """Find direct reads of quiet in flow templates."""
    for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        for match in TEMPLATE_QUIET.finditer(line):
            yield lineno, match.group(0)


def test_no_flow_reads_quiet_except_through_is_quiet():
    """No flow reads quiet except through is quiet."""
    offenders: list[str] = []
    for path in sorted(FLOWS_DIR.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix == ".py":
            reads = _python_reads_of_quiet(path)
        elif "templates" in path.parts:
            reads = _template_reads_of_quiet(path)
        else:
            continue
        offenders += [f"{path.relative_to(FLOWS_DIR)}:{n}: {text}" for n, text in reads]
    assert not offenders, "read `is_quiet` instead:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "probe, found",
    [
        ("if ss.quiet: pass", True),
        ("args += setting_flag(ss.quiet, name='quiet')", True),
        ("q = getattr(self.settings, 'quiet')", True),
        ("if ss.is_quiet: pass", False),
        ("args += setting_flag(ss.is_quiet, name='quiet')", False),
        ("quiet: bool = Field(False, description='x')", False),
        ("ss.quiet = True", False),
    ],
)
def test_the_python_sweep_sees_a_read_of_quiet(probe, found, tmp_path):
    """The python sweep sees a read of quiet."""
    source = tmp_path / "probe.py"
    source.write_text(probe + "\n")
    assert bool(list(_python_reads_of_quiet(source))) is found


@pytest.mark.parametrize(
    "probe, found",
    [
        ("{% if settings.quiet %} -q {% endif %}", True),
        ('{% if settings["quiet"] %} -q {% endif %}', True),
        ("{% if settings.is_quiet %} -quiet {% endif %}", False),
        ("report_power -quiet -file x", False),
    ],
)
def test_the_template_sweep_sees_a_read_of_quiet(probe, found, tmp_path):
    """The template sweep sees a read of quiet."""
    template = tmp_path / "probe.tcl"
    template.write_text(probe + "\n")
    assert bool(list(_template_reads_of_quiet(template))) is found


@pytest.fixture
def commands(monkeypatch) -> list[list[str]]:
    """Every command a tool would run, instead of running it."""
    recorded: list[list[str]] = []

    def record(executable, args=None, **kwargs):
        recorded.append([str(executable), *(str(a) for a in args or [])])
        return "" if kwargs.get("stdout") is True else None

    monkeypatch.setattr("xeda.tool.run_process", record)
    return recorded


@pytest.mark.parametrize(
    "settings, quiet",
    [
        ({"quiet": True}, True),
        ({"quiet": True, "verbose": 1}, False),
        ({"quiet": True, "debug": True}, False),
        ({}, False),
    ],
)
def test_yosys_is_quiet_only_when_nothing_asks_for_more(settings, quiet, tmp_path, commands):
    """Yosys is quiet only when nothing asks for more."""
    (tmp_path / "top.v").write_text("module top(input a, output y); assign y = ~a; endmodule\n")
    design = Design(name="top", design_root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})
    DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, design, settings)
    (synth,) = [cmd for cmd in commands if "-s" in cmd or "-c" in cmd]
    assert Path(synth[0]).name == "yosys"
    assert ("-q" in synth) is quiet
