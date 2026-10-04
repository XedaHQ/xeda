"""`openfpgaloader` programs one bitstream and builds nothing itself.

NOTHING HERE MAY REACH A REAL PROGRAMMER. Every test that runs the flow goes through the
`fake_loader` fixture, which puts the process fakes of `tests/fake_tools` first on PATH and
checks that `openFPGALoader` resolves to the fake dispatcher, byte for byte, before the test
body starts; the fake reads the file it is given, records its arguments, and touches no device.
"""

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from xeda import Design
from xeda.flow import FlowSettingsError, FlowSettingsException
from xeda.flow.io import declared_inputs, declared_outputs
from xeda.flow_runner import DefaultRunner
from xeda.flows import Openfpgaloader

from . import tool_utils

ECP5 = "LFE5U-25F-6BG381C"
BITSTREAM = b"\x00\xffXEDA bitstream\x00"
DISPATCHER = tool_utils.FAKE_TOOLS_DIR / "fake_fpga_tool.py"


def assert_fake_loader() -> Path:
    """The `openFPGALoader` a launch would start is the fake, or the test stops here."""
    resolved = shutil.which("openFPGALoader")
    assert resolved is not None, "no openFPGALoader on PATH: the fake is missing"
    assert Path(resolved).read_bytes() == DISPATCHER.read_bytes(), (
        f"openFPGALoader resolves to {resolved}, which is not the fake: refusing to run a "
        "programmer"
    )
    return Path(resolved)


@pytest.fixture
def fake_loader(tmp_path, monkeypatch):
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    assert assert_fake_loader() == prefix / "bin/openFPGALoader"
    return prefix


def _design(tmp_path: Path, extra=(), flows=None) -> Design:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    return Design(
        name="top",
        design_root=root,
        rtl={"sources": ["top.v", *extra], "top": "top"},
        flow=flows or {},
    )


def _prebuilt(tmp_path: Path, name="given.bit", flows=None) -> Design:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / name).write_bytes(b"a bitstream built elsewhere")
    return _design(tmp_path, extra=[{"file": name, "type": "Bitstream"}], flows=flows)


def _runner(tmp_path: Path, **kwargs) -> DefaultRunner:
    return DefaultRunner(tmp_path / "run", display_results=False, **kwargs)


