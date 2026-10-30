"""`run_remote` with the flows of the project, on the fake remote host.

Each test runs a flow on the host `somewhere` and compares the result with what a local run would
say: the same identity, the same files in the same places, and the records of the mirror in terms
of this machine. The fake FPGA toolchain, the fake Vivado and the fake OpenROAD stand in for the
tools; a test marked `real_tool` runs the installed tool on the "remote" instead.
"""

import json
import os
import shutil
from pathlib import Path

import pytest
from xeda.errors import SpecError
import yaml

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.launch_report import results_of
from xeda.outcomes import result_from_dict
from xeda.records import decode_record, encode_record, read_record
from xeda.run_root import ensure_run_root

from .io_flows import TInputTaker, TJoin, TMaker, TPartialMaker, TPlace, TTaker
from .remote_fixtures import (
    connected,
    fake_remote,
    fake_ssh,
    remote_host,
    remote_request,
)
from .tiny_platform import TINY_OPENROAD_RESIDUE, TINY_RESIDUE, tiny_store
from .tool_utils import require_bsc, require_ghdl, require_yosys
from .design_edits import design_from_dump
from .scoping import node_settings

FIXTURES = (fake_ssh, fake_remote, remote_host)

pytestmark = [pytest.mark.skipif(os.name != "posix", reason="POSIX")]

TESTS_DIR = Path(__file__).parent.absolute()
SQRT = TESTS_DIR.parent / "examples" / "vhdl" / "sqrt"
TARGETS = TESTS_DIR / "resources" / "targets"
SQRT_SETTINGS = ["fpga.part=xc7a12tcsg325-1", "clocks.main_clock.period=5ns"]


