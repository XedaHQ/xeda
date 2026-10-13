"""A refusal that depends on the settings and the design alone comes at planning.

`xeda run --dry-run` plans and `xeda run` plans first, so a configuration that a flow cannot run is
refused there, before any producer runs and before a tool starts. A flow that refuses in `init()`
only would pass a dry run and fail after its producers had run.

Two oracles:

- every case of `CASES` is refused by `DefaultRunner.plan`, and by `run_flow` before the run root
  holds a run directory;
- every `raise` that a product flow's `init()`, `prepare_inputs()` or constructor can reach is in
  `REVIEWED_RUN_TIME_REFUSALS` with the reason it needs the run (a tool on `PATH`, a file on disk,
  the resolved inputs), and every override of the two check hooks calls `super()`.
"""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

import pytest

from xeda import Design
from xeda.flow import Flow, FlowSettingsException
from xeda.flow.flow import registered_flows

from .test_tool_input_equivalence import VIVADO_SETTINGS, write_vivado_design
from .tool_utils import use_fake_tools
from xeda.flow_runner import DefaultRunner
from xeda.units import convert_unit
from xeda.flows import (
    VivadoPower,
    Bsc,
    BscSim,
    GhdlSim,
    Modelsim,
    IseSynth,
    Nvc,
    Vcs,
    Verilator,
    VivadoPostsynthSim,
    VivadoSim,
    VivadoSynth,
    Yosys,
    YosysFpga,
    YosysSim,
)

VERILOG = "module top(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n"
TESTBENCH = "module tb; reg clk = 0; top dut(clk); initial #10 $finish; endmodule\n"
VHDL = "entity top is end entity; architecture a of top is begin end architecture;\n"
VHDL_TESTBENCH = "entity tb is end entity; architecture a of tb is begin end architecture;\n"
BSV = "package Probe; (* synthesize *) module mkProbe(Empty); endmodule endpackage\n"
PART = "xc7a35tcsg324-1"


