"""The Project X-Ray data `fpga-as` packs with.

Part data is the exact part's directory when the database has one, else the directory of the
same device and package at another speed grade: the lowest, logged, with a warning if the grades
differ in the files that describe the die and its pinout. `nextpnr` keeps the exact grade for
timing. A part with no directory at any speed grade of its package is an error before anything
runs. The installed database is a tool's own file: it is not an input of the packing run.
"""

import json
import logging
import shutil
from pathlib import Path

import pytest
import yaml

from xeda import Design
from xeda.flow import FlowFatalError
from xeda.flow_runner import DefaultRunner

from . import tool_utils
from .test_fpga_pack import A100T, _design, _other_database, _runner, _tools

PART_JSON = '{"idcode": "0x3651093"}\n'


@pytest.fixture
def toolchain(tmp_path, monkeypatch):
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    return prefix


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
    for device in set(parts.values()):
        (family / device).mkdir()
        (family / device / "tilegrid.json").write_text("{}\n")
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


def _select(database: Path, part: str):
    from xeda.flows import xilinx

    return xilinx.select_xilinx_part(part, database)


def _locate(database: Path, part: str):
    from xeda.flows import xilinx

    return xilinx.locate_part_data(_select(database, part))


# ----------------------------------------------------------------------- which directory is used


def test_the_exact_part_directory_is_used_when_the_database_has_it(tmp_path):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-1", "xc7k325tffg676-2"])
    data = _locate(database, "xc7k325tffg676-2")
    assert data.exact
    assert data.name == "xc7k325tffg676-2"
    assert data.directory == database / "kintex7/xc7k325tffg676-2"


def test_another_grade_of_the_same_device_and_package_stands_in(tmp_path):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-1"])
    data = _locate(database, "xc7k325tffg676-2")
    assert not data.exact
    assert (data.requested, data.name) == ("xc7k325tffg676-2", "xc7k325tffg676-1")
    assert data.directory == database / "kintex7/xc7k325tffg676-1"


def test_the_lowest_speed_grade_is_chosen_whatever_the_order(tmp_path):
    present = ["xc7k325tffg676-3", "xc7k325tffg676-2L", "xc7k325tffg676-2"]
    for names in (present, present[::-1]):
        database = _database(tmp_path / "-".join(names), PARTS, names)
        assert _locate(database, "xc7k325tffg676-1").name == "xc7k325tffg676-2"
    # a plain grade comes before its low-power variant
    database = _database(tmp_path / "lp", PARTS, ["xc7k325tffg676-3", "xc7k325tffg676-2L"])
    assert _locate(database, "xc7k325tffg676-1").name == "xc7k325tffg676-2L"


def test_a_directory_without_part_json_is_not_part_data(tmp_path):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-1", "xc7k325tffg676-3"])
    (database / "kintex7/xc7k325tffg676-1/part.json").unlink()
    assert _locate(database, "xc7k325tffg676-2").name == "xc7k325tffg676-3"


def test_a_part_the_database_does_not_know_is_still_refused(tmp_path):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-1"])
    with pytest.raises(FlowFatalError, match="Unknown full Xilinx part"):
        _locate(database, "xc7k325tffg676-4")


# ----------------------------------------------------------------- no part data for the package


def test_another_package_or_device_never_stands_in_and_the_error_says_what_exists(tmp_path):
    present = ["xc7k325tffg900-1", "xc7k325tffg900-2", "xc7k160tffg676-1"]
    database = _database(tmp_path, PARTS, present)
    with pytest.raises(FlowFatalError) as raised:
        _locate(database, "xc7k325tffg676-2")
    message = str(raised.value)
    assert "no part data for xc7k325tffg676-2" in message
    assert str(database / "kintex7") in message  # the directory searched
    assert "xc7k325t" in message and "ffg676" in message
    # the packages of this device the database has data for, with their speed grades
    assert "ffg900" in message and "-1" in message and "-2" in message
    assert "xc7k160t" not in message  # another device is not offered
    assert "prjxray_db" in message  # and how to name another database


def test_a_device_with_no_part_data_at_all_is_named_as_such(tmp_path):
    database = _database(tmp_path, PARTS, ["xc7k160tffg676-1"])
    with pytest.raises(FlowFatalError, match="no part data for any package of xc7k325t"):
        _locate(database, "xc7k325tffg676-2")


# ------------------------------------------------------- grades that stand in but do not agree


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_grades_that_agree_in_their_pinout_files_are_used_without_a_warning(tmp_path, caplog):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-2", "xc7k325tffg676-3"])
    with caplog.at_level(logging.INFO):
        assert _locate(database, "xc7k325tffg676-1").name == "xc7k325tffg676-2"
    assert not _warnings(caplog)


