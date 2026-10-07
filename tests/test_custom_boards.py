"""A custom board database supplies the device, constraints and openFPGALoader board name."""

from pathlib import Path

import pytest

import xeda.board
from xeda import Design, introspect
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
        '[MY_BOARD]\nopenfpgaloader_board = "programmer_board"\n'
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
    assert settings.board_data()["openfpgaloader_board"] == "programmer_board"

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
        {"board": "ulx3s_85f", "nextpnr": {"board": "MY_BOARD", "custom_boards_file": str(boards)}},
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
        Nextpnr.Settings.from_input({"board": "ulx3s_85f"})


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


# --- A custom database may be TOML or YAML: the same boards, the same behavior ---

BOARD_TOML = (
    '[MY_BOARD]\nopenfpgaloader_board = "programmer_board"\n'
    'fpga.part = "LFE5U-25F-6BG381C"\nlpf = "pins.lpf"\n'
    '[OTHER_BOARD]\nopenfpgaloader_board = "other"\nfpga.part = "LFE5U-45F-6BG381C"\nlpf = "pins.lpf"\n'
)
BOARD_YAML = """\
MY_BOARD:
  openfpgaloader_board: programmer_board
  fpga:
    part: LFE5U-25F-6BG381C
  lpf: pins.lpf
OTHER_BOARD:
  openfpgaloader_board: other
  fpga:
    part: LFE5U-45F-6BG381C
  lpf: pins.lpf
"""
DATABASES = {"boards.toml": BOARD_TOML, "boards.yaml": BOARD_YAML, "boards.yml": BOARD_YAML}
SUFFIXES = pytest.mark.parametrize("filename", list(DATABASES))


def write_database(directory: Path, filename: str, content: str | None = None) -> Path:
    """`filename` in `directory`, with a `pins.lpf` beside it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pins.lpf").write_text('LOCATE COMP "clk" SITE "P3";\n')
    path = directory / filename
    path.write_text(DATABASES[filename] if content is None else content)
    return path


def test_the_same_database_in_toml_and_yaml_is_the_same_board(tmp_path):
    data = {}
    for filename in DATABASES:
        path = write_database(tmp_path / filename.replace(".", "_"), filename)
        settings = Nextpnr.Settings.from_input(
            {"board": "MY_BOARD", "custom_boards_file": str(path)}, design_root=tmp_path
        )
        data[filename] = (
            settings.board_data(),
            settings.fpga,
            xeda.board.read_board_database(path),
        )
    assert data["boards.toml"] == data["boards.yaml"] == data["boards.yml"]
    assert data["boards.toml"][0] == {
        "openfpgaloader_board": "programmer_board",
        "fpga": {"part": "LFE5U-25F-6BG381C"},
        "lpf": "pins.lpf",
    }


@SUFFIXES
def test_a_relative_custom_boards_file_resolves_against_the_design_root(tmp_path, filename):
    """The design root, not the start directory, whatever the database's format."""
    path = write_database(tmp_path / "board files", filename)
    start = tmp_path.parent
    settings = Nextpnr.Settings.from_input(
        {"board": "OTHER_BOARD", "custom_boards_file": f"board files/{filename}"},
        design_root=tmp_path,
        runner_cwd=start,
    )
    assert settings.custom_boards_file == path
    assert settings.fpga.part == "LFE5U-45F-6BG381C"
    reloaded = Nextpnr.Settings.from_input(
        settings.model_dump(mode="json"), design_root=tmp_path, runner_cwd=start
    )
    assert reloaded.custom_boards_file == path
    assert reloaded.board_data() == settings.board_data()


@SUFFIXES
def test_a_board_lpf_resolves_beside_its_database_in_either_format(tmp_path, filename):
    path = write_database(tmp_path / "board files", filename)
    # The design root is elsewhere: only the database's own directory can hold pins.lpf.
    design_root = tmp_path / "design"
    design_root.mkdir()
    settings = Nextpnr.Settings.from_input(
        {"board": "MY_BOARD", "custom_boards_file": str(path)}, design_root=design_root
    )
    with settings.board_file(settings.board_data()["lpf"]) as lpf:
        assert lpf == path.parent / "pins.lpf"
        assert lpf.read_text().startswith("LOCATE")


