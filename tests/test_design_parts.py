"""`Flow.design_parts`: which parts of the design a flow reads, and what it scopes.

A flow's identity (`design_hash`) and the files its trace records as inputs cover the parts it
declares, and only those, so a testbench edit leaves a synthesis flow fresh and makes a simulation
flow stale. The risk is one-sided: a part wrongly left out is a stale reuse of a result built from
other sources, a part wrongly left in only costs a re-run. Three checks hold that down:

* Freshness, direct: on the fake and real tools, a testbench-only edit leaves a
  `{"rtl"}` flow fresh and makes a `{"rtl", "tb"}` flow stale; an RTL edit makes every flow stale.
* The converse. A scan of everything a flow's code and templates can reach finds no read of
  `design.tb` in a `{"rtl"}` flow, and a flow that is not a simulation flow and yet declares `tb`
  is shown to read it.
* The trace of a `{"rtl"}` flow names no file of `design.tb` as an input.

The transitive half is the power graph, at the end of this module: a `tb`-only edit
leaves `vivado_synth` fresh, makes `vivado_postsynth_sim` stale, and makes `vivado_power` stale
with its producer's re-run as the reason. `vivado_power` reads no testbench itself, so it is a
`{"rtl"}` flow whose producer reads `tb`.
"""

import ast
import fnmatch
import inspect
import json
import re
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from xeda import Design
from xeda.design import DESIGN_PARTS
from xeda.flow import Flow, SimFlow
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.trace_inputs import design_files
from xeda.utils import semantic_hash

from .settings_samples import flow_classes
from .tool_utils import launch_until_fresh, require_ghdl, require_yosys, use_fake_tools

RTL, BOTH = frozenset({"rtl"}), frozenset({"rtl", "tb"})

#: The values every flow takes: the flows that read no testbench, and those that do.
RTL_FLOWS = (
    "dc diamond_synth fpga_pack ghdl_synth ise_synth nextpnr openfpgaloader openroad quartus "
    "vivado_alt_synth vivado_power vivado_synth yosys yosys_fpga"
).split()
RTL_AND_TB_FLOWS = (
    "bsc bsc_sim ghdl_sim modelsim nvc vcs verilator vivado_postsynth_sim vivado_project "
    "vivado_sim yosys_sim"
).split()


def _flow(name: str) -> type[Flow]:
    return registered_flows[name][1]


def test_the_table_names_every_flow() -> None:
    """Every flow of the product is in the table, so a new flow has to be classified here."""
    names = {name for _, name in flow_classes()}
    assert names == set(RTL_FLOWS) | set(RTL_AND_TB_FLOWS)


@pytest.mark.parametrize("name", RTL_FLOWS)
def test_a_flow_that_reads_no_testbench_declares_only_rtl(name: str) -> None:
    assert _flow(name).design_parts == RTL


@pytest.mark.parametrize("name", RTL_AND_TB_FLOWS)
def test_a_flow_that_reads_the_testbench_declares_both_parts(name: str) -> None:
    assert _flow(name).design_parts == BOTH


def test_design_parts_is_one_frozenset_and_the_old_name_is_gone() -> None:
    """One name for "which parts of the design this flow reads": a frozenset of part
    names, the parts a design has, and no second class variable beside it."""
    assert Flow.design_parts == RTL
    assert SimFlow.design_parts == BOTH
    for cls, name in flow_classes():
        assert isinstance(cls.design_parts, frozenset), name
        assert "rtl" in cls.design_parts and cls.design_parts <= DESIGN_PARTS, name
        assert not hasattr(cls, "reads_source_parts"), name


# ---------------------------------------------------------------------------------------------
# The helpers: `Design.parts_hash`, `trace_inputs.design_files`
# ---------------------------------------------------------------------------------------------


def _small_design(root: Path, tb: str = "module tb; endmodule\n", rtl: str = "") -> Design:
    root.mkdir(exist_ok=True)
    (root / "a.v").write_text(f"module a; endmodule\n{rtl}")
    (root / "tb.v").write_text(tb)
    return Design(
        name="d",
        design_root=root,
        rtl={"sources": ["a.v"], "top": "a"},
        tb={"sources": ["tb.v"], "top": "tb"},
    )


