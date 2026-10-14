"""Every bundled data file must actually load through the models that read it.

`test_documentation.py` builds each flow's *schema*, so nothing else constructs a flow setting
from the shipped data unless a test does. These tests do.
"""

import pytest

from xeda.introspect import boards_info
from xeda.platforms import bundled_platform_names


@pytest.mark.parametrize("name", bundled_platform_names())
def test_asic_flow_settings_accept_every_bundled_platform(name):
    """The path a real `xeda run yosys` takes."""
    from xeda.flows import Yosys

    settings = Yosys.Settings(platform=name)
    assert settings.platform is not None


def test_every_bundled_board_is_usable_as_a_flow_setting():
    from xeda.board import WithFpgaBoardSettings

    boards = [b["board"] for b in boards_info()]
    if not boards:
        pytest.skip("no bundled boards available")
    for board in boards:
        WithFpgaBoardSettings(board=board, clock={"period": "10 ns"})
