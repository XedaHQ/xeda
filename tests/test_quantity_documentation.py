"""The section on physical quantities in `docs/flows.rst` is executable.

The section tells a user which units each kind takes, what Xeda writes into `settings.json`, how
the file below loads, and what each error says. Each test reads one of these facts from the
document and checks it against the code, so the text cannot drift from what the code accepts.
"""

import re
import textwrap
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import TypeAdapter

from xeda import Design
from xeda.dataclass import ValidationError
from xeda.flow import flowrun_hash
from xeda.flows import Nextpnr, Yosys
from xeda.units import (
    Bounds,
    Capacitance,
    Frequency,
    Length,
    Quantity,
    Resistance,
    Time,
    Voltage,
)

FLOWS_RST = Path(__file__).parent.parent / "docs/flows.rst"
KINDS: tuple[type[Quantity], ...] = (Time, Frequency, Capacitance, Length, Voltage, Resistance)


def _section() -> str:
    text = FLOWS_RST.read_text(encoding="utf-8")
    return text[text.index(".. _quantities:") : text.index(".. _settings-groups:")]


def _blocks(language: str) -> list[str]:
    found = re.findall(rf"^\.\. code-block:: {language}\n\n((?:(?:    .*)?\n)+)", _section(), re.M)
    return [textwrap.dedent(body).strip() for body in found]


def _kind_table() -> dict[str, tuple[set[str], str]]:
    """The rows of the table of kinds: the units, and the unit that `settings.json` writes."""
    rows = re.findall(r"^   \* - (\w+)\n     - (.+)\n     - ``(\S+)``$", _section(), re.M)
    return {
        kind: (set(re.findall(r"``([^`]+)``", units)), written) for kind, units, written in rows
    }


def test_the_table_of_kinds_is_what_the_types_accept() -> None:
    table = _kind_table()
    assert set(table) == {cls.kind for cls in KINDS}
    for cls in KINDS:
        units, written = table[cls.kind]
        assert units == set(cls.units), cls.kind
        assert written == cls.canonical_unit, cls.kind


@pytest.mark.parametrize("cls", KINDS)
def test_a_value_in_each_documented_unit_is_read_and_written_in_the_unit_of_the_table(cls) -> None:
    units, written = _kind_table()[cls.kind]
    for unit in units:
        value = TypeAdapter(cls).validate_python(f"2 {unit}")
        assert isinstance(value, cls)
        assert value.to(unit) == 2
        assert TypeAdapter(cls).dump_python(value, mode="json").endswith(f" {written}")


def test_the_documented_error_messages_are_the_ones_the_code_writes() -> None:
    inputs: list[tuple[object, object]] = [
        (Time, 10),
        (Time, "5 MHz"),
        (Capacitance, "3.9 ff"),
        (Time, "5 ns * 2"),
        (Time, "10 nanoseconds"),
        (Annotated[Time, Bounds(gt="0 ns")], "-1 ns"),
    ]
    written = []
    for annotation, value in inputs:
        with pytest.raises(ValidationError) as caught:
            TypeAdapter(annotation).validate_python(value)
        (error,) = caught.value.errors()
        written.append(error["msg"])
    assert _blocks("text") == written


def test_the_documented_design_file_gives_the_values_the_text_says(tmp_path: Path) -> None:
    (text,) = _blocks("yaml")
    assert text.startswith("# clock-units.yaml\n")
    (tmp_path / "top.v").write_text("module top(input clk); endmodule\n")
    (tmp_path / "clock-units.yaml").write_text(text + "\n")
    flows = Design.from_file(tmp_path / "clock-units.yaml").flow
    yosys = Yosys.Settings.from_input(flows["yosys"], design_root=tmp_path)
    assert yosys.clocks["main_clock"].period == Time("2.5 ns")
    assert yosys.abc.delay_target == Time("500 ps")
    assert yosys.abc.load == Capacitance("3.9 fF")
    nextpnr = Nextpnr.Settings.from_input(flows["nextpnr"], design_root=tmp_path)
    assert nextpnr.clocks["main_clock"].period == Time("2.5 ns")


def test_spellings_of_one_value_are_one_run() -> None:
    """The section says that `10 ns`, `10000 ps` and `0.01 us` are one time, and that a clock
    given as `100 MHz` is the clock given as `10 ns`."""
    spellings = [
        {"clock": {"period": "10 ns"}},
        {"clock": {"period": "10000 ps"}},
        {"clock": {"period": "0.01 us"}},
        {"clock": {"freq": "100 MHz"}},
    ]
    hashes = {
        flowrun_hash("yosys", Yosys.Settings.from_input(spelling), "design")
        for spelling in spellings
    }
    assert len(hashes) == 1
    other = Yosys.Settings.from_input({"clock": {"period": "10.5 ns"}})
    assert flowrun_hash("yosys", other, "design") not in hashes
    written = Yosys.Settings.from_input(spellings[1]).model_dump(mode="json")
    assert written["clocks"]["main_clock"]["period"] == "10 ns"