def _sqrt_design(root: Path, extra: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(SQRT / "sqrt.vhdl", root)
    (root / "sqrt.yaml").write_text(
        "name: sqrt\nrtl:\n  sources: [sqrt.vhdl]\n  top: sqrt\n  clocks: [{port: clk}]\n" + extra
    )
    return root / "sqrt.yaml"


def _succeeded(outcome) -> dict:
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    return outcome.document


def _results(document: dict) -> dict:
    """What the record of the requested node says, as a flat table: its metrics by their dotted
    paths, `success`, the artifacts and the declared outputs with the paths of the mirror."""
    return results_of(result_from_dict(document).record)


def _identity(document: dict) -> str:
    """The identity of the requested node."""
    return document["nodes"][-1]["record"]["identity"]


def _run_dir(document: dict) -> Path:
    """The mirror directory of the requested node."""
    return Path(document["nodes"][-1]["run_dir"])


def _mirror(tmp_path: Path) -> Path:
    return tmp_path / "local" / "xeda_run" / "@somewhere"


def _local_hash(design: Path, flow: str, settings, tmp_path: Path) -> str:
    """The identity of the requested node in a local plan."""
    plan = DefaultRunner(tmp_path / "plan_only", display_results=False).plan(
        flow, design, flow_settings=node_settings(flow, settings)
    )
    return plan.node(flow).flowrun_hash


# --- Targets ----------------------------------------------------------------------------------


def test_a_remote_run_takes_a_target(tmp_path, remote_host):
    outcome = remote_request(tmp_path, TARGETS / "knight.yaml", "vivado_synth", target="arty")
    document = _succeeded(outcome)
    assert document["spec"]["target"] == "arty"
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere"
    assert _run_dir(document) == mirror / "knight" / "arty" / "vivado_synth"
    assert (
        remote_host.workspace() / "xeda_run" / "knight" / "arty" / "vivado_synth" / "results.json"
    ).is_file()
    # a target that the design does not have is refused before any connection
    calls = len(remote_host.remote.calls("ssh"))
    refused = remote_request(tmp_path, TARGETS / "knight.yaml", "vivado_synth", target="nexys")
    assert refused.error is not None and "arty, ulx3s" in str(refused.error)
    assert len(remote_host.remote.calls("ssh")) == calls  # not even `ssh -G`
    assert not [c for c in remote_host.remote.calls("ssh")[calls:] if "-M" in c]


def test_a_remote_run_without_a_target_runs_the_design(tmp_path, remote_host):
    document = _succeeded(
        remote_request(
            tmp_path,
            TARGETS / "knight.yaml",
            "vivado_synth",
            flow_settings=node_settings("vivado_synth", ["fpga.part=xc7a100tcsg324-1"]),
        )
    )
    assert document["spec"]["target"] is None
    # No overlay reached the design: neither the sources nor the defines of a target. The record
    # of the mirror names the files of this machine.
    settings = json.loads((_run_dir(document) / "settings.json").read_text())
    rtl = settings["design"]["rtl"]
    assert [Path(source["file"]) for source in rtl["sources"]] == [
        TARGETS / "knight.v",
        TARGETS / "por_sync.v",
    ]
    assert "CLK_HZ" not in (rtl.get("defines") or {})


# --- Identity and the records of the mirror -----------------------------------------------------


def test_a_remote_run_comes_back_whole_and_hashed_as_it_was_sent(tmp_path, remote_host):
    design_root = tmp_path / "design"
    design = _sqrt_design(
        design_root, "  parameters:\n    ROM: {file: rom.mem}\n    TRACE: {path: out/trace.txt}\n"
    )
    (design_root / "rom.mem").write_text("00 11\n")
    run_root = ensure_run_root(tmp_path / "local" / "xeda_run")
    assert run_root is not None
    # a local run in the default layout, whose directory the mirror must not touch
    local_trace = run_root / "sqrt" / "vivado_synth" / "trace.json"
    local_trace.parent.mkdir(parents=True)
    local_trace.write_text("{}")

    document = _succeeded(
        remote_request(
            tmp_path,
            design,
            "vivado_synth",
            flow_settings=node_settings("vivado_synth", SQRT_SETTINGS),
        )
    )
    assert local_trace.read_text() == "{}", "a local run's directory is not the mirror"
    mirror = _run_dir(document)
    assert mirror == run_root / "@somewhere" / "sqrt" / "vivado_synth"

    # One run, one identity: the remote hashes what this side hashed, although it unpacked
    # everything under another root.
    results = _results(document)
    assert _identity(document) == _local_hash(design, "vivado_synth", SQRT_SETTINGS, tmp_path)
    settings = json.loads((mirror / "settings.json").read_text())
    remote_dir = remote_host.workspace() / "xeda_run" / "sqrt" / "vivado_synth"
    remote_settings = json.loads((remote_dir / "settings.json").read_text())
    for key in ("design_hash", "flowrun_hash", "rtl_hash", "settings_hash"):
        assert settings[key] == remote_settings[key], key

    # A file-valued parameter reached the remote's tool as a file in the remote's own tree.
    remote_parameters = remote_settings["design"]["rtl"]["parameters"]
    inputs = remote_host.workspace() / "inputs"
    assert Path(remote_parameters["ROM"]).is_relative_to(inputs)
    assert Path(remote_parameters["ROM"]).read_text() == "00 11\n"
    assert Path(remote_parameters["TRACE"]).is_relative_to(inputs)
    # The record of the mirror is a re-runnable input: it names the files of this side, and its
    # design loads here as the same design.
    parameters = settings["design"]["rtl"]["parameters"]
    assert parameters["ROM"] == str(design_root / "rom.mem")
    assert parameters["TRACE"] == str(design_root / "out" / "trace.txt")
    recorded = design_from_dump(settings["design"])
    assert recorded.rtl_hash == settings["rtl_hash"]

    # Artifacts were fetched, and the reported paths name the mirror.
    artifacts = results["artifacts"]
    paths = [p for p in (artifacts.values() if isinstance(artifacts, dict) else artifacts) if p]
    assert paths, "the flow reported artifacts to fetch"
    for path in paths:
        if isinstance(path, str):
            assert Path(path).is_relative_to(mirror), path
            assert Path(path).exists(), path
    saved = json.loads((mirror / "results.json").read_text())
    assert saved["outcome"] == "passed"
    assert not (mirror / "trace.json").exists()


def test_a_flow_without_declared_io_has_the_identity_it_was_sent(tmp_path, remote_host):
    """`dc` has no declared inputs or outputs: the remote resolves the same one-node plan, so its
    `flow_hash` is the one this side computed."""
    design = _sqrt_design(tmp_path / "design")
    (design.parent / "cells.db").write_text("")
    settings = ["target_libraries=cells.db", "clocks.main_clock.period=5ns"]
    outcome = remote_request(tmp_path, design, "dc", flow_settings=node_settings("dc", settings))
    # the fake `dc_shell` writes no timing report, so the run itself may report failure: what is
    # compared is the identity
    assert outcome.error is None, outcome.document
    assert _identity(outcome.document) == _local_hash(design, "dc", settings, tmp_path)


@pytest.mark.parametrize("flow_name", ["vivado_postsynth_sim", "vivado_power"])
def test_a_vivado_composite_resolves_the_same_declared_graph(tmp_path, remote_host, flow_name):
    """Declared hand-over survives the relocation of the inputs and the identity check."""
    from .test_tool_input_equivalence import VIVADO_SETTINGS, write_vivado_design

    root = tmp_path / "design"
    root.mkdir()
    write_vivado_design(root)
    remote_host.configure(env={"XEDA_FAKE_XSIM_STATE": "finish5"})
    document = _succeeded(
        remote_request(
            tmp_path,
            root / "design.yaml",
            flow_name,
            flow_settings=node_settings(flow_name, list(VIVADO_SETTINGS)),
        )
    )
    expected = _local_hash(root / "design.yaml", flow_name, VIVADO_SETTINGS, tmp_path)
    assert _identity(document) == expected
    remote = remote_host.workspace() / "xeda_run" / "sim" / flow_name
    trace = json.loads((remote / "trace.json").read_text())
    assert len(trace["declared_inputs"]) == (3 if flow_name == "vivado_postsynth_sim" else 2)
    if flow_name == "vivado_power":
        assert "power.total" in _results(document)


def test_the_fpga_graph_runs_on_the_remote_and_mirrors_its_bitstream(tmp_path, remote_host):
    """`fpga_pack` on the remote: its producers run there in their own run directories, and the
    recorded bitstream comes back, verified, into the mirror."""
    from xeda.digest import content_digest

    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    design = root / "top.yaml"
    design.write_text("name: top\nrtl:\n  sources: [top.v]\n  top: top\n")
    settings = ["fpga.part=LFE5U-25F-6BG381C"]
    document = _succeeded(
        remote_request(
            tmp_path, design, "fpga_pack", flow_settings=node_settings("fpga_pack", settings)
        )
    )
    assert _identity(document) == _local_hash(design, "fpga_pack", settings, tmp_path)
    assert [node["name"] for node in document["nodes"]] == ["yosys_fpga", "nextpnr", "fpga_pack"]
    recorded = _results(document)["outputs"]["bitstream"]
    mirrored = Path(recorded["path"])
    assert mirrored.is_relative_to(_mirror(tmp_path)) and mirrored.name == "top.bit"
    assert mirrored.read_bytes() == b"\x00\xffXEDA bitstream\x00"
    assert content_digest(mirrored) == recorded["sha"]
    workspace = remote_host.workspace() / "xeda_run" / "top"
    tools = {}
    for name in ("yosys_fpga", "nextpnr", "fpga_pack"):
        calls = (workspace / name / "fake_fpga.calls.jsonl").read_text()
        tools[name] = [json.loads(line)["tool"] for line in calls.splitlines()]
    assert tools == {"yosys_fpga": ["yosys"], "nextpnr": ["nextpnr-ecp5"], "fpga_pack": ["ecppack"]}
    trace = json.loads((workspace / "fpga_pack" / "trace.json").read_text())
    assert trace["declared_inputs"][0]["origin"]


# --- A failed run -------------------------------------------------------------------------------

NETLIST_ARTIFACTS = ("netlist", "netlist_timing", "sdf", "xdc_exported")


def test_a_failed_remote_run_reports_its_failure(tmp_path, remote_host):
    """When the run failed, its results still listed the netlists, and fetching those missing
    files crashed the run instead of reporting its failure."""
    remote_host.configure(env={"XEDA_FAKE_TOOL_FAIL": "route_design"})
    design = _sqrt_design(tmp_path / "design")
    outcome = remote_request(
        tmp_path,
        design,
        "vivado_alt_synth",
        flow_settings=node_settings(
            "vivado_alt_synth", [*SQRT_SETTINGS, "write_netlist=true", "write_timing_netlist=true"]
        ),
    )
    assert outcome.error is None and not outcome.success, outcome.document
    results = _results(outcome.document)
    assert results["success"] is False
    for label in NETLIST_ARTIFACTS:
        assert label not in results["artifacts"], label
    saved = json.loads((_run_dir(outcome.document) / "results.json").read_text())
    mirror = _run_dir(outcome.document)
    assert saved["outcome"] == "failed"
    assert {key: str(mirror / name) for key, name in saved["artifacts"].items()} == dict(
        results["artifacts"]
    )


def test_a_failed_remote_run_does_not_fetch_an_output_it_never_wrote(tmp_path, remote_host):
    remote_host.configure(env={"XEDA_FAKE_TOOL_FAIL": "launch_runs"})
    design = _sqrt_design(tmp_path / "design")
    outcome = remote_request(
        tmp_path,
        design,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", [*SQRT_SETTINGS, "bitstream=top.bit"]),
    )
    assert outcome.error is None and not outcome.success, outcome.document
    assert "bitstream" not in _results(outcome.document)["artifacts"]
    mirror = _run_dir(outcome.document)
    assert not list(mirror.rglob("top.bit")), "a bitstream the run never wrote was fetched"
    assert "bitstream" not in json.loads((mirror / "results.json").read_text())["artifacts"]


def test_a_failed_remote_run_records_no_artifact_it_never_wrote(tmp_path, remote_host):
    """The record of a failed run lists only the files that the run wrote, so the client fetches
    no file that was never there. The log of the tool that failed is one of them."""
    remote_host.configure(env={"XEDA_FAKE_TOOL_FAIL": "launch_runs"})
    design = _sqrt_design(tmp_path / "design")
    outcome = remote_request(
        tmp_path,
        design,
        "vivado_synth",
        flow_settings=node_settings(
            "vivado_synth", [*SQRT_SETTINGS, "bitstream=top.bit", "write_netlist=true"]
        ),
    )
    assert outcome.error is None and not outcome.success, outcome.document
    mirror = _run_dir(outcome.document)
    saved = json.loads((mirror / "results.json").read_text())
    assert set(saved["artifacts"]) == {"log"}
    assert saved["error"]["log"] == "log"
    assert (mirror / saved["artifacts"]["log"]).is_file()
    assert set(_results(outcome.document)["artifacts"]) == {"log"}
    assert not list(mirror.rglob("top.bit")), "a bitstream the run never wrote was fetched"
    remote_results = json.loads(
        (
            remote_host.workspace() / "xeda_run" / "sqrt" / "vivado_synth" / "results.json"
        ).read_text()
    )
    assert set(remote_results["artifacts"]) == {"log"}


# --- Real tools on the remote -------------------------------------------------------------------


@pytest.mark.real_tool
def test_a_remote_simulation_reads_and_writes_its_file_parameters(tmp_path, remote_host):
    """With a real simulator: the testbench opens the file its `ROM` generic names, and writes to
    the one `TRACE` names -- a path the remote made a place for, not a file that traveled."""
    require_ghdl()
    remote_host.expose("ghdl")
    design_root = tmp_path / "design"
    design_root.mkdir()
    (design_root / "rom.mem").write_text("00 11\n")
    (design_root / "dut.vhd").write_text(
        "entity dut is end;\narchitecture a of dut is\nbegin\nend;\n"
    )
    (design_root / "tb.vhd").write_text(
        "use std.textio.all;\n"
        "entity tb is generic (ROM : string; TRACE : string); end;\n"
        "architecture a of tb is\nbegin\n"
        "  process\n"
        "    file f : text; file o : text; variable l : line; variable s : string(1 to 5);\n"
        "  begin\n"
        "    file_open(f, ROM, read_mode); readline(f, l); read(l, s);\n"
        '    assert s = "00 11" report "unexpected ROM: " & s severity failure;\n'
        '    file_open(o, TRACE, write_mode); write(l, string\'("seen ") & s); writeline(o, l);\n'
        "    std.env.finish;\n"
        "    wait;\n"
        "  end process;\n"
        "end;\n"
    )
    (design_root / "d.yaml").write_text(
        "name: d\nlanguage: {vhdl: {standard: '2008'}}\n"
        "rtl: {sources: [dut.vhd], top: dut}\n"
        "tb:\n  sources: [tb.vhd]\n  top: tb\n"
        "  parameters: {ROM: {file: rom.mem}, TRACE: {path: trace.txt}}\n"
    )
    document = _succeeded(remote_request(tmp_path, design_root / "d.yaml", "ghdl_sim"))
    results = _results(document)
    assert results["sim.evidence"]["ended_by"] == "finish"
    assert results["sim.ended_by"] == "finish"
    remote_dir = remote_host.workspace() / "xeda_run" / "d" / "ghdl_sim"
    remote_results = json.loads((remote_dir / "results.json").read_text())
    assert remote_results["metrics"]["sim"]["evidence"] == results["sim.evidence"]
    remote_settings = json.loads((remote_dir / "settings.json").read_text())
    trace = Path(remote_settings["design"]["tb"]["parameters"]["TRACE"])
    assert trace.is_relative_to(remote_host.workspace())
    assert trace.read_text().strip() == "seen 00 11"
    assert not (design_root / "trace.txt").exists(), "the local tree is not the remote's"


@pytest.mark.real_tool
def test_remote_ghdl_synth_fetches_list_artifacts(tmp_path, remote_host):
    """GHDL per-source synthesis hands its Verilog files over as the list output `verilog`."""
    require_ghdl()
    remote_host.expose("ghdl")
    design_root = tmp_path / "design"
    design_root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", design_root)
    (design_root / "sqrt.yaml").write_text(
        "name: sqrt\nlanguage: {vhdl: {standard: '2008'}}\n"
        "rtl: {sources: [sqrt.vhdl], top: sqrt}\n"
    )
    document = _succeeded(
        remote_request(
            tmp_path,
            design_root / "sqrt.yaml",
            "ghdl_synth",
            flow_settings=node_settings("ghdl_synth", ["verilog_output=vout"]),
        )
    )
    run_path = _run_dir(document)
    saved = json.loads((run_path / "results.json").read_text())
    assert document["nodes"][-1]["record"] == saved
    assert "generated_verilog" not in " ".join(saved["artifacts"])
    names = saved["outputs"]["verilog"]["names"]
    assert len(names) >= 1 and all((run_path / name).is_file() for name in names)


def _mac_design(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "mac.v").write_text(
        "module mac(input clk, input [7:0] a, b, output reg [15:0] q);\n"
        "  always @(posedge clk) q <= a * b;\nendmodule\n"
    )
    design = root / "mac.yaml"
    design.write_text("name: mac\nrtl:\n  sources: [mac.v]\n  top: mac\n  clocks: [{port: clk}]\n")
    return design


def _stores(tmp_path, monkeypatch, remote_host) -> Path:
    """The PDK store of the platform `tiny` under the home directory of this side and of the
    remote. Return the store of this side."""
    local_home = tmp_path / "store_home"
    store = tiny_store(local_home, ".ciel")
    tiny_store(remote_host.home, ".ciel")
    monkeypatch.setenv("HOME", str(local_home))
    return store


@pytest.mark.real_tool
def test_a_platform_file_s_remote_run_has_this_side_s_identity(tmp_path, remote_host, monkeypatch):
    """`yosys` with a `platform_file` and no `platform` is accepted by a remote: the residue file
    is shipped as a path setting, `pdk_root` is not (the remote uses its own store), and the
    remote's `flow_hash` equals this side's. The run maps to the cells of the library in the
    store of the remote."""
    require_yosys()
    remote_host.expose("yosys")
    _stores(tmp_path, monkeypatch, remote_host)
    design = _mac_design(tmp_path / "design")
    settings = [
        f"platform_file={TINY_RESIDUE}",
        "clocks.main_clock.period=2ns",
        "pdk_root=elsewhere",
    ]
    document = _succeeded(
        remote_request(tmp_path, design, "yosys", flow_settings=node_settings("yosys", settings))
    )
    results = _results(document)
    assert _identity(document) == _local_hash(design, "yosys", settings, tmp_path)
    remote_dir = remote_host.workspace() / "xeda_run" / "mac" / "yosys"
    shipped = json.loads((remote_dir / "settings.json").read_text())["flow_settings"]
    assert shipped["pdk_root"] is None, "the place of the store on this side is not shipped"
    assert Path(shipped["platform_file"]).resolve().is_relative_to(remote_host.home.resolve())
    assert "_X1 " in Path(results["outputs"]["netlist"]["path"]).read_text()
    assert results["platform"]["name"] == "tiny"


@pytest.mark.real_tool
def test_a_liberty_file_s_remote_run_has_this_side_s_identity(tmp_path, remote_host, monkeypatch):
    """The Liberty files of `liberty` are path settings: they are shipped, and the remote reads
    its own copies. The remote's `flow_hash` equals this side's, and its netlist is mapped to the
    cells of those files."""
    require_yosys()
    remote_host.expose("yosys")
    store = _stores(tmp_path, monkeypatch, remote_host)
    library = tmp_path / "libs" / "own.lib"
    library.parent.mkdir()
    library.write_text((store / "tiny" / "tiny.lib").read_text())
    design = _mac_design(tmp_path / "design")
    settings = [
        f"platform_file={TINY_RESIDUE}",
        f"liberty={library}",
        "clocks.main_clock.period=2ns",
    ]
    document = _succeeded(
        remote_request(tmp_path, design, "yosys", flow_settings=node_settings("yosys", settings))
    )
    results = _results(document)
    assert _identity(document) == _local_hash(design, "yosys", settings, tmp_path)
    remote_dir = remote_host.workspace() / "xeda_run" / "mac" / "yosys"
    shipped = json.loads((remote_dir / "settings.json").read_text())
    (given,) = shipped["flow_settings"]["liberty"]
    assert Path(given).resolve().is_relative_to(remote_host.home.resolve())
    assert "netlist_generic" not in results["outputs"]
    assert "_X1 " in Path(results["outputs"]["netlist"]["path"]).read_text()
    # the record of the mirror names the file of this side
    mirror_settings = json.loads((_run_dir(document) / "settings.json").read_text())
    assert mirror_settings["flow_settings"]["liberty"] == [str(library)]


def test_the_files_a_liberty_file_includes_travel_with_it(tmp_path, remote_host, monkeypatch):
    """A Liberty file of `liberty` that includes other files by their absolute names (OpenSTA
    reads such an include; yosys does not): the remote receives the included files in places of
    their own, and copies that name those places. The remote's `flow_hash` equals this side's,
    the placement runs on the library, and the included files are inputs of its run."""
    import gzip

    from .openroad_utils import openroad_design

    store = _stores(tmp_path, monkeypatch, remote_host)
    parts = tmp_path / "libs" / "parts"
    parts.mkdir(parents=True)
    original = (store / "tiny" / "tiny.lib").read_text()
    unit = '  time_unit : "1ns";\n'
    assert unit in original
    units = parts / "units.lib.gz"
    with gzip.open(units, "wt") as stream:
        stream.write(unit)
    nested = parts / "nested.lib.gz"
    with gzip.open(nested, "wt") as stream:
        stream.write(f"include_file({units});\n")
    library = tmp_path / "libs" / "own.lib"
    library.write_text(original.replace(unit, f"  include_file({nested});\n"))
    design_file = openroad_design(tmp_path / "design")
    settings = [
        f"platform_file={TINY_OPENROAD_RESIDUE}",
        f"liberty={library}",
        # the section of a flow that is not in the plan lists the same file, which then also
        # travels as a plain path setting
        f"flows.yosys.liberty={library}",
        "clocks.main_clock.period=2ns",
    ]
    document = _succeeded(
        remote_request(
            tmp_path,
            design_file,
            "openroad_place",
            flow_settings=node_settings("openroad_place", settings),
        )
    )
    assert _identity(document) == _local_hash(design_file, "openroad_place", settings, tmp_path)
    run_dir = remote_host.workspace() / "xeda_run" / "counter" / "openroad_place"
    (top,) = json.loads((run_dir / "settings.json").read_text())["flow_settings"]["liberty"]
    remote_text = Path(top).read_text()
    assert str(units) not in remote_text and str(nested) not in remote_text
    assert str(remote_host.workspace().resolve()) in remote_text
    calls = (run_dir / "fake_openroad.calls").read_text()
    assert str(Path(top).resolve()) in calls
    trace = json.loads((run_dir / "trace.json").read_text())
    inputs = {Path(name).name for name in (*trace["inputs"], *trace["implicit_inputs"])}
    assert {"nested.lib.gz", "units.lib.gz"} <= inputs


def _bluespec_design(tmp_path: Path) -> Path:
    """The Bluespec example in a directory of the test, with its flow sections replaced by a clock
    for yosys, so that yosys synthesizes for no platform."""
    root = tmp_path / "gcd"
    shutil.copytree(TESTS_DIR.parent / "examples" / "bluespec" / "gcd", root)
    document = yaml.safe_load((root / "gcd.yaml").read_text())
    document["flows"] = {"yosys": {"clocks": {"main_clock": {"period": "2 ns"}}}}
    del document["targets"]
    (root / "gcd.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    return root / "gcd.yaml"


def test_a_converted_design_is_checked_as_what_it_will_be_before_connecting(
    tmp_path, remote_host, monkeypatch
):
    """`yosys` cannot read Bluespec, and the plan has bsc convert it on the remote: the request is
    accepted here, and fails only when it reaches for the connection."""
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")
    outcome = remote_request(tmp_path, _bluespec_design(tmp_path), "yosys")
    assert outcome.error is not None and outcome.document["error"]["code"] == "remote_unreachable"
    assert "cannot connect" in outcome.document["error"]["message"]
    # a flow with no converter is still refused here, for the language
    refused = remote_request(tmp_path, _bluespec_design(tmp_path / "again"), "ghdl_sim")
    assert isinstance(refused.error, SpecError)
    assert "ghdl_sim cannot read the design's Bluespec" in str(refused.error)


@pytest.mark.real_tool
def test_a_remote_run_converts_a_bluespec_design_for_yosys_on_the_remote(tmp_path, remote_host):
    require_bsc()
    require_yosys()
    remote_host.expose("bsc", "yosys")
    design_file = _bluespec_design(tmp_path)
    document = _succeeded(remote_request(tmp_path, design_file, "yosys"))
    assert _identity(document) == _local_hash(design_file, "yosys", [], tmp_path)
    assert [node["name"] for node in document["nodes"]] == ["bsc", "yosys"]
    # bsc ran on the remote, in a directory beside the one of yosys
    parent = remote_host.workspace() / "xeda_run" / "gcd"
    assert (parent / "bsc").is_dir() and (parent / "yosys").is_dir()
    assert Path(_results(document)["outputs"]["netlist_generic"]["path"]).read_text()


@pytest.mark.real_tool
def test_a_remote_bsc_returns_its_design_record_with_the_paths_of_this_side(tmp_path, remote_host):
    require_bsc()
    remote_host.expose("bsc")
    document = _succeeded(remote_request(tmp_path, _bluespec_design(tmp_path), "bsc"))
    mirror = _mirror(tmp_path)
    record = _results(document)["outputs"]["design"]
    assert record["kind"] == "design" and record["top"] == "mkGcdUnit"
    generated = [item for item in record["sources"] if "generated" in item]
    assert generated
    for item in generated:
        path = Path(item["generated"])
        assert path.is_file() and path.resolve().is_relative_to(mirror.resolve()), item
        assert not path.resolve().is_relative_to(remote_host.home.resolve())
    assert {Path(item["generated"]).name for item in generated} >= {"mkGcdUnit.v"}


# --- Flows with declared inputs and outputs (the test flows of `io_flows`) -----------------------


def _declared_design(root: Path, sections: dict | None = None, sources=()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    design = root / "d.yaml"
    design.write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "rtl": {"sources": list(sources), "top": "t"},
                **({"flows": sections} if sections else {}),
            }
        )
    )
    return design


