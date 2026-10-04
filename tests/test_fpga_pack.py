"""`fpga_pack`: a routed configuration in, one whole bitstream out -- or none.

Every launch here runs the process fakes of `tests/fake_tools` (`use_fake_fpga_tools`): fake
yosys, nextpnr and packers that read their inputs and record their calls. No programmer is
involved: packing never programs.
"""

import json
import shutil
from pathlib import Path, PurePath

import pytest

from xeda import Design
from xeda.flow import FlowFatalError, FlowSettingsError, FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows import FpgaPack, Nextpnr
from xeda.run_root import ensure_run_root
from xeda.utils import NonZeroExitCode

from . import tool_utils

ECP5 = "LFE5U-25F-6BG381C"
ICE40 = "iCE40HX1K-TQ144"
NEXUS = "LIFCL-40-9BG400C"
A100T = "xc7a100tcsg324-1"
BITSTREAM = b"\x00\xffXEDA bitstream\x00"
PARTIAL = b"\x00\xffpartial"
PINS = "set_property LOC E3 [get_ports clk]\n"

#: part, the packer it needs, its bitstream's conventional name, the configuration it packs
FAMILIES = [
    (ECP5, "ecppack", "outputs/top.bit", "config.txt"),
    (ICE40, "icepack", "outputs/top.bin", "config.asc"),
    (A100T, "fpga-as", "outputs/top.bit", "config.fasm"),
]
BY_FAMILY = pytest.mark.parametrize(
    "part,packer,name,config", FAMILIES, ids=["ecp5", "ice40", "xilinx"]
)


@pytest.fixture
def toolchain(tmp_path, monkeypatch):
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    return prefix


def _design(tmp_path: Path, part: str = ECP5, extra=(), flows=None) -> Design:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    sources: list = ["top.v"]
    if part == A100T:
        (root / "pins.xdc").write_text(PINS)
        sources.append("pins.xdc")
    return Design(
        name="top",
        design_root=root,
        rtl={"sources": [*sources, *extra], "top": "top"},
        flow=flows or {},
    )


def _runner(tmp_path: Path, **kwargs) -> DefaultRunner:
    return DefaultRunner(tmp_path / "run", display_results=False, **kwargs)


def _calls(run_path: Path) -> list[dict]:
    record = run_path / "fake_fpga.calls.jsonl"
    return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


def _tools(tmp_path: Path) -> list[str]:
    """Every fake tool started under the run root, by run directory."""
    return sorted(
        call["tool"]
        for record in (tmp_path / "run").glob("top/*/fake_fpga.calls.jsonl")
        for call in _calls(record.parent)
    )


# ------------------------------------------------------------------------------ the declaration


def test_fpga_pack_declares_one_configuration_in_and_one_bitstream_out():
    from xeda.flow.io import declared_inputs, declared_outputs

    (config,) = declared_inputs(FpgaPack).values()
    assert (config.name, config.producer, config.output) == ("config", "nextpnr", "config")
    assert config.cardinality == "one"
    (bitstream,) = declared_outputs(FpgaPack).values()
    assert bitstream.name == "bitstream" and bitstream.cardinality == "one"
    assert [t.name for t in bitstream.types] == ["Bitstream"]
    # its settings are its own section's: no producer's settings nested in them
    assert not FpgaPack.Settings.dependency_settings
    assert not {"nextpnr", "yosys"} & set(FpgaPack.Settings.model_fields)


@pytest.mark.parametrize(
    "part,kind", [(ECP5, "EcpConfig"), (ICE40, "IceAsc"), (A100T, "Fasm"), (NEXUS, "Fasm")]
)
def test_the_family_selects_the_configuration_it_packs(part, kind):
    settings = FpgaPack.Settings(fpga=part)
    assert [t.name for t in FpgaPack.input_types(settings, "config")] == [kind]


