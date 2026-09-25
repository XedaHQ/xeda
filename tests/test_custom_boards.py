"""A custom board database supplies the device, constraints and programmer board name."""

from pathlib import Path

import pytest

import xeda.board
from xeda import Design
from xeda.flow import FlowSettingsError
from xeda.flows import Nextpnr, Openfpgaloader, OpenXC7
from xeda.flows.nextpnr import NextpnrTool
from xeda.flows.yosys import YosysFpga
from xeda.tool import Tool


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

    # OpenXC7 uses the inherited board-to-FPGA lookup, though it does not consume LPF files.
    assert OpenXC7.Settings.from_input(data, design_root=tmp_path).fpga.part == settings.fpga.part


def test_custom_board_assignment_order_uses_design_root(tmp_path):
    boards = board_file(tmp_path)
    file_first = Nextpnr.Settings.from_input(
        {"custom_boards_file": "board files/boards.toml"}, design_root=tmp_path
    )
    assert file_first.custom_boards_file == boards
    file_first.board = "MY_BOARD"
    assert file_first.fpga.part == "LFE5U-25F-6BG381C"

    board_first = Nextpnr.Settings.from_input({"board": "MY_BOARD"}, design_root=tmp_path)
    board_first.custom_boards_file = "board files/boards.toml"
    assert board_first.custom_boards_file == boards
    assert board_first.fpga.part == "LFE5U-25F-6BG381C"


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
    settings.board = "unknown"
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
    settings = Openfpgaloader.Settings.from_input(
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

    monkeypatch.setattr(xeda.board, "toml_load", unreadable)
    with pytest.raises(FlowSettingsError, match="Cannot read the bundled board database"):
        Nextpnr.Settings.from_input({"board": "ULX3S_85F"})


def nextpnr_args(tmp_path: Path, settings: Nextpnr.Settings, monkeypatch) -> list[str]:
    """Run nextpnr with its tool stubbed; return its arguments, checking its LPF exists."""
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    flow = Nextpnr(settings, design, tmp_path / "nextpnr")
    yosys = YosysFpga(YosysFpga.Settings(fpga=settings.fpga), design, tmp_path / "yosys")
    yosys.run_path.mkdir()
    (yosys.run_path / yosys.settings.netlist_json).write_text("{}")
    flow.completed_dependencies.append(yosys)
    calls = []

    def run(self, *args):
        lpfs = [arg.removeprefix("--lpf=") for arg in map(str, args) if arg.startswith("--lpf=")]
        assert all(Path(lpf).is_file() for lpf in lpfs)
        calls.append(list(map(str, args)))

    monkeypatch.setattr(NextpnrTool, "run", run)

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
    assert f"--lpf={tmp_path / 'board files' / 'pins.lpf'}" in args


def test_nextpnr_resolves_bundled_board_lpf_against_bundled_database(tmp_path, monkeypatch):
    board = {"fpga": {"part": "LFE5U-85F-6BG381C"}, "lpf": "boards/ulx3s/board.lpf"}
    monkeypatch.setattr(xeda.board, "get_board_data", lambda name, custom=None: board)
    settings = Nextpnr.Settings.from_input({"board": "LOCAL_LPF"}, design_root=tmp_path)

    args = nextpnr_args(tmp_path, settings, monkeypatch)

    lpfs = [arg for arg in args if arg.startswith("--lpf=")]
    assert len(lpfs) == 1
    assert Path(lpfs[0].removeprefix("--lpf=")).parts[-4:] == (
        "data",
        "boards",
        "ulx3s",
        "board.lpf",
    )


def test_openfpgaloader_forwards_database_and_uses_programmer_name(tmp_path, monkeypatch):
    board_file(tmp_path)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    settings = Openfpgaloader.Settings.from_input(
        {"board": "MY_BOARD", "custom_boards_file": "board files/boards.toml"},
        design_root=tmp_path,
    )
    flow = Openfpgaloader(settings, design, tmp_path / "loader")
    flow.init()
    dep_settings = flow.dependencies[0][1]
    assert dep_settings.custom_boards_file == tmp_path / "board files" / "boards.toml"
    assert dep_settings.fpga.part == "LFE5U-25F-6BG381C"

    nextpnr = Nextpnr(dep_settings, design, tmp_path / "nextpnr")
    nextpnr.run_path.mkdir()
    (nextpnr.run_path / nextpnr.settings.textcfg).write_text("config")
    flow.completed_dependencies.append(nextpnr)
    calls = []

    def fake_run(self, *args):
        calls.append((self.executable, args))
        if self.executable == "ecppack":
            Path(args[1]).write_bytes(b"bitstream")

    monkeypatch.setattr(Tool, "run", fake_run)

    flow.run()

    assert any("--board" in args and "programmer_board" in args for _, args in calls)
