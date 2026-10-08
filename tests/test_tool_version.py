"""How a tool's version compares with the version a flow requires (`Tool.minimum_version`)."""

from typing import ClassVar

import pytest

from xeda.tool import Tool, ToolException


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


def test_a_tool_older_than_its_floor_is_refused_with_the_version_found_and_the_version_needed(
    monkeypatch,
):
    monkeypatch.setattr(Tool, "probe_stdout", lambda self, *args, **kwargs: "sometool 1.5.2\n")
    with pytest.raises(ToolException) as raised:
        Tool(executable="sometool", minimum_version=(2, 0))
    assert str(raised.value) == (
        "Minimum version not met: sometool 1.5.2 was found. Xeda needs 2.0 or newer."
    )


def test_a_tool_that_says_why_its_floor_is_what_it_is_gives_the_reason_in_the_message(monkeypatch):
    class Reasoned(Tool):
        minimum_version_reason: ClassVar[str] = "Older ones lack the thing."

    monkeypatch.setattr(Tool, "probe_stdout", lambda self, *args, **kwargs: "sometool 1.5.2\n")
    with pytest.raises(ToolException, match=r"needs 2\.0 or newer\. Older ones lack the thing\.$"):
        Reasoned(executable="sometool", minimum_version=(2, 0))


def test_a_tool_of_the_floor_or_newer_is_accepted(monkeypatch):
    monkeypatch.setattr(Tool, "probe_stdout", lambda self, *args, **kwargs: "sometool 2.0.1\n")
    assert Tool(executable="sometool", minimum_version=(2, 0)).version_str == "2.0.1"


def test_a_derived_tool_is_checked_by_the_same_rule(monkeypatch):
    """`Tool.derive` copies a model and runs no constructor: the flow asks for the check."""
    monkeypatch.setattr(Tool, "probe_stdout", lambda self, *args, **kwargs: "other 1.0\n")
    derived = Tool(executable="sometool", version_flag=None).derive(
        "other", version_flag=["--version"], minimum_version=(5, 24)
    )
    with pytest.raises(ToolException, match=r"Minimum version not met: other 1\.0 was found"):
        derived.require_minimum_version()
