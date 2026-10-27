"""One setting, one value on the command line.

A setting that two items of `-s` give with different values is an error that names the items.
The same item twice is one item.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.flow import FlowSettingsError
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.error_codes import settings_codes
from xeda.flow_runner.settings_layers import registered_flow, split_flow_sections

PART = "xc7a100tfgg676-3"
ECP5 = "LFE5U-25F-6BG381C"
TWO_VALUES = "set one setting to two values; give one"


@pytest.fixture
def design_file(tmp_path: Path) -> Path:
    (tmp_path / "top.sv").write_text("module top(input clk); endmodule\n")
    path = tmp_path / "design.yaml"
    path.write_text(
        json.dumps({"name": "d", "rtl": {"sources": ["top.sv"], "top": "top", "clocks": ["clk"]}})
    )
    return path


def _run(design_file: Path, *args: str, flow: str = "yosys_fpga"):
    result = CliRunner().invoke(
        cli,
        ["run", "--dry-run", "--json", "--run-root", str(design_file.parent / "run"), flow]
        + [str(design_file), *args],
    )
    return result, json.loads(result.stdout)


def _planned(design_file: Path, *items: str, flow: str = "yosys_fpga"):
    return DefaultRunner(design_file.parent / "run", display_results=False).plan(
        flow, Design.load(design_file), xedaproject="", flow_settings=list(items)
    )


def _refused(design_file: Path, *items: str, flow: str = "yosys_fpga") -> FlowSettingsError:
    with pytest.raises(FlowSettingsError) as raised:
        _planned(design_file, *items, flow=flow)
    return raised.value


def _messages(error: FlowSettingsError) -> list[str]:
    return [message for _location, message, _context, _kind in error.errors]


def test_a_setting_given_twice_with_two_values_is_an_error(design_file):
    result, document = _run(design_file, "-s", "fpga.part=A", "-s", "fpga.part=B")
    assert result.exit_code == 2, result.output
    assert document["outcome"] is None and document["nodes"] == []
    assert document["error"]["code"] == "conflicting_keys"
    assert f"`-s fpga.part=A` and `-s fpga.part=B` {TWO_VALUES}" in document["error"]["message"]
    error = _refused(design_file, "fpga.part=A", "fpga.part=B")
    assert [location for location, *_rest in error.errors] == ["fpga.part"]
    assert settings_codes(error)[0] == "conflicting_keys"


def test_the_same_setting_twice_in_one_option_is_an_error_too(design_file):
    result, document = _run(design_file, "-s", "fpga.part=A", "fpga.part=B")
    assert result.exit_code == 2, result.output
    assert document["error"]["code"] == "conflicting_keys"
    assert f"`-s fpga.part=A` and `-s fpga.part=B` {TWO_VALUES}" in document["error"]["message"]


def test_the_error_quotes_the_items_as_written_for_a_qualified_key(design_file):
    qualified = "flows.yosys_fpga.fpga.part"
    error = _refused(design_file, f"{qualified}=A", f"{qualified}=B")
    assert _messages(error) == [f"`-s {qualified}=A` and `-s {qualified}=B` {TWO_VALUES}"]


def test_the_error_for_a_producer_quotes_the_items_as_written(design_file):
    error = _refused(
        design_file,
        f"fpga.part={ECP5}",
        "flows.nextpnr.seed=1",
        "flows.nextpnr.seed=2",
        flow="fpga_pack",
    )
    assert _messages(error) == [
        f"`-s flows.nextpnr.seed=1` and `-s flows.nextpnr.seed=2` {TWO_VALUES}"
    ]


def test_an_exact_repeat_is_one_item(design_file):
    plan = _planned(design_file, f"fpga.part={PART}", f"fpga.part={PART}")
    assert plan.node("yosys_fpga").settings.fpga.part == PART


def test_three_values_give_one_error_with_every_pair(design_file):
    error = _refused(design_file, "fpga.part=A", "fpga.part=B", "fpga.part=C")
    assert _messages(error) == [
        f"`-s fpga.part=A` and `-s fpga.part=B` {TWO_VALUES}",
        f"`-s fpga.part=A` and `-s fpga.part=C` {TWO_VALUES}",
        f"`-s fpga.part=B` and `-s fpga.part=C` {TWO_VALUES}",
    ]


def test_every_conflicting_setting_is_in_the_one_error(design_file):
    error = _refused(
        design_file, "fpga.part=A", "verbose=1", "fpga.part=B", "verbose=2", "fpga.part=A"
    )
    assert _messages(error) == [
        f"`-s fpga.part=A` and `-s fpga.part=B` {TWO_VALUES}",
        f"`-s verbose=1` and `-s verbose=2` {TWO_VALUES}",
    ]


def test_values_that_differ_as_text_are_two_values(design_file):
    error = _refused(design_file, "verbose=1", "verbose=01")
    assert _messages(error) == [f"`-s verbose=1` and `-s verbose=01` {TWO_VALUES}"]


def test_one_leaf_of_a_nested_setting_is_one_setting(design_file):
    error = _refused(
        design_file,
        f"fpga.part={PART}",
        "clocks.clk.period=5ns",
        "clocks.clk.period=6ns",
    )
    assert _messages(error) == [
        f"`-s clocks.clk.period=5ns` and `-s clocks.clk.period=6ns` {TWO_VALUES}"
    ]


def test_two_leaves_of_one_table_are_not_a_repeat(design_file):
    """The clock model judges `period` with `freq`; the rule here is about one leaf."""
    try:
        _planned(
            design_file,
            f"fpga.part={PART}",
            "clocks.clk.period=5ns",
            "clocks.clk.freq=100MHz",
        )
    except FlowSettingsError as error:
        assert TWO_VALUES not in " ".join(_messages(error))


def test_the_two_spellings_keep_their_message(design_file):
    error = _refused(design_file, "fpga.part=A", "flows.yosys_fpga.fpga.part=B")
    assert _messages(error) == [
        f"`-s fpga.part=A` and `-s flows.yosys_fpga.fpga.part=B` {TWO_VALUES}"
    ]
    _planned(design_file, f"fpga.part={PART}", f"flows.yosys_fpga.fpga.part={PART}")


def test_one_setting_in_two_nodes_is_not_a_repeat():
    sections, own = split_flow_sections(
        ["flows.nextpnr.seed=1", "flows.yosys_fpga.verbose=2", "flows.nextpnr.verbose=2"],
        "nextpnr",
        registered_flow,
    )
    assert sections["nextpnr"] == {"seed": 1, "verbose": 2}
    assert sections["yosys_fpga"] == {"verbose": 2}
    assert own == {}


def test_the_clock_shorthand_and_a_qualified_clock_are_one_setting(design_file):
    """`clock.period` is `clocks.main_clock.period`, qualified or not."""
    error = _refused(
        design_file, f"fpga.part={ECP5}", "clock.period=5ns", "flows.yosys_fpga.clock.period=6ns"
    )
    assert settings_codes(error)[0] == "conflicting_keys"
    assert (
        f"`-s clock.period=5ns` and `-s flows.yosys_fpga.clock.period=6ns` {TWO_VALUES}"
        in _messages(error)[0]
    )
