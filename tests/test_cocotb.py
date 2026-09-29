import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest

from xeda import Cocotb, Design
from xeda.cocotb import TestResults as CocotbTestResults  # alias: pytest collects `Test*`
from xeda.flow import SimFlow
from xeda.flow_runner import DefaultRunner
from xeda.flows import __builtin_flows__
from xeda.utils import WorkingDirectory

from .tool_utils import require_cocotb, require_ghdl, require_nvc, require_verilator

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"

#: Every flow that runs cocotb testbenches, by name.
COCOTB_FLOWS = sorted(
    cls.name for cls in __builtin_flows__ if getattr(cls, "cocotb_sim_name", None)
)

#: A one-gate design (y = not a) in the language each cocotb simulator reads.
_INVERTER = {
    "vhdl": (
        "dut.vhdl",
        "library ieee;\nuse ieee.std_logic_1164.all;\n"
        "entity dut is\n  port (a : in std_logic; y : out std_logic);\nend entity;\n"
        "architecture rtl of dut is\nbegin\n  y <= not a;\nend architecture;\n",
    ),
    "verilog": ("dut.sv", "module dut(input logic a, output logic y); assign y = ~a; endmodule\n"),
}

#: How to run each cocotb flow on real tools: its probe and the design language it takes.
#: Every flow in `COCOTB_FLOWS` needs an entry, so a new cocotb simulator joins these tests.
_COCOTB_SIMULATORS: Dict[str, Tuple[Callable[[], None], str]] = {
    "ghdl_sim": (require_ghdl, "vhdl"),
    "nvc": (require_nvc, "vhdl"),
    "verilator": (require_verilator, "verilog"),
}

_TEST_HEADER = "import cocotb\nfrom cocotb.triggers import Timer\n"


def _cocotb_test(name: str) -> str:
    """A cocotb test that passes on the inverter."""
    return (
        "@cocotb.test()\n"
        f"async def {name}(dut):\n"
        "    dut.a.value = 0\n"
        "    await Timer(1, 'ns')\n"
        "    assert int(dut.y.value) == 1\n"
    )


def _inverter_design(root: Path, flow_name: str, test_module: str) -> Design:
    """The inverter in `flow_name`'s language, tested by `test_module` (written to `tb_dut.py`),
    after checking that the flow's tools are installed."""
    assert flow_name in _COCOTB_SIMULATORS, f"add {flow_name} to _COCOTB_SIMULATORS"
    require, language = _COCOTB_SIMULATORS[flow_name]
    require_cocotb()
    require()
    source, text = _INVERTER[language]
    (root / source).write_text(text)
    (root / "tb_dut.py").write_text(test_module)
    return Design(
        name="cocotb_dut",
        design_root=root,
        rtl={"sources": [source], "top": "dut"},
        tb={"sources": ["tb_dut.py"], "cocotb": True},
    )


@pytest.mark.parametrize("flow_name", COCOTB_FLOWS)
def test_cocotb_results_of_a_previous_run_are_not_reused(tmp_path: Path, flow_name: str):
    """A run in which cocotb writes no results file fails, even in a directory holding one.

    A test module that fails to import runs no test, and cocotb then writes no `results.xml`;
    several simulators still exit 0. Reading whatever results file an earlier run left in the
    reused run directory judged this run by that earlier one.
    """
    design_root = tmp_path / "design"
    design_root.mkdir()
    runner = DefaultRunner(tmp_path / "runs")
    design = _inverter_design(design_root, flow_name, _TEST_HEADER + _cocotb_test("passes"))
    first = runner.run_flow(flow_name, design)
    assert first is not None and first.succeeded
    assert first.results["cocotb.tests"] == 1
    assert (first.run_path / "results.xml").exists()

    broken = "raise RuntimeError('this test module fails to import')\n"
    design = _inverter_design(design_root, flow_name, broken)
    second = runner.run_flow(flow_name, design)
    assert second is not None
    assert second.run_path == first.run_path, "the second run must reuse the directory"
    assert not second.succeeded
    assert "cocotb.tests" not in second.results