@SUFFIXES
def test_nextpnr_uses_the_lpf_beside_a_database_in_either_format(tmp_path, monkeypatch, filename):
    """The pins are the file beside the database: merged into the run's constraints, and
    recorded as an input, so an edit to it is noticed."""
    write_database(tmp_path / "board files", filename)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    settings = Nextpnr.Settings.from_input(
        {"board": "MY_BOARD", "custom_boards_file": f"board files/{filename}"},
        design_root=tmp_path,
    )
    flow = Nextpnr(settings, design, tmp_path / "nextpnr")
    flow.prepare_inputs()
    assert flow.implicit_inputs == [tmp_path / "board files" / "pins.lpf"]
    args = nextpnr_args(tmp_path, settings, monkeypatch)
    merged = tmp_path / "nextpnr" / "constraints.lpf"
    assert f"--lpf={merged}" in args
    assert merged.read_text() == (tmp_path / "board files" / "pins.lpf").read_text()


@SUFFIXES
def test_a_legacy_dependency_resolution_carries_a_database_in_either_format(tmp_path, filename):
    """A flow sharing the board with an undeclared dependency shares its database too."""
    path = write_database(tmp_path, filename)
    settings = _LegacyParent.from_input(
        {"board": "MY_BOARD", "custom_boards_file": filename}, design_root=tmp_path
    )
    dependency = settings.resolve_dependency("nextpnr")
    assert dependency.custom_boards_file == path
    assert dependency.fpga.part == "LFE5U-25F-6BG381C"


def database_error(tmp_path: Path, filename: str, content: str, **extra) -> str:
    """The message of the error a custom database with `content` raises."""
    path = tmp_path / filename
    path.write_text(content)
    data = {"board": "MY_BOARD", "custom_boards_file": str(path), **extra}
    with pytest.raises(FlowSettingsError) as raised:
        Nextpnr.Settings.from_input(data, design_root=tmp_path)
    return str(raised.value)


def test_a_yaml_database_with_a_duplicate_key_names_the_file_and_both_lines(tmp_path):
    message = database_error(
        tmp_path,
        "boards.yaml",
        "MY_BOARD:\n  fpga:\n    part: LFE5U-25F-6BG381C\n  fpga:\n    part: LFE5U-45F-6BG381C\n",
    )
    assert f'Cannot load board database "{tmp_path / "boards.yaml"}", line 4, column 3' in message
    assert "duplicate mapping key 'fpga'" in message
    assert "first occurrence of key 'fpga' (line 2, column 3)" in message


def test_a_yaml_database_with_a_non_string_key_is_rejected(tmp_path):
    message = database_error(tmp_path, "boards.yaml", "MY_BOARD:\n  1: x\n")
    assert "mapping keys must be strings; quote this key" in message
    assert "boards.yaml" in message


def test_a_yaml_database_goes_through_the_strict_loader(tmp_path):
    """Only the shared strict loader (YAML 1.2 core) reads `on` as text and `010` as ten; a
    YAML 1.1 reader gives `True` and `8`, which fit `pins` and `package` without a word.

    No board field is a boolean, so "`on` is text, not a boolean: write `true`" (which a bool
    field gives) cannot arise here. `pins` is an integer: there the project's other YAML 1.1
    message appears, and only for the text `on` the strict loader leaves (PyYAML's own reader
    would give `True`, which pydantic accepts as the integer 1)."""
    prefix = "MY_BOARD:\n  fpga:\n    part: LFE5U-25F-6BG381C\n"
    # `on` is a YAML 1.1 boolean: here it is text, and no integer.
    message = database_error(tmp_path, "boards.yaml", prefix + "    pins: on\n")
    assert "`on` is text in xeda YAML (YAML 1.2): write `true`" in message
    # `010` is the number ten, not octal eight, and no text.
    message = database_error(tmp_path, "boards.yaml", prefix + "    package: 010\n")
    assert '`10` is a number in xeda YAML (YAML 1.2): write `"10"` for text' in message
    # Where text fits, it stays the text that was written.
    path = tmp_path / "text.yaml"
    path.write_text(prefix + "    package: on\n  openfpgaloader_board: no\n")
    board = xeda.board.get_board_data("MY_BOARD", path)
    assert board["openfpgaloader_board"] == "no"
    assert board["fpga"]["package"] == "on"


