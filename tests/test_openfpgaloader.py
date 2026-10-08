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
from xeda.board import WithFpgaBoardSettings
from xeda.flow import FlowSettingsError, FlowSettingsException
from xeda.flow.io import declared_inputs, declared_outputs
from xeda.flow_runner import DefaultRunner
from xeda.flows import Openfpgaloader
from xeda.flows.openfpgaloader import OpenfpgaloaderTool

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


def test_the_loader_always_runs_because_it_programs(tmp_path, fake_loader):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    flow = Openfpgaloader(Openfpgaloader.Settings(fpga=ECP5), design, tmp_path / "loader")
    assert flow.always_runs() == "it programs a device"
    flow.init()  # makes the loader's tool: asks the (fake) loader for its version
    assert not hasattr(flow, "packer")


def test_the_loader_is_a_tool_of_its_flow_like_any_other(tmp_path, fake_loader):
    """A tool made as a class attribute never finds its flow: no `dockerized`, no entry in the
    results' `tools`."""
    assert "ofpga_loader" not in vars(Openfpgaloader)
    flow = _program(tmp_path, _prebuilt(tmp_path), {"fpga": ECP5})
    assert [tool["executable"] for tool in flow.results["tools"]] == ["openFPGALoader"]
    # the call record holds the one programming call: the fake answers the version query
    # without recording it
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"][0] == "--bitstream"


def test_the_loader_records_its_version_in_the_results(tmp_path, fake_loader):
    """The programmer is asked for its version, which makes a programming run reproducible;
    through the fake, which answers `-V` (capital V) as openFPGALoader v1.1.1 does and
    rejects `--version`, so a flow asking the wrong way would record an empty version."""
    flow = _program(tmp_path, _prebuilt(tmp_path), {"fpga": ECP5})
    expected = [{"executable": "openFPGALoader", "version": "1.0.0"}]
    assert flow.results["tools"] == expected
    results = json.loads((tmp_path / "run/top/openfpgaloader/results.json").read_text())
    assert results["tools"] == expected


def test_the_loader_is_asked_for_its_version_with_a_capital_v(fake_loader):
    """openFPGALoader v1.1.1 lists `-V, --Version` and rejects `--version`. A tool with a
    minimum version asks for it when it is made, so this runs on the fake, which answers `-V`
    as the real one does."""
    tool = OpenfpgaloaderTool()
    assert (tool.executable, tool.version_flag) == ("openFPGALoader", ["-V"])
    assert tool.version == ("1", "0", "0")  # what the fake prints for `-V`
    # the one line a real v1.1.1 prints for it
    assert tool.process_version_output("openFPGALoader v1.1.1\n") == ("1", "1", "1")


def test_the_loader_needs_release_0_13_1_or_newer():
    """The DONE state of a Xilinx FPGA is printed only since release 0.13.0 (0.12.1 has no such
    line): with an older loader, a load that left DONE low would look like any other. The tag
    v0.13.0 names its version 0.12.1 (its CMakeLists), so `-V` cannot tell it from the release
    before it; 0.13.1 is the first to name itself."""
    from xeda.flows.openfpgaloader import MIN_OPENFPGALOADER_VERSION

    assert MIN_OPENFPGALOADER_VERSION == (0, 13, 1)
    # read from the model: making the tool would start the loader to ask for its version
    assert OpenfpgaloaderTool.model_fields["minimum_version"].default == MIN_OPENFPGALOADER_VERSION


@pytest.mark.parametrize("version", ["v0.13.1", "v0.13.2", "v0.14.0", "v1.0.0", "v1.1.1"])
def test_a_loader_of_the_minimum_release_or_newer_programs(
    tmp_path, fake_loader, monkeypatch, version
):
    monkeypatch.setenv("XEDA_FAKE_FPGA_LOADER_VERSION", f"openFPGALoader {version}")
    assert _program(tmp_path, _prebuilt(tmp_path), {"fpga": ECP5}).succeeded
    assert len(_calls(tmp_path, "openfpgaloader")) == 1


