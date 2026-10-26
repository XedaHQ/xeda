"""What a remote request refuses before it connects, and the deliveries from the mirror.

A request is judged on this side with the code of a local launch: the design, the project, the
settings and `--outputs-to`. A mistake shows before any connection exists (`connected` tells
whether the client opened one). A delivery copies from the mirror under the lock of its run
directory, and never replaces a file that the run reads.
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from xeda.deliver import DeliveryError
from xeda.flow import FlowException, FlowSettingsError
from xeda.flow_runner.default_runner import ProjectFileError
from xeda.flow_runner.run_lock import lock_file
from xeda.remote import client
from xeda.remote.errors import RemoteConnectionError
from xeda.run_root import RunRootError, is_run_root
from xeda.xedaproject import PROJECT_FILE_NAMES

from .project_files import PROJECT_FILE, TOML_PROJECT_FILE
from .remote_fixtures import connected, fake_remote, fake_ssh, remote_host, remote_request

FIXTURES = (fake_ssh, fake_remote, remote_host)

pytestmark = [pytest.mark.skipif(os.name != "posix", reason="POSIX")]

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"
SQRT_SETTINGS = ["fpga.part=xc7a12tcsg325-1", "clock.period=5ns"]


def _sqrt_design(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(SQRT / "sqrt.vhdl", root)
    (root / "sqrt.yaml").write_text(
        "name: sqrt\nrtl:\n  sources: [sqrt.vhdl]\n  top: sqrt\n  clock: {port: clk}\n"
    )
    return root / "sqrt.yaml"


def _nextpnr_design(tmp_path: Path) -> Path:
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    design = tmp_path / "d.yaml"
    design.write_text(
        "name: d\nrtl:\n  sources: [top.v]\n  top: top\n"
        "flows:\n  nextpnr:\n    fpga: {part: LFE5U-25F-6BG381C}\n"
    )
    return design


def _succeeded(outcome) -> dict:
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    return outcome.document


# --- What the request says ----------------------------------------------------------------------


def test_a_remote_run_takes_dash_s_flows_node_key_as_a_local_run_does(
    tmp_path, remote_host, monkeypatch
):
    """`-s flows.<node>.key` reaches the remote as the setting of that node; a flow outside the
    run is refused before connecting."""
    design = _nextpnr_design(tmp_path)
    outcome = remote_request(
        tmp_path, design, "nextpnr", flow_settings=["flows.yosys_fpg.flatten=true"]
    )
    assert isinstance(outcome.error, FlowSettingsError)
    assert re.search(r"yosys_fpg.*yosys_fpga", str(outcome.error))
    assert not connected(remote_host)

    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")
    outcome = remote_request(
        tmp_path, design, "nextpnr", flow_settings=["flows.yosys_fpga.flatten=true"]
    )
    assert isinstance(outcome.error, RemoteConnectionError)


def test_a_remote_run_refuses_a_directory_with_two_project_files_as_a_local_run_does(
    tmp_path, remote_host, monkeypatch
):
    """One discovery rule for both: more than one project file is a `ProjectFileError` naming
    them, before anything connects."""
    monkeypatch.chdir(tmp_path)
    design = _nextpnr_design(tmp_path)
    (tmp_path / PROJECT_FILE).write_text("flows: {}\n")
    (tmp_path / TOML_PROJECT_FILE).write_text("[flows]\n")
    for request in (design, "d"):
        outcome = remote_request(tmp_path, request, "nextpnr")
        assert isinstance(outcome.error, ProjectFileError)
        assert re.search(rf"{PROJECT_FILE}.*{TOML_PROJECT_FILE}.*keep one", str(outcome.error))
    assert not connected(remote_host)


@pytest.mark.parametrize("given", ["missing.yaml", Path("missing.toml")])
def test_a_remote_run_reports_a_missing_project_file_as_a_local_run_does(
    tmp_path, remote_host, monkeypatch, given
):
    monkeypatch.chdir(tmp_path)
    outcome = remote_request(tmp_path, _nextpnr_design(tmp_path), "nextpnr", xedaproject=given)
    assert isinstance(outcome.error, ProjectFileError)
    assert re.search(r"Cannot open project file .*missing.*no such file", str(outcome.error))
    assert not connected(remote_host)


def test_a_remote_run_treats_an_empty_project_name_as_none_given(
    tmp_path, remote_host, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")
    outcome = remote_request(tmp_path, _nextpnr_design(tmp_path), "nextpnr", xedaproject="")
    assert isinstance(outcome.error, RemoteConnectionError)


@pytest.mark.parametrize("name", PROJECT_FILE_NAMES)
def test_a_remote_run_finds_a_lone_project_file_by_any_accepted_name(
    tmp_path, remote_host, monkeypatch, name
):
    """The project is found (a broken one is reported, not skipped), whichever of the accepted
    names it has."""
    monkeypatch.chdir(tmp_path)
    design = _nextpnr_design(tmp_path)
    (tmp_path / name).write_text(
        "flows: {nextpnr: {flatten: 1, flatten: 2}}\n"
        if not name.endswith(".toml")
        else "[flows.nextpnr\nflatten = 1\n"
    )
    outcome = remote_request(tmp_path, design, "nextpnr")
    assert outcome.error is not None and re.search(r"flatten|line 1", str(outcome.error))
    assert not connected(remote_host)

    (tmp_path / name).write_text("flows: {}\n" if not name.endswith(".toml") else "[flows]\n")
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")
    outcome = remote_request(tmp_path, design, "nextpnr")
    assert isinstance(outcome.error, RemoteConnectionError)


def test_a_remote_run_rejects_a_testbench_the_simulator_cannot_run_before_connecting(
    tmp_path, remote_host
):
    """Here, not on the remote: a remote on a release without the check would run a cocotb
    testbench on modelsim as an ordinary simulation, its tests silently never run."""
    outcome = remote_request(tmp_path, SQRT / "sqrt.yaml", "modelsim")
    assert isinstance(outcome.error, FlowException)
    assert "modelsim cannot run cocotb tests" in str(outcome.error)
    assert not connected(remote_host)
    assert not (tmp_path / "local").exists(), "nor a run root set up for it"


def test_a_remote_run_refuses_a_users_local_results_directory_before_connecting(
    tmp_path, remote_host
):
    """The local directory a remote run's results are fetched into is chosen by xeda under the
    run root: a directory of the user's named as the run root is refused, naming it, before
    connecting -- the files that the fetch writes would overwrite the user's files."""
    mine = tmp_path / "myrundir"
    canary = mine / "sqrt" / "vivado_synth" / "settings.json"
    canary.parent.mkdir(parents=True)
    canary.write_text("the user's own file\n")
    outcome = remote_request(
        tmp_path,
        _sqrt_design(tmp_path / "design"),
        "vivado_synth",
        run_root=mine,
        flow_settings=SQRT_SETTINGS,
    )
    assert isinstance(outcome.error, RunRootError) and str(mine) in str(outcome.error)
    assert not connected(remote_host)
    assert canary.read_text() == "the user's own file\n"
    assert sorted(p.name for p in mine.iterdir()) == ["sqrt"]
    assert sorted(p.name for p in canary.parent.iterdir()) == ["settings.json"]