@pytest.mark.parametrize(
    "testcase,ran", [("check", ["check"]), ("foo_check", ["foo_check"]), ("does_not_exist", [])]
)
@pytest.mark.parametrize("flow_name", COCOTB_FLOWS)
def test_cocotb_testcase_selects_the_tests_that_run(
    tmp_path: Path, flow_name: str, testcase: str, ran: List[str]
):
    """`cocotb.testcase` runs exactly the tests it names -- `check` is not `foo_check` -- and
    naming none of them runs no test, which does not succeed.

    cocotb 2.x ignores the `TESTCASE` variable of 1.x, so the setting used to do nothing: every
    test ran, and a name no test has passed.
    """
    module = _TEST_HEADER + _cocotb_test("check") + _cocotb_test("foo_check")
    design = _inverter_design(tmp_path, flow_name, module)
    flow = DefaultRunner(tmp_path / "runs").run_flow(
        flow_name, design, {"cocotb": {"testcase": [testcase]}}
    )
    assert flow is not None
    assert flow.results.get("cocotb.tests", 0) == len(ran)
    assert flow.succeeded is bool(ran)
    if ran:
        results = CocotbTestResults.parse_results(flow.run_path / "results.xml")
        assert [case.name for suite in results.test_suites for case in suite.test_cases] == ran


def test_cocotb_version():
    cocotb = Cocotb(sim_name="ghdl")  # type: ignore

    assert cocotb.version_gte(0)
    assert cocotb.version_gte(0, 0)
    assert cocotb.version_gte(0, 1)
    assert cocotb.version_gte(0, 1, 1)
    assert cocotb.version_gte(0, 1, 1, 1)
    assert cocotb.version_gte(1)
    assert cocotb.version_gte(1, 5)
    assert cocotb.version_gte(1, 6)


def test_cocotb_parse_xml():
    assert (RESOURCES_DIR / "cocotb" / "results.xml").exists()
    with WorkingDirectory(RESOURCES_DIR / "cocotb"):
        cocotb = Cocotb(sim_name="dummy")  # type: ignore
        if cocotb.results is not None:
            for suite in cocotb.results.test_suites:
                for case in suite.test_cases:
                    print(case)
        results: Dict[str, Any] = {"success": True}
        cocotb.add_results(results)
        print("Results:", results)
        assert results["success"] is False


def test_all_cocotb_flows_use_shared_verdict_check():
    flows = {cls for cls in __builtin_flows__ if getattr(cls, "cocotb_sim_name", None)}
    assert flows
    for cls in flows:
        assert issubclass(cls, SimFlow)
        assert cls.check_results is SimFlow.check_results


@pytest.mark.parametrize("content", [None, "<broken>"])
def test_missing_or_unreadable_cocotb_results_fail(tmp_path, monkeypatch, content):
    monkeypatch.chdir(tmp_path)
    if content is not None:
        (tmp_path / "results.xml").write_text(content)
    results: Dict[str, Any] = {"success": True}
    assert Cocotb(sim_name="verilator").add_results(results) is False
    assert results["success"] is False


@pytest.mark.parametrize(
    "xml",
    [
        # what cocotb 2.1 writes when `testcase` matches no test
        '<testsuites name="cocotb tests" />',
        '<testsuites name="results"><testsuite name="all" package="all" /></testsuites>',
    ],
)
def test_cocotb_results_without_a_test_fail(tmp_path, monkeypatch, xml):
    """A results file in which no test ran verified nothing."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "results.xml").write_text(xml)
    results: Dict[str, Any] = {"success": True}
    assert Cocotb(sim_name="verilator").add_results(results) is False
    assert results["success"] is False
    assert results["cocotb.tests"] == 0


# ------------------------------------------------------------------ results.xml, both generations

#: cocotb moved its per-test metadata out of `<testcase>` attributes and into a standard JUnit
#: `<properties>` block in 2.0. Both layouts must be read: the parser previously understood
#: neither -- the attribute branch was unreachable (`if sim_time_ns is None` tested the local it
#: had just initialised to None, not the parsed attribute), so every test reported 0 ns.
COCOTB1_XML = RESOURCES_DIR / "cocotb" / "results.xml"
COCOTB2_XML = RESOURCES_DIR / "cocotb" / "results_cocotb2.xml"


def test_parses_cocotb1_testcase_attributes():
    results = CocotbTestResults.parse_results(COCOTB1_XML)
    suite = results.test_suites[0]
    case = suite.test_cases[0]
    assert suite.random_seed == 1646240102  # a <property> under <testsuite>
    assert case.sim_time_ns == pytest.approx(200.000001)
    assert case.status == "FAILURE"
    assert case.file.endswith("tb_sqrt.py")
    assert case.lineno == "7"
    assert results.failures == 1 and not results.success


def test_parses_cocotb2_properties():
    results = CocotbTestResults.parse_results(COCOTB2_XML)
    suite = results.test_suites[0]
    assert suite.random_seed == 1789608322  # a <property> under each <testcase>
    passed, failed = suite.test_cases
    # the fixture's testbench awaits Timer(10, "ns") then Timer(20, "ns")
    assert passed.sim_time_ns == pytest.approx(10.0)
    assert failed.sim_time_ns == pytest.approx(20.0)
    assert results.total_sim_time_ns == pytest.approx(30.0)
    assert (passed.status, failed.status) == ("PASSED", "FAILURE")
    assert (passed.file, passed.lineno) == ("tb_dut.py", "4")
    assert results.failures == 1 and not results.success


def test_a_passing_cocotb2_test_is_not_reported_as_failed():
    """Every cocotb 2.x testcase has a `<properties>` child, and the parser counted *every*
    child element as an outcome -- so a passing test's status read "PROPERTIES"."""
    results = CocotbTestResults.parse_results(COCOTB2_XML)
    statuses = {tc.name: tc.status for tc in results.test_suites[0].test_cases}
    assert statuses["test_passes"] == "PASSED"
    assert "PROPERTIES" not in str(statuses)