@pytest.mark.parametrize("file", ["part.json", "package_pins.csv"])
def test_a_grade_that_differs_in_a_pinout_file_is_named_in_a_warning(tmp_path, caplog, file):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-2", "xc7k325tffg676-3"])
    (database / "kintex7/xc7k325tffg676-3" / file).write_text("another die\n")
    with caplog.at_level(logging.INFO):
        data = _locate(database, "xc7k325tffg676-1")
    assert data.name == "xc7k325tffg676-2"  # the lowest grade, as always
    (message,) = _warnings(caplog)
    assert "xc7k325tffg676-1" in message  # the part asked for
    assert "xc7k325tffg676-2" in message  # the data used
    assert "xc7k325tffg676-3" in message and file in message  # what differs
    other = "package_pins.csv" if file == "part.json" else "part.json"
    assert other not in message


def test_the_exact_part_needs_no_comparison_and_no_warning(tmp_path, caplog):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-2", "xc7k325tffg676-3"])
    (database / "kintex7/xc7k325tffg676-3/part.json").write_text("another die\n")
    with caplog.at_level(logging.INFO):
        assert _locate(database, "xc7k325tffg676-2").exact
    assert not _warnings(caplog)


def test_a_single_grade_has_nothing_to_differ_from(tmp_path, caplog):
    database = _database(tmp_path, PARTS, ["xc7k325tffg676-1"])
    with caplog.at_level(logging.INFO):
        assert _locate(database, "xc7k325tffg676-2").name == "xc7k325tffg676-1"
    assert not _warnings(caplog)


# -------------------------------------------------------------------------- the packing flow

PART = "xc7k325tffg676-2"


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


def test_a_part_without_data_fails_before_any_tool_runs_and_the_failure_is_recorded(
    tmp_path, toolchain
):
    """The whole default graph is requested; the check is the packer's, made when its flow is
    set up, so neither the synthesis nor the placement runs first."""
    design = _design(tmp_path, A100T, flows={"fpga_pack": {"prjxray_db": "db"}})
    database = _other_database(tmp_path, toolchain)
    shutil.rmtree(database / "artix7" / A100T)
    with pytest.raises(FlowFatalError, match=f"no part data for {A100T}") as raised:
        _runner(tmp_path).run("fpga_pack", design, flow_settings={"fpga": A100T})
    assert str(database / "artix7") in str(raised.value)
    assert _tools(tmp_path) == []
    results = json.loads((tmp_path / "run/top/fpga_pack/results.json").read_text())
    assert results["success"] is False
    assert results["error"]["type"] == "FlowFatalError"
    assert f"no part data for {A100T}" in results["error"]["message"]
    assert not (tmp_path / "run/top/yosys_fpga").exists()


def test_a_missing_packer_fails_before_any_tool_runs(tmp_path, toolchain, monkeypatch):
    monkeypatch.setattr("xeda.flows.fpga_pack.which", lambda name: None)
    design = _design(tmp_path, A100T)
    with pytest.raises(FlowFatalError, match="fpga-as is missing on PATH"):
        _runner(tmp_path).run("fpga_pack", design, flow_settings={"fpga": A100T})
    assert _tools(tmp_path) == []


# --------------------------------------------------------- the installed database is not an input

DATABASE_FILES = [
    "mapping/parts.yaml",
    "mapping/devices.yaml",
    "xc7a100t/tilegrid.json",
    "xc7a100tcsg324-1/part.json",
    "xc7a100tcsg324-1/package_pins.csv",
]


def test_no_file_of_the_installed_database_is_an_input_of_the_run(tmp_path, toolchain):
    """A tool's own installed files are never flow inputs, so an in-place change of the installed
    Project X-Ray data is not noticed when packing (`--rebuild-all` packs again)."""
    first = _launch(tmp_path, part="xc7a100tcsg324-1")
    assert not first.reused
    installation = toolchain.resolve()
    inside = [
        path for path in first.implicit_inputs if Path(path).resolve().is_relative_to(installation)
    ]
    assert inside == []
    database = toolchain / "share/nextpnr/prjxray-db/artix7"
    for name in DATABASE_FILES:
        path = database / name
        path.write_text(path.read_text() + "# edited\n")
        assert _launch(tmp_path, part="xc7a100tcsg324-1").reused, name
    assert len(_calls_of(tmp_path, "fpga-as")) == 1


def test_a_database_the_user_names_is_tracked_as_a_setting_s_directory(tmp_path, toolchain):
    """The `prjxray_db` setting names a directory the run depends on, as any path setting does."""
    database = _database(tmp_path / "db", PARTS, ["xc7k325tffg676-1"])
    assert not _launch(tmp_path, database).reused
    assert _launch(tmp_path, database).reused
    part_json = database / "kintex7/xc7k325tffg676-1/part.json"
    part_json.write_text('{"idcode": "0x1"}\n')
    assert not _launch(tmp_path, database).reused
    assert _launch(tmp_path, database).reused


def _calls_of(tmp_path: Path, tool: str) -> list[dict]:
    record = tmp_path / "run/top/fpga_pack/fake_fpga.calls.jsonl"
    return [
        call for call in map(json.loads, record.read_text().splitlines()) if call["tool"] == tool
    ]
