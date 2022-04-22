from pathlib import Path
from typing import Any, Dict

from xeda.flows.flow import Cocotb
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
    assert cocotb.version_gte(1, 6, 0)
    assert cocotb.version_gte(1, 6, 1)
    assert cocotb.version_gte(1, 6, 2)
    assert not cocotb.version_gte(1, 7)
    assert not cocotb.version_gte(1, 7, 0)
    assert not cocotb.version_gte(2)
    assert not cocotb.version_gte(2, 0, 0)
    assert not cocotb.version_gte(2, 0, 0, 0)


def test_cocotb_parse_xml():
    assert (RESOURCES_DIR / "cocotb" / "results.xml").exists()
    with WorkingDirectory(RESOURCES_DIR / "cocotb"):
        cocotb = Cocotb(sim_name="dummy")  # type: ignore
        for case in cocotb.result_testcases:
            print(case)
        results: Dict[str, Any] = {}
        cocotb.add_results(results)
        if not results["success"] is False:
            raise AssertionError()


if __name__ == "__main__":
    test_cocotb_version()
    test_cocotb_parse_xml()