@pytest.mark.parametrize(
    "unit,value,expected_ns",
    [("ns", "12.5", 12.5), ("ps", "1000", 1.0), ("us", "2", 2000.0), ("fs", "1000000", 1.0)],
)
def test_sim_time_unit_is_honored(tmp_path, unit, value, expected_ns):
    """cocotb hardcodes `ns` today, but the unit is reported, so it is read rather than assumed."""
    xml = tmp_path / "results.xml"
    xml.write_text(
        '<testsuites><testsuite name="s"><testcase classname="c" name="t" time="1.0">'
        "<properties>"
        f'<property name="sim_time_unit" value="{unit}" />'
        f'<property name="sim_time_duration" value="{value}" />'
        "</properties></testcase></testsuite></testsuites>"
    )
    results = CocotbTestResults.parse_results(xml)
    assert results.total_sim_time_ns == pytest.approx(expected_ns)


if __name__ == "__main__":
    test_cocotb_version()
    test_cocotb_parse_xml()


# --------------------------------------------------------------------- GPI_USERS (cocotb >= 2.1)


def _fake_cocotb_config(entry_point=None, libpython="/usr/lib/libpython3.so", version="2.1.0"):
    """Stand in for `cocotb-config`, which answers differently across cocotb versions."""

    def run_get_stdout(self, *args, **kwargs):
        if "--pygpi-entry-point" in args:
            return entry_point  # None on cocotb < 2.1, which rejects the flag
        if "--libpython" in args:
            return libpython
        if "--version" in args:
            return version
        return None

    return run_get_stdout


def test_gpi_users_is_unset_for_cocotb_before_2_1(monkeypatch):
    """Up to cocotb 2.0 the interface library registers its own entry point."""
    monkeypatch.delenv("GPI_USERS", raising=False)
    monkeypatch.setattr(Cocotb, "run_get_stdout", _fake_cocotb_config(entry_point=None))
    assert Cocotb(sim_name="nvc").gpi_users() is None


def test_gpi_users_pairs_libpython_with_the_entry_point(monkeypatch):
    """From cocotb 2.1 the GPI exits with "No GPI_USERS specified" unless told explicitly."""
    monkeypatch.delenv("GPI_USERS", raising=False)
    monkeypatch.setattr(
        Cocotb, "run_get_stdout", _fake_cocotb_config(entry_point="/x/simulator.so,initialize")
    )
    assert Cocotb(sim_name="nvc").gpi_users() == "/usr/lib/libpython3.so;/x/simulator.so,initialize"


def test_gpi_users_respects_an_existing_environment_value(monkeypatch):
    monkeypatch.setenv("GPI_USERS", "/preset/libpython.so;/preset/entry.so,init")
    monkeypatch.setattr(
        Cocotb, "run_get_stdout", _fake_cocotb_config(entry_point="/x/simulator.so,initialize")
    )
    assert Cocotb(sim_name="nvc").gpi_users() == "/preset/libpython.so;/preset/entry.so,init"


