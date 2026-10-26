"""`tb.python_packages` names the Python distributions a testbench imports.

A non-cocotb simulation checks Xeda's Python at planning. Cocotb checks its selected Python at
runtime. A flow that is not a simulation does not check. A name is a bare distribution name.
"""

import importlib.metadata as metadata
import subprocess
import sys
import venv
from pathlib import Path

import pytest

import xeda.flows  # noqa: F401  (registers every flow)
from xeda import Design
from xeda.cocotb import Cocotb
from xeda.design import DesignValidationError
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim, Yosys
from xeda.run_dir import RunDirectory
from xeda.tool import ToolException

MISSING = "xeda-no-such-distribution"
FIFO = Path(__file__).parent.parent / "examples" / "sv" / "fifo" / "fifo_cocotb.xeda.yaml"


def _design(packages, tmp_path) -> Design:
    (tmp_path / "d.vhd").write_text("entity d is end; architecture a of d is begin end;\n")
    return Design(
        name="d",
        root=tmp_path,
        rtl={"sources": ["d.vhd"], "top": "d"},
        tb={"sources": [], "top": "tb", "python_packages": packages},
    )


def _cocotb_design(packages, tmp_path: Path) -> Design:
    (tmp_path / "d.vhd").write_text("entity d is end; architecture a of d is begin end;\n")
    (tmp_path / "tb_d.py").write_text("")
    return Design(
        name="d",
        root=tmp_path,
        rtl={"sources": ["d.vhd"], "top": "d"},
        tb={"sources": ["tb_d.py"], "cocotb": True, "python_packages": packages},
    )


def _config(tmp_path: Path, python_bin: str | None = None) -> Path:
    config = tmp_path / "cocotb-config"
    config.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if '--version' in sys.argv: print('2.1.0')\n"
        f"elif '--python-bin' in sys.argv: print({python_bin!r} or sys.executable)\n"
    )
    config.chmod(0o755)
    return config


def test_fifo_declares_its_coverage_dependency():
    assert "cocotb-coverage" in Design.load(FIFO).tb.python_packages


def test_cocotb_checks_the_selected_python_not_xeda_python(tmp_path, monkeypatch):
    package = "xeda-cocotb-only-test-package"
    environment = tmp_path / "cocotb-env"
    venv.EnvBuilder(with_pip=False).create(environment)
    python_bin = (
        environment
        / ("Scripts" if sys.platform == "win32" else "bin")
        / ("python.exe" if sys.platform == "win32" else "python")
    )
    purelib = subprocess.check_output(
        [str(python_bin), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        text=True,
        timeout=30,
    ).strip()
    distribution = Path(purelib) / "xeda_cocotb_only_test_package-1.0.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(f"Name: {package}\nVersion: 1.0\n")
    with pytest.raises(metadata.PackageNotFoundError):
        metadata.version(package)
    design = _cocotb_design([package], tmp_path)
    GhdlSim.check_design_supported(design)
    monkeypatch.setattr(Cocotb, "working_directory", lambda self: tmp_path)
    monkeypatch.delenv("PYGPI_PYTHON_BIN", raising=False)
    env = Cocotb(executable=str(_config(tmp_path, str(python_bin))), sim_name="ghdl").env(design)
    assert env["PYGPI_PYTHON_BIN"] == str(python_bin)


def test_cocotb_checks_an_explicit_python_override(tmp_path, monkeypatch):
    design = _cocotb_design(["pytest"], tmp_path)
    monkeypatch.setattr(Cocotb, "working_directory", lambda self: tmp_path)
    monkeypatch.setenv("PYGPI_PYTHON_BIN", sys.executable)
    env = Cocotb(executable=str(_config(tmp_path, "/missing/python")), sim_name="ghdl").env(design)
    assert env["PYGPI_PYTHON_BIN"] == sys.executable


def test_cocotb_package_check_uses_the_simulators_python_path(tmp_path, monkeypatch):
    package = "xeda-python-path-test-package"
    package_dir = tmp_path / "python_packages"
    distribution = package_dir / "xeda_python_path_test_package-1.0.dist-info"
    distribution.mkdir(parents=True)
    (distribution / "METADATA").write_text(f"Name: {package}\nVersion: 1.0\n")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYGPI_PYTHON_BIN", raising=False)
    monkeypatch.setattr(Cocotb, "working_directory", lambda self: tmp_path)
    design = _cocotb_design([package], tmp_path)
    with pytest.raises(ToolException, match=package):
        Cocotb(executable=str(_config(tmp_path)), sim_name="ghdl").env(design)
    monkeypatch.setenv("PYTHONPATH", str(package_dir))
    Cocotb(executable=str(_config(tmp_path)), sim_name="ghdl").env(design)


def test_cocotb_reports_a_package_missing_from_its_python_before_removing_results(
    tmp_path, monkeypatch
):
    design = _cocotb_design([MISSING], tmp_path)
    result = tmp_path / "results.xml"
    result.write_text("previous result")
    monkeypatch.setattr(Cocotb, "working_directory", lambda self: tmp_path)
    monkeypatch.delenv("PYGPI_PYTHON_BIN", raising=False)
    cocotb = Cocotb(executable=str(_config(tmp_path)), sim_name="ghdl")
    cocotb._run_directory = RunDirectory.claimed(tmp_path, tmp_path.parent)
    with pytest.raises(ToolException, match=MISSING):
        cocotb.env(design)
    assert result.read_text() == "previous result"


def test_the_default_is_no_package(tmp_path):
    assert _design([], tmp_path).tb.python_packages == ()


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
def test_only_a_distribution_name_is_accepted(name, tmp_path):
    with pytest.raises(DesignValidationError, match="only the name of the package"):
        _design([name], tmp_path)


@pytest.mark.parametrize("name", ["cocotb", "cocotb-bus", "zope.interface", "a_b", "7zip"])
def test_a_distribution_name_is_accepted(name, tmp_path):
    assert _design([name], tmp_path).tb.python_packages == (name,)


def test_planning_refuses_a_missing_package_before_any_run_directory(tmp_path):
    run_root = tmp_path / "xeda_run"
    with pytest.raises(FlowSettingsException) as raised:
        DefaultRunner(run_root).plan("ghdl_sim", design=_design([MISSING], tmp_path))
    assert MISSING in str(raised.value)
    assert not run_root.exists() or not any(run_root.iterdir())