#: What `-V` prints for each: a real 0.13.0 announces itself as `v0.12.1`, so its row is the one
#: of 0.12.1 (the row for `v0.13.0` stands for a loader built from a tree that names it so).
OLDER_LOADERS = ["v0.13.0", "v0.12.1", "v0.12.0", "v0.9.0", "v0.3.0"]


@pytest.mark.parametrize("version", OLDER_LOADERS)
def test_an_older_loader_is_refused_before_it_programs_and_the_message_names_both_versions(
    tmp_path, fake_loader, monkeypatch, version
):
    """The version comes from the loader's `-V` (the only start that is no programming call)."""
    from xeda.tool import ToolException

    monkeypatch.setenv("XEDA_FAKE_FPGA_LOADER_VERSION", f"openFPGALoader {version}")
    with pytest.raises(ToolException) as raised:
        _runner(tmp_path).run("openfpgaloader", _prebuilt(tmp_path), flow_settings={"fpga": ECP5})
    message = str(raised.value)
    assert message.startswith("Minimum version not met: openFPGALoader ")
    assert f"{version.removeprefix('v')} was found" in message
    assert "Xeda needs 0.13.1 or newer" in message
    assert "whether a Xilinx FPGA finished its configuration" in message  # why
    # a user whose loader is release 0.13.0, which prints 0.12.1, is told why it is refused too
    assert "Release 0.13.0 does, but it prints 0.12.1 as its version" in message
    assert not _calls(tmp_path, "openfpgaloader")  # the loader was never started to program
    results = json.loads((tmp_path / "run/top/openfpgaloader/results.json").read_text())
    assert results["success"] is False and results["error"]["type"] == "ToolException"
    assert results["error"]["message"] == message


def test_an_older_loader_is_refused_before_any_producer_runs(tmp_path, fake_loader, monkeypatch):
    """The loader is asked for its version when the flow is prepared (`init`), which comes before
    the producers: neither synthesis, placement nor packing runs for a launch that is refused."""
    from xeda.tool import ToolException

    monkeypatch.setenv("XEDA_FAKE_FPGA_LOADER_VERSION", "openFPGALoader v0.12.1")
    assert_fake_loader()
    with pytest.raises(ToolException, match="Xeda needs 0.13.1 or newer"):
        _runner(tmp_path).run("openfpgaloader", _design(tmp_path), flow_settings={"fpga": ECP5})
    # only the loader's own directory was made, with the failure in it
    assert [p.name for p in (tmp_path / "run/top").iterdir() if p.is_dir()] == ["openfpgaloader"]
    for built in ("yosys_fpga", "nextpnr", "fpga_pack"):
        assert not _calls(tmp_path, built), f"{built} ran"
    results = json.loads((tmp_path / "run/top/openfpgaloader/results.json").read_text())
    assert results["success"] is False and results["error"]["type"] == "ToolException"


def test_planning_never_starts_the_loader(tmp_path, programmer_guard):
    """A plan (and so a dry run) constructs no flow, so it needs no loader on `PATH`: with the
    sentinel as the only `openFPGALoader`, nothing starts it."""
    assert shutil.which("openFPGALoader") == str(programmer_guard.sentinel)
    plan = _runner(tmp_path).plan(Openfpgaloader, _design(tmp_path), flow_settings={"fpga": ECP5})
    assert [n.name for n in plan.nodes] == ["yosys_fpga", "nextpnr", "fpga_pack", "openfpgaloader"]
    assert not programmer_guard.reached()


def test_a_loader_whose_version_cannot_be_read_is_not_compared(tmp_path, fake_loader, monkeypatch):
    """The rule of every tool (`Tool._version_is_gte`): a version that could not be read is not
    compared. Every release since 0.3 prints `openFPGALoader v<version>` for `-V`."""
    monkeypatch.setenv("XEDA_FAKE_FPGA_LOADER_VERSION", "")
    assert _program(tmp_path, _prebuilt(tmp_path), {"fpga": ECP5}).succeeded


