"""7-series ordering codes retain device boundaries and opaque speed suffixes."""

import re
from pathlib import Path

import pytest
import yaml

from xeda.flow import FPGA


@pytest.mark.parametrize(
    "part, device, family, package, pins, speed",
    [
        ("xc7a100tcsg324-1", "xc7a100t", "artix-7", "csg", 324, "-1"),
        ("xc7a35tcpg236-2L", "xc7a35t", "artix-7", "cpg", 236, "-2L"),
        ("XC7K70TFBG484-2", "xc7k70t", "kintex-7", "fbg", 484, "-2"),
        ("xc7s25csga225-1IL", "xc7s25", "spartan-7", "csga", 225, "-1IL"),
        ("xc7s50ftgb196-1Q", "xc7s50", "spartan-7", "ftgb", 196, "-1Q"),
        ("xc7z020clg400-1", "xc7z020", "zynq-7", "clg", 400, "-1"),
        ("xc7v2000tflg1925-2", "xc7v2000t", "virtex-7", "flg", 1925, "-2"),
        ("xc7vx485tffg1761-3", "xc7vx485t", "virtex-7", "ffg", 1761, "-3"),
        ("xc7a100tcsg324", "xc7a100t", "artix-7", "csg", 324, None),
    ],
)
def test_seven_series_part_fields(part, device, family, package, pins, speed):
    fpga = FPGA(part)
    assert (fpga.device, fpga.family, fpga.package, fpga.pins, fpga.speed, fpga.grade) == (
        device,
        family,
        package,
        pins,
        speed,
        None,
    )
    assert fpga.vendor == "xilinx" and fpga.generation == "7"
    assert FPGA.model_validate(fpga.model_dump(mode="json")) == fpga
    assigned = fpga.model_copy(deep=True)
    assigned.part = part
    assert assigned == fpga


@pytest.mark.parametrize("tail", ["-1junk", "!", "-1-extra", "324"])
def test_a_partial_seven_series_match_does_not_invent_fields(tail):
    fpga = FPGA("xc7a100tcsg324-1" + tail)
    assert fpga.device is None and fpga.package is None and fpga.speed is None


def test_explicit_attributes_and_temperature_are_preserved():
    fpga = FPGA("xc7a100tcsg324-2L", device="explicit", package="override", speed=-3, grade="I")
    assert (fpga.device, fpga.package, fpga.speed, fpga.grade) == (
        "explicit",
        "override",
        "-3",
        "I",
    )
    fpga.speed = "-1IL"
    assert FPGA.model_validate(fpga.model_dump()) == fpga


def _check_mapping(path):
    parts = yaml.safe_load(path.read_text())
    for part, entry in parts.items():
        fpga = FPGA(part)
        package = re.fullmatch(r"([a-z]+)(\d+)", entry["package"])
        assert package is not None
        assert (fpga.device, fpga.package, fpga.pins, fpga.speed, fpga.grade) == (
            entry["device"],
            package[1],
            int(package[2]),
            "-" + str(entry["speedgrade"]),
            None,
        ), part
    return len(parts)


def test_recorded_project_xray_part_mapping():
    assert _check_mapping(Path(__file__).parent / "resources" / "fpga_parts.yaml") == 8


def test_installed_project_xray_part_mappings():
    root = Path("/opt/openxc7/share/nextpnr/prjxray-db")
    paths = [
        root / family / "mapping" / "parts.yaml"
        for family in ("artix7", "kintex7", "spartan7", "zynq7", "virtex7")
    ]
    if not all(path.is_file() for path in paths):
        pytest.skip("The optional installed openXC7 part mappings are absent")
    assert sum(_check_mapping(path) for path in paths) >= 332