def test_a_remote_run_marks_its_local_results_directory_and_reuses_it(tmp_path, remote_host):
    """The mirror lies in a run root that the run marks as xeda's, so the next remote run of the
    flow reuses it."""
    design = _sqrt_design(tmp_path / "design")
    run_root = tmp_path / "local" / "xeda_run"
    mirrors = []
    for attempt in (1, 2):
        document = _succeeded(
            remote_request(tmp_path, design, "vivado_synth", flow_settings=SQRT_SETTINGS)
        )
        mirror = Path(document["run_path"])
        assert mirror == run_root / "@somewhere" / "sqrt" / "vivado_synth"
        assert is_run_root(run_root), f"run {attempt}"
        mirrors.append(mirror)
    assert mirrors[0] == mirrors[1]
    assert [n["state"] for n in document["nodes"]] == ["fresh"]


# --- Deliveries from the mirror -----------------------------------------------------------------


def test_a_remote_run_delivers_outputs_to_but_never_onto_a_read_input(tmp_path, remote_host):
    """`--outputs-to` from the mirror; a file a read setting names is never a destination, even
    with --overwrite-outputs."""
    design = _sqrt_design(tmp_path / "design")
    got = tmp_path / "got"
    first = _succeeded(
        remote_request(
            tmp_path, design, "vivado_synth", flow_settings=SQRT_SETTINGS, outputs_to=got
        )
    )
    assert first["results"]["deliveries"]
    delivered = Path(first["results"]["deliveries"][0]["to"])
    assert delivered.is_relative_to(got) and delivered.is_file()
    assert Path(first["run_path"]).is_relative_to(Path(first["mirror"]))
    kept = delivered.read_bytes()
    outcome = remote_request(
        tmp_path,
        design,
        "vivado_synth",
        outputs_to=got,
        overwrite_outputs=True,
        flow_settings=[*SQRT_SETTINGS, f"xdc_files={delivered}"],
    )
    assert isinstance(outcome.error, DeliveryError) and "an input of the run" in str(outcome.error)
    assert delivered.read_bytes() == kept


