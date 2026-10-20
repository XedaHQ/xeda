"""`tb.python_packages` names the Python packages a testbench imports.

A simulation flow refuses, at planning, a package that is not installed. A flow that is not a
simulation does not check. A name is a bare distribution name.
"""

import pytest

import xeda.flows  # noqa: F401  (registers every flow)
from xeda import Design
from xeda.design import DesignValidationError
from xeda.flow import FlowSettingsException
from xeda.flows import GhdlSim, Yosys

MISSING = "xeda-no-such-distribution"


def _design(packages, tmp_path=None) -> Design:
    sources = []
    if tmp_path is not None:
        (tmp_path / "d.vhd").write_text("entity d is end; architecture a of d is begin end;\n")
        sources = ["d.vhd"]
    return Design.model_validate(
        {
            "name": "d",
            "design_root": str(tmp_path) if tmp_path is not None else None,
            "rtl": {"sources": sources, "top": "d"},
            "tb": {"sources": [], "top": "tb", "python_packages": packages},
        }
    )


def test_the_default_is_no_package():
    assert _design([]).tb.python_packages == []


def test_installed_packages_pass(tmp_path):
    GhdlSim.check_design_supported(_design(["pytest", "pydantic"], tmp_path))


def test_a_missing_package_is_refused_by_name(tmp_path):
    with pytest.raises(FlowSettingsException) as raised:
        GhdlSim.check_design_supported(_design(["pytest", MISSING], tmp_path))
    message = str(raised.value)
    assert MISSING in message
    assert "Python environment that runs Xeda" in message


def test_a_synthesis_flow_does_not_check(tmp_path):
    Yosys.check_design_supported(_design([MISSING], tmp_path))


@pytest.mark.parametrize(
    "name", ["cocotb>=1.9", "cocotb[bus]", "cocotb==1.9.0", "", " cocotb", "-x"]
)
def test_only_a_distribution_name_is_accepted(name):
    with pytest.raises(DesignValidationError, match="only the name of the package"):
        _design([name])


@pytest.mark.parametrize("name", ["cocotb", "cocotb-bus", "zope.interface", "a_b", "7zip"])
def test_a_distribution_name_is_accepted(name):
    assert _design([name]).tb.python_packages == [name]
