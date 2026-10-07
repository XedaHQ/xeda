"""Board selection is strict in the selected database and safe for archive resources."""

import tomllib
from copy import deepcopy
from importlib.resources import files

import pytest

import xeda.board
from xeda.board import WithFpgaBoardSettings, get_board_data
from xeda.dataclass import ValidationError
from xeda.flow import FlowSettingsError


@pytest.mark.parametrize("explicit", [False, True])
def test_unknown_bundled_board_names_database_and_suggestions(explicit):
    values = {"board": "ulx3s_85"}
    if explicit:
        values["fpga"] = {"part": "LFE5U-25F-6BG381C"}
    with pytest.raises(FlowSettingsError) as raised:
        WithFpgaBoardSettings.from_input(values)
    message = str(raised.value)
    assert "Unknown board" in message and "ulx3s_85f" in message and "boards.toml" in message


def test_custom_database_replaces_bundled_and_preserves_explicit_fpga(tmp_path):
    database = tmp_path / "boards.toml"
    database.write_text('[PRIVATE]\nfpga.part = "LFE5U-25F-6BG381C"\nlpf = "pins.lpf"\n')
    values = {
        "board": "PRIVATE",
        "custom_boards_file": "boards.toml",
        "fpga": {"part": "LFE5U-85F-6BG381C"},
    }
    original = deepcopy(values)
    settings = WithFpgaBoardSettings.from_input(values, design_root=tmp_path)
    assert values == original
    assert settings.fpga.part == "LFE5U-85F-6BG381C"
    with settings.board_file(settings.board_data()["lpf"]) as path:
        assert path == tmp_path / "pins.lpf"
    assert (
        WithFpgaBoardSettings.from_input(settings.model_dump(mode="json")).model_dump()
        == settings.model_dump()
    )
    for name in ("PRIVAT", "ulx3s_85f"):
        with pytest.raises(FlowSettingsError) as raised:
            WithFpgaBoardSettings.from_input({**values, "board": name}, design_root=tmp_path)
        assert str(database) in str(raised.value)
        if name == "PRIVAT":
            assert "PRIVATE" in str(raised.value)
        else:
            assert "Did you mean" not in str(raised.value)
    saved = settings.model_dump()
    with pytest.raises(ValidationError, match="Unknown board"):
        settings.board = "missing"
    assert settings.model_dump() == saved


def test_bundled_lookup_reads_text_without_extraction(monkeypatch):
    def no_extraction(*args):
        raise AssertionError("board lookup must not extract a resource")

    monkeypatch.setattr(xeda.board, "as_file", no_extraction)
    assert get_board_data("ulx3s_85f")["fpga"]["part"] == "LFE5U-85F-6BG381C"