def test_gpi_users_uses_libpython_loc_when_set(monkeypatch):
    monkeypatch.delenv("GPI_USERS", raising=False)
    monkeypatch.setenv("LIBPYTHON_LOC", "/from/env/libpython.so")
    monkeypatch.setattr(
        Cocotb, "run_get_stdout", _fake_cocotb_config(entry_point="/x/simulator.so,initialize")
    )
    users = Cocotb(sim_name="nvc").gpi_users()
    assert users.startswith("/from/env/libpython.so;")


def test_gpi_users_agrees_with_the_installed_cocotb(monkeypatch):
    """Version-agnostic: whatever cocotb is installed, the two must stay consistent."""
    monkeypatch.delenv("GPI_USERS", raising=False)
    cocotb = Cocotb(sim_name="nvc")
    entry_point = cocotb.pygpi_entry_point
    users = cocotb.gpi_users()
    if entry_point:  # cocotb >= 2.1
        assert users and users.endswith(entry_point)
    else:  # cocotb < 2.1
        assert users is None


# ------------------------------------------------------------------- test selection (`testcase`)


def _testcase_env(tmp_path, monkeypatch, version: str, testcase) -> Dict[str, Any]:
    """The simulator environment for `testcase`, as a cocotb reporting `version` gets it."""
    monkeypatch.delenv("GPI_USERS", raising=False)
    monkeypatch.setattr(Cocotb, "run_get_stdout", _fake_cocotb_config(version=version))
    (tmp_path / "dut.sv").write_text("module dut; endmodule\n")
    (tmp_path / "tb_dut.py").write_text("")
    design = Design(
        name="dut",
        design_root=tmp_path,
        rtl={"sources": ["dut.sv"], "top": "dut"},
        tb={"sources": ["tb_dut.py"], "cocotb": True},
    )
    with WorkingDirectory(tmp_path):
        return Cocotb(sim_name="verilator", testcase=testcase).env(design)  # type: ignore


@pytest.mark.parametrize("version", ["2.0.0", "2.1.0", "3.0.0.dev0"])
def test_cocotb_2_selects_tests_by_filter(tmp_path, monkeypatch, version):
    """cocotb 2.x reads `COCOTB_TEST_FILTER` (and a deprecated `COCOTB_TESTCASE`), never
    `TESTCASE`. It searches the filter in each test's `<module>.<name>`; a name selects exactly
    the test of that name, and may be qualified by its module."""
    env = _testcase_env(tmp_path, monkeypatch, version, ["test_a", "tb.q", "t/x=1", "b.c"])
    assert "TESTCASE" not in env and "COCOTB_TESTCASE" not in env
    test_filter = re.compile(env["COCOTB_TEST_FILTER"])
    fullnames = {
        "tb.test_a": True,
        "pkg.tb.test_a": True,
        "tb.foo_test_a": False,  # a test whose name ends with the name
        "tb.test_a2": False,  # one whose name starts with it
        "tb.q": True,  # qualified by its module
        "pkg.tb.q": True,
        "other.q": False,  # another module's `q`
        "xtb.q": False,
        "tb.t/x=1": True,  # one instance of a parametrized test
        "tb.t/x=10": False,
        "b.c": True,
        "tb.bxc": False,  # `.` is not a wildcard
    }
    assert {name: bool(test_filter.search(name)) for name in fullnames} == fullnames


@pytest.mark.parametrize("version", ["1.8.1", "1.9.2"])
def test_cocotb_1_selects_tests_by_name(tmp_path, monkeypatch, version):
    """cocotb 1.x reads a comma-separated list of test names from `TESTCASE`."""
    env = _testcase_env(tmp_path, monkeypatch, version, ["test_a", "test_b"])
    assert env["TESTCASE"] == "test_a,test_b"
    assert "COCOTB_TEST_FILTER" not in env


@pytest.mark.parametrize("version", ["1.9.2", "2.1.0"])
def test_cocotb_runs_every_test_without_testcase(tmp_path, monkeypatch, version):
    env = _testcase_env(tmp_path, monkeypatch, version, [])
    assert not {"TESTCASE", "COCOTB_TESTCASE", "COCOTB_TEST_FILTER"} & set(env)