def _calls(tmp_path: Path, flow: str) -> list[dict]:
    record = tmp_path / "run/top" / flow / "fake_fpga.calls.jsonl"
    return [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []


def _program(tmp_path, design, settings=None, **launcher):
    assert_fake_loader()
    flow = _runner(tmp_path, **launcher).run("openfpgaloader", design, flow_settings=settings or {})
    assert flow is not None
    return flow


# ------------------------------------------------------------------------------ the declaration


def test_the_loader_takes_one_bitstream_and_declares_no_output():
    (bitstream,) = declared_inputs(Openfpgaloader).values()
    assert (bitstream.name, bitstream.cardinality) == ("bitstream", "one")
    assert (bitstream.producer, bitstream.output) == ("fpga_pack", "bitstream")
    assert [t.name for t in bitstream.types] == ["Bitstream"]
    assert not declared_outputs(Openfpgaloader)
    fields = set(Openfpgaloader.Settings.model_fields)
    assert not {"nextpnr", "packer_args", "bitstream_file", "bitstream"} & fields
    assert not Openfpgaloader.Settings.dependency_settings


def test_the_loader_always_runs_because_it_programs(tmp_path):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    flow = Openfpgaloader(Openfpgaloader.Settings(fpga=ECP5), design, tmp_path / "loader")
    assert flow.always_runs() == "it programs a device"
    flow.init()
    assert not flow.dependencies and not hasattr(flow, "packer")


@pytest.mark.parametrize(
    "removed,replacement",
    [
        ({"nextpnr": {"seed": 3}}, "flows.nextpnr"),
        ({"nextpnr": {"no_such_setting": 1}}, "flows.nextpnr"),
        ({"packer_args": ["--compress"]}, "flows.fpga_pack.packer_args"),
        ({"bitstream_file": "top.bit"}, "flows.fpga_pack.bitstream"),
    ],
)
def test_the_loader_s_build_settings_were_removed_and_name_their_replacement(
    tmp_path, removed, replacement
):
    (name,) = removed
    with pytest.raises(FlowSettingsError) as error:
        Openfpgaloader.Settings.from_input({"fpga": ECP5, **removed}, design_root=tmp_path)
    assert f"`{name}` was removed: use" in str(error.value) and replacement in str(error.value)
    if name == "bitstream_file":
        assert "Bitstream" in str(error.value)  # or a typed source, to program an existing file


def test_a_removed_nested_setting_in_a_design_s_section_is_reported(tmp_path, fake_loader):
    flows = {"openfpgaloader": {"board": "ULX3S_85F", "nextpnr": {"timing_allow_fail": True}}}
    with pytest.raises(FlowSettingsError, match="`nextpnr` was removed: use .*flows.nextpnr"):
        _runner(tmp_path).run("openfpgaloader", _design(tmp_path, flows=flows))
    assert not (tmp_path / "run").exists() or not list((tmp_path / "run").rglob("*.jsonl"))


# -------------------------------------------------------------------------- verify needs flash


def test_verify_without_write_flash_is_rejected_wherever_it_is_given(tmp_path):
    message = "verify.*write_flash"
    with pytest.raises(ValidationError, match=message):
        Openfpgaloader.Settings(fpga=ECP5, verify=True)
    with pytest.raises(FlowSettingsError, match=message):
        Openfpgaloader.Settings.from_input({"fpga": ECP5, "verify": True}, design_root=tmp_path)
    settings = Openfpgaloader.Settings(fpga=ECP5)
    with pytest.raises(ValidationError, match=message):
        settings.verify = True
    assert settings.verify is False
    both = Openfpgaloader.Settings(fpga=ECP5, verify=True, write_flash=True)
    with pytest.raises(ValidationError, match=message):
        both.write_flash = False
    # the order they are given in does not matter
    assert Openfpgaloader.Settings(fpga=ECP5, write_flash=True, verify=True).verify is True


def test_verify_without_write_flash_fails_before_anything_is_built(tmp_path, fake_loader):
    with pytest.raises(FlowSettingsError, match="verify.*write_flash"):
        _runner(tmp_path).run(
            "openfpgaloader", _design(tmp_path), flow_settings={"fpga": ECP5, "verify": True}
        )
    assert not list((tmp_path / "run").rglob("fake_fpga.calls.jsonl"))


# ------------------------------------------------------------------------------ the command line


def test_flash_and_verify_are_openfpgaloader_s_own_flags(tmp_path, fake_loader):
    settings = {
        "fpga": ECP5,
        "cable": "ft2232",
        "write_flash": True,
        "verify": True,
        "freq": 6000000,
        "extra_args": ["--scan-usb"],
    }
    flow = _program(tmp_path, _prebuilt(tmp_path), settings)
    assert flow.succeeded
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["tool"] == "openFPGALoader"
    assert call["argv"] == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--cable",
        "ft2232",
        "--fpga-part",
        ECP5,
        "--write-flash",
        "--verify",
        "--freq",
        "6000000",
        "--scan-usb",
    ]
    assert "--flash" not in call["argv"]


def test_a_ulx3s_is_programmed_by_its_board_name_and_part(tmp_path, fake_loader):
    flow = _program(tmp_path, _prebuilt(tmp_path), {"board": "ULX3S_85F"})
    assert flow.succeeded
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--board",
        "ulx3s",
        "--fpga-part",
        "LFE5U-85F-6BG381C",
    ]


def test_a_cable_takes_precedence_over_the_board_s(tmp_path, fake_loader):
    _program(tmp_path, _prebuilt(tmp_path), {"board": "ULX3S_85F", "cable": "ft231X"})
    argv = _calls(tmp_path, "openfpgaloader")[0]["argv"]
    assert argv[2:4] == ["--cable", "ft231X"] and "--board" not in argv