def test_parts_hash_covers_the_parts_it_is_given(tmp_path: Path) -> None:
    # each hash is taken before the next design rewrites the files: sources are read when hashed
    before = _small_design(tmp_path)
    rtl, both = before.parts_hash(RTL), before.parts_hash(BOTH)
    after = _small_design(tmp_path, tb="module tb; wire changed; endmodule\n")
    assert after.parts_hash(RTL) == rtl
    assert after.parts_hash(BOTH) != both
    assert rtl != both
    again = _small_design(tmp_path, rtl="module b; endmodule\n")
    assert again.parts_hash(RTL) != rtl


def test_both_parts_are_the_hash_a_design_always_had(tmp_path: Path) -> None:
    """A simulation's `design_hash` does not move with this change: both parts hash as the whole
    design did (`rtl_hash` and `tb_hash`, in that order)."""
    design = _small_design(tmp_path)
    assert design.parts_hash(BOTH) == semantic_hash(
        dict(rtl_hash=design.rtl_hash, tb_hash=design.tb_hash)
    )


def test_parts_hash_does_not_depend_on_the_order_the_parts_are_named_in(tmp_path: Path) -> None:
    design = _small_design(tmp_path)
    assert (
        design.parts_hash(["tb", "rtl"])
        == design.parts_hash(("rtl", "tb"))
        == design.parts_hash(BOTH)
    )


def test_parts_hash_refuses_a_part_a_design_does_not_have(tmp_path: Path) -> None:
    design = _small_design(tmp_path)
    with pytest.raises(ValueError, match="nonsense"):
        design.parts_hash(frozenset({"rtl", "nonsense"}))


def test_design_files_cover_the_parts_they_are_given(tmp_path: Path) -> None:
    design = _small_design(tmp_path)
    a, tb = (tmp_path / "a.v").resolve(), (tmp_path / "tb.v").resolve()
    assert design_files(design, RTL) == [a]
    assert design_files(design, BOTH) == [a, tb]
    # the default is the whole design: the launcher's isolation checks (a delivery may never land
    # on a testbench file) and a remote run's read inputs name every file of it
    assert design_files(design) == [a, tb]


# ---------------------------------------------------------------------------------------------
# A testbench edit, on the fake and the real tools
# ---------------------------------------------------------------------------------------------

VHDL_INVERTER = (
    "library ieee; use ieee.std_logic_1164.all;\n"
    "entity inv is port(a: in std_logic; y: out std_logic); end;\n"
    "architecture rtl of inv is begin y <= not a; end;\n"
)
VHDL_TB = (
    "library ieee; use ieee.std_logic_1164.all;\n"
    "use std.env.all;\n"
    "entity tb is end;\n"
    "architecture sim of tb is\n"
    "  signal a, y : std_logic := '0';\n"
    "begin\n"
    "  dut : entity work.inv port map (a, y);\n"
    "  process begin wait for 10 ns; finish; end process;\n"
    "end;\n"
)
VERILOG_RTL = "module inv(input a, output y); assign y = ~a; endmodule\n"
VERILOG_TB = "module tb; initial $finish; endmodule\n"


@dataclass(frozen=True)
class Case:
    flow: str
    #: the files of the design: the RTL source and the testbench source
    rtl: tuple[str, str]
    tb: tuple[str, str]
    settings: dict
    #: the tools the flow runs: the fakes, or a probe for the real one
    tools: str


CASES = [
    Case(
        "vivado_synth",
        ("inv.vhd", VHDL_INVERTER),
        ("tb.vhd", VHDL_TB),
        {"fpga": "xc7a12tcsg325-1", "clock_period": 5.5},
        "fake",
    ),
    Case("yosys", ("inv.v", VERILOG_RTL), ("tb.v", VERILOG_TB), {}, "yosys"),
    Case("ghdl_sim", ("inv.vhd", VHDL_INVERTER), ("tb.vhd", VHDL_TB), {}, "ghdl"),
    Case("vivado_sim", ("inv.vhd", VHDL_INVERTER), ("tb.vhd", VHDL_TB), {}, "fake"),
]
IDS = [case.flow for case in CASES]


