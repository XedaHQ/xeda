from pathlib import Path
from typing import Any, Dict

import pytest

from xeda import Cocotb
from xeda.cocotb import TestResults as CocotbTestResults  # alias: pytest collects `Test*`
from xeda.utils import WorkingDirectory

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"


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


def _fake_cocotb_config(entry_point=None, libpython="/usr/lib/libpython3.so"):
    """Stand in for `cocotb-config`, which answers differently across cocotb versions."""

    def run_get_stdout(self, *args, **kwargs):
        if "--pygpi-entry-point" in args:
            return entry_point  # None on cocotb < 2.1, which rejects the flag
        if "--libpython" in args:
            return libpython
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