def test_a_base_class_reason_to_always_run_comes_first(tmp_path, monkeypatch):
    from xeda.flow import Flow

    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    flow = Openfpgaloader(Openfpgaloader.Settings(fpga=ECP5), design, tmp_path / "loader")
    monkeypatch.setattr(Flow, "always_runs", lambda self: "the base class says so")
    assert flow.always_runs() == "the base class says so"


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
    flows = {"openfpgaloader": {"board": "ulx3s_85f", "nextpnr": {"timing_allow_fail": True}}}
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


@pytest.mark.parametrize("verify", ["true", "True"])
@pytest.mark.parametrize("write_flash", [None, False, "false", "False"])
def test_verify_needs_flash_in_whatever_form_the_two_are_spelled(tmp_path, verify, write_flash):
    """The rule is checked on the input as the settings read it, not on `True` alone: `true` as
    text (how a command line writes a boolean) is true as well."""
    given = {"fpga": ECP5, "verify": verify}
    if write_flash is not None:
        given["write_flash"] = write_flash
    with pytest.raises(FlowSettingsError, match="verify.*write_flash"):
        Openfpgaloader.Settings.from_input(given, design_root=tmp_path)
    settings = Openfpgaloader.Settings.from_input({"fpga": ECP5}, design_root=tmp_path)
    with pytest.raises(ValidationError, match="verify.*write_flash"):
        settings.verify = verify
    assert settings.verify is False
    accepted = Openfpgaloader.Settings.from_input(
        {"fpga": ECP5, "verify": verify, "write_flash": "true"}, design_root=tmp_path
    )
    assert accepted.verify is True and accepted.write_flash is True


@pytest.mark.parametrize("spelling", [1, "1", "yes", "on"])
@pytest.mark.parametrize("write_flash", [None, True])
def test_a_verify_that_is_no_boolean_is_refused_as_such_not_read_as_on(
    tmp_path, spelling, write_flash
):
    """A setting accepts only a boolean (or `true`/`false` as text): `yes`, `on` and numbers
    are refused with the message about booleans, whether or not `write_flash` is given, and
    never taken for true by the verify-needs-flash rule."""
    given = {"fpga": ECP5, "verify": spelling}
    if write_flash is not None:
        given["write_flash"] = write_flash
    with pytest.raises(FlowSettingsError, match="not a boolean.*verify"):
        Openfpgaloader.Settings.from_input(given, design_root=tmp_path)
    settings = Openfpgaloader.Settings.from_input({"fpga": ECP5}, design_root=tmp_path)
    with pytest.raises(ValidationError, match="not a boolean"):
        settings.verify = spelling
    assert settings.verify is False


@pytest.mark.parametrize("spelling", [1, "1", "yes", "on"])
def test_a_write_flash_that_is_no_boolean_is_refused_as_such_beside_verify(tmp_path, spelling):
    """With `verify` on, a `write_flash` that is no boolean is reported as that, not as a missing
    flash: the rule stands aside for what the field itself refuses."""
    with pytest.raises(FlowSettingsError, match="not a boolean.*write_flash") as error:
        Openfpgaloader.Settings.from_input(
            {"fpga": ECP5, "verify": True, "write_flash": spelling}, design_root=tmp_path
        )
    assert "needs write_flash" not in str(error.value)
    settings = Openfpgaloader.Settings.from_input({"fpga": ECP5}, design_root=tmp_path)
    with pytest.raises(ValidationError, match="not a boolean"):
        settings.write_flash = spelling
    assert settings.write_flash is False


def test_verify_given_with_dash_s_is_refused_in_planning(tmp_path, fake_loader):
    with pytest.raises(FlowSettingsError, match="verify.*write_flash"):
        _runner(tmp_path).plan(
            Openfpgaloader, _prebuilt(tmp_path), flow_settings=["fpga=" + ECP5, "verify=true"]
        )
    plan = _runner(tmp_path).plan(
        Openfpgaloader,
        _prebuilt(tmp_path),
        flow_settings=["fpga=" + ECP5, "verify=true", "write_flash=true"],
    )
    assert plan.node("openfpgaloader").settings.verify is True


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


