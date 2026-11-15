"""The results tables of the command line show each metric with its unit.

The descriptor type of a metric decides how its value shows (`units.integer_with_unit`,
`units.number_text`), never the Python type. The stored values do not change.
"""

import io
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner
from rich.console import Console

import xeda._internal.cli as cli_module
from xeda._internal import cli_utils
from xeda._internal.flow.sim import readable_time
from xeda._internal.records import MetricInfo
from xeda._internal.units import integer_with_unit, number_text
from xeda._internal.workspace import Workspace

pytestmark = pytest.mark.python_compat


@pytest.mark.parametrize(
    ("value", "unit", "text", "shown_unit"),
    [
        (30_000_000_000, "fs", "30", "us"),
        (30_000_000_001, "fs", "30.000000001", "us"),
        (125_000, "fs", "125", "ps"),
        (1500, "fs", "1.5", "ps"),
        (999, "fs", "999", "fs"),
        (-1500, "fs", "-1.5", "ps"),
        (10**19 + 1, "fs", "10,000.000000000000001", "s"),
        (3600 * 10**15, "fs", "3,600", "s"),
        (0, "fs", "0", "fs"),
        (0, "ns", "0", "ns"),
        (1500, "ns", "1.5", "us"),
        (1500, "µs", "1.5", "ms"),
        (2_500, "µW", "2.5", "mW"),
        (1_234_567, "um", "1,234.567", "mm"),
        (1500, "MHz", "1.5", "GHz"),
        (2000, "GHz", "2,000", "GHz"),
        (1500, "nW", "1.5", "uW"),
        (1500, "fF", "1.5", "pF"),
        (1500, "mV", "1.5", "V"),
        (1500, "kohm", "1.5", "Mohm"),
        (1500, "min", "1,500", "min"),
        (1500, "weird", "1,500", "weird"),
        (1500, "um^2", "1,500", "um^2"),
        (1234567, None, "1,234,567", ""),
    ],
)
def test_an_integer_shows_in_the_largest_unit_that_keeps_it_exact(value, unit, text, shown_unit):
    assert integer_with_unit(value, unit) == (text, shown_unit)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (225.98870056497177, "225.989"),
        (10.0, "10"),
        (10, "10"),
        (-0.123, "-0.123"),
        (0.00023, "0.00023"),
        (2.3e-4, "0.00023"),
        (1234567.891, "1,234,570"),
        (1e-10, "1e-10"),
        (-1e-10, "-1e-10"),
        (1e13, "1e+13"),
        (1e-9, "0.000000001"),
        (0.0, "0"),
        (-0.0, "0"),
        (123456789012.0, "123,457,000,000"),
        (999999999999.6, "1,000,000,000,000"),
        (1e12, "1e+12"),
        (1234567, "1,234,570"),
        (10**12, "1e+12"),
        (10**400, "1e+400"),
        (-(10**400), "-1e+400"),
    ],
)
def test_a_number_shows_six_significant_digits(value, text):
    assert number_text(value) == text


@pytest.mark.parametrize(
    ("time", "unit", "text"),
    [(1500, "1fs", "1.5 ps"), (3, "1ns", "3 ns"), (3600 * 10**15, "1fs", "3,600 s")],
)
def test_a_simulated_time_follows_the_same_rule(time, unit, text):
    assert readable_time(time, unit) == text


def _info(path, kind, unit=None):
    return MetricInfo(path=path, description="d", type=kind, unit=unit)


def _table(monkeypatch, results, info, **options):
    """The rows of the table that `print_results` prints, as lists of cell text."""
    printed = []
    monkeypatch.setattr(cli_utils.console, "print", lambda *args: printed.extend(args))
    cli_utils.print_results(results, metric_info=info, **options)
    table = printed[0]
    columns = [[str(cell) for cell in column._cells] for column in table.columns]
    return list(zip(*columns))


RESULTS = {
    "success": True,
    "runtime": 3.2,
    "fmax": 225.98870056497177,
    "sim.time": 30_000_000_000,
    "lut": 12345,
    "power.device.components": {"clocks": 0.00023, "logic": 1.5},
    "cell_types": {"LUT4": 1200, "DFF": 7},
    "equivalent": True,
    "note": "[bold]x[/bold]",
    "slack": 2.0,
    "clock_period": 10,
    "unused": 0,
    "failures": 0,
}
INFO = {
    "fmax": _info("fmax", "number", "MHz"),
    "sim.time": _info("sim.time", "integer", "fs"),
    "lut": _info("lut", "integer"),
    "power.device.components": _info("power.device.components", "numbers", "W"),
    "cell_types": _info("cell_types", "counts"),
    "equivalent": _info("equivalent", "boolean"),
    "note": _info("note", "text"),
    "clock_period": _info("clock_period", "number", "ns"),
}