@pytest.mark.parametrize("location", ["design", "cwd", "external"])
def test_a_producers_settings_only_file_is_shipped(tmp_path, remote_host, monkeypatch, location):
    remote_host.load_test_flows()
    start = tmp_path / "started_in"
    start.mkdir()
    monkeypatch.chdir(start)
    root = tmp_path / "design"
    root.mkdir()
    input_path = {
        "design": root / "input.txt",
        "cwd": start / "input.txt",
        "external": tmp_path / "input.txt",
    }[location]
    input_path.write_text("from settings\n")
    design = _declared_design(root, {"t_input_maker": {"input_file": str(input_path)}})
    document = _succeeded(remote_request(tmp_path, design, TInputTaker.name))
    assert _results(document)["read"] == "from settings\n"
    producer = remote_host.workspace() / "xeda_run" / "d" / "t_input_maker"
    settings = json.loads((producer / "settings.json").read_text())
    input_file = Path(settings["flow_settings"]["input_file"])
    assert input_file.is_relative_to(remote_host.workspace())
    assert input_file.read_text() == "from settings\n"
    # the mirror's record names the file of this side
    mirror_settings = json.loads(
        (_mirror(tmp_path) / "d" / "t_input_maker" / "settings.json").read_text()
    )
    assert mirror_settings["flow_settings"]["input_file"] == str(input_path.resolve()) or Path(
        mirror_settings["flow_settings"]["input_file"]
    ).samefile(input_path)
    expected = DefaultRunner(tmp_path / "plan", display_results=False).resolve(
        TInputTaker, Design.load(design), {}
    )
    assert settings["flowrun_hash"] == expected.node("t_input_maker").flowrun_hash