@pytest.mark.parametrize(
    "part,name", [(ECP5, "outputs/d.bit"), (ICE40, "outputs/d.bin"), (A100T, "outputs/d.bit")]
)
def test_the_bitstream_is_named_for_its_family_s_format(part, name):
    settings = FpgaPack.Settings.from_input({"fpga": part})
    assert settings.conventional_output("bitstream", "d") == PurePath(name)


@pytest.mark.parametrize(
    "settings,message",
    [
        ({"fpga": NEXUS}, "no bitstream packer"),
        ({"fpga": {"vendor": "gowin", "family": "gowin", "device": "GW1N-9"}}, "no bitstream"),
        ({"fpga": "xc7a35tcsg324"}, "full part"),
        ({"fpga": ECP5, "prjxray_db": "db"}, "prjxray_db"),
    ],
)
def test_a_target_it_cannot_pack_is_rejected_by_its_settings_alone(settings, message):
    with pytest.raises(FlowSettingsException, match=message):
        FpgaPack.check_settings_supported(FpgaPack.Settings(**settings))


# --------------------------------------------------------------------------- the default graph


@BY_FAMILY
def test_the_default_graph_synthesizes_places_and_packs(
    tmp_path, toolchain, part, packer, name, config
):
    design = _design(tmp_path, part)
    runner = _runner(tmp_path)
    plan = runner.plan(FpgaPack, design, flow_settings={"fpga": part})
    assert [node.name for node in plan.nodes] == ["yosys_fpga", "nextpnr", "fpga_pack"]
    flow = runner.run("fpga_pack", design, flow_settings={"fpga": part})
    assert flow is not None and flow.succeeded
    placed = tmp_path / "run/top/nextpnr"
    assert flow.inputs.config == placed / config
    (call,) = _calls(flow.run_path)
    assert call["tool"] == packer and call["inputs"] == [str(placed / config)]
    assert flow.outputs.bitstream == flow.run_path / name
    assert flow.outputs.bitstream.read_bytes() == BITSTREAM
    assert flow.results["outputs"]["bitstream"]["path"] == str(flow.run_path / name)
    assert flow.artifacts["bitstream"] == flow.run_path / name
    assert not list(flow.run_path.glob(".xeda-pack-*"))
    assert len(_tools(tmp_path)) == 3
    again = runner.run("fpga_pack", design, flow_settings={"fpga": part})
    assert again is not None and again.reused
    assert len(_tools(tmp_path)) == 3


def test_scratch_a_killed_run_left_is_removed_and_never_recorded(tmp_path, toolchain):
    """A run killed while packing leaves its `.xeda-pack-*` directory: nothing else removes it,
    and what is in a run directory after a run is recorded as that run's output."""
    design = _design(tmp_path)
    runner = _runner(tmp_path)
    flow = runner.run("fpga_pack", design, flow_settings={"fpga": ECP5})
    left = flow.run_path / ".xeda-pack-killed0"
    left.mkdir()
    (left / "top.bit").write_bytes(PARTIAL)
    again = runner.run("fpga_pack", design, flow_settings={"fpga": ECP5})
    assert again is not None and again.succeeded and not again.reused
    assert not list(again.run_path.glob(".xeda-pack-*"))
    assert ".xeda-pack" not in (again.run_path / "trace.json").read_text()
    assert again.outputs.bitstream.read_bytes() == BITSTREAM


def test_ecppack_and_icepack_are_given_their_options_then_the_configuration_and_a_scratch_output(
    tmp_path, toolchain
):
    design = _design(tmp_path)
    settings = {"fpga": ECP5, "packer_args": ["--compress", "--freq", "38.8"]}
    flow = _runner(tmp_path).run("fpga_pack", design, flow_settings=settings)
    argv = _calls(flow.run_path)[0]["argv"]
    assert argv[:3] == ["--compress", "--freq", "38.8"]
    assert argv[3] == str(flow.inputs.config)
    scratch = Path(argv[4])
    assert scratch.name == "top.bit" and scratch.parent.name.startswith(".xeda-pack-")
    assert scratch.parent.parent == flow.run_path and not scratch.parent.exists()
    assert len(argv) == 5


