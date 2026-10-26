"""The model of the metrics: simulated time, temperatures, lists and tables of cell counts."""

import pytest

from xeda.flow.metrics import metric_unit_known
from xeda.flows import Bsc, Nextpnr, VivadoPower, Yosys, YosysFpga

from .metric_types import paths_inside_paths
from .test_documentation import FLOW_CLASSES, FLOW_IDS

pytestmark = pytest.mark.python_compat


def test_a_temperature_and_a_temperature_difference_have_different_units():
    described = VivadoPower.results_description
    assert described["power.junction_temperature"].unit == "degC"
    assert described["power.thermal_margin"].unit == "delta_degC"
    assert described["power.effective_tja"].unit == "K/W"
    assert all(metric_unit_known(unit) for unit in ("degC", "delta_degC", "K/W"))


def test_the_bsc_hierarchy_is_a_text_list():
    assert Bsc.results_description["modules"].type == "text_list"


def test_cell_counts_are_one_table_and_cells_stays_the_total():
    assert Yosys.results_description["cells"].type == "integer"
    assert Yosys.results_description["cell_types"].type == "counts"
    assert Nextpnr.results_description["cell_types"].type == "counts"
    assert YosysFpga.results_description["cell_types"].type == "counts"


def test_yosys_fpga_describes_its_resource_sums():
    described = YosysFpga.results_description
    for path in ("latch", "bram.ramb18", "bram.ramb36", "dsp"):
        assert described[path].type == "integer", path
    for old in ("LATCH", "RAMB18", "RAMB36", "DSP"):
        assert old not in described


@pytest.mark.parametrize("flow_class", FLOW_CLASSES, ids=FLOW_IDS)
def test_no_described_path_is_inside_another(flow_class):
    """A path that is the start of another path cannot nest into one object: `bram` and
    `bram.ramb18` would be a value and an object at one place."""
    inside = paths_inside_paths(flow_class.results_description)
    assert not inside, f"{flow_class.name}: {inside}"


def test_the_oracle_finds_a_path_inside_another():
    assert paths_inside_paths(["bram", "bram.ramb18", "bramx", "lut_use.ram"]) == [
        ("bram", "bram.ramb18")
    ]
    assert not paths_inside_paths(["bram.ramb18", "bram.ramb36", "bram_x"])