def _design(root: Path, kind: str = "verilog", **sections: Any) -> Design:
    """A design in `root`: `verilog` (RTL and a testbench), `bsv`, or `cpp` (a C++ driver, too).
    A keyword replaces a whole section (`rtl=`, `tb=`, `flows=`)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "top.v").write_text(VERILOG)
    (root / "tb.v").write_text(TESTBENCH)
    (root / "Probe.bsv").write_text(BSV)
    (root / "top.vhd").write_text(VHDL)
    (root / "tb.vhd").write_text(VHDL_TESTBENCH)
    (root / "imported.c").write_text("int imported(void) { return 0; }\n")
    (root / "main.cpp").write_text("int main() { return 0; }\n")
    (root / "tb_top.py").write_text("")
    data: dict[str, Any] = {"name": "d", "design_root": root}
    if kind == "bsv":
        data["rtl"] = {"sources": ["Probe.bsv"], "top": "mkProbe"}
    elif kind == "vhdl":
        data["rtl"] = {"sources": ["top.vhd"], "top": "top"}
        data["tb"] = {"sources": ["tb.vhd"], "top": "tb"}
    else:
        data["rtl"] = {"sources": ["top.v"], "top": "top", "clock": "clk"}
        data["tb"] = {"sources": ["tb.v"], "top": "tb"}
    data.update(sections)
    data["flows"] = {"vivado_synth": {"fpga": PART}, **data.get("flows", {})}
    return Design(**data)


def _without_top(root: Path) -> Design:
    return _design(root, rtl={"sources": ["top.v"], "clock": "clk"})


class Case:
    def __init__(
        self,
        flow: type[Flow],
        match: str,
        settings: dict[str, Any] | None = None,
        design: Callable[[Path], Design] = _design,
        error: type[Exception] = FlowSettingsException,
    ) -> None:
        self.flow, self.match, self.settings, self.design, self.error = (
            flow,
            match,
            settings or {},
            design,
            error,
        )


def _bsv(**sections: Any) -> Callable[[Path], Design]:
    return lambda root: _design(root, "bsv", **sections)


def _vhdl(root: Path) -> Design:
    return _design(root, "vhdl")


def _verilog(**sections: Any) -> Callable[[Path], Design]:
    return lambda root: _design(root, **sections)


COCOTB = {"sources": ["tb_top.py"], "cocotb": True}

CASES: dict[str, Case] = {
    # settings alone
    "verilator-main-with-cocotb": Case(
        Verilator, "cocotb's own main", {"main": True}, _verilog(tb=COCOTB)
    ),
    "verilator-systemc-without-own-main": Case(
        Verilator, "generate_systemc needs `main: false`", {"generate_systemc": True}
    ),
    "verilator-waveform-name": Case(Verilator, "`vcd` names a waveform file", {"vcd": "wave.vcd"}),
    "bsc-unspecified-without-undetermined": Case(
        Bsc,
        "requires `opt_undetermined_vals: true`",
        {"unspecified_to": "X", "opt_undetermined_vals": False},
        _bsv(),
    ),
    "bsc-sim-bluesim-unspecified": Case(
        BscSim,
        "Bluesim is two-valued",
        {"unspecified_to": "X", "opt_undetermined_vals": True},
        _bsv(),
    ),
    "bsc-sim-uncertified-simulator": Case(BscSim, "uncertified", {"simulator": "cvc"}, _bsv()),
    "bsc-sim-bluesim-owned-option": Case(BscSim, "owned -m/-c/-f", {"sim_args": ["-c"]}, _bsv()),
    "bsc-sim-owned-protocol": Case(
        BscSim, "owned runtime evidence protocol", {"simulator": "xsim", "sim_args": ["-R"]}, _bsv()
    ),
    "bsc-sim-stop-time": Case(
        BscSim, "cannot stop at a simulated time", {"stop_time": "1us"}, _bsv()
    ),
    "bsc-sim-max-cycles-elsewhere": Case(
        BscSim, "max_cycles bounds Bluesim", {"simulator": "iverilog", "max_cycles": 10}, _bsv()
    ),
    "vcs-sdf-without-instance": Case(Vcs, "`sdf_instance` is required", {"sdf_file": "x.sdf"}),
    "vivado-sim-elab-debug-off": Case(VivadoPostsynthSim, "elab_debug: off", {"elab_debug": "off"}),
    "vivado-sim-controlled-flag": Case(
        VivadoPostsynthSim, "conflicts with xsim runtime evidence", {"sim_flags": ["-R"]}
    ),
    "vivado-sim-negative-time": Case(
        VivadoPostsynthSim, "not a nonnegative simulation time", {"prerun_time": "-1ns"}
    ),
    "vivado-sim-saif-without-uut": Case(VivadoSim, "set `tb.uut`", {"saif": True}),
    "vivado-postsynth-sim-saif-without-uut": Case(
        VivadoPostsynthSim, "set `tb.uut`", {"saif": True}
    ),
    "vivado-postsynth-sim-timing-activity-without-uut": Case(
        VivadoPostsynthSim, "set `tb.uut`", {"timing_sim": True}
    ),
    "nvc-library-without-directory": Case(
        Nvc,
        "no way to name a library without its directory",
        {"lib_paths": [("vendor", None)]},
        _vhdl,
    ),
    "vivado-sim-sdf-without-root-or-uut": Case(
        VivadoSim, "neither `sdf.root` nor `tb.uut`", {"sdf": {"max": "x.sdf"}}
    ),
    "ghdl-sim-sdf-without-root-or-uut": Case(
        GhdlSim, "neither `sdf.root` nor `tb.uut`", {"sdf": {"max": "x.sdf"}}, _vhdl
    ),
    "modelsim-sdf-without-root-or-uut": Case(
        Modelsim, "neither `sdf.root` nor `tb.uut`", {"sdf": {"max": "x.sdf"}}
    ),
    "vivado-postsynth-sim-sdf-without-root-or-uut": Case(
        VivadoPostsynthSim, "neither `sdf.root` nor `tb.uut`", {"sdf": {"max": "x.sdf"}}
    ),
    "yosys-sta-without-flatten": Case(
        Yosys, "cannot be combined with flatten=false", {"sta": True, "flatten": False}
    ),
    "yosys-fpga-sta-without-flatten": Case(
        YosysFpga,
        "cannot be combined with flatten=false",
        {"sta": True, "flatten": False, "fpga": "ice40"},
    ),
    "yosys-sim-stop-time": Case(YosysSim, "cannot enforce stop_time", {"stop_time": "1us"}),
    "ghdl-run-flag-against-severity": Case(
        GhdlSim, "conflicts with fail_severity", {"run_flags": ["--assert-level=note"]}, _vhdl
    ),
    "nvc-run-flag-against-severity": Case(
        Nvc, "conflicts with fail_severity", {"run_flags": ["--exit-severity=note"]}, _vhdl
    ),
    "nvc-run-flag-against-stop-time": Case(
        Nvc, "bypasses stop_time", {"run_flags": ["--stop-time=1us"]}, _vhdl
    ),
    # the design alone, or the design with the settings
    "bsc-without-top": Case(
        Bsc, "bsc needs the design's top module", design=_bsv(rtl={"sources": ["Probe.bsv"]})
    ),
    "bsc-sim-cocotb-testbench": Case(
        BscSim,
        r"bsc_sim cannot run cocotb tests; use one of: .*\. bsc_sim runs Bluespec testbenches; "
        "for a cocotb testbench, simulate the Verilog that the `bsc` flow generates",
        design=_bsv(tb=COCOTB),
    ),
    "bsc-sim-two-tops": Case(
        BscSim,
        "simulates one top module",
        design=_bsv(tb={"sources": ["Probe.bsv"], "top": ["mkProbe", "mkOther"]}),
    ),
    "bsc-sim-testbench-without-top": Case(
        BscSim,
        "needs the testbench module",
        design=_bsv(tb={"sources": ["imported.c"]}),
    ),
    "bsc-sim-verilator-without-dpi": Case(
        BscSim,
        "through the DPI only",
        {"simulator": "verilator"},
        _bsv(tb={"sources": ["Probe.bsv", "imported.c"], "top": "mkProbe"}),
    ),
    "ise-without-top": Case(IseSynth, "rtl.top", {"fpga": "xc6slx9"}, _without_top),
    "synth-clock-without-port": Case(
        VivadoSynth,
        "no corresponding clock port",
        {"clocks": {"fast": {"period": 5}, "slow": {"period": 9}}},
        _verilog(
            rtl={"sources": ["top.v"], "top": "top", "clocks": [{"port": "clk"}, {"port": "rst"}]}
        ),
    ),
}


def _flow_settings(case: Case) -> dict[str, Any]:
    return dict(case.settings)


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_dry_run_shows_the_refusal(tmp_path, name):
    case = CASES[name]
    design = case.design(tmp_path / "design")
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    with pytest.raises(case.error, match=case.match):
        runner.plan(case.flow, design, flow_settings=_flow_settings(case))
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_launch_refuses_before_any_run_directory_exists(tmp_path, name):
    case = CASES[name]
    design = case.design(tmp_path / "design")
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    with pytest.raises(case.error, match=case.match):
        runner.run_flow(case.flow, design, _flow_settings(case))
    assert not (tmp_path / "runs").exists()


def test_a_producer_in_the_middle_of_a_chain_is_refused_before_the_first_producer_runs(
    tmp_path, monkeypatch
):
    """`vivado_power` runs `vivado_synth`, then `vivado_postsynth_sim`: a setting that the second
    refuses must not come after the first has run."""
    use_fake_tools(monkeypatch)
    write_vivado_design(tmp_path)
    settings = [*VIVADO_SETTINGS, "flows.vivado_postsynth_sim.sim_flags=-R"]
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    with pytest.raises(FlowSettingsException, match="conflicts with xsim runtime evidence"):
        runner.plan(VivadoPower, tmp_path / "design.yaml", flow_settings=settings)
    with pytest.raises(FlowSettingsException, match="conflicts with xsim runtime evidence"):
        runner.run(VivadoPower, tmp_path / "design.yaml", flow_settings=settings)
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").rglob("fake_vivado*"))


@pytest.mark.parametrize("settings", [(), ("timing_sim=true",)])
def test_the_activity_that_power_demands_needs_the_instance_to_record(
    tmp_path, monkeypatch, settings
):
    """`vivado_power` switches the activity of the simulation on, and the simulation records the
    instance `tb.uut`: a design without it is refused at planning, naming the setting to set."""
    use_fake_tools(monkeypatch)
    write_vivado_design(tmp_path)
    design = tmp_path / "design.yaml"
    design.write_text(design.read_text().replace("  uut: dut\n", ""))
    runner = DefaultRunner(tmp_path / "runs", display_results=False)
    flow_settings = [
        *VIVADO_SETTINGS,
        *(f"flows.vivado_postsynth_sim.{item}" for item in settings),
    ]
    with pytest.raises(FlowSettingsException, match="set `tb.uut`"):
        runner.plan(VivadoPower, design, flow_settings=flow_settings)
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize(
    "flow,kind", [(VivadoSim, "verilog"), (GhdlSim, "vhdl"), (Modelsim, "verilog")]
)
def test_an_sdf_root_or_tb_uut_gives_the_sdf_annotation_its_instance(tmp_path, flow, kind):
    with_root = flow.Settings.from_input({"sdf": {"max": "x.sdf", "root": "tb/dut"}})
    flow.check_design_supported(_design(tmp_path / "plain", kind), with_root)
    without_root = flow.Settings.from_input({"sdf": {"max": "x.sdf"}})
    tb = {"sources": ["tb.vhd" if kind == "vhdl" else "tb.v"], "top": "tb", "uut": "dut"}
    flow.check_design_supported(_design(tmp_path / "uut", kind, tb=tb), without_root)


# ---------------------------------------------------------------------------------------------
# The scan

HOOKS = ("check_settings_supported", "check_design_supported", "check_run_directory")
STAGES = ("init", "prepare_inputs", "__init__")

#: Functions that a product flow's `init()`, `prepare_inputs()` or constructor reaches, that raise,
#: and that are not part of a check hook, as `<module>:<qualified name>`; a module entry
#: (`<module>:*`) covers all its functions. The number is how many `raise` statements the function
#: has: a new one fails the scan until someone reviews it. Each says why the refusal needs the run: it judges a
#: tool, a file on disk, or the resolved inputs, none of which a plan reads. A refusal that depends
#: on the settings and the design alone belongs in a hook (`Flow.check_settings_supported`,
#: `Flow.check_design_supported`), where planning, the remote preflight and the exploration make
#: it.
REVIEWED_RUN_TIME_REFUSALS: dict[str, tuple[int | None, str]] = {
    "xeda.flow.flow:Flow.__init__": (
        1,
        "the launcher handed a run directory that is not the path given: a bug in the caller, no "
        "user input",
    ),
    "xeda.flow.flow:Flow.Settings.from_input": (
        2,
        "validates settings given as a mapping to a flow built directly; a launched flow gets "
        "validated settings",
    ),
    "xeda.flow.flow:Flow.add_template_filter": (
        1,
        "a template helper name used twice: a bug in the flow, no user input",
    ),
    "xeda.flow.flow:Flow.add_template_global_func": (
        1,
        "a template helper name used twice: a bug in the flow, no user input",
    ),
    "xeda.flows.fpga_pack:FpgaPack.init": (
        1,
        "needs `fpga-as` on PATH and the Project X-Ray data of the installation",
    ),
    "xeda.flows.xilinx:*": (
        None,
        "reads the openXC7 installation, the Project X-Ray database and the chip database on disk",
    ),
    "xeda.board:*": (None, "reads a board database file on disk"),
    "xeda.digest:*": (None, "reads the files of the run on disk"),
    "xeda.flow_runner.run_lock:*": (None, "locks files of the run root"),
    "xeda.proc_utils:*": (None, "runs a tool process"),
    "xeda.utils:replacing_file": (1, "writes a file of the run root"),
    "xeda.utils:_set_hierarchy": (
        3,
        "expands the dotted keys of the board entry that `board_data()` reads from the database "
        "on disk; the settings validation judges the same entry first",
    ),
    "xeda.utils:refuse_both_spellings": (
        2,
        "judges the keys of the board entry that `board_data()` reads from the database on disk; "
        "the settings validation judges the same entry first",
    ),
    "xeda.flows.nextpnr:Nextpnr._xilinx_layout_and_selection": (
        1,
        "needs `nextpnr-himbaechel` on PATH",
    ),
    "xeda.flows.nextpnr:Nextpnr._locate_board_input": (
        2,
        "judges the resolved `constraints` input (a binding or a source supplies it) and the "
        "run root of a directly built flow",
    ),
    "xeda.flows.nextpnr:Nextpnr._check_board_cache": (
        1,
        "reads a cached board file in the run root",
    ),
    "xeda.flows.nextpnr:Nextpnr._prepare_board_inputs": (
        1,
        "needs the board's pin file on disk",
    ),
}


SCANNED_MODULES = ("xeda.", __name__)


def _is_xeda_function(obj: Any) -> bool:
    obj = getattr(obj, "__func__", obj)
    return isinstance(obj, types.FunctionType) and obj.__module__.startswith(SCANNED_MODULES)


def _function(obj: Any) -> types.FunctionType | None:
    obj = getattr(obj, "__func__", obj)
    return obj if _is_xeda_function(obj) else None


def _tree(fn: types.FunctionType) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def _callees(cls: type, fn: types.FunctionType) -> Iterator[types.FunctionType]:
    """The functions of xeda that `fn` calls and the scan can name: methods of the flow (`self.m`,
    `cls.m`, `super().m`), methods of its settings (`settings.m`, `ss.m`), and functions of any
    xeda module (`f`, `module.f`). A property that code reaches by attribute access is not a call
    and is not followed: code review is the backstop for it."""
    settings = getattr(cls, "Settings", None)
    for node in ast.walk(_tree(fn)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        found: list[Any] = []
        if isinstance(func, ast.Name):
            found.append(fn.__globals__.get(func.id))
        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Call) and getattr(func.value.func, "id", "") == "super":
                found += [base.__dict__.get(func.attr) for base in cls.__mro__]
            else:
                found.append(getattr(cls, func.attr, None))
                found.append(getattr(settings, func.attr, None))
                if isinstance(func.value, ast.Name):  # `module.function(...)`
                    module = fn.__globals__.get(func.value.id)
                    if isinstance(module, types.ModuleType):
                        found.append(getattr(module, func.attr, None))
        for obj in found:
            callee = _function(obj)
            if callee is not None:
                yield callee


def _reachable(cls: type, roots: list[types.FunctionType]) -> dict[int, types.FunctionType]:
    seen: dict[int, types.FunctionType] = {}
    stack = list(roots)
    while stack:
        fn = stack.pop()
        if id(fn) in seen:
            continue
        seen[id(fn)] = fn
        stack.extend(_callees(cls, fn))
    return seen


def _key(fn: types.FunctionType) -> str:
    return f"{fn.__module__}:{fn.__qualname__}"


def run_time_refusals(cls: type) -> dict[str, int]:
    """The functions that raise, reached from the run-time stages of `cls`, outside its hooks,
    with how many `raise` statements each has."""
    hooks = [_function(getattr(cls, name, None)) for name in HOOKS]
    judged = _reachable(cls, [fn for fn in hooks if fn is not None])
    stages = [_function(getattr(cls, name, None)) for name in STAGES]
    reached = _reachable(cls, [fn for fn in stages if fn is not None])
    found = {}
    for ident, fn in reached.items():
        if ident in judged:
            continue
        raises = sum(
            isinstance(node, ast.Raise) and node.exc is not None for node in ast.walk(_tree(fn))
        )
        if raises:
            found[_key(fn)] = raises
    return found


def product_flows() -> list[type[Flow]]:
    import xeda.flows  # noqa: F401  (registers the built-in flows)

    return sorted(
        {
            cls
            for _, cls in registered_flows.values()
            if cls.__module__.startswith("xeda.flows.") or cls.__module__ == "xeda.flows"
        },
        key=lambda cls: cls.__name__,
    )


def _reviewed(key: str, raises: int) -> bool:
    entry = REVIEWED_RUN_TIME_REFUSALS.get(key)
    return (entry is not None and entry[0] == raises) or (
        f"{key.split(':')[0]}:*" in REVIEWED_RUN_TIME_REFUSALS
    )


def test_every_run_time_refusal_of_a_flow_is_reviewed():
    unreviewed = {
        cls.name: sorted(
            key for key, raises in run_time_refusals(cls).items() if not _reviewed(key, raises)
        )
        for cls in product_flows()
    }
    unreviewed = {name: keys for name, keys in unreviewed.items() if keys}
    assert not unreviewed, (
        "a flow raises in init(), prepare_inputs() or its constructor: if it judges the settings "
        "and the design alone, move it into check_settings_supported or "
        "check_design_supported; else review it in REVIEWED_RUN_TIME_REFUSALS (with its number of "
        f"raise statements): {unreviewed}"
    )


def test_every_reviewed_run_time_refusal_is_still_there():
    found: dict[str, int] = {}
    for cls in product_flows():
        found.update(run_time_refusals(cls))
    modules = {key.split(":")[0] for key in found}
    stale = [
        key
        for key, (raises, _) in REVIEWED_RUN_TIME_REFUSALS.items()
        if (key.endswith(":*") and key[:-2] not in modules)
        or (not key.endswith(":*") and found.get(key) != raises)
    ]
    assert not stale, f"a reviewed refusal is gone or has changed, review its entry: {stale}"


def test_every_override_of_a_check_hook_calls_super():
    """A hook that does not call `super()` silently drops the rules of its bases."""
    missing = set()
    for cls in product_flows():
        for base in cls.__mro__:
            for name in HOOKS:
                fn = _function(base.__dict__.get(name))
                if fn is None or not fn.__module__.startswith("xeda.flows"):
                    continue
                calls_super = any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == name
                    and isinstance(node.func.value, ast.Call)
                    and getattr(node.func.value.func, "id", "") == "super"
                    for node in ast.walk(_tree(fn))
                )
                if not calls_super:
                    missing.add(_key(fn))
    assert not missing, f"these hooks do not call super(): {sorted(missing)}"


# The scan has to see what it claims to see.


class _Settings:
    def judge(self):
        raise ValueError("bad")


def _helper():
    raise ValueError("in a helper")


class _Probe:
    Settings = _Settings

    def __init__(self):
        self.settings = _Settings()

    def init(self):
        self.settings.judge()

    def prepare_inputs(self):
        _helper()


class _Direct(_Probe):
    def init(self):
        raise ValueError("direct")


class _Hooked(_Probe):
    @classmethod
    def check_settings_supported(cls, settings):
        settings.judge()

    def init(self):
        self.check_settings_supported(self.settings)


def test_the_scan_sees_a_raise_in_init_a_settings_method_and_a_helper():
    assert run_time_refusals(_Direct) == {f"{__name__}:_Direct.init": 1, f"{__name__}:_helper": 1}
    assert run_time_refusals(_Probe) == {f"{__name__}:_Settings.judge": 1, f"{__name__}:_helper": 1}


this_module = sys.modules[__name__]


class _ThroughAModule(_Probe):
    def init(self):
        this_module._helper()


def test_the_scan_follows_a_function_called_through_a_module():
    assert f"{__name__}:_helper" in run_time_refusals(_ThroughAModule)


class _ThroughAnotherModule(_Probe):
    def init(self):
        convert_unit("1 parsec", "ns")  # a helper of `xeda.units`, which raises on a bad unit


def test_the_scan_follows_a_helper_in_another_xeda_module():
    assert any(key.startswith("xeda.units:") for key in run_time_refusals(_ThroughAnotherModule))


def test_the_scan_leaves_alone_what_a_hook_judges():
    assert run_time_refusals(_Hooked) == {f"{__name__}:_helper": 1}