#: Every setting of the flow that has a loader option of its own, each set to a value the loader
#: accepts together with the others: the command line the flow builds from all of them.
ALL_LOADER_SETTINGS = {
    "reset": True,
    "cable": "ft2232",
    "write_flash": True,
    "verify": True,
    "freq": 6000000,
    "offset": 4096,
    "cable_index": 1,
    "usb_serial_num": "FT123456",
    "index_chain": 0,
    "file_type": "bit",
    "target_flash": "primary",
    "skip_reset": True,
    "skip_load_bridge": True,
    "verbose_level": 1,
    "extra_args": ["--scan-usb"],
}


def test_every_setting_of_the_loader_is_an_option_the_loader_has(tmp_path, fake_loader):
    """The fake rejects an option that openFPGALoader v1.1.1 does not have, as the real parser
    does (`fake_fpga_tool.LOADER_OPTIONS`, from the loader's `src/main.cpp`): so a setting passed
    under a name that the loader does not know fails here, not on a board. `usb_serial_num` was
    passed as `--usb-serial-num`; the option is `--ftdi-serial`."""
    own = set(Openfpgaloader.Settings.model_fields) - set(WithFpgaBoardSettings.model_fields)
    assert own == set(ALL_LOADER_SETTINGS), "a setting of the loader that this table lacks"
    flow = _program(
        tmp_path, _prebuilt(tmp_path), {"fpga": ECP5, "verbose": 1, **ALL_LOADER_SETTINGS}
    )
    assert flow.succeeded
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--cable",
        "ft2232",
        "--fpga-part",
        ECP5,
        "--reset",
        "--write-flash",
        "--verify",
        "--skip-reset",
        "--skip-load-bridge",
        "--verbose",
        "--freq",
        "6000000",
        "--offset",
        "4096",
        "--cable-index",
        "1",
        "--ftdi-serial",
        "FT123456",
        "--index-chain",
        "0",
        "--file-type",
        "bit",
        "--target-flash",
        "primary",
        "--verbose-level",
        "1",
        "--scan-usb",
    ]


@pytest.mark.parametrize(
    "argument,message",
    [
        (["--usb-serial-num", "FT123456"], "Option 'usb-serial-num' does not exist"),
        (["--no-such-option"], "Option 'no-such-option' does not exist"),
        (["-Z"], "Option 'Z' does not exist"),
        (["--cable"], "Option 'cable' requires an argument"),
    ],
)
def test_the_fake_loader_rejects_what_the_real_option_parser_rejects(
    tmp_path, fake_loader, monkeypatch, argument, message
):
    """The oracle above is only as good as the fake: it fails an option the loader does not have,
    with the loader's message (a `printError`) and status 1 (`parse_opt` returns -1)."""
    flow = _runner(tmp_path).run(
        "openfpgaloader",
        _prebuilt(tmp_path),
        flow_settings={"fpga": ECP5, "extra_args": argument},
    )
    assert flow is not None and not flow.succeeded
    assert flow.results["error"]["type"] == "NonZeroExitCode"
    log = (tmp_path / "run/top/openfpgaloader/openfpgaloader.log").read_text()
    assert f"Error parsing options: {message}" in log
    assert not _calls(tmp_path, "openfpgaloader")  # it stopped before it read the bitstream


def test_a_ulx3s_is_programmed_by_its_board_name(tmp_path, fake_loader):
    flow = _program(tmp_path, _prebuilt(tmp_path), {"board": "ulx3s_85f"})
    assert flow.succeeded
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == ["--bitstream", str(tmp_path / "design/given.bit"), "--board", "ulx3s"]


def test_a_cable_takes_precedence_over_the_board_s(tmp_path, fake_loader):
    _program(tmp_path, _prebuilt(tmp_path), {"board": "ulx3s_85f", "cable": "ft231X"})
    argv = _calls(tmp_path, "openfpgaloader")[0]["argv"]
    assert argv[2:4] == ["--cable", "ft231X"] and "--board" not in argv


