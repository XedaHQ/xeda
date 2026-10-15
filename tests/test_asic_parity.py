"""`tools/asic_parity.py` compares xeda's ASIC flow with OpenROAD-flow-scripts (ORFS).

These tests run no tool and need no ORFS checkout. They check what the tool decides on its own:
the translation of a design file to a xeda design, the environment and the command lines of the
subprocesses, the reading of the result files, the table, and the bar that a result must meet.
The real comparison is a manual run of the tool.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml

from xeda import Design
from xeda.flows import Yosys
from xeda.introspect import all_flow_classes
from xeda.platforms import bundled_platform_names
from xeda.units import Time

ROOT = Path(__file__).parent.parent
TOOL = ROOT / "tools" / "asic_parity.py"
DESIGNS = ROOT / "tools" / "asic_parity" / "designs"
OVERRIDES = ROOT / "tools" / "asic_parity" / "overrides"

pytestmark = pytest.mark.python_compat


def _load_tool() -> types.ModuleType:
    """The script as a module. `tools/` is no package, and importing the file would write its
    bytecode into the checkout, so its source runs in a module of its own."""
    module = types.ModuleType("asic_parity")
    module.__file__ = str(TOOL)
    exec(compile(TOOL.read_text(encoding="utf-8"), str(TOOL), "exec"), module.__dict__)
    return module


tool = _load_tool()

#: What `flow/designs/<platform>/<design>/config.mk`, the platform `config.mk` and the SDC of
#: ORFS say about each design. It is a second copy of the facts, so that a wrong design file
#: fails here. `adder` is whether `ADDER_MAP_FILE` is non-empty and `swap` is
#: `SWAP_ARITH_OPERATORS`.
FACTS: dict[str, dict[str, Any]] = {
    "nangate45/gcd": dict(
        top="gcd", port="clk", period=0.25, abc_area="1", adder=False, swap="0", slang=False
    ),
    "nangate45/aes": dict(
        top="aes_cipher_top", port="clk", period=0.82, abc_area="0", adder=True, swap="1",
        slang=False,
    ),
    "nangate45/ibex": dict(
        top="ibex_core", port="clk_i", period=2.2, abc_area="0", adder=True, swap="0", slang=True
    ),
    "nangate45/jpeg": dict(
        top="jpeg_encoder", port="clk", period=1.0, abc_area="1", adder=True, swap="0",
        slang=False,
    ),
    "sky130hd/gcd": dict(
        top="gcd", port="clk", period=1.1, abc_area="0", adder=False, swap="1", slang=False
    ),
    "sky130hd/aes": dict(
        top="aes_cipher_top", port="clk", period=3.6, abc_area="0", adder=True, swap="1",
        slang=False,
    ),
    "sky130hd/ibex": dict(
        top="ibex_core", port="clk_i", period=8.0, abc_area="0", adder=False, swap="1", slang=True
    ),
}  # fmt: skip
SPECS = list(FACTS)


def _skip_without_platform(spec: str) -> None:
    platform = spec.split("/")[0]
    if platform not in bundled_platform_names() or not (OVERRIDES / f"{platform}.yaml").is_file():
        pytest.skip(f"the platform {platform} is not bundled with this checkout yet")


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def sdc_text(period: float, port: str) -> str:
    return (
        f"current_design x\n\nset clk_name core_clock\nset clk_port_name {port}\n"
        f"set clk_period {period}\nset clk_io_pct 0.2\n"
    )


def make_vars(tmp_path: Path, spec: str) -> dict[str, str]:
    """The `make print-` values that ORFS gives for `spec`, with files under `tmp_path`."""
    facts = FACTS[spec]
    platform, design = spec.split("/")
    orfs = tmp_path / "orfs" / "flow"
    src = orfs / "designs" / "src"
    if design == "ibex":
        files = [write(src / "ibex_sv" / f"{n}.sv") for n in ("ibex_alu", "ibex_core")]
        files.append(write(src / "ibex_sv" / "syn" / "rtl" / "prim_clock_gating.v"))
        include = src / "ibex_sv" / "vendor" / "lowrisc_ip" / "prim" / "rtl"
        write(include / "prim_assert_dummy_macros.svh")
        write(include / "prim_assert.sv")
    elif design == "jpeg":
        files = [write(src / "jpeg" / f"{n}.v") for n in ("dct", "jpeg_encoder")]
        include = src / "jpeg" / "include"
        write(include / "dct_cos_table.v")
    else:
        files = [write(src / design / f"{n}.v") for n in ("a_module", "b_module")]
        include = None
    sdc = write(orfs / "designs" / platform / design / "constraint.sdc",
                sdc_text(facts["period"], facts["port"]))  # fmt: skip
    plat_dir = orfs / "platforms" / platform
    return {
        "PLATFORM": platform,
        "DESIGN_NAME": facts["top"],
        "DESIGN_NICKNAME": design,
        "PLATFORM_DIR": str(plat_dir),
        "RESULTS_DIR": str(tmp_path / "work" / "results" / platform / design / "base"),
        "VERILOG_FILES": " ".join(str(f) for f in files),
        "VERILOG_INCLUDE_DIRS": "" if include is None else str(include) + "/",
        "SDC_FILE": str(sdc),
        "ABC_AREA": facts["abc_area"],
        "SWAP_ARITH_OPERATORS": facts["swap"],
        "SYNTH_HIERARCHICAL": "0",
        "OPENROAD_HIERARCHICAL": facts["swap"],
        "SYNTH_MINIMUM_KEEP_SIZE": "10000",
        "SYNTH_OPERATIONS_ARGS": "",
        "SYNTH_HDL_FRONTEND": "slang" if facts["slang"] else "",
        "ADDER_MAP_FILE": str(plat_dir / "cells_adders.v") if facts["adder"] else "",
        "DONT_USE_CELLS": "TAPCELL_X1 FILLCELL_X1",
    }  # fmt: skip


def settings_of(config, tmp_path: Path, flow: str = "yosys") -> dict[str, Any]:
    """The settings of `flow` in the generated design file, with the check of the model."""
    variables = make_vars(tmp_path, config.spec)
    generated = tool.generated_design(
        config,
        variables,
        orfs=tmp_path / "orfs",
        orfs_commit="a" * 40,
        override_file=OVERRIDES / f"{config.platform}.yaml",
        sources=tool.design_sources(variables),
    )
    return generated["flows"][flow]


# ---- design files ---------------------------------------------------------------------------


def test_every_design_has_a_file_and_every_file_a_design() -> None:
    files = sorted(f"{p.parent.name}/{p.stem}" for p in DESIGNS.glob("*/*.yaml"))
    assert files == sorted(SPECS)
    for path in DESIGNS.glob("*/*.yaml"):
        assert path.read_text(encoding="utf-8").startswith(f"# {path.name}\n"), path


@pytest.mark.parametrize("spec", SPECS)
def test_a_design_file_loads_and_names_registered_flows(spec: str) -> None:
    config = tool.load_design_config(tool.design_config_path(spec))
    facts = FACTS[spec]
    assert config.spec == spec
    assert config.platform == spec.split("/")[0]
    assert config.clock_port == facts["port"]
    assert Time(config.clock_period).to("ns") == pytest.approx(facts["period"])
    registered = {name for cls in all_flow_classes() for name in [cls.name, *cls.aliases]}
    assert config.flows, "a design file configures at least one flow"
    assert set(config.flows) <= registered


@pytest.mark.parametrize("spec", SPECS)
def test_a_design_file_agrees_with_the_orfs_configuration(spec: str, tmp_path: Path) -> None:
    config = tool.load_design_config(tool.design_config_path(spec))
    variables = make_vars(tmp_path, spec)
    sdc = Path(variables["SDC_FILE"]).read_text(encoding="utf-8")
    assert tool.design_config_problems(config, variables, sdc) == []


@pytest.mark.parametrize("spec", SPECS)
def test_the_settings_of_a_design_file_validate(spec: str, tmp_path: Path) -> None:
    _skip_without_platform(spec)
    config = tool.load_design_config(tool.design_config_path(spec))
    table = settings_of(config, tmp_path)
    settings = Yosys.Settings.from_input(table, design_root=tmp_path)
    assert settings.platform == config.platform
    assert settings.pdk_provider == "directory"
    assert settings.pdk_version == "a" * 40
    assert settings.pdk_root == tmp_path / "orfs" / "flow" / "platforms"
    assert settings.platform_file == OVERRIDES / f"{config.platform}.yaml"
    (clock,) = settings.clocks.values()
    assert clock.period.to("ns") == pytest.approx(FACTS[spec]["period"])
    Yosys.check_settings_supported(settings)


@pytest.mark.parametrize("spec", SPECS)
def test_the_generated_design_is_a_valid_design(spec: str, tmp_path: Path) -> None:
    _skip_without_platform(spec)
    config = tool.load_design_config(tool.design_config_path(spec))
    variables = make_vars(tmp_path, spec)
    generated = tool.generated_design(
        config,
        variables,
        orfs=tmp_path / "orfs",
        orfs_commit="a" * 40,
        override_file=OVERRIDES / f"{config.platform}.yaml",
        sources=tool.design_sources(variables),
    )
    path = write(tmp_path / "design.yaml", yaml.safe_dump(generated, sort_keys=False))
    design = Design.from_file(path)
    assert design.name == f"{config.platform}_{spec.split('/')[1]}"
    assert design.rtl.top == FACTS[spec]["top"]
    (clock,) = design.rtl.clocks
    assert clock.port == FACTS[spec]["port"]
    listed = [str(s.path) for s in design.rtl.sources]
    for word in variables["VERILOG_FILES"].split():
        assert word in listed


def test_the_flag_of_each_orfs_variable_reaches_the_settings(tmp_path: Path) -> None:
    got = {}
    for spec in SPECS:
        config = tool.load_design_config(tool.design_config_path(spec))
        table = tool.expand_dotted(config.flows["yosys"])
        settings = Yosys.Settings.from_input(table, design_root=tmp_path)
        got[spec] = (
            settings.abc.strategy,
            settings.abc.mapper,
            settings.arithmetic.adder_map,
            settings.arithmetic.wrap_operators,
            settings.script_format,
            settings.systemverilog,
            settings.arithmetic.adder_architecture,
        )
    assert got["nangate45/gcd"] == ("area", "abc", "none", False, "ys", "slang", "kogge_stone")
    assert got["nangate45/jpeg"][0] == "area"
    assert got["nangate45/ibex"][:5] == ("speed", "abc", "platform", False, "ys")
    assert got["nangate45/aes"] == ("area", "abc_new", "platform", True, "tcl", "slang", "yosys")
    assert got["sky130hd/gcd"] == ("area", "abc_new", "none", True, "tcl", "slang", "yosys")
    for spec, found in got.items():
        wrapped = FACTS[spec]["swap"] == "1"
        assert found[-1] == ("yosys" if wrapped else "kogge_stone"), spec


def test_the_design_sources_list_each_file_of_an_include_directory_as_a_header(
    tmp_path: Path,
) -> None:
    ibex = make_vars(tmp_path, "nangate45/ibex")
    sources = tool.design_sources(ibex)
    typed = {Path(s["file"]).name: s["type"] for s in sources if isinstance(s, dict)}
    assert typed == {"prim_assert_dummy_macros.svh": "SVHeader", "prim_assert.sv": "SVHeader"}
    plain = [s for s in sources if isinstance(s, str)]
    assert plain == ibex["VERILOG_FILES"].split()
    jpeg = tool.design_sources(make_vars(tmp_path, "nangate45/jpeg"))
    assert [(Path(s["file"]).name, s["type"]) for s in jpeg if isinstance(s, dict)] == [
        ("dct_cos_table.v", "VerilogHeader")
    ]
    assert all(
        isinstance(s, str) for s in tool.design_sources(make_vars(tmp_path, "nangate45/gcd"))
    )


def test_the_sources_keep_the_order_of_orfs(tmp_path: Path) -> None:
    variables = make_vars(tmp_path, "nangate45/ibex")
    variables["VERILOG_FILES"] = " ".join(reversed(variables["VERILOG_FILES"].split()))
    plain = [s for s in tool.design_sources(variables) if isinstance(s, str)]
    assert plain == variables["VERILOG_FILES"].split()


def _config(**given: Any):
    table = {
        "orfs": "nangate45/gcd",
        "clock": {"port": "clk", "period": "0.25 ns"},
        "flows": {
            "yosys": {
                "abc.strategy": "area",
                "arithmetic.adder_map": "none",
                "arithmetic.adder_architecture": "kogge_stone",
            }
        },
    }
    table.update(given)
    return tool.config_from_table(table, Path("gcd.yaml"))


def test_a_clock_period_that_differs_from_the_sdc_is_refused(tmp_path: Path) -> None:
    variables = make_vars(tmp_path, "nangate45/gcd")
    config = _config(clock={"port": "clk", "period": "0.3 ns"})
    problems = tool.design_config_problems(config, variables, sdc_text(0.25, "clk"))
    assert len(problems) == 1
    assert "0.3 ns" in problems[0] and "clk_period 0.25" in problems[0]


def test_the_clock_period_is_compared_in_one_unit(tmp_path: Path) -> None:
    variables = make_vars(tmp_path, "nangate45/gcd")
    config = _config(clock={"port": "clk", "period": "250 ps"})
    assert tool.design_config_problems(config, variables, sdc_text(0.25, "clk")) == []


def test_a_clock_port_that_differs_from_the_sdc_is_refused(tmp_path: Path) -> None:
    variables = make_vars(tmp_path, "nangate45/gcd")
    problems = tool.design_config_problems(_config(), variables, sdc_text(0.25, "clk_i"))
    assert len(problems) == 1 and "clk_i" in problems[0]


def test_a_period_without_a_unit_is_refused() -> None:
    with pytest.raises(tool.ParityError, match="clock.period"):
        _config(clock={"port": "clk", "period": 0.25})
    with pytest.raises(tool.ParityError, match="clock.period"):
        _config(clock={"port": "clk", "period": "0.25"})


@pytest.mark.parametrize(
    ("variable", "value", "words"),
    [
        ("ABC_AREA", "0", ("abc.strategy", "ABC_AREA")),
        ("ADDER_MAP_FILE", "/p/cells_adders.v", ("arithmetic.adder_map", "ADDER_MAP_FILE")),
        ("SWAP_ARITH_OPERATORS", "1", ("arithmetic.wrap_operators", "SWAP_ARITH_OPERATORS")),
        ("SYNTH_HIERARCHICAL", "1", ("hierarchy.keep_min_cost", "SYNTH_HIERARCHICAL")),
        ("SYNTH_OPERATIONS_ARGS", "-booth", ("SYNTH_OPERATIONS_ARGS", "by hand")),
    ],
)
def test_a_setting_that_differs_from_the_orfs_variable_is_refused(
    tmp_path: Path, variable: str, value: str, words: tuple[str, str]
) -> None:
    variables = make_vars(tmp_path, "nangate45/gcd")
    variables[variable] = value
    sdc = Path(variables["SDC_FILE"]).read_text(encoding="utf-8")
    problems = tool.design_config_problems(_config(), variables, sdc)
    assert problems, variable
    assert all(word in " ".join(problems) for word in words)


def test_the_adder_architecture_of_orfs_is_kogge_stone_unless_operators_are_wrapped(
    tmp_path: Path,
) -> None:
    variables = make_vars(tmp_path, "nangate45/gcd")
    sdc = Path(variables["SDC_FILE"]).read_text(encoding="utf-8")
    yosys = {"abc.strategy": "area", "arithmetic.adder_map": "none"}
    problems = tool.design_config_problems(_config(flows={"yosys": yosys}), variables, sdc)
    assert len(problems) == 1
    assert "arithmetic.adder_architecture" in problems[0] and "kogge_stone" in problems[0]
    assert "SYNTH_OPERATIONS_ARGS" in problems[0]
    wrapped = {
        **yosys,
        "script_format": "tcl",
        "arithmetic.wrap_operators": True,
        "abc.mapper": "abc_new",
    }
    variables["SWAP_ARITH_OPERATORS"] = "1"
    assert tool.design_config_problems(_config(flows={"yosys": wrapped}), variables, sdc) == []
    wrapped["arithmetic.adder_architecture"] = "kogge_stone"
    # xeda refuses the pair itself: ORFS wraps the adders with its own architectures
    refused = tool.design_config_problems(_config(flows={"yosys": wrapped}), variables, sdc)
    assert len(refused) == 1 and "adder_architecture" in refused[0]


def test_a_flow_that_xeda_does_not_have_is_refused() -> None:
    with pytest.raises(tool.ParityError, match="no_such_flow"):
        tool.check_flow_names(_config(flows={"no_such_flow": {}}), {"yosys"})


def test_a_design_file_with_an_unknown_key_is_refused() -> None:
    with pytest.raises(tool.ParityError, match="colour"):
        _config(colour="red")
    with pytest.raises(tool.ParityError, match="orfs"):
        tool.config_from_table({"clock": {"port": "c", "period": "1 ns"}}, Path("x.yaml"))


# ---- the processes ---------------------------------------------------------------------------


def test_the_environment_of_a_tool_is_scrubbed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "x")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    home = tmp_path / "home"
    env = tool.scrubbed_env(home, ["/opt/OpenROAD/bin/openroad", "/opt/homebrew/bin/yosys"])
    assert set(env) == {"PATH", "HOME", "TMPDIR", "QT_QPA_PLATFORM"}
    assert env["HOME"] == str(home)
    assert env["TMPDIR"] == str(home / "tmp")
    path = env["PATH"].split(os.pathsep)
    assert path[:2] == ["/opt/OpenROAD/bin", "/opt/homebrew/bin"]
    assert path[-2:] == ["/usr/bin", "/bin"]
    assert len(path) == len(set(path))


@pytest.mark.parametrize(("system", "qt"), [("darwin", "cocoa"), ("linux", "offscreen")])
def test_the_qt_platform_follows_the_operating_system(tmp_path: Path, system: str, qt: str):
    assert tool.scrubbed_env(tmp_path, [], system=system)["QT_QPA_PLATFORM"] == qt


def test_the_scrubbed_environment_makes_no_directory(tmp_path: Path) -> None:
    tool.scrubbed_env(tmp_path / "home", [])
    assert not (tmp_path / "home").exists()
    tool.prepare_home(tmp_path / "home")
    assert (tmp_path / "home" / "tmp").is_dir()


def test_the_chosen_yosys_comes_first_on_the_path_of_xeda(tmp_path: Path) -> None:
    env = tool.xeda_env(tmp_path, yosys="/opt/cad/bin/yosys", openroad="/opt/OpenROAD/bin/openroad")
    assert env["PATH"].split(os.pathsep)[:2] == ["/opt/cad/bin", "/opt/OpenROAD/bin"]
    assert set(env) == {"PATH", "HOME", "TMPDIR", "QT_QPA_PLATFORM"}


def test_the_make_command_has_one_target_and_the_orfs_variables() -> None:
    argv = tool.make_argv(
        make="/usr/bin/make",
        orfs=Path("/orfs"),
        config=Path("/orfs/flow/designs/nangate45/gcd/config.mk"),
        work=Path("/w/work"),
        openroad="/opt/OpenROAD/bin/openroad",
        yosys="/opt/homebrew/bin/yosys",
        python="/venv/bin/python",
        cores=4,
        targets=["do-floorplan"],
    )
    assert argv == [
        "/usr/bin/make",
        "-f",
        "/orfs/flow/Makefile",
        "DESIGN_CONFIG=/orfs/flow/designs/nangate45/gcd/config.mk",
        "WORK_HOME=/w/work",
        "OPENROAD_EXE=/opt/OpenROAD/bin/openroad",
        "YOSYS_EXE=/opt/homebrew/bin/yosys",
        "PYTHON_EXE=/venv/bin/python",
        "NUM_CORES=4",
        "do-floorplan",
    ]


def test_the_xeda_command_runs_the_chain_on_the_generated_design() -> None:
    argv = tool.xeda_argv("/venv/bin/python", "yosys", Path("/w/design.yaml"), Path("/w/xeda_run"))
    assert argv == [
        "/venv/bin/python",
        "-m",
        "xeda",
        "run",
        "yosys",
        "--design",
        "/w/design.yaml",
        "--run-root",
        "/w/xeda_run",
        "--json",
    ]


def test_the_make_print_output_gives_the_variables() -> None:
    text = (
        "DESIGN_NAME: gcd\nADDER_MAP_FILE: \nSDC_FILE: /a b/c.sdc\nOTHER: 1\n"
        "VERILOG_FILES: /x/a.v /x/b.v\n"
    )
    got = tool.parse_make_print(text)
    assert got == {
        "DESIGN_NAME": "gcd",
        "ADDER_MAP_FILE": "",
        "SDC_FILE": "/a b/c.sdc",
        "VERILOG_FILES": "/x/a.v /x/b.v",
    }


def test_the_make_variables_that_the_tool_prints_include_the_ones_it_checks() -> None:
    for name in (
        "ABC_AREA",
        "SWAP_ARITH_OPERATORS",
        "SYNTH_HIERARCHICAL",
        "SDC_FILE",
        "ADDER_MAP_FILE",
    ):
        assert name in tool.MAKE_VARS


def test_the_canonicalization_script_reads_the_design_with_slang(tmp_path: Path) -> None:
    variables = make_vars(tmp_path, "nangate45/ibex")
    variables["LIB_FILES"] = "/p/lib/a.lib"
    variables["CLKGATE_MAP_FILE"] = "/p/cells_clkgate.v"
    script = tool.canonical_script(variables, tmp_path / "ibex.rtlil")
    assert "read_liberty -overwrite -setattr liberty_cell -lib /p/lib/a.lib" in script
    assert "read_slang -D SYNTHESIS --keep-hierarchy --compat=vcs --ignore-assertions" in script
    assert "--top ibex_core" in script
    assert "-I" + variables["VERILOG_INCLUDE_DIRS"] in script
    assert script.rstrip().endswith(f"write_rtlil {tmp_path / 'ibex.rtlil'}")


# ---- the seam to the OpenROAD flows -----------------------------------------------------------

ALL_FLOWS = {"yosys", "openroad_place", "openroad_cts", "openroad_grt", "openroad"}


def test_the_synthesis_half_is_the_yosys_flow_on_the_design_sources() -> None:
    plan = tool.xeda_plan(
        "synthesis", {"yosys"}, design_sources=["a.v"], orfs_netlist=None, sdc=Path("/s.sdc")
    )
    assert (plan.chain, plan.sources) == ("yosys", ["a.v"])


def test_the_pnr_half_runs_openroad_on_the_orfs_netlist_and_the_sdc() -> None:
    plan = tool.xeda_plan(
        "pnr",
        ALL_FLOWS,
        design_sources=["a.v"],
        orfs_netlist=Path("/orfs/1_2_yosys.v"),
        sdc=Path("/s.sdc"),
    )
    assert plan.chain == "openroad"
    assert plan.sources == [
        {"file": "/orfs/1_2_yosys.v", "type": "VerilogNetlist"},
        {"file": "/s.sdc", "type": "Sdc"},
    ]


def test_the_both_half_chains_the_flows_from_the_design_sources() -> None:
    plan = tool.xeda_plan(
        "both", ALL_FLOWS, design_sources=["a.v"], orfs_netlist=None, sdc=Path("/s.sdc")
    )
    assert plan.chain == "yosys+openroad_place+openroad_cts+openroad_grt+openroad"
    assert plan.sources == ["a.v", {"file": "/s.sdc", "type": "Sdc"}]


@pytest.mark.parametrize("half", ["pnr", "both"])
def test_a_half_without_its_flows_names_the_missing_flows(half: str) -> None:
    with pytest.raises(tool.MissingFlows) as error:
        tool.xeda_plan(
            half, {"yosys"}, design_sources=[], orfs_netlist=Path("/n.v"), sdc=Path("/s.sdc")
        )
    message = str(error.value)
    assert "openroad" in message and f"half {half}" in message
    assert "synthesis" in message, "the message says which half runs today"


def test_the_registered_flow_names_come_from_xeda() -> None:
    names = tool.registered_flow_names()
    assert "yosys" in names
    assert isinstance(names, frozenset)


def test_the_synthesis_results_of_xeda_become_a_row_part() -> None:
    document = {
        "success": True,
        "results": {
            "success": True,
            "tools": [{"executable": "yosys", "version": "0.69+post"}],
            "runtime": 0.8,
            "area": 506.996,
            "cells": 357,
            "flow_hash": "f",
            "settings_hash": "s",
            "design_hash": "d",
            "run_path": "/w/xeda_run/nangate45_gcd/yosys",
        },
    }
    part = tool.read_xeda_results(document, "synthesis")
    assert part["yosys_area"] == 506.996 and part["yosys_cells"] == 357
    assert part["runtime_s"] == 0.8
    assert part["tools"] == {"yosys": "0.69+post"}
    assert part["flow_hash"] == "f" and part["run_path"].endswith("/yosys")


def test_the_openroad_results_of_xeda_use_the_orfs_metric_names() -> None:
    results = {key: 1.0 for key in tool.METRIC_KEYS}
    document = {"success": True, "results": {"success": True, "runtime": 3.0, **results}}
    part = tool.read_xeda_results(document, "both")
    assert part["metrics"] == results


def test_a_missing_openroad_metric_is_named() -> None:
    document = {"success": True, "results": {"success": True, "runtime": 3.0}}
    with pytest.raises(tool.ParityError, match="finish__timing__setup__ws"):
        tool.read_xeda_results(document, "pnr")


def test_a_failed_xeda_run_gives_the_error() -> None:
    document = {"success": False, "error": {"type": "FlowFatalError", "message": "boom"}}
    with pytest.raises(tool.ParityError, match="FlowFatalError.*boom"):
        tool.read_xeda_results(document, "synthesis")


# ---- the ORFS results and the record ----------------------------------------------------------


def test_the_orfs_metrics_come_from_the_four_logs(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    write(logs / "1_synth.json", json.dumps({"synth__design__instance__area": 630.1,
                                              "synth__design__instance__count": 503}))  # fmt: skip
    write(logs / "5_1_grt.json", json.dumps({"globalroute__timing__setup__ws": -0.02}))
    write(logs / "5_2_route.json", json.dumps({"detailedroute__route__drc_errors": 0,
                                                "detailedroute__antenna__violating__nets": 2}))  # fmt: skip
    write(logs / "6_report.json", json.dumps({"finish__timing__setup__ws": -0.158,
                                               "finish__other": 7, "x": 1}))  # fmt: skip
    got = tool.read_orfs_metrics(logs)
    assert got == {
        "synth__design__instance__area": 630.1,
        "synth__design__instance__count": 503,
        "globalroute__timing__setup__ws": -0.02,
        "detailedroute__route__drc_errors": 0,
        "detailedroute__antenna__violating__nets": 2,
        "finish__timing__setup__ws": -0.158,
    }


def test_a_missing_log_gives_fewer_metrics(tmp_path: Path) -> None:
    assert tool.read_orfs_metrics(tmp_path / "none") == {}


def test_the_git_head_of_a_checkout_is_read_without_git(tmp_path: Path) -> None:
    commit = "6b305a712966be72d8908695c04398acdbe273ca"
    git = tmp_path / ".git"
    write(git / "HEAD", "ref: refs/heads/master\n")
    assert tool.git_commit(tmp_path) is None
    write(git / "refs" / "heads" / "master", commit + "\n")
    assert tool.git_commit(tmp_path) == commit
    (git / "refs" / "heads" / "master").unlink()
    write(git / "packed-refs", f"# pack-refs\n{commit} refs/heads/master\n")
    assert tool.git_commit(tmp_path) == commit
    write(git / "HEAD", "1" * 40 + "\n")
    assert tool.git_commit(tmp_path) == "1" * 40
    assert tool.git_commit(tmp_path / "no-checkout") is None


def test_the_digest_of_a_file_is_sha256(tmp_path: Path) -> None:
    path = write(tmp_path / "f.sdc", "set clk_period 1\n")
    assert tool.sha256_file(path) == hashlib.sha256(b"set clk_period 1\n").hexdigest()


def test_the_source_pins_digest_each_file_by_its_path_in_the_checkout(tmp_path: Path) -> None:
    a = write(tmp_path / "orfs" / "flow" / "designs" / "src" / "a.v", "a")
    sdc = write(tmp_path / "orfs" / "flow" / "designs" / "p" / "constraint.sdc", "s")
    outside = write(tmp_path / "elsewhere" / "b.v", "b")
    pins = tool.source_pins(tmp_path / "orfs", [a, outside], sdc)
    assert pins["files"] == {
        "flow/designs/src/a.v": hashlib.sha256(b"a").hexdigest(),
        str(outside): hashlib.sha256(b"b").hexdigest(),
    }
    assert pins["sdc"] == {"flow/designs/p/constraint.sdc": hashlib.sha256(b"s").hexdigest()}


def test_the_pinned_files_include_every_file_of_an_include_directory(tmp_path: Path) -> None:
    """A header can change without a change of the ORFS commit (a local edit), so every file of
    an include directory is part of the identity of a row, whatever its suffix."""
    top = write(tmp_path / "src" / "top.v", "module top; endmodule\n")
    include = tmp_path / "include"
    header = write(include / "defs.vh", "`define A 1\n")
    data = write(include / "table.txt", "1\n")
    variables = {"VERILOG_FILES": str(top), "VERILOG_INCLUDE_DIRS": str(include)}
    assert tool.pinned_files(variables) == [str(top), str(header), str(data)]
    added = write(include / "more.vh", "`define B 2\n")
    assert str(added) in tool.pinned_files(variables)


def test_the_residue_digest_is_that_of_the_bundled_file() -> None:
    from importlib import resources

    text = (resources.files("xeda.platforms") / "nangate45" / "platform.yaml").read_bytes()
    assert tool.residue_digest("nangate45") == hashlib.sha256(text).hexdigest()
    assert tool.residue_digest("no_such_platform") is None


def test_a_tool_version_is_the_first_line_of_its_output(tmp_path: Path) -> None:
    fake = write(tmp_path / "fake_tool", "#!/bin/sh\necho 'fake 1.2.3 (abc)'\necho more\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    assert tool.tool_version(str(fake), "-V", tmp_path / "home") == "fake 1.2.3 (abc)"
    assert tool.tool_version(str(tmp_path / "missing"), "-V", tmp_path / "home") is None


def test_the_result_files_are_under_the_root_by_design_and_half(tmp_path: Path) -> None:
    assert tool.result_path(tmp_path, "nangate45/gcd", "synthesis") == (
        tmp_path / "nangate45" / "gcd" / "synthesis" / "result.json"
    )
    assert tool.result_path(tmp_path, "nangate45/gcd", "orfs") == (
        tmp_path / "nangate45" / "gcd" / "orfs" / "result.json"
    )


# ---- the bar and the table ---------------------------------------------------------------------

GOOD = {
    "finish__timing__setup__ws": -0.100,
    "finish__timing__setup__tns": -1.0,
    "finish__timing__hold__ws": 0.05,
    "finish__timing__hold__tns": 0.0,
    "finish__design__instance__area__stdcell": 1000.0,
    "finish__power__total": 0.003,
    "detailedroute__route__drc_errors": 0,
    "detailedroute__antenna__violating__nets": 0,
    "synth__design__instance__area": 900.0,
    "synth__design__instance__count": 500,
}


def row(half: str = "synthesis", success: bool = True, **metrics: Any) -> dict[str, Any]:
    return {
        "schema": 1,
        "design": "nangate45/gcd",
        "half": half,
        "success": success,
        "failed_at": None if success else "do-route",
        "error": None,
        "metrics": {**GOOD, **metrics},
        "times": {"synthesis_s": 1.0, "pnr_s": 20.0},
    }


REFERENCE = row("orfs")


def test_a_result_that_equals_the_reference_meets_the_bar() -> None:
    assert tool.bar_problems(row(), REFERENCE) == []


@pytest.mark.parametrize(
    ("metrics", "problem"),
    [
        ({"finish__timing__setup__ws": -0.1149}, None),
        ({"finish__timing__setup__ws": -0.115}, None),
        ({"finish__timing__setup__ws": -0.1151}, "WNS"),
        ({"finish__timing__setup__ws": 0.2}, None),
        ({"finish__design__instance__area__stdcell": 1020.0}, None),
        ({"finish__design__instance__area__stdcell": 1020.1}, "area"),
        ({"finish__design__instance__area__stdcell": 900.0}, None),
        ({"detailedroute__route__drc_errors": 0}, None),
        ({"detailedroute__route__drc_errors": 1}, "DRC"),
        ({"detailedroute__antenna__violating__nets": 0}, None),
        ({"detailedroute__antenna__violating__nets": 3}, "antenna"),
        ({"finish__timing__hold__ws": 0.001}, None),
        ({"finish__timing__hold__ws": 0.0}, "hold"),
        ({"finish__timing__hold__ws": -0.01}, "hold"),
    ],
)
def test_each_condition_of_the_bar_has_both_sides(metrics: dict, problem: Optional[str]) -> None:
    problems = tool.bar_problems(row(**metrics), REFERENCE)
    if problem is None:
        assert problems == []
    else:
        assert len(problems) == 1 and problem in problems[0]


def test_several_failed_conditions_are_all_listed() -> None:
    problems = tool.bar_problems(
        row(
            finish__timing__setup__ws=-1.0,
            detailedroute__route__drc_errors=4,
            finish__timing__hold__ws=-0.5,
        ),
        REFERENCE,
    )
    assert len(problems) == 3


def test_a_failed_row_and_a_missing_metric_are_problems() -> None:
    assert "do-route" in tool.bar_problems(row(success=False), REFERENCE)[0]
    broken = row()
    del broken["metrics"]["finish__timing__setup__ws"]
    assert any("finish__timing__setup__ws" in p for p in tool.bar_problems(broken, REFERENCE))


def test_a_missing_reference_is_a_problem() -> None:
    assert tool.bar_problems(row(), None) == ["no ORFS reference row"]


def test_the_table_has_the_reference_row_first_and_a_verdict_per_half(tmp_path: Path) -> None:
    ref = row("orfs")
    ref["times"] = {"synthesis_s": 2.0, "pnr_s": 40.0}
    bad = row("synthesis", finish__timing__setup__ws=-0.3, detailedroute__route__drc_errors=2)
    for half, content in (("synthesis", bad), ("orfs", ref)):
        write(tool.result_path(tmp_path, "nangate45/gcd", half), json.dumps(content))
    text = tool.render_tables(tmp_path, ["nangate45/gcd"])
    lines = text.splitlines()
    assert lines[0] == "### nangate45/gcd"
    header = next(line for line in lines if line.startswith("| row"))
    for column in ("WNS", "TNS", "hold WNS", "hold TNS", "area", "power", "DRC", "antenna",
                   "synth area", "cells", "bar"):  # fmt: skip
        assert column in header
    body = [line for line in lines if line.startswith("| ") and not line.startswith("| row")]
    body = [line for line in body if not set(line) <= set("|-: ")]
    assert [line.split("|")[1].strip() for line in body] == ["orfs", "xeda synthesis"]
    assert "ok" in body[0].split("|")[-2]
    assert "WNS" in body[1].split("|")[-2] and "DRC" in body[1].split("|")[-2]
    assert "-0.100" in body[0] and "-0.300" in body[1]
    assert "3.000" in body[0], "power in mW"


def test_the_table_of_all_recorded_designs(tmp_path: Path) -> None:
    for spec in ("nangate45/gcd", "sky130hd/aes"):
        write(tool.result_path(tmp_path, spec, "orfs"), json.dumps({**REFERENCE, "design": spec}))
    write(tmp_path / ".resolve" / "stray.json", "{}")
    text = tool.render_tables(tmp_path, [])
    assert text.index("### nangate45/gcd") < text.index("### sky130hd/aes")


def test_the_table_marks_a_failed_row_and_a_missing_value(tmp_path: Path) -> None:
    failed = row("synthesis", success=False)
    failed["metrics"] = {}
    write(tool.result_path(tmp_path, "nangate45/gcd", "orfs"), json.dumps(REFERENCE))
    write(tool.result_path(tmp_path, "nangate45/gcd", "synthesis"), json.dumps(failed))
    text = tool.render_tables(tmp_path, ["nangate45/gcd"])
    assert "failed at do-route" in text


def test_a_design_without_results_says_so(tmp_path: Path) -> None:
    assert "no results" in tool.render_tables(tmp_path, ["nangate45/gcd"])


def test_the_table_command_needs_no_tool_and_no_checkout(tmp_path: Path) -> None:
    write(tool.result_path(tmp_path, "nangate45/gcd", "orfs"), json.dumps(REFERENCE))
    done = subprocess.run(
        [sys.executable, "-I", str(TOOL), "--root", str(tmp_path), "table"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert "### nangate45/gcd" in done.stdout


def test_the_help_lists_the_commands_and_options() -> None:
    done = subprocess.run(
        [sys.executable, "-I", str(TOOL), "--help"], capture_output=True, text=True, cwd=ROOT
    )
    assert done.returncode == 0
    for word in ("--orfs", "--root", "--openroad", "--yosys", "--xeda-yosys",
                 "--yosys-frontend", "--cores", "--timeout", "run", "table", "script"):  # fmt: skip
        assert word in done.stdout
    assert re.search(r"\bscript\b", done.stdout)


# ---- the run directories ---------------------------------------------------------------------


def _context(root: Path):
    return tool.Context(
        orfs=root / "orfs",
        root=root,
        openroad="openroad",
        yosys="yosys",
        xeda_yosys="yosys",
        yosys_frontend="yosys",
        cores=1,
        timeout=1,
    )


def test_a_rerun_empties_its_directory_under_the_root(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "root")
    old = write(tmp_path / "root" / "nangate45" / "gcd" / "synthesis" / "old.log", "x")
    tool.fresh_directory(ctx, old.parent)
    assert old.parent.is_dir() and not old.exists()


def test_a_directory_outside_the_root_is_never_deleted(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "root")
    precious = write(tmp_path / "work" / "mine.txt", "x")
    with pytest.raises(tool.ParityError, match="not under"):
        tool.fresh_directory(ctx, precious.parent)
    with pytest.raises(tool.ParityError, match="not under"):
        tool.fresh_directory(ctx, ctx.root)
    assert precious.exists()


# ---- the reference row must be current -----------------------------------------------------


def saved_row(**changes: Any) -> dict[str, Any]:
    """A row with the fields that identify a run."""
    row = {
        "orfs_commit": "a" * 40,
        "sources": {"orfs_commit": "a" * 40, "files": {"gcd.v": "1"}, "sdc": {"c.sdc": "2"}},
        "platform_residue_sha256": "3",
        "override_sha256": "4",
        "design_file_sha256": "5",
        "tools": {"openroad_sha256": "6", "yosys_orfs": "Yosys 0.69", "xeda": "0.5"},
        "make_vars": {"ABC_AREA": "1", "SDC_FILE": "/c.sdc"},
        "machine": {"platform": "macOS", "cores": 4, "cpu_count": 10},
        "metrics": {"x": 1},
        "times": {"pnr_s": 1.0},
    }
    for key, value in changes.items():
        row[key] = value
    return row


def test_a_row_of_the_same_inputs_is_current() -> None:
    other = saved_row(metrics={"x": 2}, times={"pnr_s": 9.0})
    other["machine"] = {"platform": "Linux", "cores": 4, "cpu_count": 64}
    assert tool.stale_reasons(saved_row(), other) == []


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"orfs_commit": "b" * 40}, "orfs_commit"),
        ({"sources": {"orfs_commit": "a" * 40, "files": {"gcd.v": "9"}, "sdc": {"c.sdc": "2"}}},
         "sources.files.gcd.v"),
        ({"sources": {"orfs_commit": "a" * 40, "files": {"gcd.v": "1"}, "sdc": {"c.sdc": "9"}}},
         "sources.sdc.c.sdc"),
        ({"platform_residue_sha256": "9"}, "platform_residue_sha256"),
        ({"override_sha256": "9"}, "override_sha256"),
        ({"design_file_sha256": "9"}, "design_file_sha256"),
        ({"tools": {"openroad_sha256": "9", "yosys_orfs": "Yosys 0.69", "xeda": "0.5"}},
         "tools.openroad_sha256"),
        ({"tools": {"openroad_sha256": "6", "yosys_orfs": "Yosys 0.70", "xeda": "0.5"}},
         "tools.yosys_orfs"),
        ({"make_vars": {"ABC_AREA": "0", "SDC_FILE": "/c.sdc"}}, "make_vars.ABC_AREA"),
        ({"machine": {"platform": "macOS", "cores": 8, "cpu_count": 10}}, "cores"),
        ({"reference_sha256": "9"}, "reference_sha256"),
    ],
)  # fmt: skip
def test_a_changed_input_makes_a_row_stale(changes: dict[str, Any], named: str) -> None:
    reasons = tool.stale_reasons(saved_row(), saved_row(**changes))
    assert len(reasons) == 1 and named in reasons[0]


def test_a_row_without_the_identity_fields_is_stale() -> None:
    old = {"metrics": {"x": 1}}
    reasons = tool.stale_reasons(old, saved_row())
    assert reasons and "orfs_commit" in " ".join(reasons)


def _design(spec: str = "nangate45/gcd"):
    return types.SimpleNamespace(config=types.SimpleNamespace(spec=spec))


def test_a_current_reference_is_reused(tmp_path: Path, monkeypatch, capsys) -> None:
    ctx = _context(tmp_path)
    saved = saved_row(success=True, half="orfs", design="nangate45/gcd")
    write(tool.result_path(tmp_path, "nangate45/gcd", "orfs"), json.dumps(saved))
    monkeypatch.setattr(tool, "new_row", lambda *args: saved_row())
    monkeypatch.setattr(tool, "run_reference", lambda *args: pytest.fail("reran"))
    assert tool.ensure_reference(ctx, _design(), force=False) == saved
    assert "stale" not in capsys.readouterr().out


def test_a_stale_reference_is_rerun_and_the_user_is_told(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    ctx = _context(tmp_path)
    write(tool.result_path(tmp_path, "nangate45/gcd", "orfs"), json.dumps(saved_row()))
    monkeypatch.setattr(tool, "new_row", lambda *args: saved_row(orfs_commit="b" * 40))
    monkeypatch.setattr(tool, "run_reference", lambda *args: {"rerun": True})
    assert tool.ensure_reference(ctx, _design(), force=False) == {"rerun": True}
    out = capsys.readouterr().out
    assert "stale" in out and "orfs_commit" in out


def test_force_reruns_a_current_reference(tmp_path: Path, monkeypatch) -> None:
    ctx = _context(tmp_path)
    write(tool.result_path(tmp_path, "nangate45/gcd", "orfs"), json.dumps(saved_row()))
    monkeypatch.setattr(tool, "new_row", lambda *args: saved_row())
    monkeypatch.setattr(tool, "run_reference", lambda *args: {"rerun": True})
    assert tool.ensure_reference(ctx, _design(), force=True) == {"rerun": True}


def test_a_half_row_is_reused_only_when_current(tmp_path: Path, monkeypatch, capsys) -> None:
    ctx = _context(tmp_path)
    write(tool.result_path(tmp_path, "nangate45/gcd", "synthesis"), json.dumps(saved_row()))
    monkeypatch.setattr(tool, "new_row", lambda *args: saved_row())
    assert tool.recorded_row(ctx, _design(), "synthesis", force=False) is not None
    monkeypatch.setattr(tool, "new_row", lambda *args: saved_row(override_sha256="x"))
    assert tool.recorded_row(ctx, _design(), "synthesis", force=False) is None
    assert "stale" in capsys.readouterr().out
    assert tool.recorded_row(ctx, _design(), "pnr", force=False) is None  # nothing recorded


def test_the_pnr_row_depends_on_the_reference_row_it_read(tmp_path: Path, monkeypatch) -> None:
    ctx = _context(tmp_path)
    write(tool.result_path(tmp_path, "nangate45/gcd", "orfs"), '{"a": 1}\n')
    digest = tool.sha256_file(tool.result_path(tmp_path, "nangate45/gcd", "orfs"))
    monkeypatch.setattr(tool, "new_row", lambda *args: saved_row())
    write(
        tool.result_path(tmp_path, "nangate45/gcd", "pnr"),
        json.dumps(saved_row(reference_sha256=digest)),
    )
    assert tool.recorded_row(ctx, _design(), "pnr", force=False) is not None
    write(tool.result_path(tmp_path, "nangate45/gcd", "orfs"), '{"a": 2}\n')
    assert tool.recorded_row(ctx, _design(), "pnr", force=False) is None
    # the synthesis half does not read the reference row, so a new reference leaves it current
    write(tool.result_path(tmp_path, "nangate45/gcd", "synthesis"), json.dumps(saved_row()))
    assert tool.recorded_row(ctx, _design(), "synthesis", force=False) is not None


# ---- a reference without a bar metric ----------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "word"),
    [
        ("finish__timing__setup__ws", "WNS"),
        ("finish__design__instance__area__stdcell", "area"),
    ],
)
def test_a_reference_without_a_bar_metric_is_a_problem(key: str, word: str) -> None:
    reference = row("orfs")
    del reference["metrics"][key]
    problems = tool.bar_problems(row(), reference)
    assert len(problems) == 1
    assert "the ORFS row has no" in problems[0] and word in problems[0] and key in problems[0]


def test_a_reference_with_an_area_of_zero_is_a_problem() -> None:
    reference = row("orfs", finish__design__instance__area__stdcell=0)
    problems = tool.bar_problems(row(), reference)
    assert len(problems) == 1 and "the ORFS row has no area" in problems[0]