@pytest.mark.parametrize(
    "sections",
    [
        {"t_place": {"fpga": {"part": "LFE5U-25F-6BG256C"}}},
        {"t_synth": {"fpga": {"part": "LFE5U-25F-6BG256C"}}},
    ],
    ids=["requested", "producer"],
)
def test_the_remote_runs_the_nodes_planning_names(tmp_path, remote_host, sections):
    """The device is written in one section of the design only, the requested flow's or its
    producer's: the remote runs the nodes, with the identities, that the plan names here."""
    remote_host.load_test_flows()
    design = _declared_design(tmp_path / "design", sections)
    planned = DefaultRunner(tmp_path / "plan", display_results=False).resolve(
        TPlace, Design.load(design), {}
    )
    document = _succeeded(remote_request(tmp_path, design, TPlace.name))
    assert _identity(document) == planned.node(TPlace.name).flowrun_hash
    producer = json.loads(
        (remote_host.workspace() / "xeda_run" / "d" / "t_synth" / "settings.json").read_text()
    )
    assert producer["flowrun_hash"] == planned.node("t_synth").flowrun_hash


@pytest.mark.parametrize("source", [False, True])
def test_a_declared_graph_has_the_same_input_origin_on_the_remote(tmp_path, remote_host, source):
    remote_host.load_test_flows()
    root = tmp_path / "design"
    root.mkdir()
    entries = []
    if source:
        (root / "given.dat").write_text("given\n")
        entries = [{"file": "given.dat", "type": "Data"}]
    design = _declared_design(root, sources=entries)
    expected = DefaultRunner(tmp_path / "plan", display_results=False).resolve(
        TTaker, Design.load(design), {}
    )
    document = _succeeded(remote_request(tmp_path, design, TTaker.name))
    assert _results(document)["read"] == ("given\n" if source else "made\n")
    remote_path = remote_host.workspace() / "xeda_run" / "d" / TTaker.name
    trace = json.loads((remote_path / "trace.json").read_text())
    assert trace["declared_inputs"][0]["origin"] == expected.node(TTaker.name).inputs[0].origin
    assert _identity(document) == expected.node(TTaker.name).flowrun_hash