def test_a_custom_board_is_programmed_by_its_programmer_name(tmp_path, fake_loader):
    boards = tmp_path / "design" / "boards.toml"
    boards.parent.mkdir()
    boards.write_text(f'[MY_BOARD]\nname = "programmer_board"\nfpga.part = "{ECP5}"\n')
    settings = {"board": "MY_BOARD", "custom_boards_file": "boards.toml"}
    _program(tmp_path, _prebuilt(tmp_path), settings)
    argv = _calls(tmp_path, "openfpgaloader")[0]["argv"]
    assert argv[2:6] == ["--board", "programmer_board", "--fpga-part", ECP5]


# ------------------------------------------------------------------------- a prebuilt bitstream


@pytest.mark.parametrize(
    "name,part",
    [
        ("given.bit", ECP5),
        ("image.bin", "iCE40HX1K-TQ144"),  # `.bin` is ambiguous: its type is given
        ("top_vivado.bit", "xc7a100tcsg324-1"),  # one Vivado wrote: no openXC7 build involved
        ("nexus.bit", "LIFCL-40-9BG400C"),  # a family xeda cannot pack is still programmed
    ],
)
def test_a_typed_bitstream_source_launches_the_loader_alone(tmp_path, fake_loader, name, part):
    # a build flow's own section that no longer applies: it does not run, so it cannot conflict
    design = _prebuilt(tmp_path, name, flows={"nextpnr": {"fpga": ECP5, "seed": 7}})
    runner = _runner(tmp_path)
    plan = runner.plan(Openfpgaloader, design, flow_settings={"fpga": part})
    assert [node.name for node in plan.nodes] == ["openfpgaloader"]
    assert_fake_loader()
    flow = runner.run("openfpgaloader", design, flow_settings={"fpga": part})
    assert flow.succeeded and flow.inputs.bitstream == tmp_path / "design" / name
    assert [p.name for p in (tmp_path / "run/top").iterdir() if p.is_dir()] == ["openfpgaloader"]
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["inputs"] == [str(tmp_path / "design" / name)]
    assert call["input_bytes"] == [len(b"a bitstream built elsewhere")]
    assert "outputs" not in flow.results or not flow.results["outputs"]
    assert "bitstream" not in flow.artifacts


def test_two_bitstreams_to_program_are_an_error(tmp_path, fake_loader):
    root = tmp_path / "design"
    root.mkdir()
    for name in ("a.bit", "b.bit"):
        (root / name).write_bytes(b"bits")
    design = _design(tmp_path, extra=["a.bit", "b.bit"])
    with pytest.raises(FlowSettingsException, match="one Bitstream file.*a.bit.*b.bit"):
        _runner(tmp_path).plan(Openfpgaloader, design, flow_settings={"fpga": ECP5})


# ---------------------------------------------------------------------------- the default graph


def test_the_default_graph_builds_then_programs_and_a_relaunch_only_programs(tmp_path, fake_loader):
    design = _design(tmp_path)
    runner = _runner(tmp_path)
    plan = runner.plan(Openfpgaloader, design, flow_settings={"fpga": ECP5})
    assert [n.name for n in plan.nodes] == ["yosys_fpga", "nextpnr", "fpga_pack", "openfpgaloader"]
    first = _program(tmp_path, design, {"fpga": ECP5})
    assert first.succeeded
    packed = tmp_path / "run/top/fpga_pack/outputs/top.bit"
    assert first.inputs.bitstream == packed and packed.read_bytes() == BITSTREAM
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"][:2] == ["--bitstream", str(packed)]
    assert call["input_bytes"] == [len(BITSTREAM)]
    assert not (first.run_path / "trace.json").exists()  # an always-running flow keeps none
    assert [p.name for p in first.run_path.rglob("*") if p.suffix in (".bit", ".bin")] == []
    again = _program(tmp_path, design, {"fpga": ECP5})
    assert again.succeeded and not again.reused
    assert again.stale_reason == "it programs a device"
    assert len(_calls(tmp_path, "openfpgaloader")) == 2
    for built in ("yosys_fpga", "nextpnr", "fpga_pack"):
        assert len(_calls(tmp_path, built)) == 1, f"{built} ran again"


