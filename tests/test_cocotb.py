from pathlib import Path
from typing import Any, Dict

from xeda import Cocotb
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