@pytest.mark.parametrize(
    "part,packer,args,operands",
    [
        (ECP5, "ecppack", ["--compress", "--freq", "38.8"], 2),
        (ICE40, "icepack", ["-s", "-v"], 2),
        (A100T, "fpga-as", ["--dump_frames_file=frames"], 1),
    ],
    ids=["ecp5", "ice40", "xilinx"],
)
def test_every_packer_is_given_all_its_options_before_its_operands(
    tmp_path, toolchain, part, packer, args, operands
):
    """`icepack`'s usage is `[options] [input-file [output-file]]`, and a strict POSIX `getopt`
    (BSD, musl) stops at the first operand: `icepack config.asc out.bin -s` would read `-s` as
    a third operand and ignore it. One order for every packer: options, then operands."""
    design = _design(tmp_path, part)
    flow = _runner(tmp_path).run(
        "fpga_pack", design, flow_settings={"fpga": part, "packer_args": args}
    )
    (call,) = _calls(flow.run_path)
    assert call["tool"] == packer
    argv = call["argv"]
    operand_start = len(argv) - operands
    assert not any(arg.startswith("-") for arg in argv[operand_start:])
    assert all(arg in argv[:operand_start] for arg in args)
    assert argv[operand_start] == str(flow.inputs.config)


def test_a_device_given_only_for_nextpnr_is_the_packer_s_too(tmp_path, toolchain):
    design = _design(tmp_path, flows={"nextpnr": {"fpga": ICE40}})
    runner = _runner(tmp_path)
    plan = runner.plan(FpgaPack, design)
    assert plan.node("fpga_pack").settings.fpga.part == ICE40
    assert plan.node("yosys_fpga").settings.fpga.part == ICE40
    assert plan.node("nextpnr").settings.asc == Path("config.asc")
    flow = runner.run("fpga_pack", design)
    assert flow.succeeded and _tools(tmp_path) == ["icepack", "nextpnr-ice40", "yosys"]


def test_nextpnr_always_writes_its_configuration_so_requests_do_not_ping_pong(tmp_path, toolchain):
    """`nextpnr` has no switch for its configuration: requested alone or as the packer's
    producer it is one configuration and one identity, so `run nextpnr`, `run fpga_pack`,
    `run nextpnr` runs nextpnr once."""
    design = _design(tmp_path, flows={"nextpnr": {"fpga": ECP5}})
    runner = _runner(tmp_path)
    alone = runner.plan(Nextpnr, design).node("nextpnr")
    packed = runner.plan(FpgaPack, design).node("nextpnr")
    assert alone.settings.textcfg == Path("config.txt") and packed.switched_on == ()
    assert (alone.flowrun_hash, alone.run_path) == (packed.flowrun_hash, packed.run_path)
    first = runner.run("nextpnr", design)
    assert first.succeeded and not first.reused
    assert runner.run("fpga_pack", design).succeeded
    assert [f.reused for f in runner.launched if f.name == "nextpnr"] == [False, True]
    assert runner.run("nextpnr", design).reused