def test_declared_outputs_are_mirrored_and_delivered_without_artifact_labels(tmp_path, remote_host):
    from xeda.digest import content_digest

    remote_host.load_test_flows()
    design = _declared_design(tmp_path / "design")
    out = tmp_path / "out"
    document = _succeeded(remote_request(tmp_path, design, TMaker.name, outputs_to=out))
    recorded = _results(document)["outputs"]["made"]
    mirrored = Path(recorded["path"])
    assert mirrored.is_relative_to(_mirror(tmp_path))
    assert content_digest(mirrored) == recorded["sha"]
    assert mirrored.read_text() == (out / "made.txt").read_text() == "made\n"
    assert not _results(document).get("artifacts")
    # the record of the mirror names the mirrored output
    saved = json.loads((mirrored.parent / "results.json").read_text())
    assert saved["outputs"]["made"]["names"] == [mirrored.name]


def test_a_declared_output_collision_is_refused_before_connecting(tmp_path, remote_host):

    remote_host.load_test_flows()
    design = _declared_design(tmp_path / "design")
    first = _succeeded(remote_request(tmp_path, design, TMaker.name))
    assert _results(first)["outputs"] and not _results(first).get("artifacts")
    destination = tmp_path / "out"
    destination.mkdir()
    conflict = destination / "made.txt"
    conflict.write_text("user's file\n")
    calls = len([c for c in remote_host.remote.calls("ssh") if "-M" in c])
    outcome = remote_request(tmp_path, design, TMaker.name, outputs_to=destination)
    assert isinstance(outcome.error, SpecError) and "made.txt" in str(outcome.error)
    assert len([c for c in remote_host.remote.calls("ssh") if "-M" in c]) == calls
    assert conflict.read_text() == "user's file\n"