#: Every bundled board, with its name in openFPGALoader (the board list of openFPGALoader's own
#: src/board.hpp) and the FPGA part `--fpga-part` takes when the board is not named: a Xilinx part
#: has no speed grade there.
BUNDLED_BOARDS = {
    "arty_a7_100t": ("arty_a7_100t", "xc7a100tcsg324"),
    "arty_a7_35t": ("arty_a7_35t", "xc7a35tcsg324"),
    "basys_3": ("basys3", "xc7a35tcpg236"),
    "stlv7325_v2": ("stlv7325", "xc7k325tffg676"),
    "ulx3s_85f": ("ulx3s", "LFE5U-85F-6BG381C"),
}


def test_the_table_of_bundled_boards_below_names_every_bundled_board():
    from xeda.board import bundled_boards

    assert set(BUNDLED_BOARDS) == set(bundled_boards())
    # each has the name openFPGALoader knows it by, as text
    assert all(entry["openfpgaloader_board"] for entry in bundled_boards().values())


@pytest.mark.parametrize("board", sorted(BUNDLED_BOARDS))
def test_every_bundled_board_is_programmed_by_its_openfpgaloader_board_alone(
    tmp_path, fake_loader, board
):
    """`--board` makes openFPGALoader use the board's own part. `--fpga-part` would replace it,
    and the loader names the bridge bitstream that writes a flash by it, as written: no speed
    grade, so a part with one finds no bridge."""
    loader, _part = BUNDLED_BOARDS[board]
    flow = _program(tmp_path, _prebuilt(tmp_path), {"board": board})
    assert flow.succeeded
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == ["--bitstream", str(tmp_path / "design/given.bit"), "--board", loader]


@pytest.mark.parametrize("board", sorted(BUNDLED_BOARDS))
def test_a_cable_takes_precedence_over_every_bundled_board(tmp_path, fake_loader, board):
    """No board name, so the part is given, as the loader writes it: without a speed grade."""
    _loader, part = BUNDLED_BOARDS[board]
    _program(tmp_path, _prebuilt(tmp_path), {"board": board, "cable": "ft231X"})
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--cable",
        "ft231X",
        "--fpga-part",
        part,
    ]


def test_a_bundled_board_in_capitals_is_programmed_by_the_same_name(tmp_path, fake_loader):
    _program(tmp_path, _prebuilt(tmp_path), {"board": "BASYS_3"})
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"][2:] == ["--board", "basys3"]


@pytest.mark.parametrize(
    "part,given",
    [
        ("xc7a35tcpg236-1", "xc7a35tcpg236"),
        ("xc7k325tffg676-2", "xc7k325tffg676"),
        ("xc7z035ffg676-2L", "xc7z035ffg676"),
        ("xc7z035ffg676-2l", "xc7z035ffg676"),  # the part keeps its case, its grade is `-2L`
        ("XC7VX485TFFG1761-3", "XC7VX485TFFG1761"),
        ("xc7a35tcpg236", "xc7a35tcpg236"),  # no grade to take away
    ],
)
def test_a_xilinx_part_is_given_to_the_loader_without_its_speed_grade(
    tmp_path, fake_loader, part, given
):
    """openFPGALoader uses `--fpga-part` as written to name the bridge bitstream
    (`spiOverJtag_<device><package>.bit.gz`), which has no speed grade."""
    _program(tmp_path, _prebuilt(tmp_path), {"fpga": part})
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == ["--bitstream", str(tmp_path / "design/given.bit"), "--fpga-part", given]


@pytest.mark.parametrize("part", [ECP5, "iCE40HX1K-TQ144"])
def test_a_part_of_another_vendor_is_given_to_the_loader_as_it_is(tmp_path, fake_loader, part):
    _program(tmp_path, _prebuilt(tmp_path), {"fpga": part})
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == ["--bitstream", str(tmp_path / "design/given.bit"), "--fpga-part", part]