def test_the_table_has_a_unit_column(monkeypatch):
    rows = _table(monkeypatch, RESULTS, INFO)
    assert ("fmax", "225.989", "MHz") in rows
    assert ("sim.time", "30", "us") in rows
    assert ("lut", "12,345", "") in rows
    assert ("power.device.components:", "", "") in rows
    assert (" clocks", "0.00023", "W") in rows
    assert (" logic", "1.5", "W") in rows
    assert ("cell_types:", "", "") in rows
    assert (" LUT4", "1,200", "") in rows
    assert (" DFF", "7", "") in rows
    assert ("equivalent", "True", "") in rows
    # Text is literal, never markup.
    assert ("note", "[bold]x[/bold]", "") in rows
    # A key without a descriptor shows as it always did.
    assert ("slack", "2.000", "") in rows
    # An integer-valued `number` metric shows with its unit.
    assert ("clock_period", "10", "ns") in rows
    assert ("Run time", "0:00:03", "") in rows
    assert rows[0][0] == "Status"


def test_the_subset_and_the_skip_rule_still_select_rows(monkeypatch):
    rows = _table(monkeypatch, RESULTS, INFO, subset=["fmax", "lut"])
    assert [row[0] for row in rows] == ["Status", "fmax", "lut"]


def test_the_skip_rule_hides_a_false_row_only_where_it_applies(monkeypatch):
    shown = ["fmax", "unused", "failures"]
    rows = _table(monkeypatch, RESULTS, INFO, subset=shown)
    assert [row[0] for row in rows] == ["Status", "fmax", "unused", "failures"]
    rows = _table(monkeypatch, RESULTS, INFO, subset=shown, skip_if_false={"unused"})
    assert [row[0] for row in rows] == ["Status", "fmax", "failures"]
    rows = _table(monkeypatch, RESULTS, INFO, subset=shown, skip_if_false=True)
    assert [row[0] for row in rows] == ["Status", "fmax"]


def test_a_long_value_is_not_cut(monkeypatch):
    console = Console(file=io.StringIO(), width=30, force_terminal=False)
    monkeypatch.setattr(cli_utils, "console", console)
    cli_utils.print_results(
        {"sim.time": 30_000_000_001, "note": "x" * 50},
        metric_info={"sim.time": INFO["sim.time"], "note": INFO["note"]},
    )
    shown = console.file.getvalue()
    assert "…" not in shown
    assert shown.count("x") == 50


def test_the_run_tables_pass_the_descriptors_of_the_record(monkeypatch):
    info = {"fmax": INFO["fmax"]}
    record = SimpleNamespace(passed=False, metric_info=info)
    node = SimpleNamespace(record=record, flow="f")
    seen = []
    monkeypatch.setattr(cli_utils, "results_of", lambda record: {"fmax": 1.0, "artifacts": {}})
    monkeypatch.setattr(cli_utils, "print_results", lambda results, **options: seen.append(options))
    cli_utils.print_run_tables(SimpleNamespace(nodes={"n": node}), "d")
    assert seen[0]["metric_info"] is info


def test_a_remote_run_shows_the_units_too(tmp_path, monkeypatch):
    """The remote branch of `xeda run` prints the record that came back, with its descriptors."""
    config = tmp_path / "cfg" / "xeda"
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.delenv("XEDA_RUN_ROOT", raising=False)
    monkeypatch.delenv("XEDA_REMOTE", raising=False)
    monkeypatch.chdir(tmp_path)
    (config / "config.yaml").write_text("{}\n")
    (config / "remotes.yaml").write_text(yaml.safe_dump({"hosts": {"venona": {"root": "~/xeda"}}}))
    design = tmp_path / "design.yaml"
    design.write_text("name: demo\nrtl:\n  sources: []\n  top: top\n")
    info = {"fmax": INFO["fmax"]}
    record = SimpleNamespace(passed=True, metric_info=info)
    result = SimpleNamespace(
        error=None,
        roots=["n"],
        nodes={"n": SimpleNamespace(record=record)},
        record=record,
        passed=True,
    )
    seen = []
    monkeypatch.setattr(Workspace, "run", lambda self, what, /, **options: result)
    monkeypatch.setattr(cli_module, "print_results", lambda results, **opts: seen.append(opts))
    monkeypatch.setattr(cli_module, "results_of", lambda record: {"fmax": 1.0})
    answer = CliRunner().invoke(
        cli_module.cli, ["run", "yosys_fpga", str(design), "--remote", "venona"]
    )
    assert answer.exit_code == 0, answer.output
    assert seen[0]["metric_info"] is info
