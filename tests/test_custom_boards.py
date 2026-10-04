"""A custom board database supplies the device, constraints and programmer board name."""

from pathlib import Path

import pytest

import xeda.board
from xeda import Design
from xeda.board import WithFpgaBoardSettings
from xeda.dataclass import Field, ValidationError
from xeda.flow import FlowSettingsError
from xeda.flows import Nextpnr, Openfpgaloader
from xeda.flows.nextpnr import NextpnrTool

from .test_nextpnr import write_nextpnr_config


class _LegacyParent(WithFpgaBoardSettings):
    """Settings holding a board-aware dependency's, as an undeclared flow's do: no built-in
    flow nests one any more, and `resolve_dependency` still serves such flows."""

    nextpnr: Nextpnr.Settings = Field(
        default_factory=Nextpnr.Settings, description="The dependency's settings."
    )
    dependency_settings = {"nextpnr": ("fpga", "board", "custom_boards_file", "clocks")}


def board_file(tmp_path: Path) -> Path:
    config = tmp_path / "board files"
    config.mkdir()
    (config / "pins.lpf").write_text('LOCATE COMP "clk" SITE "P3";\n')
    boards = config / "boards.toml"
    boards.write_text(
        '[MY_BOARD]\nname = "programmer_board"\n'
        'fpga.part = "LFE5U-25F-6BG381C"\nlpf = "pins.lpf"\n'
    )
    return boards


def test_custom_board_resolves_from_design_and_survives_reload(tmp_path):
    boards = board_file(tmp_path)
    data = {"board": "MY_BOARD", "custom_boards_file": "board files/boards.toml"}
    settings = Nextpnr.Settings.from_input(data, design_root=tmp_path, runner_cwd=tmp_path.parent)
    assert settings.fpga is not None
    assert settings.fpga.part == "LFE5U-25F-6BG381C"
    assert settings.custom_boards_file == boards
    assert settings.board_data()["name"] == "programmer_board"

    reloaded = Nextpnr.Settings.from_input(
        settings.model_dump(mode="json"), design_root=tmp_path, runner_cwd=tmp_path.parent
    )
    assert reloaded.custom_boards_file == boards
    assert reloaded.fpga == settings.fpga
    assert reloaded.board_data() == settings.board_data()

    # Every board-aware flow uses the inherited board-to-FPGA lookup, whatever files it reads.
    loader = Openfpgaloader.Settings.from_input(data, design_root=tmp_path)
    assert loader.fpga.part == settings.fpga.part


def test_custom_board_assignment_order_uses_design_root(tmp_path):
    boards = board_file(tmp_path)
    file_first = Nextpnr.Settings.from_input(
        {"custom_boards_file": "board files/boards.toml"}, design_root=tmp_path
    )
    assert file_first.custom_boards_file == boards
    file_first.board = "MY_BOARD"
    assert file_first.fpga.part == "LFE5U-25F-6BG381C"

    with pytest.raises(FlowSettingsError, match="Unknown board"):
        Nextpnr.Settings.from_input({"board": "MY_BOARD"}, design_root=tmp_path)


def test_changing_board_database_refreshes_only_a_board_derived_fpga():
    board_dir = Path(__file__).parent / "resources"
    first = board_dir / "boards_a.toml"
    second = board_dir / "boards_b.toml"

    settings = Nextpnr.Settings.from_input(
        {"board": "ulx3s", "custom_boards_file": first}, design_root=board_dir
    )
    settings = Nextpnr.Settings.from_input(settings.model_dump(mode="json"), design_root=board_dir)
    assert settings.fpga.part == "LFE5U-25F-6BG381C"
    settings.custom_boards_file = second
    assert settings.fpga.part == "LFE5U-45F-6BG381C"
    with pytest.raises(ValidationError, match="Unknown board"):
        settings.board = "unknown"
    assert settings.board == "ulx3s"
    settings.board = None
    assert settings.fpga is None
    settings.board = "ulx3s"
    assert settings.fpga.part == "LFE5U-45F-6BG381C"

    explicit = Nextpnr.Settings.from_input(
        {
            "board": "ulx3s",
            "custom_boards_file": first,
            "fpga": {"part": "LFE5U-85F-6BG381C"},
        },
        design_root=board_dir,
    )
    explicit.custom_boards_file = second
    assert explicit.fpga.part == "LFE5U-85F-6BG381C"


