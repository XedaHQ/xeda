"""The Project X-Ray part data `fpga-as` packs with: the exact part's directory when the database
has one, else a directory of the same device and package at another speed grade.

Configuration data (pin map, id code) does not depend on the speed grade; only timing does, and
`nextpnr` keeps the exact grade for that. The packer is never given another device or package.
"""

import logging
import shutil
from pathlib import Path

import pytest
import yaml

from xeda import Design
from xeda.flow import FlowFatalError
from xeda.flow_runner import DefaultRunner

from . import tool_utils

PART_JSON = '{"idcode": "0x3651093"}\n'


def _database(root: Path, parts: dict[str, str], present: list[str]) -> Path:
    """A kintex7 database: `parts` maps each part to its device; `present` have a directory."""
    family = root / "kintex7"
    (family / "mapping").mkdir(parents=True)
    (family / "mapping/parts.yaml").write_text(
        yaml.safe_dump({name: {"device": device} for name, device in parts.items()})
    )
    (family / "mapping/devices.yaml").write_text(
        yaml.safe_dump({device: {"fabric": device} for device in set(parts.values())})
    )
    (family / "xc7k325t").mkdir()
    for name in present:
        (family / name).mkdir()
        for file in ("part.json", "part.yaml", "package_pins.csv"):
            (family / name / file).write_text(PART_JSON)
    return root


PARTS = {
    "xc7k325tffg676-1": "xc7k325t",
    "xc7k325tffg676-2": "xc7k325t",
    "xc7k325tffg676-2L": "xc7k325t",
    "xc7k325tffg676-3": "xc7k325t",
    "xc7k325tffg900-1": "xc7k325t",
    "xc7k325tffg900-2": "xc7k325t",
    "xc7k160tffg676-1": "xc7k160t",
}


def _locate(database: Path, part: str):
    from xeda.flows import xilinx

    return xilinx.locate_part_data(xilinx.select_xilinx_part(part, database))


def test_the_exact_part_directory_is_used_when_the_database_has_it(tmp_path):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-1", "xc7k325tffg676-2"])
    data = _locate(database, "xc7k325tffg676-2")
    assert data is not None and data.exact
    assert data.name == "xc7k325tffg676-2"
    assert data.directory == database / "kintex7/xc7k325tffg676-2"


def test_another_grade_of_the_same_device_and_package_stands_in(tmp_path):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-1"])
    data = _locate(database, "xc7k325tffg676-2")
    assert data is not None and not data.exact
    assert (data.requested, data.name) == ("xc7k325tffg676-2", "xc7k325tffg676-1")
    assert data.directory == database / "kintex7/xc7k325tffg676-1"


def test_the_lowest_speed_grade_is_chosen_whatever_the_order(tmp_path):
    present = ["xc7k325tffg676-3", "xc7k325tffg676-2L", "xc7k325tffg676-2"]
    for names in (present, present[::-1]):
        database = _database(tmp_path / "-".join(names), PARTS, names)
        data = _locate(database, "xc7k325tffg676-1")
        assert data is not None and data.name == "xc7k325tffg676-2"
    # a plain grade comes before its low-power variant
    database = _database(tmp_path / "lp", PARTS, ["xc7k325tffg676-3", "xc7k325tffg676-2L"])
    data = _locate(database, "xc7k325tffg676-1")
    assert data is not None and data.name == "xc7k325tffg676-2L"


def test_another_package_or_device_never_stands_in(tmp_path):
    database = _database(
        tmp_path, PARTS, ["xc7k325tffg900-1", "xc7k325tffg900-2", "xc7k160tffg676-1"]
    )
    assert _locate(database, "xc7k325tffg676-2") is None


def test_a_directory_without_part_json_is_not_part_data(tmp_path):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-1", "xc7k325tffg676-3"])
    (database / "kintex7/xc7k325tffg676-1/part.json").unlink()
    data = _locate(database, "xc7k325tffg676-2")
    assert data is not None and data.name == "xc7k325tffg676-3"