class Project:
    """A design on disk, one flow of it, and a runner that launches it as `xeda run` would."""

    def __init__(self, case: Case, root: Path) -> None:
        self.case, self.root = case, root
        root.mkdir()
        for name, text in (case.rtl, case.tb):
            (root / name).write_text(text)
        self.runner = DefaultRunner(root.parent / "xeda_run", display_results=False)

    def design(self) -> Design:
        language = {"vhdl": {"standard": "2008"}} if self.case.rtl[0].endswith(".vhd") else {}
        return Design(
            name="inv",
            design_root=self.root,
            rtl={"sources": [self.case.rtl[0]], "top": "inv", "clock_port": "a"},
            tb={"sources": [self.case.tb[0]], "top": "tb"},
            language=language,
        )

    def launch(self) -> Flow:
        return self.runner.launch_flow(self.case.flow, self.design(), dict(self.case.settings))

    def settle(self) -> Flow:
        """The flow after a first run, found up to date (`launch_until_fresh`: a run that read
        a file of another file system is judged once more)."""
        first = self.launch()
        assert first.succeeded and not first.reused
        return launch_until_fresh(self.runner, self.launch)

    def edit(self, which: str) -> None:
        """Change the content of one source, and nothing else."""
        name = (self.case.rtl if which == "rtl" else self.case.tb)[0]
        path = self.root / name
        comment = "--" if path.suffix == ".vhd" else "//"
        path.write_text(path.read_text() + f"{comment} edited {which}\n")


@pytest.fixture(params=CASES, ids=IDS)
def project(request, tmp_path: Path, monkeypatch) -> Project:
    case: Case = request.param
    if case.tools == "fake":
        use_fake_tools(monkeypatch)
    elif case.tools == "yosys":
        require_yosys()
    else:
        require_ghdl()
    monkeypatch.chdir(tmp_path)
    return Project(case, tmp_path / "design")


def test_a_testbench_edit_leaves_a_flow_reading_only_rtl_fresh(project: Project) -> None:
    """The flow has no producer: the one case in which `design_parts` is the whole
    story (a flow downstream of a producer that reads `tb` runs again with it)."""
    parts = _flow(project.case.flow).design_parts
    assert project.settle().succeeded

    project.edit("tb")
    flow = project.launch()

    assert flow.succeeded
    if "tb" in parts:
        assert not flow.reused
        assert flow.stale_reason, "a flow that reads the testbench says why it ran again"
    else:
        assert flow.reused and flow.stale_reason is None, flow.stale_reason


def test_an_rtl_edit_makes_every_flow_stale(project: Project) -> None:
    assert project.settle().succeeded

    project.edit("rtl")
    flow = project.launch()

    assert flow.succeeded and not flow.reused
    assert flow.stale_reason


def test_a_testbench_edit_separates_synthesis_from_simulation() -> None:
    """A testbench edit separates synthesis from simulation: `vivado_synth` reads
    no testbench and `ghdl_sim` does. (The launches that show it are the two tests above, run for
    `vivado_synth` and `ghdl_sim`.)"""
    assert _flow("vivado_synth").design_parts == RTL
    assert _flow("ghdl_sim").design_parts == BOTH