def test_dependency_resolves_parent_board_and_database_together(tmp_path):
    parent_file = tmp_path / "parent.toml"
    parent_file.write_text('[PARENT]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    child_file = tmp_path / "child.toml"
    child_file.write_text('[CHILD]\nfpga.part = "LFE5U-45F-6BG381C"\n')
    settings = _LegacyParent.from_input(
        {
            "board": "PARENT",
            "custom_boards_file": "parent.toml",
            "nextpnr": {"board": "CHILD", "custom_boards_file": "child.toml"},
        },
        design_root=tmp_path,
    )

    dependency = settings.resolve_dependency("nextpnr")

    assert dependency.board == "PARENT"
    assert dependency.custom_boards_file == parent_file
    assert dependency.fpga.part == "LFE5U-25F-6BG381C"


def test_dependency_adopts_a_custom_board_pair_without_changing_the_given_settings(tmp_path):
    boards = board_file(tmp_path)
    settings = _LegacyParent.from_input(
        {"nextpnr": {"board": "MY_BOARD", "custom_boards_file": str(boards)}},
        design_root=tmp_path,
    )
    given = settings.nextpnr.model_dump()
    dependency = settings.resolve_dependency("nextpnr")
    assert settings.board == dependency.board == "MY_BOARD"
    assert settings.custom_boards_file == dependency.custom_boards_file == boards
    assert settings.fpga == dependency.fpga
    assert settings.nextpnr.model_dump() == given
    assert dependency is not settings.nextpnr


def test_invalid_combined_board_pair_leaves_dependency_settings_unchanged(tmp_path):
    boards = board_file(tmp_path)
    settings = _LegacyParent.from_input(
        {"board": "ULX3S_85F", "nextpnr": {"board": "MY_BOARD", "custom_boards_file": str(boards)}},
        design_root=tmp_path,
    )
    before = settings.model_dump()
    with pytest.raises(FlowSettingsError, match="Unknown board"):
        settings.resolve_dependency("nextpnr")
    assert settings.model_dump() == before


def test_custom_board_path_variable_and_missing_file(tmp_path):
    board_file(tmp_path)
    settings = Nextpnr.Settings.from_input(
        {"board": "MY_BOARD", "custom_boards_file": "$DESIGN_ROOT/board files/boards.toml"},
        design_root=tmp_path,
    )
    assert settings.custom_boards_file == tmp_path / "board files" / "boards.toml"
    with pytest.raises(FlowSettingsError, match="Cannot read custom boards file"):
        Nextpnr.Settings.from_input(
            {"board": "MY_BOARD", "custom_boards_file": "missing.toml"},
            design_root=tmp_path,
        )


def test_custom_database_is_checked_even_with_explicit_fpga(tmp_path):
    path = tmp_path / "boards.toml"
    for content, message in (
        ("broken = [", "Invalid value"),
        ('MY_BOARD = "not a table"\n', "must be a table"),
    ):
        path.write_text(content)
        with pytest.raises(FlowSettingsError, match=message):
            Nextpnr.Settings.from_input(
                {
                    "board": "MY_BOARD",
                    "custom_boards_file": str(path),
                    "fpga": {"part": "LFE5U-25F-6BG381C"},
                },
                design_root=tmp_path,
            )


def test_board_database_is_read_only_when_board_or_database_changes(tmp_path, monkeypatch):
    boards = board_file(tmp_path)
    settings = Nextpnr.Settings.from_input(
        {"board": "MY_BOARD", "custom_boards_file": str(boards)}, design_root=tmp_path
    )
    reads = []
    get_board_data = xeda.board.get_board_data
    monkeypatch.setattr(
        xeda.board, "get_board_data", lambda *a: reads.append(a) or get_board_data(*a)
    )

    settings.seed = 3
    settings.fpga = {"part": "LFE5U-45F-6BG381C"}
    assert reads == []
    settings.board = "MY_BOARD"
    settings.custom_boards_file = str(boards)
    assert reads and all(call == ("MY_BOARD", boards) for call in reads)


def test_unreadable_bundled_database_is_named(monkeypatch):
    def unreadable(path):
        raise OSError("unreadable")

    monkeypatch.setattr(xeda.board, "toml_loads", unreadable)
    with pytest.raises(FlowSettingsError, match="Cannot read the bundled board database"):
        Nextpnr.Settings.from_input({"board": "ULX3S_85F"})


def nextpnr_args(tmp_path: Path, settings: Nextpnr.Settings, monkeypatch) -> list[str]:
    """Run nextpnr with its tool stubbed; return its arguments, checking its LPF exists."""
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    flow = Nextpnr(settings, design, tmp_path / "nextpnr")
    netlist = tmp_path / "yosys" / "netlist.json"
    netlist.parent.mkdir()
    netlist.write_text("{}")
    flow.inputs.netlist = netlist
    calls = []

    def run(self, *args, env=None):
        lpfs = [arg.removeprefix("--lpf=") for arg in map(str, args) if arg.startswith("--lpf=")]
        assert all(Path(lpf).is_file() for lpf in lpfs)
        write_nextpnr_config(flow, args)
        calls.append(list(map(str, args)))

    monkeypatch.setattr(NextpnrTool, "run", run)

    flow.prepare_inputs()
    flow.run()

    assert len(calls) == 1
    return calls[0]


def test_nextpnr_uses_custom_board_lpf(tmp_path, monkeypatch):
    board_file(tmp_path)
    settings = Nextpnr.Settings.from_input(
        {"board": "MY_BOARD", "custom_boards_file": "board files/boards.toml"},
        design_root=tmp_path,
    )
    args = nextpnr_args(tmp_path, settings, monkeypatch)
    merged = tmp_path / "nextpnr" / "constraints.lpf"
    assert f"--lpf={merged}" in args
    assert merged.read_text() == (tmp_path / "board files" / "pins.lpf").read_text()


def test_nextpnr_records_the_board_lpf_it_reads_as_an_implicit_input(tmp_path, monkeypatch):
    """No setting names a board's pin constraints: the flow registers the file it read, so a
    trace notices an edit to it."""
    board_file(tmp_path)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    settings = Nextpnr.Settings.from_input(
        {"board": "MY_BOARD", "custom_boards_file": "board files/boards.toml"},
        design_root=tmp_path,
    )
    flow = Nextpnr(settings, design, tmp_path / "nextpnr")
    flow.prepare_inputs()
    assert flow.implicit_inputs == [tmp_path / "board files" / "pins.lpf"]
    assert flow.always_runs() is None  # a local file: the trace can verify it


def test_nextpnr_resolves_bundled_board_lpf_against_bundled_database(tmp_path, monkeypatch):
    board = {"fpga": {"part": "LFE5U-85F-6BG381C"}, "lpf": "boards/ulx3s/board.lpf"}
    monkeypatch.setattr(xeda.board, "get_board_data", lambda name, custom=None: board)
    settings = Nextpnr.Settings.from_input({"board": "LOCAL_LPF"}, design_root=tmp_path)

    args = nextpnr_args(tmp_path, settings, monkeypatch)

    lpfs = [arg for arg in args if arg.startswith("--lpf=")]
    assert len(lpfs) == 1
    assert (
        Path(lpfs[0].removeprefix("--lpf=")).read_text()
        == (Path(xeda.board.__file__).parent / "data/boards/ulx3s/board.lpf").read_text()
    )