@pytest.mark.parametrize("where", ["openfpgaloader", "fpga_pack", "nextpnr", "yosys_fpga"])
def test_a_board_given_for_any_stage_is_the_whole_graph_s(tmp_path, fake_loader, where):
    boards = tmp_path / "design" / "boards.toml"
    boards.parent.mkdir()
    boards.write_text(f'[MY_BOARD]\nname = "programmer_board"\nfpga.part = "{ECP5}"\n')
    section = {"board": "MY_BOARD", "custom_boards_file": "boards.toml"}
    if where == "yosys_fpga":  # it has no board setting: its device is the leaf it shares
        section = {"fpga": ECP5}
    design = _design(tmp_path, flows={where: section})
    plan = _runner(tmp_path).plan(Openfpgaloader, design)
    for node in plan.nodes:
        assert node.settings.fpga.part == ECP5, node.name
        if where != "yosys_fpga" and node.name != "yosys_fpga":
            assert node.settings.board == "MY_BOARD", node.name
            assert node.settings.custom_boards_file == boards, node.name
    flow = _program(tmp_path, design)
    assert flow.succeeded
    argv = _calls(tmp_path, "openfpgaloader")[0]["argv"]
    assert argv[-2:] == ["--fpga-part", ECP5]
    assert ("--board" in argv) == (where != "yosys_fpga")


def test_devices_that_differ_between_the_loader_and_a_build_stage_are_an_error(
    tmp_path, fake_loader
):
    flows = {"openfpgaloader": {"fpga": ECP5}, "nextpnr": {"fpga": "iCE40HX1K-TQ144"}}
    with pytest.raises(FlowSettingsError, match="disagrees"):
        _runner(tmp_path).plan(Openfpgaloader, _design(tmp_path, flows=flows))


def test_a_family_that_cannot_be_packed_is_refused_before_anything_runs(tmp_path, fake_loader):
    with pytest.raises(FlowSettingsException, match="no bitstream packer"):
        _runner(tmp_path).run(
            "openfpgaloader", _design(tmp_path), flow_settings={"fpga": "LIFCL-40-9BG400C"}
        )
    assert not list((tmp_path / "run").rglob("fake_fpga.calls.jsonl"))


def test_the_placing_and_packing_settings_live_in_their_own_sections(tmp_path, fake_loader):
    flows = {
        "nextpnr": {"seed": 7, "timing_allow_fail": True},
        "fpga_pack": {"packer_args": ["--compress"], "bitstream": "board.bit"},
    }
    flow = _program(tmp_path, _design(tmp_path, flows=flows), {"fpga": ECP5})
    assert flow.succeeded
    assert "--seed=7" in _calls(tmp_path, "nextpnr")[0]["argv"]
    assert _calls(tmp_path, "fpga_pack")[0]["argv"][2:] == ["--compress"]
    assert flow.inputs.bitstream == tmp_path / "run/top/fpga_pack/board.bit"


# ---------------------------------------------------------------- nothing unbuilt is programmed


@pytest.mark.parametrize("mode", ["partial", "no-output", "fail"])
def test_a_bitstream_the_packer_did_not_finish_never_reaches_the_programmer(
    tmp_path, fake_loader, monkeypatch, mode
):
    """Even with an earlier, whole bitstream still in the packer's run directory."""
    design = _design(tmp_path)
    assert _program(tmp_path, design, {"fpga": ECP5}).succeeded
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "ecppack")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", mode)
    assert_fake_loader()
    with pytest.raises(Exception) as error:  # noqa: PT011 - the dependency's failure
        _runner(tmp_path, rebuild_all=True).run(
            "openfpgaloader", design, flow_settings={"fpga": ECP5}
        )
    assert type(error.value).__name__ == "FlowDependencyFailure"
    assert len(_calls(tmp_path, "openfpgaloader")) == 1  # the first launch's only
    assert (tmp_path / "run/top/fpga_pack/outputs/top.bit").read_bytes() == BITSTREAM
    results = json.loads((tmp_path / "run/top/openfpgaloader/results.json").read_text())
    assert results["success"] is False and results["error"]["type"] == "FlowDependencyFailure"