def test_a_board_openfpgaloader_does_not_know_is_programmed_by_its_part_alone(
    tmp_path, fake_loader
):
    """The field is optional: a board without it adds no `--board`, and its part is given."""
    boards = tmp_path / "design" / "boards.toml"
    boards.parent.mkdir()
    boards.write_text('[MY_BOARD]\nfpga.part = "xc7a35tcpg236-1"\n')
    settings = {"board": "MY_BOARD", "custom_boards_file": "boards.toml"}
    _program(tmp_path, _prebuilt(tmp_path), settings)
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == [
        "--bitstream",
        str(tmp_path / "design/given.bit"),
        "--fpga-part",
        "xc7a35tcpg236",
    ]


def test_a_custom_board_is_programmed_by_its_programmer_name(tmp_path, fake_loader):
    boards = tmp_path / "design" / "boards.toml"
    boards.parent.mkdir()
    boards.write_text(
        f'[MY_BOARD]\nopenfpgaloader_board = "programmer_board"\nfpga.part = "{ECP5}"\n'
    )
    settings = {"board": "MY_BOARD", "custom_boards_file": "boards.toml"}
    _program(tmp_path, _prebuilt(tmp_path), settings)
    argv = _calls(tmp_path, "openfpgaloader")[0]["argv"]
    assert argv[2:] == ["--board", "programmer_board"]


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


def test_a_prebuilt_bitstream_is_programmed_without_a_device_for_the_loader_to_detect(
    tmp_path, fake_loader
):
    """Loading SRAM needs no part: with none known the loader is started without `--fpga-part`
    and detects the device itself."""
    design = _prebuilt(tmp_path)
    plan = _runner(tmp_path).plan(Openfpgaloader, design)
    assert plan.node("openfpgaloader").settings.fpga is None
    flow = _program(tmp_path, design)
    assert flow.succeeded
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == ["--bitstream", str(tmp_path / "design/given.bit")]


def test_a_cable_with_no_part_known_gives_the_loader_no_part(tmp_path, fake_loader):
    """A cable names no device: with no part known, the loader detects the device itself."""
    _program(tmp_path, _prebuilt(tmp_path), {"cable": "ft231X"})
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"] == ["--bitstream", str(tmp_path / "design/given.bit"), "--cable", "ft231X"]


def test_programming_the_flash_needs_the_device(tmp_path, fake_loader):
    """openFPGALoader programs a Xilinx flash through a bridge made for the part, so
    `write_flash` needs the device: refused before anything runs, saying why."""
    with pytest.raises(FlowSettingsException, match="openfpgaloader needs `fpga`") as raised:
        _runner(tmp_path).plan(
            Openfpgaloader, _prebuilt(tmp_path), flow_settings={"write_flash": True}
        )
    assert "write_flash" in str(raised.value)
    assert not _calls(tmp_path, "openfpgaloader")


def test_a_board_gives_the_device_the_flash_needs(tmp_path, fake_loader):
    """A board with a part is the device `write_flash` needs; the loader is given the board's
    name, which carries its part."""
    flow = _program(tmp_path, _prebuilt(tmp_path), {"board": "ulx3s_85f", "write_flash": True})
    assert flow.succeeded
    (call,) = _calls(tmp_path, "openfpgaloader")
    assert call["argv"][2:] == ["--board", "ulx3s", "--write-flash"]


def test_a_board_without_a_device_does_not_give_the_flash_one(tmp_path, fake_loader):
    """A board entry without an `fpga` names no device: `write_flash` is refused, as without a
    board, although the loader would be given the board's name."""
    boards = tmp_path / "design" / "boards.toml"
    boards.parent.mkdir()
    boards.write_text('[MY_BOARD]\nopenfpgaloader_board = "programmer_board"\n')
    settings = {"board": "MY_BOARD", "custom_boards_file": "boards.toml", "write_flash": True}
    with pytest.raises(FlowSettingsException, match="openfpgaloader needs `fpga`"):
        _runner(tmp_path).plan(Openfpgaloader, _prebuilt(tmp_path), flow_settings=settings)
    assert not _calls(tmp_path, "openfpgaloader")