def test_a_testbench_edit_reruns_the_simulation_and_power_but_not_synthesis(
    tmp_path: Path, monkeypatch
) -> None:
    """`vivado_power` reads no testbench, yet the activity it reports is a simulation
    of one: the edit leaves the synthesis it reports against fresh, makes the simulation stale
    and makes power stale through that simulation's new run, not through its own design hash."""
    from .test_tool_input_equivalence import VIVADO_SETTINGS, write_vivado_design

    use_fake_tools(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_XSIM_STATE", "finish5")
    monkeypatch.chdir(tmp_path)
    write_vivado_design(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)

    def launch() -> Flow:
        flow = runner.run("vivado_power", tmp_path / "design.yaml", flow_settings=VIVADO_SETTINGS)
        assert flow and flow.succeeded
        return flow

    first = launch()
    assert not first.reused
    settled = launch_until_fresh(runner, launch)
    assert settled.reused

    (tmp_path / "tb.sv").write_text((tmp_path / "tb.sv").read_text() + "// edited tb\n")
    entered = len(runner.launched)
    power = launch()
    ran = {flow.name: flow for flow in runner.launched[entered:]}

    assert set(ran) == {"vivado_synth", "vivado_postsynth_sim", "vivado_power"}
    assert ran["vivado_synth"].reused and ran["vivado_synth"].stale_reason is None
    assert not ran["vivado_postsynth_sim"].reused and ran["vivado_postsynth_sim"].stale_reason
    assert power is ran["vivado_power"] and not power.reused
    assert "vivado_postsynth_sim" in power.stale_reason, power.stale_reason
    assert "tb.sv" not in power.stale_reason
    assert power.design_hash == settled.design_hash


def _trace_inputs(flow: Flow) -> set[Path]:
    trace = json.loads((flow.run_path / "trace.json").read_text())
    return {Path(name).resolve() for name in {**trace["inputs"], **trace["implicit_inputs"]}}


def test_the_trace_of_a_flow_names_the_files_of_the_parts_it_reads(project: Project) -> None:
    """A `{"rtl"}` flow's trace names no file of `design.tb` -- through its inputs or
    anything it read after the run -- though the testbench is in the design it was given. A flow
    that declares `tb` names them: the same trace, as the control that the check can see."""
    parts = _flow(project.case.flow).design_parts
    flow = project.settle()
    named = _trace_inputs(flow)
    rtl, tb = (project.root / project.case.rtl[0]).resolve(), (
        project.root / project.case.tb[0]
    ).resolve()

    assert rtl in named
    assert (tb in named) == ("tb" in parts), sorted(map(str, named))


# ---------------------------------------------------------------------------------------------
# A flow that declares only `rtl` never reads `tb`, in code or in a template
# ---------------------------------------------------------------------------------------------

#: The calls that take a `tb` argument, and the number of positional arguments that precede it.
TB_TAKERS = {"sources_of_type": None, "header_dirs": 1, "sources_read": 1}

#: Flows declared `{"rtl"}` that nevertheless read `tb`, each with why it is allowed. Nothing is
#: allowed today: a part wrongly left out is a stale reuse of other sources, and a read that
#: cannot be removed is declared in `design_parts` instead (`bsc`). An entry is a finding.
READS_TB_THOUGH_RTL_ONLY: dict[str, str] = {}

#: Flows that are not simulation flows and still declare `tb`: each reads it, and the reason
#: says where. The scan below proves the read is there, so an entry whose read is gone fails
#: and says to narrow the flow's parts.
DECLARES_TB_THOUGH_NOT_A_SIMULATION: dict[str, str] = {
    "bsc": "BscFlow._path_flags adds the testbench's Verilog directories to -vsearch "
    "(`sources_of_type(..., rtl=True, tb=True)`); declared both until that read is narrowed",
    "vivado_project": "the project's simulation fileset holds the testbench "
    "(`vivado_project.tcl`: `sources_read(rtl=false, tb=true)`, `design.tb.top`)",
}


def python_tb_reads(
    source: str, skip_classes: Iterable[str] = (), supplied: set[str] | None = None
) -> list[str]:
    """Where the Python in `source` reads the design's testbench, as `line: what`.

    A read is: an attribute `.tb` (`design.tb`, `self.design.tb.top`); `getattr(x, "tb")`; a call
    of `sources_of_type`, `header_dirs` or `sources_read` that is not told `tb=False` outright
    (one that forwards its own parameter `tb` is fine when that parameter defaults to False,
    which `test_the_defaults_of_the_calls_that_take_tb_are_false` pins); an argument expansion
    into such a call. Top-level classes named in `skip_classes` are not visited: a module holds
    other flows' classes beside this one's, and only the classes of the flow's MRO are the flow's.
    `supplied` collects the keyword names given to calls, for the template check.
    """
    tree = ast.parse(source)
    hits: list[str] = []
    skip = set(skip_classes)

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            if node in tree.body and node.name in skip:
                return
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if node.attr == "tb":
                hits.append(f"{node.lineno}: `.tb`")
            self.generic_visit(node)

        def _forwards_a_false_default(self, value: ast.expr) -> bool:
            if isinstance(value, ast.Constant) and value.value is False:
                return True
            if not (isinstance(value, ast.Name) and value.id == "tb" and self.functions):
                return False
            args = self.functions[-1].args
            names = [a.arg for a in [*args.posonlyargs, *args.args]]
            defaults = [None] * (len(names) - len(args.defaults)) + list(args.defaults)
            pairs = dict(zip(names, defaults))
            pairs.update({a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults)})
            default = pairs.get("tb")
            return isinstance(default, ast.Constant) and default.value is False

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if supplied is not None:
                supplied.update(k.arg for k in node.keywords if k.arg)
            if name == "getattr" and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and arg.value == "tb":
                    hits.append(f"{node.lineno}: getattr(..., 'tb')")
            if name in TB_TAKERS:
                keywords = {k.arg: k.value for k in node.keywords}
                if None in keywords:
                    hits.append(f"{node.lineno}: {name}(**...)")
                elif "tb" in keywords:
                    if not self._forwards_a_false_default(keywords["tb"]):
                        hits.append(f"{node.lineno}: {name}(tb=...)")
                position = TB_TAKERS[name]
                if position is not None and len(node.args) > position:
                    hits.append(f"{node.lineno}: {name}(..., tb positionally)")
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