@pytest.mark.parametrize("stage", ["init", "producer"])
def test_declared_setup_failures_return_the_failure_document(tmp_path, remote_host, stage):
    """The flow of the remote fails in `init()` or in its producer: the document says so, with
    the same keys as any failed run, and nothing is delivered."""
    target = "TTaker.init" if stage == "init" else "TMaker.run"
    remote_host.load_test_flows(
        after=(
            "from xeda.flow import FlowFatalError\n"
            "import tests.io_flows as io\n"
            "def broken(self):\n"
            "    raise FlowFatalError('broken setup')\n"
            f"io.{target} = broken\n"
        )
    )
    design = _declared_design(tmp_path / "design")
    outcome = remote_request(tmp_path, design, TTaker.name, outputs_to=tmp_path / "out")
    assert not outcome.success
    document = outcome.document
    assert "broken setup" in document["error"]["message"]
    assert document["outcome"] == "failed"
    assert document["error"]["code"] == "reported_failure"
    assert document["spec"]["design"] == "d"
    assert Path(document["nodes"][0]["run_dir"]).is_relative_to(_mirror(tmp_path))
    assert not (tmp_path / "out").exists()


def test_a_failed_declared_run_hands_over_no_partial_output(tmp_path, remote_host):
    remote_host.load_test_flows()
    design = _declared_design(tmp_path / "design")
    outcome = remote_request(tmp_path, design, TPartialMaker.name, outputs_to=tmp_path / "out")
    assert not outcome.success
    assert outcome.document["outcome"] == "failed"
    assert outcome.document["error"]["code"] == "missing_output"
    assert outcome.document["nodes"][-1]["deliveries"] == []
    assert not (tmp_path / "out").exists()