def test_a_part_the_database_does_not_know_is_still_refused(tmp_path):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-1"])
    with pytest.raises(FlowFatalError, match="Unknown full Xilinx part"):
        _locate(database, "xc7k325tffg676-4")


# -------------------------------------------------------------------------- the packing flow

PART = "xc7k325tffg676-2"


@pytest.fixture
def toolchain(tmp_path, monkeypatch):
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    return prefix


def _launch(tmp_path: Path, database: Path | None = None, part: str = PART):
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "routed.fasm").write_text("TILE.FEATURE\n")
    flows = {"fpga_pack": {"prjxray_db": str(database)}} if database else {}
    design = Design(
        name="top",
        design_root=root,
        rtl={"sources": ["routed.fasm"], "top": "top"},
        flow=flows,
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    return runner.run("fpga_pack", design, flow_settings={"fpga": part})


def _argv(flow) -> list[str]:
    import json

    record = flow.run_path / "fake_fpga.calls.jsonl"
    return json.loads(record.read_text().splitlines()[-1])["argv"]


def test_fpga_as_is_given_the_directory_of_the_grade_that_stands_in(tmp_path, toolchain, caplog):
    database = _database(tmp_path / "db", PARTS, ["xc7k325tffg676-1"])
    with caplog.at_level(logging.INFO):
        flow = _launch(tmp_path, database)
    assert flow.succeeded
    argv = _argv(flow)
    assert f"--prjxray_db_path={database / 'kintex7'}" in argv
    assert "--part=xc7k325tffg676-1" in argv and f"--part={PART}" not in argv
    line = next(r.getMessage() for r in caplog.records if "xc7k325tffg676-1" in r.getMessage())
    assert PART in line and str(database / "kintex7/xc7k325tffg676-1") in line


def test_fpga_as_is_given_the_exact_part_when_the_database_has_it(tmp_path, toolchain, caplog):
    database = _database(tmp_path / "db", PARTS, ["xc7k325tffg676-1", "xc7k325tffg676-2"])
    with caplog.at_level(logging.INFO):
        flow = _launch(tmp_path, database)
    assert f"--part={PART}" in _argv(flow)
    assert not [r for r in caplog.records if "part data" in r.getMessage()]


def test_without_any_directory_of_the_package_the_exact_part_is_passed_on(tmp_path, toolchain):
    database = _database(tmp_path / "db", PARTS, ["xc7k325tffg900-1", "xc7k160tffg676-1"])
    flow = _launch(tmp_path, database)
    assert f"--part={PART}" in _argv(flow)


def test_the_part_data_used_is_part_of_what_the_run_depended_on(tmp_path, toolchain):
    """The database installed with the packer, where no setting names a directory to track."""
    artix = toolchain / "share/nextpnr/prjxray-db/artix7"
    parts = artix / "mapping/parts.yaml"
    for grade in ("2", "3"):
        parts.write_text(
            parts.read_text() + f"xc7a100tcsg324-{grade}:\n  device: xc7a100t\n  package: csg324\n"
            f"  speedgrade: '{grade}'\n"
        )
    for grade in ("1", "3"):
        directory = artix / f"xc7a100tcsg324-{grade}"
        directory.mkdir()
        (directory / "part.json").write_text(PART_JSON)
    part = "xc7a100tcsg324-2"
    first = _launch(tmp_path, part=part)
    assert not first.reused and "--part=xc7a100tcsg324-1" in _argv(first)
    assert _launch(tmp_path, part=part).reused
    # an edit of the file read is noticed
    (artix / "xc7a100tcsg324-1/part.json").write_text('{"idcode": "0x1"}\n')
    assert not _launch(tmp_path, part=part).reused
    assert _launch(tmp_path, part=part).reused
    # the exact part's own directory appearing changes the data used
    shutil.copytree(artix / "xc7a100tcsg324-1", artix / part)
    again = _launch(tmp_path, part=part)
    assert not again.reused and f"--part={part}" in _argv(again)