TB_KEYWORD = re.compile(r"\btb\s*=\s*(\w+)")
TB_ATTRIBUTE = re.compile(r"\.tb\b")


def template_tb_reads(text: str, supplied: set[str]) -> list[str]:
    """Where a template reads the design's testbench, as `line: what`: `design.tb`, or a call
    given `tb=true`. `tb=<name>` is a read unless the name is set from a context variable the
    flow's code never passes (`{% set include_tb = read_tb_sources|default(false) %}` is the
    flag only `yosys_sim` hands its template), or is `false`."""
    set_from = dict(
        re.findall(r"{%-?\s*set\s+(\w+)\s*=\s*(\w+)\s*\|\s*default\(\s*false\s*\)", text)
    )
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        if TB_ATTRIBUTE.search(line):
            hits.append(f"{number}: `.tb`")
        for value in TB_KEYWORD.findall(line):
            if value == "false":
                continue
            if value in set_from and set_from[value] not in supplied:
                continue
            hits.append(f"{number}: tb={value}")
    return hits


def test_the_scan_sees_each_way_of_reading_the_testbench() -> None:
    """The teeth of the scan: a scan that finds nothing in anything proves nothing."""
    code = {
        "attribute": "def f(self): return self.design.tb.top",
        "getattr": "def f(d): return getattr(d, 'tb')",
        "tb=True": "def f(d): return d.sources_of_type('*', rtl=True, tb=True)",
        "tb=name": "def f(d, flag): return d.sources_read(tb=flag)",
        "positional": "def f(d): return d.header_dirs(True, True)",
        "expansion": "def f(d, **kw): return d.sources_of_type('*', **kw)",
        "forwarded default True": "def f(d, tb=True): return d.sources_of_type('*', tb=tb)",
    }
    for what, source in code.items():
        assert python_tb_reads(source), what
    clean = {
        "tb=False": "def f(d): return d.sources_of_type('*', rtl=True, tb=False)",
        "no tb": "def f(d): return d.sources_of_type('*') + d.header_dirs()",
        "forwarded default False": "def f(d, tb=False): return d.sources_of_type('*', tb=tb)",
        "forwarded default False, keyword only": "def f(d, *, tb=False): return d.sources_read(tb=tb)",
        "a skipped class": "class Other:\n    def f(self): return self.design.tb\n",
    }
    for what, source in clean.items():
        assert not python_tb_reads(source, skip_classes=["Other"]), what

    flag = "{% set include_tb = read_tb_sources|default(false) -%}\n{{ design.sources_of_type('*', tb=include_tb) }}"
    assert template_tb_reads("{{ design.tb.top[0] }}", set())
    assert template_tb_reads("{% for s in sources_read(rtl=false, tb=true) %}{% endfor %}", set())
    assert template_tb_reads(flag, {"read_tb_sources"})
    assert not template_tb_reads(flag, set())
    assert not template_tb_reads("{{ design.sources_of_type('*', rtl=true, tb=false) }}", set())


def test_the_defaults_of_the_calls_that_take_tb_are_false() -> None:
    """A call that does not name `tb` reads none, which holds only while every default is False."""
    from xeda.design import Design as D

    for function in (D.sources_of_type, D.header_dirs, Flow.sources_read):
        default = inspect.signature(function).parameters["tb"].default
        assert default is False, function.__qualname__


def _mro(cls: type[Flow]) -> list[type]:
    return [k for k in cls.__mro__ if k.__module__.startswith("xeda.")]


def _flow_nodes(cls: type[Flow]) -> Iterable[ast.AST]:
    """Every AST node of the code of `cls`: its MRO's classes, and the code of their modules
    outside any class, which they call. The classes of other flows in those modules are left out."""
    names_by_module: dict[str, set[str]] = {}
    for klass in _mro(cls):
        names_by_module.setdefault(klass.__module__, set()).add(klass.__name__)
    for module_name, names in names_by_module.items():
        for statement in ast.parse(inspect.getsource(sys.modules[module_name])).body:
            if isinstance(statement, ast.ClassDef) and statement.name not in names:
                continue
            yield from ast.walk(statement)