def test_the_yaml_reader_is_the_shared_strict_loader(tmp_path, monkeypatch):
    """No second YAML reader: the database is read by `xeda.yaml_loader.load_yaml`."""
    path = write_database(tmp_path, "boards.yaml")
    seen = []
    load_yaml = xeda.board.load_yaml
    monkeypatch.setattr(xeda.board, "load_yaml", lambda p: seen.append(p) or load_yaml(p))
    assert xeda.board.get_board_data("MY_BOARD", path)["openfpgaloader_board"] == "programmer_board"
    assert seen == [path]


ACCEPTED = "a board database is TOML or YAML ('.toml', '.yaml', '.yml')"


@pytest.mark.parametrize(
    "filename, reason",
    [
        ("boards.json", "unsupported file suffix '.json'"),
        ("boards.cfg", "unsupported file suffix '.cfg'"),
        ("boards.toml.bak", "unsupported file suffix '.bak'"),
        ("boards", "no file suffix"),
        (
            "boards.TOML",
            "unsupported file suffix '.TOML' (suffixes are case-sensitive: did you mean '.toml'?)",
        ),
        (
            "boards.Yaml",
            "unsupported file suffix '.Yaml' (suffixes are case-sensitive: did you mean '.yaml'?)",
        ),
        (
            "boards.YML",
            "unsupported file suffix '.YML' (suffixes are case-sensitive: did you mean '.yml'?)",
        ),
    ],
)
def test_an_unknown_database_suffix_is_an_error_saying_what_is_accepted(tmp_path, filename, reason):
    """Every refused suffix says what it is and what is accepted; a mis-cased one also says
    the right spelling. Pinned whole, as the setting reports it and as the function raises it."""
    path = tmp_path / filename
    path.write_text(BOARD_TOML)  # valid TOML: the suffix alone decides, nothing is guessed
    expected = f'Cannot load board database "{path}": {reason}; {ACCEPTED}'
    with pytest.raises(ValueError) as direct:
        xeda.board.board_database_format(path)
    assert str(direct.value) == expected
    for data in ({"board": "MY_BOARD"}, {}):  # a database nobody has asked a board of too
        with pytest.raises(FlowSettingsError) as raised:
            Nextpnr.Settings.from_input(
                {**data, "custom_boards_file": str(path)}, design_root=tmp_path
            )
        assert expected in str(raised.value)


def test_a_directory_is_no_database_and_says_so_like_any_suffixless_path(tmp_path):
    directory = tmp_path / "boards"
    directory.mkdir()
    with pytest.raises(ValueError) as raised:
        xeda.board.board_database_format(directory)
    assert str(raised.value) == (
        f'Cannot load board database "{directory}": no file suffix; {ACCEPTED}'
    )


def test_a_yaml_database_that_is_broken_names_the_file(tmp_path):
    path = tmp_path / "boards.yaml"
    assert f'"{path}", line 3' in database_error(tmp_path, "boards.yaml", "MY_BOARD:\n  a: [\n")
    assert "a board database is a table (mapping) of boards by name, not a list" in (
        database_error(tmp_path, "list.yaml", "- MY_BOARD\n")
    )
    assert "Board 'MY_BOARD' must be a table" in database_error(
        tmp_path, "scalar.yaml", "MY_BOARD: x\n", fpga={"part": "LFE5U-25F-6BG381C"}
    )
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"MY_BOARD: \xff\xfe\n")
    expected = (
        f'Cannot load board database "{binary}": not UTF-8 text: invalid start byte at byte 10'
    )
    with pytest.raises(ValueError) as direct:
        xeda.board.read_board_database(binary)
    assert str(direct.value) == expected
    with pytest.raises(FlowSettingsError) as raised:
        Nextpnr.Settings.from_input(
            {"board": "MY_BOARD", "custom_boards_file": "binary.yaml"}, design_root=tmp_path
        )
    assert expected in str(raised.value)


