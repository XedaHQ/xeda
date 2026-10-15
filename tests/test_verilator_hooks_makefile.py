"""The make fragment that hands the declaration of Xeda's Verilator hooks to verilated.cpp alone
(`xeda/flows/verilator/templates/xeda_hooks.mk`), read by the make of the host (macOS has GNU make
3.81) through the environment variable MAKEFILES."""

import os
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

from xeda.flows.verilator import HOOKS_MAKEFILE

#: A stand-in for Verilator's makefile: a global `CPPFLAGS`, a runtime object and a model object.
MAKEFILE = """\
CPPFLAGS += -DMODEL
.PHONY: verilated.o model.o
all: verilated.o model.o
verilated.o model.o:
\t@echo $@ $(CPPFLAGS)
"""


def _make(build: Path, makefiles: str) -> dict[str, str]:
    """Run make in `build` from elsewhere, as Verilator and bsc do (`make -C`), and return the
    flags of each object."""
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is not installed")
    result = subprocess.run(
        [make, "-C", str(build), "-f", "Makefile"],
        env={**os.environ, "MAKEFILES": makefiles},
        capture_output=True,
        text=True,
        check=True,
    )
    # `-C` makes make print the directory it enters, on lines of its own
    objects = [line.split(" ", 1) for line in result.stdout.splitlines() if ".o " in line]
    return {target: flags for target, flags in objects}


def _build_directory(tmp_path: Path) -> Path:
    build = tmp_path / "build"
    build.mkdir()
    (build / "Makefile").write_text(MAKEFILE)
    template = files("xeda.flows.verilator").joinpath("templates/" + HOOKS_MAKEFILE)
    (build / HOOKS_MAKEFILE).write_text(template.read_text())
    return build


def test_the_fragment_named_relative_to_the_build_forces_the_header_into_the_runtime_only(
    tmp_path,
):
    """The verilator flow names the fragment by name: make reads it in the directory of `-C`."""
    flags = _make(_build_directory(tmp_path), HOOKS_MAKEFILE)
    assert flags["verilated.o"].split() == ["-DMODEL", "-include", "./xeda_hooks.h"]
    assert flags["model.o"].split() == ["-DMODEL"]


def test_the_fragment_named_by_its_path_names_the_header_beside_it(tmp_path):
    """bsc's link step runs make in a directory that bsc names: the fragment's path is absolute."""
    build = _build_directory(tmp_path)
    flags = _make(build, str(build / HOOKS_MAKEFILE))
    assert flags["verilated.o"].split() == ["-DMODEL", "-include", f"{build}/xeda_hooks.h"]
    assert flags["model.o"].split() == ["-DMODEL"]
