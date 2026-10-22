"""A unit written after a space on the command line gets a hint.

`-s clock.period=5.5 ns` ends the list of settings at `ns`, and click reports an extra argument.
That message alone does not say that the unit must follow the number without a space.
"""

import json

import click
import pytest
from click.testing import CliRunner

from xeda.cli import cli

pytestmark = pytest.mark.python_compat


def _invoke(*arguments: str):
    result = CliRunner().invoke(cli, list(arguments))
    return result, click.unstyle(result.output)


@pytest.fixture(name="design")
def design_fixture(tmp_path) -> str:
    path = tmp_path / "d.yaml"
    path.write_text("name: d\n")
    return str(path)


def test_a_unit_after_a_space_is_explained(design):
    result, text = _invoke("run", "yosys_fpga", design, "-s", "clock.period=5.5", "ns")

    assert result.exit_code == 2, text
    assert "unexpected extra argument (ns)" in text
    assert "Write the unit without a space (-s clock.period=5.5ns)" in text
    assert 'or quote the item (-s "clock.period=5.5 ns")' in text


def test_the_hint_names_the_option_that_was_typed(design):
    _, text = _invoke("run", "yosys_fpga", design, "--settings", "a=1", "clock.period=200", "MHz")

    assert "(--settings clock.period=200MHz)" in text


def test_the_error_document_has_the_hint_and_is_one_json_document(design):
    result = CliRunner().invoke(
        cli, ["run", "yosys_fpga", design, "-s", "clock.period=5.5", "ns", "--json"]
    )
    document = json.loads(result.stdout)

    assert result.exit_code == 2
    assert document["outcome"] is None and document["error"]["code"] == "usage_error"
    assert "Write the unit without a space" in document["error"]["message"]


@pytest.mark.parametrize(
    "tail",
    [
        ["-s", "clock.period=5.5", "nanoseconds"],  # not a unit of a quantity
        ["-s", "clock.period=5.5", "NS"],  # units are case-sensitive: no unit named NS
        ["-s", "clock.period=fast", "ns"],  # the item before it does not end in a number
        ["-s", "clock.period=5.5ns", "ns"],  # the number has its unit already
        ["ns"],  # no item before it
    ],
)
def test_other_extra_arguments_keep_the_plain_message(tail, design):
    _, text = _invoke("run", "yosys_fpga", design, *tail)

    assert "unexpected extra argument" in text
    assert "without a space" not in text