def test_the_loader_s_output_is_kept_in_its_run_directory(tmp_path, fake_loader, monkeypatch):
    """What the loader printed is in `openfpgaloader.log` in its run directory, a failed run's
    too: openFPGALoader's own error is all there is to read when programming fails."""
    log = tmp_path / "run/top/openfpgaloader/openfpgaloader.log"
    assert _program(tmp_path, _prebuilt(tmp_path), {"fpga": ECP5}).succeeded
    assert log.is_file()
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "openFPGALoader")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", "fail")
    try:
        flow = _runner(tmp_path).run(
            "openfpgaloader", _prebuilt(tmp_path), flow_settings={"fpga": ECP5}
        )
    except Exception as error:  # reported or raised, it is a failure
        assert type(error).__name__ == "NonZeroExitCode"
    else:
        assert flow is None or not flow.succeeded
    assert "requested failure in openFPGALoader" in log.read_text()


def test_a_relaunch_whose_loader_is_missing_leaves_no_earlier_log(
    tmp_path, fake_loader, monkeypatch
):
    """The log is the run's own: a relaunch that cannot start the loader leaves no earlier
    run's output to be read as its own."""
    log = tmp_path / "run/top/openfpgaloader/openfpgaloader.log"
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "openFPGALoader")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", "fail")
    try:
        _runner(tmp_path).run("openfpgaloader", _prebuilt(tmp_path), flow_settings={"fpga": ECP5})
    except Exception as error:  # reported or raised, it is a failure
        assert type(error).__name__ == "NonZeroExitCode"
    assert "requested failure in openFPGALoader" in log.read_text()

    class _NotInstalled(OpenfpgaloaderTool):
        executable: str = "openFPGALoader-not-installed"

    monkeypatch.setattr("xeda.flows.openfpgaloader.OpenfpgaloaderTool", _NotInstalled)
    with pytest.raises(Exception) as raised:
        _runner(tmp_path).run("openfpgaloader", _prebuilt(tmp_path), flow_settings={"fpga": ECP5})
    assert type(raised.value).__name__ == "ExecutableNotFound", raised.value
    assert not log.exists() or "requested failure" not in log.read_text()


def test_outputs_to_a_programmer_of_a_design_source_is_refused_before_it_runs(
    tmp_path, fake_loader
):
    """The loader writes no outputs, and what it programs here is the design's own file:
    `--outputs-to` has nothing to deliver, and says so before anything runs."""
    from xeda.deliver import DeliveryError

    with pytest.raises(DeliveryError, match="openfpgaloader writes no outputs") as raised:
        _program(tmp_path, _prebuilt(tmp_path), {"fpga": ECP5}, outputs_to=tmp_path / "out")
    assert str(tmp_path / "design/given.bit") in str(raised.value)
    assert not _calls(tmp_path, "openfpgaloader")
    assert not (tmp_path / "run").exists() and not (tmp_path / "out").exists()


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
    boards.write_text(
        f'[MY_BOARD]\nopenfpgaloader_board = "programmer_board"\nfpga.part = "{ECP5}"\n'
    )
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
    # the board is named to the loader, which knows its part; with no board, the part is given
    expected = ["--fpga-part", ECP5] if where == "yosys_fpga" else ["--board", "programmer_board"]
    assert argv[2:] == expected


