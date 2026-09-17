"""Every bundled data file must actually load through the models that read it.

This is the gap that let `asap7` and `nangate45` become unloadable: `test_documentation.py`
builds each flow's *schema*, and `list-platforms` reads the TOML directly, so nothing ever
constructed an `AsicsPlatform` from the shipped PDK descriptions. pydantic v1 quietly truncated
their fractional values (a 22.4um halo became 22); v2 rejected the file outright and no test
noticed.
"""

import pytest

from xeda.introspect import boards_info

ASIC_PLATFORMS = ["asap7", "nangate45", "sky130hd", "sky130hs"]


@pytest.mark.parametrize("name", ASIC_PLATFORMS)
def test_bundled_asic_platform_loads(name):
    from xeda.platforms.asics import AsicsPlatform

    try:
        platform = AsicsPlatform.from_resource(name)
    except (FileNotFoundError, ModuleNotFoundError) as e:  # not shipped in the wheel
        pytest.skip(f"platform {name} not available: {e}")
    assert platform.root_dir


@pytest.mark.parametrize("name", ASIC_PLATFORMS)
def test_bundled_asic_platform_keeps_fractional_values(name):
    """`abc_load_in_ff` and the macro-placement margins are physical, not integral."""
    from xeda.platforms.asics import AsicsPlatform

    try:
        platform = AsicsPlatform.from_resource(name)
    except (FileNotFoundError, ModuleNotFoundError) as e:
        pytest.skip(f"platform {name} not available: {e}")
    for value in [
        platform.abc_load_in_ff,
        *platform.macro_place_halo,
        *platform.macro_place_channel,
    ]:
        if value is not None:
            assert isinstance(value, float)
    if name == "nangate45":
        assert platform.macro_place_halo == [22.4, 15.12], "PDK value was truncated"


@pytest.mark.parametrize("name", ASIC_PLATFORMS)
def test_asic_flow_settings_accept_every_bundled_platform(name):
    """The path a real `xeda run openroad` takes."""
    from xeda.flows.openroad import Openroad

    try:
        settings = Openroad.Settings(platform=name, clock_period=5.0)
    except (FileNotFoundError, ModuleNotFoundError) as e:
        pytest.skip(f"platform {name} not available: {e}")
    assert settings.platform is not None


def test_every_bundled_board_is_usable_as_a_flow_setting():
    from xeda.board import WithFpgaBoardSettings

    boards = [b["board"] for b in boards_info()]
    if not boards:
        pytest.skip("no bundled boards available")
    for board in boards:
        WithFpgaBoardSettings(board=board, clock_period=10.0)
