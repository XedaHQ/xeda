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
