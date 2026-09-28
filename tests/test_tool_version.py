"""How a tool's version compares with the version a flow requires (`Tool.minimum_version`)."""

import pytest

from xeda.tool import Tool


@pytest.mark.parametrize(
    "version, required, expected",
    [
        (("2026", "07", "1"), (2026, 7, 1), True),
        (("2026", "07", "2"), (2026, 7, 1), True),
        (("2027", "01"), (2026, 7, 1), True),
        # a component the tool's version lacks is 0: 2026.07 is before 2026.07.1
        (("2026", "07"), (2026, 7, 1), False),
        (("2026", "07"), (2026, 7, 0), True),
        (("0", "36"), (0, 21), True),
        (("0", "9"), (0, 21), False),
        (("5",), (5, 0), True),
        (("1", "6", "0", "dev"), (1, 6), True),
        # a version that could not be read is not compared
        ((), (2026, 7, 1), True),
    ],
)
def test_version_is_gte(version, required, expected):
    assert Tool._version_is_gte(version, required) is expected