def test_a_remote_run_never_delivers_into_a_directory_a_setting_reads(tmp_path, remote_host):
    """Outputs delivered into a directory that a later run's `lib_paths` names are inputs of that
    run -- every file under it is -- so that run's `--outputs-to` there is refused before
    connecting, --overwrite-outputs or not, and xeda's earlier copies stay as they were."""
    design = _sqrt_design(tmp_path / "design")
    got = tmp_path / "got"
    first = _succeeded(
        remote_request(
            tmp_path, design, "vivado_synth", flow_settings=SQRT_SETTINGS, outputs_to=got
        )
    )
    assert first["results"]["deliveries"]
    kept = {p: p.read_bytes() for p in got.rglob("*") if p.is_file()}
    assert kept
    calls = len([c for c in remote_host.remote.calls("ssh") if "-M" in c])
    outcome = remote_request(
        tmp_path,
        design,
        "vivado_synth",
        outputs_to=got,
        overwrite_outputs=True,
        flow_settings=[*SQRT_SETTINGS, {"lib_paths": [["work", str(got)]]}],
    )
    assert isinstance(outcome.error, DeliveryError)
    assert re.search(
        rf"`lib_paths\[0\]\[1\]` names {re.escape(str(got.resolve()))}", str(outcome.error)
    )
    assert len([c for c in remote_host.remote.calls("ssh") if "-M" in c]) == calls
    assert {p: p.read_bytes() for p in got.rglob("*") if p.is_file()} == kept


def test_a_remote_run_guards_the_directories_its_flow_reads_as_the_command_line_leaves_them(
    tmp_path, remote_host
):
    """The requested flow's own `[flows.<flow>]` section is registered as the launch uses it, the
    command line's settings over it -- `lib_paths` given on the command line replaces the
    section's, so the section's directory is no input and `--outputs-to` may deliver there --
    while a destination inside the directory the run does read is refused."""
    design = _sqrt_design(tmp_path / "design")
    old, new = tmp_path / "old", tmp_path / "new"
    for directory in (old, new):
        directory.mkdir()
        (directory / "cells.v").write_text("module cell; endmodule\n")
    with design.open("a") as yaml_file:
        yaml_file.write(
            f"flows:\n  vivado_synth:\n    lib_paths: [[work, {json.dumps(str(old))}]]\n"
        )
    overridden = [*SQRT_SETTINGS, {"lib_paths": [["work", str(new)]]}]
    result = _succeeded(
        remote_request(tmp_path, design, "vivado_synth", flow_settings=overridden, outputs_to=old)
    )
    assert result["results"]["deliveries"]
    assert all(Path(d["to"]).is_relative_to(old) for d in result["results"]["deliveries"])
    outcome = remote_request(
        tmp_path,
        design,
        "vivado_synth",
        flow_settings=overridden,
        outputs_to=new / "got",
        overwrite_outputs=True,
    )
    assert isinstance(outcome.error, DeliveryError)
    assert re.search(
        rf"`lib_paths\[0\]\[1\]` names {re.escape(str(new.resolve()))}", str(outcome.error)
    )
    assert sorted(p.name for p in new.iterdir()) == ["cells.v"]


