"""Board selection is strict in the selected database and safe for archive resources."""

from copy import deepcopy

import pytest

import xeda.board
from xeda.board import WithFpgaBoardSettings, get_board_data
from xeda.dataclass import ValidationError
from xeda.flow import FlowSettingsError


@pytest.mark.parametrize("explicit", [False, True])
def test_unknown_bundled_board_names_database_and_suggestions(explicit):
    values = {"board": "ULX3S_85"}
    if explicit:
        values["fpga"] = {"part": "LFE5U-25F-6BG381C"}
    with pytest.raises(FlowSettingsError) as raised:
        WithFpgaBoardSettings.from_input(values)
    message = str(raised.value)
    assert "Unknown board" in message and "ULX3S_85F" in message and "boards.toml" in message


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
    for name in ("PRIVAT", "ULX3S_85F"):
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
    assert get_board_data("ULX3S_85F")["fpga"]["part"] == "LFE5U-85F-6BG381C"


@pytest.mark.parametrize("size", [35, 100])
def test_arty_boards_have_active_pin_only_resources(size):
    settings = WithFpgaBoardSettings.from_input({"board": f"ARTY_A7_{size}T"})
    assert settings.fpga.part == f"xc7a{size}tcsg324-1"
    assert settings.board_data()["name"] == f"arty_a7_{size}t"
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
    settings = WithFpgaBoardSettings.from_input({"board": "ULX3S_85F"})
    assert settings.board_data()["lpf"] == "boards/ulx3s/board.lpf"
    with settings.board_file(settings.board_data()["lpf"]) as path:
        assert 'FREQUENCY PORT "clk_25mhz" 25 MHZ;' in path.read_text()