def flow_code_reads(cls: type[Flow]) -> tuple[list[str], set[str]]:
    """What the code of `cls` and of every class of its MRO reads of the testbench, in the
    modules they live in with the classes of other flows left out, and the keyword names that
    code passes to calls (the context a template can see)."""
    mro = _mro(cls)
    names_by_module: dict[str, set[str]] = {}
    for klass in mro:
        names_by_module.setdefault(klass.__module__, set()).add(klass.__name__)
    hits: list[str] = []
    supplied: set[str] = set()
    for module_name, names in names_by_module.items():
        module = sys.modules[module_name]
        source = inspect.getsource(module)
        defined = {node.name for node in ast.parse(source).body if isinstance(node, ast.ClassDef)}
        found = python_tb_reads(source, skip_classes=defined - names, supplied=supplied)
        hits += [f"{module_name}:{hit}" for hit in found]
    return hits, supplied


def _template_names(env, text_of) -> list[str]:
    return sorted(env.loader.list_templates())


def shipped_template_suffixes() -> frozenset[str]:
    """The file suffixes the wheel ships as package data (`pyproject.toml`'s
    `[tool.setuptools.package-data]`, `*.tcl`, `*.ys`, ...): the suffixes a template can have."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())["tool"]["setuptools"]["package-data"]
    return frozenset(
        Path(pattern).suffix for patterns in data.values() for pattern in patterns if "*" in pattern
    )


def renderable(names: Iterable[str]) -> list[str]:
    """The names among `names`, as a Jinja loader lists them, that are templates. A loader lists
    every file under a template directory, and a `templates` package keeps its compiled
    bytecode there (`templates/__pycache__/__init__.cpython-313.pyc`, which is no text); only a
    file of a shipped template suffix, outside any cache directory, can be rendered."""
    suffixes = shipped_template_suffixes()
    return [
        name
        for name in names
        if Path(name).suffix in suffixes and "__pycache__" not in Path(name).parts
    ]


def test_only_shipped_template_suffixes_are_templates() -> None:
    """A compiled file in a template directory, which a loader lists and which is no text, is
    not one -- nor a Python source or a cache directory's file of any name."""
    listed = [
        "synth.tcl",
        "read_files.ys",
        "macros.tcl.j2",
        "nvc_end.cpp",
        "sim_record.h",
        "__pycache__/__init__.cpython-313.pyc",
        "__pycache__/synth.tcl",
        "__init__.py",
        "stray.pyc",
        "notes.bin",
    ]
    assert renderable(listed) == [
        "synth.tcl",
        "read_files.ys",
        "macros.tcl.j2",
        "nvc_end.cpp",
        "sim_record.h",
    ]


def reachable_templates(cls: type[Flow]) -> dict[str, str]:
    """The templates `cls`'s code can render, by name, with their text: the loader's templates
    (`Flow._create_jinja_env`, the one the flow renders with: its module and its bases', so a
    subclass inherits its parent's) that the code of its MRO names -- a string, or an f-string as
    a pattern (`f"constraints.{ext}"`) -- and every template those include, an include of a
    computed name (`{% include step + '.tcl' %}`) being any template that matches its tail."""
    env = cls._create_jinja_env(extra_modules=[cls.__module__])
    available = {
        name: env.loader.get_source(env, name)[0]
        for name in renderable(env.loader.list_templates())
    }
    names: set[str] = set()
    patterns: set[str] = set()
    for node in _flow_nodes(cls):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
        elif isinstance(node, ast.JoinedStr):
            pattern = "".join(
                part.value if isinstance(part, ast.Constant) else "*" for part in node.values
            )
            # an f-string names a template by what it spells of its file name
            # (`f"constraints.{ext}"`, `f"yosys_synth{self.script_ext}"`); one that spells
            # next to nothing (`f"{x}"`) names none
            if len(pattern.replace("*", "")) >= 4:
                patterns.add(pattern)
    reached: dict[str, str] = {}
    pending = [
        name
        for name in available
        if name in names or any(fnmatch.fnmatchcase(name, p) for p in patterns)
    ]
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached[name] = available[name]
        for target in re.findall(
            r"""{%-?\s*(?:include|from|import|extends)\s+['"]([^'"]+)['"]""", available[name]
        ):
            pending.append(target)
        for tail in re.findall(
            r"""{%-?\s*include\s+\w+\s*\+\s*['"]([^'"]+)['"]""", available[name]
        ):
            pending += [n for n in available if n.endswith(tail)]
    return reached