def test_a_partial_delivery_is_reported_before_an_oserror_re_raises(tmp_path):
    """As the local launch does (`Deliveries.deliver`): an `OSError` on the second file of a
    delivery must not lose the first from `results["deliveries"]`, the list that `--json`
    shows."""
    import xeda.deliver
    from xeda.deliver import Deliveries, ReadInputs
    from xeda.remote.fetch import deliver_fetched
    from xeda.run_root import ensure_run_root

    root = ensure_run_root(tmp_path / "run")
    assert root is not None
    mirror = root / "@host" / "d" / "flow"
    mirror.mkdir(parents=True)
    for name in ("a.txt", "b.txt"):
        (mirror / name).write_text(name)
    real_copy = xeda.deliver.Deliveries._copy
    made: list = []

    def copy_then_fail_second(self, delivery, source, destination, expected, sha, *rest):
        made.append(destination)
        if len(made) == 2:
            raise OSError("disk full")
        return real_copy(self, delivery, source, destination, expected, sha, *rest)

    results = {"success": True}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Deliveries, "_copy", copy_then_fail_second)
        with pytest.raises(OSError, match="disk full"):
            deliver_fetched(
                Deliveries(mirror, root, inputs=ReadInputs()),
                results,
                {"a": "/remote/run/a.txt", "b": "/remote/run/b.txt"},
                "/remote/run",
                mirror,
                outputs_to=tmp_path / "got",
            )
    assert len(results["deliveries"]) == 1
    delivered = Path(results["deliveries"][0]["to"])
    assert delivered.is_relative_to(tmp_path / "got") and delivered.is_file()


def _locked_elsewhere(run_path: Path) -> bool:
    """Whether another holder has `run_path`'s run directory lock: a lock taken through a new
    open file is refused while any other open file holds it, in this process too."""
    import fcntl

    with open(lock_file(run_path), "a") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return False


@pytest.mark.skipif(sys.platform == "win32", reason="no run directory lock on Windows")
def test_the_mirror_is_written_and_delivered_under_its_run_directory_lock(
    tmp_path, remote_host, monkeypatch
):
    """The mirror directory is another remote run's too: every write to it, and its delivery,
    holds its lock, as a local launch does, so concurrent runs take turns instead of mixing
    their files."""
    from xeda.deliver import Deliveries

    design = _sqrt_design(tmp_path / "design")
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere" / "sqrt" / "vivado_synth"
    writes: list = []

    def recorded(what, real):
        def call(*args, **kwargs):
            writes.append((what, _locked_elsewhere(mirror)))
            return real(*args, **kwargs)

        return call

    monkeypatch.setattr(client, "publish_mirror", recorded("publish", client.publish_mirror))
    monkeypatch.setattr(client, "rewrite_records", recorded("records", client.rewrite_records))
    monkeypatch.setattr(Deliveries, "deliver", recorded("deliver", Deliveries.deliver))
    document = _succeeded(
        remote_request(
            tmp_path,
            design,
            "vivado_synth",
            flow_settings=SQRT_SETTINGS,
            outputs_to=tmp_path / "got",
        )
    )
    assert document["results"]["deliveries"]
    assert {what for what, _ in writes} == {"publish", "records", "deliver"}
    assert [what for what, locked in writes if not locked] == []
    assert not _locked_elsewhere(mirror), "the lock is released once the run is delivered"