def test_declared_shared_settings_must_agree_before_connecting(tmp_path, remote_host):

    design = _declared_design(
        tmp_path / "design",
        {
            "t_place": {"fpga": {"part": "LFE5U-25F-6BG256C"}},
            "t_synth": {"fpga": {"part": "LFE5U-85F-6BG381C"}},
        },
    )
    outcome = remote_request(tmp_path, design, TPlace.name)
    assert isinstance(outcome.error, SpecError) and "disagree" in str(outcome.error)
    assert not connected(remote_host)
    assert not (tmp_path / "local" / "xeda_run").exists()


# --- Explicit input bindings travel to the remote ------------------------------------------------


def _bound_design(root: Path) -> Path:
    """A design whose file binds `t_taker.made` to `t_input_maker`, not to the default producer."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "input.txt").write_text("from the bound producer\n")
    return _declared_design(
        root,
        {
            "t_taker": {"inputs": {"made": "t_input_maker.made"}},
            "t_input_maker": {"input_file": "input.txt"},
        },
    )


def test_a_design_file_binding_reaches_the_remote_plan(tmp_path, remote_host):
    remote_host.load_test_flows()
    design = _bound_design(tmp_path / "design")
    planned = DefaultRunner(tmp_path / "plan", display_results=False).resolve(
        TTaker, Design.load(design), {}
    )
    assert [node.name for node in planned.nodes] == ["t_input_maker", "t_taker"]
    document = _succeeded(remote_request(tmp_path, design, TTaker.name))
    assert _results(document)["read"] == "from the bound producer\n"
    assert _identity(document) == planned.node(TTaker.name).flowrun_hash
    assert [node["name"] for node in document["nodes"]] == ["t_input_maker", "t_taker"]


def test_a_command_line_binding_over_a_file_binding_reaches_the_remote_plan(tmp_path, remote_host):
    remote_host.load_test_flows()
    root = tmp_path / "design"
    root.mkdir()
    (root / "input.txt").write_text("from the bound producer\n")
    design = _declared_design(
        root,
        {
            "t_taker": {"inputs": {"made": "t_maker.made"}},
            "t_input_maker": {"input_file": "input.txt"},
        },
    )
    document = _succeeded(
        remote_request(
            tmp_path,
            design,
            TTaker.name,
            flow_settings=node_settings(
                TTaker.name, ["flows.t_taker.inputs.made=t_input_maker.made"]
            ),
        )
    )
    assert _results(document)["read"] == "from the bound producer\n"
    assert [node["name"] for node in document["nodes"]] == ["t_input_maker", "t_taker"]


def test_two_producers_of_a_many_input_run_remotely(tmp_path, remote_host):
    remote_host.load_test_flows()
    root = tmp_path / "design"
    root.mkdir()
    (root / "input.txt").write_text("second\n")
    design = _declared_design(
        root,
        {
            "t_join": {"inputs": {"more": ["t_maker.made", "t_input_maker.made"]}},
            "t_input_maker": {"input_file": "input.txt"},
        },
    )
    planned = DefaultRunner(tmp_path / "plan", display_results=False).resolve(
        TJoin, Design.load(design), {}
    )
    document = _succeeded(remote_request(tmp_path, design, TJoin.name))
    assert _results(document)["more"] == ["made\n", "second\n"]
    assert _identity(document) == planned.node(TJoin.name).flowrun_hash
    assert {node.name for node in planned.nodes} >= {"t_maker", "t_input_maker"}


def test_the_record_of_a_remote_many_input_has_one_row_for_each_producer(tmp_path, remote_host):
    """The record of a remote run whose `many` input two producers feed has one input row for
    each binding, in binding order, as a local record does. Each row names its producer, the run
    id of that producer's record and its run directory in the mirror, and the rows survive the
    round trip of the record."""
    remote_host.load_test_flows()
    root = tmp_path / "design"
    root.mkdir()
    (root / "input.txt").write_text("second\n")
    design = _declared_design(
        root,
        {
            "t_join": {"inputs": {"more": ["t_maker.made", "t_input_maker.made"]}},
            "t_input_maker": {"input_file": "input.txt"},
        },
    )
    document = _succeeded(remote_request(tmp_path, design, TJoin.name))
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere"
    run_dirs = {node["name"]: Path(node["run_dir"]) for node in document["nodes"]}
    record = read_record(run_dirs[TJoin.name], run_root=mirror)
    rows = [row for row in record.inputs if row.name == "more"]
    assert [(row.origin, row.producer, row.output) for row in rows] == [
        ("producer", "t_maker", "made"),
        ("producer", "t_input_maker", "made"),
    ]
    for row in rows:
        assert row.producer is not None and row.producer_run_dir == run_dirs[row.producer]
        assert row.producer_run_dir.is_relative_to(mirror)
        producer = read_record(run_dirs[row.producer], run_root=mirror)
        assert row.producer_run_id == producer.run_id
    again = decode_record(encode_record(record), run_dir=record.run_dir, run_root=mirror)
    assert again.inputs == record.inputs