@pytest.mark.parametrize("setting", ["textcfg", "asc", "fasm"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_the_configuration_file_cannot_be_switched_off(setting, value):
    with pytest.raises(ValueError, match="always writes its configuration"):
        Nextpnr.Settings(fpga=ECP5, **{setting: value})


def test_devices_that_differ_between_the_packer_and_nextpnr_are_an_error(tmp_path, toolchain):
    design = _design(tmp_path, flows={"nextpnr": {"fpga": ICE40}, "fpga_pack": {"fpga": ECP5}})
    with pytest.raises(FlowSettingsError, match="disagrees"):
        _runner(tmp_path).plan(FpgaPack, design)


def test_nexus_is_placed_on_its_own_but_refused_for_packing_before_synthesis(tmp_path, toolchain):
    design = _design(tmp_path)
    runner = _runner(tmp_path)
    with pytest.raises(FlowSettingsException, match="no bitstream packer"):
        runner.run("fpga_pack", design, flow_settings={"fpga": NEXUS})
    assert not _tools(tmp_path)
    placed = runner.run("nextpnr", design, flow_settings={"fpga": NEXUS})
    assert placed is not None and placed.succeeded


# ------------------------------------------------------------------- a configuration as a source


@pytest.mark.parametrize(
    "part,packer,kind",
    [(ECP5, "ecppack", "EcpConfig"), (ICE40, "icepack", "IceAsc"), (A100T, "fpga-as", "Fasm")],
)
def test_a_typed_configuration_source_is_packed_without_building(
    tmp_path, toolchain, part, packer, kind
):
    design = _design(tmp_path)
    routed = design.root_path / "routed.cfg"
    routed.write_text("a routed design\n")
    # a producer's own settings that no longer apply: it does not run, so they do not conflict
    design = _design(
        tmp_path, extra=[{"file": "routed.cfg", "type": kind}], flows={"nextpnr": {"fpga": NEXUS}}
    )
    runner = _runner(tmp_path)
    plan = runner.plan(FpgaPack, design, flow_settings={"fpga": part})
    assert [node.name for node in plan.nodes] == ["fpga_pack"]
    flow = runner.run("fpga_pack", design, flow_settings={"fpga": part})
    assert flow.succeeded and flow.inputs.config == routed
    assert _tools(tmp_path) == [packer]
    assert flow.outputs.bitstream.read_bytes() == BITSTREAM
    assert routed.read_text() == "a routed design\n"


def test_a_configuration_of_another_family_does_not_stand_in(tmp_path, toolchain):
    design = _design(tmp_path)
    (design.root_path / "other.fasm").write_text("TILE.FEATURE\n")
    design = _design(tmp_path, extra=["other.fasm"])
    plan = _runner(tmp_path).plan(FpgaPack, design, flow_settings={"fpga": ECP5})
    assert [node.name for node in plan.nodes] == ["yosys_fpga", "nextpnr", "fpga_pack"]


def test_two_configurations_for_one_bitstream_are_an_error(tmp_path, toolchain):
    design = _design(tmp_path)
    for name in ("a.cfg", "b.cfg"):
        (design.root_path / name).write_text("routed\n")
    design = _design(tmp_path, extra=[{"file": n, "type": "EcpConfig"} for n in ("a.cfg", "b.cfg")])
    with pytest.raises(FlowSettingsException, match="one EcpConfig file.*a.cfg.*b.cfg"):
        _runner(tmp_path).plan(FpgaPack, design, flow_settings={"fpga": ECP5})


# ----------------------------------------------------------------- the Project X-Ray database


def _other_database(tmp_path: Path, toolchain: Path) -> Path:
    other = tmp_path / "design" / "db"
    shutil.copytree(toolchain / "share/nextpnr/prjxray-db", other)
    return other


@pytest.mark.parametrize("owner", ["fpga_pack", "nextpnr"])
def test_one_database_override_is_the_whole_graph_s(tmp_path, toolchain, owner):
    design = _design(tmp_path, A100T, flows={owner: {"prjxray_db": "db"}})
    other = _other_database(tmp_path, toolchain)
    runner = _runner(tmp_path)
    plan = runner.plan(FpgaPack, design, flow_settings={"fpga": A100T})
    assert plan.node("fpga_pack").settings.prjxray_db == other
    assert plan.node("nextpnr").settings.prjxray_db == other
    flow = runner.run("fpga_pack", design, flow_settings={"fpga": A100T})
    assert flow.succeeded
    assert _calls(flow.run_path)[0]["argv"][0] == f"--prjxray_db_path={other / 'artix7'}"


def test_two_database_overrides_that_differ_are_an_error(tmp_path, toolchain):
    flows = {"fpga_pack": {"prjxray_db": "db"}, "nextpnr": {"prjxray_db": "elsewhere"}}
    design = _design(tmp_path, A100T, flows=flows)
    with pytest.raises(FlowSettingsError, match="prjxray_db.*disagrees"):
        _runner(tmp_path).plan(FpgaPack, design, flow_settings={"fpga": A100T})


# -------------------------------------------------------------------------------- all or nothing


def _built(tmp_path, part):
    design = _design(tmp_path, part)
    first = _runner(tmp_path).run("fpga_pack", design, flow_settings={"fpga": part})
    assert first is not None and first.succeeded
    return design, first


def _results(flow) -> dict:
    return json.loads((flow.run_path / "results.json").read_text())


def _fails(runner: DefaultRunner, design: Design, settings: dict) -> None:
    """A launch that ends in failure, reported or raised."""
    try:
        flow = runner.run("fpga_pack", design, flow_settings=settings)
    except (NonZeroExitCode, FlowFatalError):
        return
    assert flow is None or not flow.succeeded


@BY_FAMILY
@pytest.mark.parametrize("mode", ["partial", "no-output", "fail"])
def test_a_packer_that_fails_or_writes_nothing_leaves_the_earlier_bitstream(
    tmp_path, toolchain, monkeypatch, part, packer, name, config, mode
):
    """A nonzero exit after part of the output (fpga-as writes its bitstream to stdout), an exit
    of zero with no output at all, a plain failure: none publishes or records a bitstream."""
    design, first = _built(tmp_path, part)
    placed = first.inputs.config.read_bytes()
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", packer)
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", mode)
    _fails(_runner(tmp_path, rebuild_all=True), design, {"fpga": part})
    final = first.run_path / name
    assert final.read_bytes() == BITSTREAM  # the earlier run's, whole
    assert not [p for p in first.run_path.rglob("*") if p.is_file() and p.read_bytes() == PARTIAL]
    assert not list(first.run_path.glob(".xeda-pack-*"))
    results = _results(first)
    assert results["success"] is False and "bitstream" not in results.get("outputs", {})
    if mode == "no-output":
        assert "wrote no bitstream" in results["error"]["message"]
    assert "bitstream" not in results.get("artifacts", {})
    assert not (first.run_path / "trace.json").exists()
    assert first.inputs.config.read_bytes() == placed


def test_a_configuration_nextpnr_did_not_write_again_is_never_packed(
    tmp_path, toolchain, monkeypatch
):
    from xeda.flow import FlowDependencyFailure

    design, first = _built(tmp_path, ECP5)
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "nextpnr-ecp5")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", "no-output")
    with pytest.raises(FlowDependencyFailure):
        _runner(tmp_path, rebuild_all=True).run("fpga_pack", design, flow_settings={"fpga": ECP5})
    assert first.inputs.config.is_file()  # the earlier run's configuration is still there
    assert len(_calls(first.run_path)) == 1  # and was not packed a second time
    assert first.outputs.bitstream.read_bytes() == BITSTREAM


def test_a_link_at_the_bitstream_s_name_is_replaced_never_written_through(tmp_path, toolchain):
    design = _design(tmp_path)
    outside = tmp_path / "outside.bit"
    outside.write_bytes(b"the user's own file")
    final = ensure_run_root(tmp_path / "run") / "top/fpga_pack/outputs/top.bit"
    final.parent.mkdir(parents=True)
    final.symlink_to(outside)
    flow = _runner(tmp_path).run("fpga_pack", design, flow_settings={"fpga": ECP5})
    assert flow.succeeded
    assert not final.is_symlink() and final.read_bytes() == BITSTREAM
    assert outside.read_bytes() == b"the user's own file"


def test_a_bitstream_given_a_location_is_delivered_only_after_success(
    tmp_path, toolchain, monkeypatch
):
    design = _design(tmp_path)
    location = tmp_path / "delivered" / "board.bit"
    location.parent.mkdir()
    settings = {"fpga": ECP5, "bitstream": str(location)}
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "ecppack")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", "partial")
    _fails(_runner(tmp_path), design, settings)
    assert not location.exists()
    monkeypatch.delenv("XEDA_FAKE_FPGA_MODE")
    flow = _runner(tmp_path).run("fpga_pack", design, flow_settings=settings)
    assert flow.succeeded
    assert flow.outputs.bitstream == flow.run_path / "outputs/top.bit"
    assert location.read_bytes() == BITSTREAM