def test_an_empty_yaml_database_has_no_boards(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    assert xeda.board.read_board_database(path) == {}
    with pytest.raises(ValueError) as raised:
        xeda.board.get_board_data("MY_BOARD", path)
    assert str(raised.value) == f"Unknown board 'MY_BOARD' in {path}"


def test_a_missing_yaml_database_is_named(tmp_path):
    with pytest.raises(FlowSettingsError, match="Cannot read custom boards file"):
        Nextpnr.Settings.from_input(
            {"board": "MY_BOARD", "custom_boards_file": "missing.yaml"}, design_root=tmp_path
        )


def test_the_bundled_database_is_still_toml_and_loads():
    assert xeda.board.get_board_data("ulx3s_85f")["fpga"] == {"part": "LFE5U-85F-6BG381C"}
    names = [row["board"] for row in introspect.boards_info()]
    assert "ulx3s_85f" in names
    assert names == sorted(names)
    assert xeda.board.read_board_database(
        Path(xeda.board.__file__).parent / "data" / "boards.toml"
    ) == {
        row["board"]: {k: v for k, v in row.items() if k != "board"}
        for row in introspect.boards_info()
    }


def test_the_custom_boards_file_setting_says_both_formats_are_accepted():
    description = Nextpnr.Settings.model_fields["custom_boards_file"].description
    assert "TOML or YAML" in description


@pytest.mark.parametrize("entry", ["MY_BOARD:\n", "MY_BOARD: null\n", "MY_BOARD: ~\n"])
@pytest.mark.parametrize("fpga", [{}, {"fpga": {"part": "LFE5U-25F-6BG381C"}}])
def test_a_yaml_board_with_no_value_is_an_error_naming_board_and_database(tmp_path, entry, fpga):
    """`MY_BOARD:` is a null entry, only writable in YAML: neither no board nor a table. It is
    refused with or without an explicit `fpga`, never taken as a board with no data."""
    message = database_error(tmp_path, "boards.yaml", entry, **fpga)
    assert f"Board 'MY_BOARD' has no value in custom boards file {tmp_path / 'boards.yaml'}" in (
        message
    )
    assert "`{}` for a board with none" in message


@pytest.mark.parametrize("content", ["MY_BOARD: [a]\n", "MY_BOARD: text\n", "MY_BOARD: 3\n"])
def test_a_yaml_board_that_is_no_table_is_an_error(tmp_path, content):
    assert "Board 'MY_BOARD' must be a table" in database_error(tmp_path, "boards.yaml", content)


@pytest.mark.parametrize(
    "filename, content", [("boards.yaml", "MY_BOARD: {}\n"), ("boards.toml", "[MY_BOARD]\n")]
)
def test_a_board_with_an_empty_table_is_a_board_with_no_data_in_either_format(
    tmp_path, filename, content
):
    path = tmp_path / filename
    path.write_text(content)
    settings = Nextpnr.Settings.from_input(
        {
            "board": "MY_BOARD",
            "custom_boards_file": str(path),
            "fpga": {"part": "LFE5U-25F-6BG381C"},
        },
        design_root=tmp_path,
    )
    assert settings.board_data() == {}


def test_assigning_a_board_with_no_value_is_refused_and_changes_nothing(tmp_path):
    path = tmp_path / "boards.yaml"
    path.write_text("MY_BOARD:\n  fpga: LFE5U-25F-6BG381C\nEMPTY:\n")
    settings = Nextpnr.Settings.from_input(
        {"board": "MY_BOARD", "custom_boards_file": str(path)}, design_root=tmp_path
    )
    with pytest.raises(ValidationError, match="Board 'EMPTY' has no value"):
        settings.board = "EMPTY"
    assert settings.board == "MY_BOARD"
    assert settings.fpga.part == "LFE5U-25F-6BG381C"