@pytest.mark.parametrize("size", [35, 100])
def test_arty_boards_have_active_pin_only_resources(size):
    settings = WithFpgaBoardSettings.from_input({"board": f"arty_a7_{size}t"})
    assert settings.fpga.part == f"xc7a{size}tcsg324-1"
    assert settings.board_data()["openfpgaloader_board"] == f"arty_a7_{size}t"
    with settings.board_file(settings.board_data()["xdc"]) as path:
        text = path.read_text()
    active = [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    assert active and all(line.startswith("set_property ") for line in active)
    assert not any("create_clock" in line for line in active)
    assert "[get_ports {CLK100MHZ}]" in text and "[get_ports {led[0]}]" in text
    assert "Digilent" in text


def test_ulx3s_uses_the_bundled_lpf():
    settings = WithFpgaBoardSettings.from_input({"board": "ulx3s_85f"})
    assert settings.board_data()["lpf"] == "boards/ulx3s/board.lpf"
    with settings.board_file(settings.board_data()["lpf"]) as path:
        assert 'FREQUENCY PORT "clk_25mhz" 25 MHZ;' in path.read_text()


# ------------------------------------------------------------------- names and letter case


@pytest.mark.parametrize(
    "spelling", ["arty_a7_100t", "ARTY_A7_100T", "Arty_A7_100T", "aRTY_a7_100t"]
)
def test_a_bundled_board_is_found_by_its_name_in_any_letter_case(spelling):
    settings = WithFpgaBoardSettings.from_input({"board": spelling})
    assert settings.board == "arty_a7_100t"  # stored under its one name
    assert settings.fpga is not None and settings.fpga.part == "xc7a100tcsg324-1"
    assert get_board_data(spelling) == get_board_data("arty_a7_100t")
    assert settings.board_data() == get_board_data("arty_a7_100t")


def test_the_spellings_of_a_bundled_board_make_one_setting():
    spellings = ("ULX3S_85F", "ulx3s_85f", "Ulx3s_85F")
    dumps = [WithFpgaBoardSettings.from_input({"board": name}).model_dump() for name in spellings]
    assert dumps[0] == dumps[1] == dumps[2]
    assert dumps[0]["board"] == "ulx3s_85f"
    # and the settings reload unchanged from what they dump
    reloaded = WithFpgaBoardSettings.from_input(dumps[0])
    assert reloaded.model_dump() == dumps[0]


def test_assigning_a_bundled_board_in_any_case_stores_its_one_name():
    settings = WithFpgaBoardSettings.from_input({"board": "ulx3s_85f"})
    settings.board = "ARTY_A7_35T"
    assert settings.board == "arty_a7_35t"
    assert settings.fpga is not None and settings.fpga.part == "xc7a35tcsg324-1"
    with pytest.raises(ValidationError, match="Unknown board 'Nope'"):
        settings.board = "Nope"  # the message keeps the spelling that was given
    assert settings.board == "arty_a7_35t"


def test_an_unknown_bundled_board_suggests_names_in_lower_case():
    with pytest.raises(FlowSettingsError, match=r"Did you mean 'ulx3s_85f'\?"):
        WithFpgaBoardSettings.from_input({"board": "ULX3S_85"})


@pytest.mark.parametrize("filename", ["boards.toml", "boards.yaml"])
def test_the_names_in_a_custom_database_are_case_sensitive(tmp_path, filename):
    database = tmp_path / filename
    if filename.endswith(".toml"):
        database.write_text('[Foo]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    else:
        database.write_text("Foo:\n  fpga:\n    part: LFE5U-25F-6BG381C\n")
    values = {"custom_boards_file": filename}
    found = WithFpgaBoardSettings.from_input({**values, "board": "Foo"}, design_root=tmp_path)
    assert found.board == "Foo"  # as written
    for other in ("foo", "FOO"):
        with pytest.raises(FlowSettingsError, match=f"Unknown board '{other}'.*{filename}"):
            WithFpgaBoardSettings.from_input({**values, "board": other}, design_root=tmp_path)


def test_a_custom_database_neither_folds_case_nor_sees_a_bundled_name(tmp_path):
    database = tmp_path / "boards.toml"
    database.write_text('[ARTY_A7_100T]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    values = {"custom_boards_file": "boards.toml"}
    found = WithFpgaBoardSettings.from_input(
        {**values, "board": "ARTY_A7_100T"}, design_root=tmp_path
    )
    assert found.board == "ARTY_A7_100T" and found.fpga is not None
    assert found.fpga.part == "LFE5U-25F-6BG381C"  # the custom board, not the bundled one
    with pytest.raises(FlowSettingsError, match="Unknown board 'arty_a7_100t'"):
        WithFpgaBoardSettings.from_input({**values, "board": "arty_a7_100t"}, design_root=tmp_path)


def test_the_bundled_database_names_its_boards_in_lower_case_and_none_twice():
    text = files("xeda.data").joinpath("boards.toml").read_text()
    names = list(tomllib.loads(text))
    assert names and all(name == name.lower() for name in names)
    assert len({name.lower() for name in names}) == len(names)
    assert list(xeda.board.bundled_boards()) == names


@pytest.mark.parametrize("keys", [["Foo"], ["foo", "Foo"], ["foo", "FOO"]])
def test_a_bundled_database_with_a_name_that_is_not_lower_case_is_refused(monkeypatch, keys):
    """Two names that differ only in case cannot be told apart by a lookup in any case."""
    text = "".join(f'[{key}]\nfpga.part = "LFE5U-25F-6BG381C"\n' for key in keys)
    monkeypatch.setattr(xeda.board, "toml_loads", lambda _text: tomllib.loads(text))
    with pytest.raises(ValueError, match="lower case"):
        xeda.board.bundled_boards()
    with pytest.raises(ValueError, match="lower case"):
        get_board_data("foo")


def test_a_board_entry_with_the_removed_name_key_says_what_replaces_it(tmp_path):
    database = tmp_path / "boards.toml"
    database.write_text('[MY_BOARD]\nname = "ulx3s"\nfpga.part = "LFE5U-25F-6BG381C"\n')
    with pytest.raises(FlowSettingsError) as raised:
        WithFpgaBoardSettings.from_input(
            {"board": "MY_BOARD", "custom_boards_file": "boards.toml"}, design_root=tmp_path
        )
    message = str(raised.value)
    assert (
        "MY_BOARD" in message
        and "`name` was removed" in message
        and "`openfpgaloader_board`" in message
    )


def test_list_boards_gives_lower_case_names_and_the_name_each_board_has_in_openfpgaloader():
    from xeda import introspect

    rows = introspect.boards_info()
    assert [row["board"] for row in rows] == sorted(row["board"] for row in rows)
    assert all(row["board"] == row["board"].lower() for row in rows)
    by_name = {row["board"]: row for row in rows}
    assert {name: row["openfpgaloader_board"] for name, row in by_name.items()} == {
        "arty_a7_100t": "arty_a7_100t",
        "arty_a7_35t": "arty_a7_35t",
        "basys_3": "basys3",
        "stlv7325_v2": "stlv7325",
        "ulx3s_85f": "ulx3s",
    }
    assert all("name" not in row for row in rows)


def test_the_list_boards_table_names_the_loader_column_for_what_it_is():
    import click
    from click.testing import CliRunner

    from xeda.cli import cli

    result = CliRunner().invoke(cli, ["list-boards"], env={"COLUMNS": "100"})
    text = click.unstyle(result.output)
    assert result.exit_code == 0, text
    assert "openFPGALoader board" in text and "arty_a7_100t" in text and "ARTY_A7_100T" not in text
    assert "basys3" in text and "stlv7325" in text


@pytest.mark.parametrize("value", ["5", '""', '"  "', '["basys3"]', "true"])
def test_a_board_entry_gives_openfpgaloader_board_as_text_or_not_at_all(tmp_path, value):
    database = tmp_path / "boards.toml"
    database.write_text(
        f'[MY_BOARD]\nopenfpgaloader_board = {value}\nfpga.part = "LFE5U-25F-6BG381C"\n'
    )
    with pytest.raises(FlowSettingsError) as raised:
        WithFpgaBoardSettings.from_input(
            {"board": "MY_BOARD", "custom_boards_file": "boards.toml"}, design_root=tmp_path
        )
    message = str(raised.value)
    assert "MY_BOARD" in message and "`openfpgaloader_board`" in message and "as text" in message


def test_a_board_entry_may_leave_openfpgaloader_board_out(tmp_path):
    database = tmp_path / "boards.toml"
    database.write_text('[MY_BOARD]\nfpga.part = "LFE5U-25F-6BG381C"\n')
    settings = WithFpgaBoardSettings.from_input(
        {"board": "MY_BOARD", "custom_boards_file": "boards.toml"}, design_root=tmp_path
    )
    assert settings.board_data() == {"fpga": {"part": "LFE5U-25F-6BG381C"}}