def test_a_name_for_the_bitstream_stays_in_the_run_directory(tmp_path, toolchain):
    design = _design(tmp_path)
    flow = _runner(tmp_path).run(
        "fpga_pack", design, flow_settings={"fpga": ECP5, "bitstream": "board.bit"}
    )
    assert flow.outputs.bitstream == flow.run_path / "board.bit"
    assert flow.outputs.bitstream.read_bytes() == BITSTREAM


def test_a_changed_configuration_source_is_packed_again(tmp_path, toolchain):
    design = _design(tmp_path)
    routed = design.root_path / "routed.cfg"
    routed.write_text("a routed design\n")

    def launch():
        design = _design(tmp_path, extra=[{"file": "routed.cfg", "type": "EcpConfig"}])
        return _runner(tmp_path).run("fpga_pack", design, flow_settings={"fpga": ECP5})

    first = launch()
    assert launch().reused
    routed.write_text("another routed design\n")
    assert not launch().reused
    assert [call["input_bytes"] for call in _calls(first.run_path)] == [[16], [22]]


def test_nextpnr_is_registered_and_listed():
    from xeda.flow_runner import get_flow_class
    from xeda.flows import __all__ as exported

    assert get_flow_class("fpga_pack") is get_flow_class("fpga-pack") is FpgaPack
    assert "FpgaPack" in exported and Nextpnr.name == "nextpnr"