@pytest.mark.parametrize("suffix", [".toml", ".yaml", ".yml"])
@pytest.mark.parametrize("where", ["openfpgaloader", "nextpnr"])
def test_a_custom_board_database_in_any_format_serves_the_whole_declared_graph(
    tmp_path, fake_loader, suffix, where
):
    """The database is read for every board-aware node of the graph, TOML or YAML alike: the
    device, the programmer's board name, and the pins beside the database, which nextpnr reads."""
    root = tmp_path / "design"
    root.mkdir()
    (root / "pins.lpf").write_text('LOCATE COMP "clk" SITE "P3";\n')
    database = root / f"boards{suffix}"
    if suffix == ".toml":
        database.write_text(
            f'[MY_BOARD]\nopenfpgaloader_board = "programmer_board"\nfpga.part = "{ECP5}"\nlpf = "pins.lpf"\n'
        )
    else:
        database.write_text(
            f"MY_BOARD:\n  openfpgaloader_board: programmer_board\n  fpga:\n    part: {ECP5}\n  lpf: pins.lpf\n"
        )
    section = {"board": "MY_BOARD", "custom_boards_file": database.name}
    design = _design(tmp_path, flows={where: section})
    plan = _runner(tmp_path).plan(Openfpgaloader, design)
    assert [n.name for n in plan.nodes] == ["yosys_fpga", "nextpnr", "fpga_pack", "openfpgaloader"]
    for node in plan.nodes:
        assert node.settings.fpga.part == ECP5, node.name
        if node.name != "yosys_fpga":
            assert node.settings.board == "MY_BOARD", node.name
            assert node.settings.custom_boards_file == database, node.name
    flow = _program(tmp_path, design)
    assert flow.succeeded
    argv = _calls(tmp_path, "openfpgaloader")[0]["argv"]
    assert argv[2:] == ["--board", "programmer_board"]
    (placed,) = _calls(tmp_path, "nextpnr")
    (lpf,) = [a for a in placed["argv"] if a.startswith("--lpf=")]
    assert Path(lpf.removeprefix("--lpf=")).parent == tmp_path / "run/top/nextpnr"
    assert (tmp_path / "run/top/nextpnr/constraints.lpf").read_text() == (
        root / "pins.lpf"
    ).read_text()


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
    assert _calls(tmp_path, "fpga_pack")[0]["argv"][:1] == ["--compress"]
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
    path = Path(__file__).parent.parent / "examples/boards/ulx3s" / project / "xedaproject.yaml"
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
    ]


# ----------------------------------------------------------- the guard no test can forget


def test_a_launch_without_the_fake_reaches_the_sentinel_not_a_programmer(
    tmp_path, programmer_guard
):
    """No fake toolchain here, as in a test that forgot it: what the launch starts is the
    suite's sentinel (`conftest.programmer_guard`), whatever programmer the machine has."""
    assert shutil.which("openFPGALoader") == str(programmer_guard.sentinel)
    flow = None
    try:
        flow = _runner(tmp_path).run(
            "openfpgaloader", _prebuilt(tmp_path), flow_settings={"fpga": ECP5}
        )
    except Exception:  # a failed launch, raised or returned: either way nothing was programmed
        pass
    assert flow is None or not flow.succeeded
    assert programmer_guard.reached()  # the sentinel ran (and the marker is taken back)


def test_a_version_query_without_the_fake_reaches_the_sentinel_too(programmer_guard):
    """The guard does not excuse the flow's version query: against anything but the fake, that
    start is a loader started by a test, whatever it asks. The fake never touches the guard, so
    the query the flow makes under it costs nothing -- and a loader reached without it, even
    for `-V`, is the same `PATH` that would program on the next call."""
    assert shutil.which("openFPGALoader") == str(programmer_guard.sentinel)
    assert OpenfpgaloaderTool().version_output is None  # the sentinel exits 97: no version
    assert programmer_guard.reached()  # ... and the guard saw it (the marker is taken back)


def test_the_guard_refuses_a_path_that_selects_another_loader(tmp_path, programmer_guard):
    other = tmp_path / "bin/openFPGALoader"
    other.parent.mkdir()
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o755)
    with pytest.raises(AssertionError, match="neither the fake nor the sentinel"):
        programmer_guard.check(str(other.parent))
    programmer_guard.check(str(programmer_guard.directory))
    programmer_guard.check(str(tool_utils.FAKE_TOOLS_DIR))
    programmer_guard.check(str(tmp_path))  # none at all