def tb_reads(cls: type[Flow]) -> list[str]:
    """Every read of the testbench by `cls`'s code and by the templates it can render."""
    hits, supplied = flow_code_reads(cls)
    for name, text in sorted(reachable_templates(cls).items()):
        hits += [f"template {name}:{hit}" for hit in template_tb_reads(text, supplied)]
    return hits


@pytest.mark.parametrize(
    ("name", "templates"),
    [
        ("vivado_synth", {"vivado_synth.tcl", "post_step_hook.tcl", "clock.xdc", "util.tcl"}),
        ("diamond_synth", {"synth.tcl", "constraints.ldc", "constraints.sdc", "constraints.fdc"}),
        ("openroad", {"orflow.tcl", "utils.tcl", "cts.tcl", "global_place.tcl", "clocks.sdc"}),
        ("yosys", {"yosys_synth.ys", "read_files.ys", "post_rtl.ys", "write_netlist.ys"}),
        ("yosys_fpga", {"read_files.ys"}),
        ("dc", {"dc_script.tcl", "constraints.sdc"}),
    ],
)
def test_the_scan_reaches_the_templates_a_flow_renders_and_only_those(name, templates) -> None:
    """An oracle over an empty set proves nothing: the templates a flow names, directly, by an
    f-string or through an include (`{% include step + '.tcl' %}`), are all reached; and a
    template of another flow in the same directory is not (`vivado_sim.tcl` is `vivado_sim`'s)."""
    reached = set(reachable_templates(_flow(name)))
    assert templates <= reached, templates - reached
    assert not {"vivado_sim.tcl", "vivado_project.tcl", "run.tcl"} & reached


def test_the_scan_finds_the_testbench_read_where_there_is_one() -> None:
    """`yosys_sim` hands its template the one flag that makes `read_files.ys` read `tb`, and
    reads `design.tb` in its own code; `yosys`, which shares that template, does neither."""
    assert any("read_files" in hit for hit in tb_reads(_flow("yosys_sim")))
    assert tb_reads(_flow("yosys_sim"))
    assert not tb_reads(_flow("yosys"))


RTL_ONLY_CLASSES = [(cls, name) for cls, name in flow_classes() if cls.design_parts == RTL]


@pytest.mark.parametrize(("cls", "name"), RTL_ONLY_CLASSES, ids=[n for _, n in RTL_ONLY_CLASSES])
def test_a_flow_declaring_only_rtl_never_reads_the_testbench(cls: type[Flow], name: str) -> None:
    """Over the classes of the flow's MRO and the templates its loader offers and its code
    names, in the modules they live in, never only the flow's own (`GhdlSynth` shares a module
    with `GhdlSim`, a Vivado template directory holds every Vivado flow's)."""
    hits = tb_reads(cls)
    if name in READS_TB_THOUGH_RTL_ONLY:
        assert hits, f"{name} is listed as reading `tb` and does not: delete its entry"
    else:
        assert not hits, (
            f"{name} declares design_parts={set(cls.design_parts)} and reads the testbench; "
            "declare `tb` (over-approximation costs a re-run, the other error a stale reuse):\n"
            + "\n".join(hits)
        )


NOT_SIMULATIONS = [
    (cls, name)
    for cls, name in flow_classes()
    if "tb" in cls.design_parts and not issubclass(cls, SimFlow)
]


def test_every_non_simulation_flow_declaring_tb_is_reviewed() -> None:
    assert {name for _, name in NOT_SIMULATIONS} == set(DECLARES_TB_THOUGH_NOT_A_SIMULATION)


@pytest.mark.parametrize(("cls", "name"), NOT_SIMULATIONS, ids=[n for _, n in NOT_SIMULATIONS])
def test_a_synthesis_flow_declaring_tb_is_shown_to_read_it(cls: type[Flow], name: str) -> None:
    """The other side of the same risk: `tb` on a flow that does not read it costs a re-run
    for nothing. Where the read goes (narrowed away), the flow's parts go with it."""
    assert tb_reads(cls), f"{name} declares `tb` and reads none: make it design_parts={{'rtl'}}"
