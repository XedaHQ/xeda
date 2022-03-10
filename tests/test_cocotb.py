from pathlib import Path
from xeda.flows.flow import Cocotb
from . import RESOURCES_DIR


def test_cocotb_version():
    settings = Cocotb.Settings()
    cocotb = Cocotb(settings, "dummy", Path.cwd())

    assert cocotb.has_min_version("0")
    assert cocotb.has_min_version("0.0")
    assert cocotb.has_min_version("0.1")
    assert cocotb.has_min_version("0.1.1")
    assert cocotb.has_min_version("0.1.1.1")
    assert cocotb.has_min_version("1")
    assert cocotb.has_min_version("1.5")
    assert cocotb.has_min_version("1.6")
    assert cocotb.has_min_version("1.6.0")
    assert cocotb.has_min_version("1.6.1")
    assert cocotb.has_min_version("1.6.2")
    assert not cocotb.has_min_version("1.7")
    assert not cocotb.has_min_version("1.7.0")
    assert not cocotb.has_min_version("2")
    assert not cocotb.has_min_version("2.0.0")
    assert not cocotb.has_min_version("2.0.0.0")


def test_cocotb_parse_xml():
    settings = Cocotb.Settings()
    cocotb = Cocotb(settings, "dummy", RESOURCES_DIR / "cocotb")
    assert not cocotb.parse_results()
