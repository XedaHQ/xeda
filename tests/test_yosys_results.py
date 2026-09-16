"""The `yosys` flow's declared result contract against a real `stat -json` report.

`results_description` is what `xeda list-results yosys` promises a script will find in
`results.json`. `cells` and `sequential_cells` were both advertised while `parse_reports`
wrote neither, so the machine-readable contract named values that never appeared. These tests
pin the contract to what the parser actually delivers, using a report captured verbatim from
yosys rather than one hand-written to match the parser.
"""

import json
import shutil
from pathlib import Path

import pytest

from xeda import Design
from xeda.flows import Yosys
from xeda.introspect import results_info

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"

#: A liberty-mapped `stat -json` report, as the ASIC `yosys` flow produces it.
STAT_REPORT = RESOURCES_DIR / "yosys" / "stat_liberty.json"


@pytest.fixture
def parsed_flow(tmp_path: Path) -> Yosys:
    report = tmp_path / "reports" / "utilization.json"
    report.parent.mkdir(parents=True)
    shutil.copy(STAT_REPORT, report)
    design = Design.from_toml(RESOURCES_DIR / "design0" / "design0.toml")
    flow = Yosys(Yosys.Settings(clock_period=10.0), design, tmp_path)
    flow.init()
    # `get_utilization` opens the artifact path as given, so make it absolute rather than
    # depending on the process working directory.
    flow.artifacts.utilization_report = str(report.resolve())
    assert flow.parse_reports()
    return flow


def test_every_documented_key_is_actually_reported(parsed_flow: Yosys):
    """A key in `results_description` that `parse_reports` never writes is a false promise."""
    # `common` marks the keys the runner fills in (success, runtime, run_path, ...); everything
    # else is what this flow's own parse_reports() promised.
    flow_specific = [k["name"] for k in results_info(Yosys)["keys"] if not k["common"]]
    undelivered = sorted(k for k in flow_specific if parsed_flow.results.get(k) is None)
    assert not undelivered, (
        f"`xeda list-results yosys` advertises {undelivered}, but parse_reports() does not "
        "write them to results.json."
    )


def test_cells_comes_from_the_report(parsed_flow: Yosys):
    expected = json.loads(STAT_REPORT.read_text())["design"]["num_cells"]
    assert parsed_flow.results.get("cells") == expected


def test_area_comes_from_the_report(parsed_flow: Yosys):
    expected = json.loads(STAT_REPORT.read_text())["design"]["area"]
    assert parsed_flow.results.get("area") == pytest.approx(expected)


def test_per_cell_type_counts_are_reported(parsed_flow: Yosys):
    """`num_cells_by_type` is splatted into the results, keyed by liberty cell name."""
    by_type = json.loads(STAT_REPORT.read_text())["design"]["num_cells_by_type"]
    for cell, count in by_type.items():
        assert parsed_flow.results.get(cell) == count, cell