def test_a_bitstream_edited_after_packing_is_packed_again_not_programmed(tmp_path, fake_loader):
    design = _design(tmp_path)
    assert _program(tmp_path, design, {"fpga": ECP5}).succeeded
    packed = tmp_path / "run/top/fpga_pack/outputs/top.bit"
    packed.write_bytes(b"tampered")
    assert _program(tmp_path, design, {"fpga": ECP5}).succeeded
    assert packed.read_bytes() == BITSTREAM
    assert len(_calls(tmp_path, "fpga_pack")) == 2
    assert [call["input_bytes"] for call in _calls(tmp_path, "openfpgaloader")] == [
        [len(BITSTREAM)],
        [len(BITSTREAM)],
    ]


def test_a_bitstream_removed_after_packing_is_packed_again(tmp_path, fake_loader):
    design = _design(tmp_path)
    assert _program(tmp_path, design, {"fpga": ECP5}).succeeded
    (tmp_path / "run/top/fpga_pack/outputs/top.bit").unlink()
    assert _program(tmp_path, design, {"fpga": ECP5}).succeeded
    assert len(_calls(tmp_path, "fpga_pack")) == 2 and len(_calls(tmp_path, "openfpgaloader")) == 2


def test_a_failing_programmer_fails_the_flow(tmp_path, fake_loader, monkeypatch):
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "openFPGALoader")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", "fail")
    assert_fake_loader()
    try:
        flow = _runner(tmp_path).run(
            "openfpgaloader", _prebuilt(tmp_path), flow_settings={"fpga": ECP5}
        )
    except Exception as error:  # noqa: BLE001 - reported or raised, it is a failure
        assert type(error).__name__ == "NonZeroExitCode"
    else:
        assert flow is None or not flow.succeeded
    results = json.loads((tmp_path / "run/top/openfpgaloader/results.json").read_text())
    assert results["success"] is False


# ------------------------------------------------------------------------- the example projects


@pytest.mark.parametrize("project", ["blinky", "dvi_test"])
def test_the_ulx3s_example_projects_plan_the_whole_graph_from_their_sections(project, tmp_path):
    path = Path(__file__).parent.parent / "examples/boards/ulx3s" / project / "xedaproject.toml"
    plan = _runner(tmp_path).plan(Openfpgaloader, design=project, xedaproject=str(path))
    assert [n.name for n in plan.nodes] == ["yosys_fpga", "nextpnr", "fpga_pack", "openfpgaloader"]
    placed = plan.node("nextpnr").settings
    assert placed.timing_allow_fail is True and placed.detailed_timing_report is False
    for node in plan.nodes:
        assert node.settings.fpga.part == "LFE5U-85F-6BG381C", node.name
        assert [clock.period for clock in node.settings.clocks.values()] == [40.0], node.name


def test_the_ulx3s_blinky_example_builds_and_programs_under_the_fakes(tmp_path, fake_loader):
    """Command construction only (the fake loader): `--board ulx3s` and the 85F part."""
    example = Path(__file__).parent.parent / "examples/boards/ulx3s/blinky/blinky.xeda.yaml"
    assert_fake_loader()
    flow = _runner(tmp_path).run("openfpgaloader", example)
    assert flow is not None and flow.succeeded
    record = tmp_path / "run/blinky/openfpgaloader/fake_fpga.calls.jsonl"
    (call,) = [json.loads(line) for line in record.read_text().splitlines()]
    assert call["tool"] == "openFPGALoader"
    assert call["argv"] == [
        "--bitstream",
        str(tmp_path / "run/blinky/fpga_pack/outputs/blinky.bit"),
        "--board",
        "ulx3s",
        "--fpga-part",
        "LFE5U-85F-6BG381C",
    ]
