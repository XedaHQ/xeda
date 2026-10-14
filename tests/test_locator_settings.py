"""A setting that names where installed data lives (`LOCATOR`) is a place on this machine.

It is no part of the identity of a run, a launch never traverses it, and a remote run never
receives it. The flow registers the exact files it opens, and the trace records their content.
"""

import json
import zipfile
from pathlib import Path

import pytest

from xeda import Design
from xeda.dataclass import LOCATOR, WORKING, Field, XedaBaseModel, is_locator
from xeda.flow import flowrun_hash
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner import remote as remote_module
from xeda.flow_runner.remote import RemoteRunner
from xeda.flow_runner.trace_inputs import setting_path_leaves

from .io_flows import _InstalledTaker, _ReadsInstalled, _ReadsInstalledNested, _Where
from .test_remote_run import TESTS_DIR, _remote_run_dir, remote_host  # noqa: F401

LIB = "library contents\n"


def _store(path: Path, extra_files: int = 0) -> Path:
    path.mkdir(parents=True)
    (path / "lib.txt").write_text(LIB)
    for index in range(extra_files):
        (path / f"extra{index}.txt").write_text(f"{index}\n")
    return path


def _design(tmp_path: Path) -> Design:
    return Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "t"})


def test_is_locator_tells_the_marked_fields():
    assert LOCATOR == {"x-xeda-locator": True}
    assert is_locator(_ReadsInstalled.Settings, "store")
    assert not is_locator(_ReadsInstalled.Settings, "text")
    assert not is_locator(_ReadsInstalled.Settings, "no_such_field")
    assert is_locator(_Where, "store")


def test_the_marker_is_no_role_of_a_written_path():
    class Model(XedaBaseModel):
        place: Path | None = Field(None, json_schema_extra=LOCATOR)
        work: Path | None = Field(None, json_schema_extra=WORKING)

    assert is_locator(Model, "place") and not is_locator(Model, "work")


def test_a_locator_is_no_part_of_the_identity(tmp_path):
    a, b = _store(tmp_path / "a"), _store(tmp_path / "b")
    first = _ReadsInstalled.Settings.from_input(
        {"store": a}, design_root=tmp_path, runner_cwd=tmp_path
    )
    second = _ReadsInstalled.Settings.from_input(
        {"store": b}, design_root=tmp_path, runner_cwd=tmp_path
    )
    unset = _ReadsInstalled.Settings.from_input({}, design_root=tmp_path, runner_cwd=tmp_path)
    assert flowrun_hash("x", first) == flowrun_hash("x", second) == flowrun_hash("x", unset)
    assert first.store == a, "the identity works on a copy"


def test_a_locator_in_a_nested_model_is_no_part_of_the_identity(tmp_path):
    a, b = _store(tmp_path / "a"), _store(tmp_path / "b")
    first, second = (
        _ReadsInstalledNested.Settings.from_input(
            {"where": {"store": store}}, design_root=tmp_path, runner_cwd=tmp_path
        )
        for store in (a, b)
    )
    assert flowrun_hash("x", first) == flowrun_hash("x", second)
    assert first.where.store == a


def test_a_locator_is_never_traversed(tmp_path):
    store = _store(tmp_path / "s", extra_files=500)
    settings = _ReadsInstalled.Settings.from_input(
        {"store": store}, design_root=tmp_path, runner_cwd=tmp_path
    )
    assert setting_path_leaves(settings, written=False) == []
    nested = _ReadsInstalledNested.Settings.from_input(
        {"where": {"store": store}}, design_root=tmp_path, runner_cwd=tmp_path
    )
    assert setting_path_leaves(nested, written=False) == []


@pytest.mark.parametrize("hashed", [False, True])
def test_moving_the_store_keeps_the_identity_and_makes_the_run_stale(tmp_path, hashed):
    a, b = _store(tmp_path / "a"), _store(tmp_path / "b")
    design = _design(tmp_path)

    def launch(store):
        runner = DefaultRunner(tmp_path / "run", display_results=False, hashed_run_dirs=hashed)
        flow = runner.run_flow(_ReadsInstalled, design, {"store": store})
        assert flow is not None and flow.succeeded
        return flow

    first = launch(a)
    second = launch(b)
    assert second.run_path == first.run_path and second.flow_hash == first.flow_hash
    assert not second.reused
    assert second.stale_reason == f"new input: {b.resolve() / 'lib.txt'}"
    again = launch(b)
    assert again.reused and again.flow_hash == first.flow_hash
    (b / "lib.txt").write_text("other bytes\n")
    third = launch(b)
    assert not third.reused and third.flow_hash == first.flow_hash
    assert third.stale_reason == f"input changed: {b.resolve() / 'lib.txt'}"


def test_settings_json_keeps_the_locator(tmp_path):
    store = _store(tmp_path / "s")
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow(_ReadsInstalled, _design(tmp_path), {"store": store})
    assert flow is not None
    recorded = json.loads((flow.run_path / "settings.json").read_text())
    assert Path(recorded["flow_settings"]["store"]) == store


def _remote_design(tmp_path: Path, sections: dict) -> Design:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    return Design(name="d", design_root=root, rtl={"sources": [], "top": "t"}, flow=sections)


@pytest.mark.parametrize("place", ["requested", "producer", "nested"])
def test_a_remote_run_does_not_ship_a_locator(
    tmp_path, remote_host, monkeypatch, place  # noqa: F811
):
    monkeypatch.setattr(
        remote_module,
        "REMOTE_PROBE",
        f"import sys\nsys.path.insert(0, {str(TESTS_DIR.parent)!r})\nimport tests.io_flows\n"
        + remote_module.REMOTE_PROBE,
    )
    store = _store(tmp_path / "store")
    secret = "the content of a store file, which must stay on this machine\n"
    (store / "lib.txt").write_text(secret)
    command_line: list[str] | None = None
    if place == "requested":
        flow_class, key = _ReadsInstalled, "__reads_installed"
        sections: dict = {}
        command_line = [f"store={store}"]
        field = "store"
    elif place == "producer":
        flow_class, key = _InstalledTaker, "__reads_installed"
        sections = {key: {"store": str(store)}}
        field = "store"
    else:
        flow_class, key = _ReadsInstalledNested, "__reads_installed_nested"
        sections = {key: {"where": {"store": str(store)}}}
        field = "where"
    design = _remote_design(tmp_path, sections)
    runner = RemoteRunner(tmp_path / "mirror", display_results=False)
    results = runner.run_remote(design, flow_class.name, "fake", flow_settings=command_line)
    assert results and results["success"]

    settings = json.loads((_remote_run_dir(remote_host, key) / "settings.json").read_text())
    shipped = settings["flow_settings"][field]
    assert shipped is None or shipped == {"store": None}
    (archive_path,) = remote_host.glob(".xeda/remote_run/*/*.zip")
    with zipfile.ZipFile(archive_path) as archive:
        for entry in archive.namelist():
            assert str(store) not in entry and not entry.endswith("lib.txt"), entry
            assert secret.encode() not in archive.read(entry), entry
        assert str(store).encode() not in b"".join(archive.read(e) for e in archive.namelist())
    assert not list(remote_host.glob(".xeda/remote_run/*/**/lib.txt"))