# ------------------------------------------------------------------------------- the real tools


def test_fpga_pack_ecp5_end_to_end(tmp_path):
    """yosys, nextpnr-ecp5 and ecppack for real, on the ULX3S blinky: a bitstream, not loaded."""
    tool_utils.require_nextpnr_ecp5()
    tool_utils._require_command("ecppack", ["ecppack", "--help"])
    design = Path(__file__).parent.parent / "examples/boards/ulx3s/blinky/blinky.xeda.yaml"
    flow = DefaultRunner(tmp_path / "run", display_results=False).run(FpgaPack, design)
    assert flow is not None and flow.succeeded
    assert flow.outputs.bitstream == flow.run_path / "outputs/blinky.bit"
    assert flow.outputs.bitstream.stat().st_size > 1000
    assert flow.inputs.config == tmp_path / "run/blinky/nextpnr/config.txt"


def test_fpga_pack_ice40_end_to_end(tmp_path, monkeypatch):
    """yosys, nextpnr-ice40 and icepack for real."""
    tool_utils.require_nextpnr_ice40()
    # icepack has no option that exits with zero status without packing
    tool_utils._require("icepack", shutil.which("icepack") is not None, "finding it on PATH")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "blink.v").write_text(
        "module blink(input clk, output reg q); always @(posedge clk) q <= ~q; endmodule\n"
    )
    design = Design(
        name="blink",
        design_root=tmp_path,
        rtl={"sources": ["blink.v"], "top": "blink", "clock": {"port": "clk"}},
        flow={"nextpnr": {"pcf_allow_unconstrained": True, "clock": {"period": 20.0}}},
    )
    flow = DefaultRunner(tmp_path / "run", display_results=False).run(
        FpgaPack, design, flow_settings={"fpga": "iCE40HX1K-TQ144"}
    )
    assert flow is not None and flow.succeeded
    assert flow.outputs.bitstream == flow.run_path / "outputs/blink.bin"
    assert flow.outputs.bitstream.stat().st_size > 1000