def test_a_refused_delivery_leaves_the_mirror_whole_and_unlocked(
    tmp_path, remote_host, monkeypatch
):
    design = _sqrt_design(tmp_path / "design")
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere" / "sqrt" / "vivado_synth"

    def refuse(*args, **kwargs):
        raise DeliveryError("refused")

    monkeypatch.setattr(client, "deliver_fetched", refuse)
    outcome = remote_request(
        tmp_path,
        design,
        "vivado_synth",
        flow_settings=SQRT_SETTINGS,
        outputs_to=tmp_path / "got",
    )
    assert isinstance(outcome.error, DeliveryError) and not outcome.success
    assert outcome.document["error"]["type"] == "DeliveryError"
    # the records of the mirror were written before the delivery, and name the mirror
    saved = json.loads((mirror / "results.json").read_text())
    assert saved["outcome"] == "passed" and str(mirror) not in json.dumps(saved)
    assert not _locked_elsewhere(mirror)
    assert not (tmp_path / "got").exists()


def test_a_failed_delivery_keeps_the_result_the_mirror_and_the_copies_in_the_document(
    tmp_path, remote_host, monkeypatch
):
    """As a local run does: the copies made before the error stay in the document. The remote
    result and the places in the mirror stay too, and the mirror stays whole."""
    import xeda.deliver
    from xeda.deliver import Deliveries

    design = _sqrt_design(tmp_path / "design")
    mirror = tmp_path / "local" / "xeda_run" / "@somewhere" / "sqrt" / "vivado_synth"
    real_copy = xeda.deliver.Deliveries._copy
    made: list = []

    def copy_then_fail_second(self, delivery, source, destination, expected, sha, *rest):
        made.append(destination)
        if len(made) == 2:
            raise OSError("disk full")
        return real_copy(self, delivery, source, destination, expected, sha, *rest)

    monkeypatch.setattr(Deliveries, "_copy", copy_then_fail_second)
    outcome = remote_request(
        tmp_path,
        design,
        "vivado_synth",
        flow_settings=SQRT_SETTINGS,
        outputs_to=tmp_path / "got",
    )
    document = outcome.document
    assert isinstance(outcome.error, OSError) and not outcome.success
    assert document["success"] is False
    assert document["error"]["type"] == "OSError"
    assert "disk full" in document["error"]["message"]
    assert document["remote"] == "somewhere" and document["mirror"] == str(mirror.parent.parent)
    assert document["run_path"] == str(mirror)
    assert document["results"]["success"] is True  # what the remote run reported
    (delivered,) = document["results"]["deliveries"]
    assert Path(delivered["to"]).is_relative_to(tmp_path / "got")
    assert Path(delivered["to"]).is_file()
    (node,) = [n for n in document["nodes"] if n["node"] == "vivado_synth"]
    assert node["run_path"] == str(mirror)
    assert node["deliveries"] == document["results"]["deliveries"]
    assert (mirror / "results.json").is_file()  # the mirror stays whole


def test_a_local_run_root_with_a_space_does_not_stop_a_remote_build(
    tmp_path, remote_host, monkeypatch
):
    """The run directories of the build are on the remote host, and the launch there judges
    them. The mirror on this side is no build directory."""
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "ssh")  # the request stops where it connects
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    design = tmp_path / "d.yaml"
    design.write_text(
        "name: d\nrtl:\n  sources: [top.v]\n  top: top\ntb:\n  sources: [top.v]\n  top: top\n"
    )
    outcome = remote_request(tmp_path, design, "verilator", run_root=tmp_path / "a b" / "xeda_run")
    assert isinstance(outcome.error, RemoteConnectionError), outcome.document
