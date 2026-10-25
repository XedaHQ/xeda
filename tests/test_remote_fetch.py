"""The fetch rules of a remote run: what comes back, how it lands in the mirror, and how the
results point at it."""

import io
import json
import logging
import os
from pathlib import Path, PurePosixPath

import pytest

from xeda.deliver import DeliveryError
from xeda.digest import content_digest
from xeda.remote.errors import RemoteFetchError, RemoteProtocolError
from xeda.remote.fetch import (
    NodeResult,
    SkippedSource,
    mirror_relative,
    publish_mirror,
    rewrite_results,
    select_fetch,
)
from xeda.remote.manifest import Entry, build_manifest, write_tar

from .record_documents import record_document

pytestmark = pytest.mark.python_compat


def _run_dir(root: Path, name: str = "d/flow") -> Path:
    """A run directory with a file of every kind that the rules tell apart."""
    run = root / name
    (run / "reports").mkdir(parents=True)
    (run / "reports" / "deep").mkdir()
    (run / "other").mkdir()
    for relative in (
        "results.json",
        "settings.json",
        "trace.json",
        "flow.log",
        "notes.txt",
        "out.bit",
        "reports/timing.rpt",
        "reports/deep/more.rpt",
        "other/x.v",
    ):
        (run / relative).write_text(f"{name}/{relative}\n")
    return run


def _node(run: Path, *, named=False, success=True, written=None, **results) -> NodeResult:
    document = record_document("passed" if success else "failed", **results)
    return NodeResult(run.name, run, document, named, written)


def test_fetch_default_selection(tmp_path):
    run = _run_dir(tmp_path)
    selected = select_fetch(tmp_path, [_node(run)], fetch_all=False).files
    assert set(selected) == {
        "d/flow/results.json",
        "d/flow/settings.json",
        "d/flow/flow.log",
        "d/flow/reports/timing.rpt",
        "d/flow/reports/deep/more.rpt",
    }
    assert all(path.is_file() for path in selected.values())


def test_a_named_node_also_fetches_its_outputs_and_artifacts(tmp_path):
    run = _run_dir(tmp_path)
    node = _node(
        run,
        named=True,
        artifacts={"bitstream": "out.bit", "trace": "trace.json"},
        outputs={"netlist": ["other/x.v"]},
    )
    selected = select_fetch(tmp_path, [node], fetch_all=False).files
    assert set(selected) == {
        "d/flow/results.json",
        "d/flow/settings.json",
        "d/flow/flow.log",
        "d/flow/reports/timing.rpt",
        "d/flow/reports/deep/more.rpt",
        "d/flow/out.bit",
        "d/flow/other/x.v",
    }


def _input_row(name: str, producer: str) -> dict:
    return {
        "name": name,
        "origin": "producer",
        "producer": producer,
        "output": "out",
        "producer_run_id": "f" * 32,
        "producer_run_dir": f"d/{producer}",
        "generation": None,
        "sources": [],
    }


def test_a_record_with_several_rows_for_one_input_is_still_a_record(tmp_path):
    """An input that several producers feed repeats its name in `inputs`: the client reads the
    record, so the node still comes back with its outputs."""
    run = _run_dir(tmp_path)
    rows = [_input_row("more", name) for name in ("right", "left", "right")]
    node = _node(run, named=True, outputs={"netlist": ["other/x.v"]}, inputs=rows)
    selected = select_fetch(tmp_path, [node], fetch_all=False)
    assert "d/flow/other/x.v" in selected


def test_a_node_the_request_does_not_name_returns_no_outputs(tmp_path):
    run = _run_dir(tmp_path)
    selected = select_fetch(
        tmp_path, [_node(run, artifacts={"bit": "out.bit"})], fetch_all=False
    ).files
    assert "d/flow/out.bit" not in selected


def test_fetch_all_selection(tmp_path):
    run = _run_dir(tmp_path)
    selected = select_fetch(tmp_path, [_node(run)], fetch_all=True).files
    assert set(selected) == {
        f"d/flow/{name}"
        for name in (
            "results.json",
            "settings.json",
            "flow.log",
            "notes.txt",
            "out.bit",
            "reports/timing.rpt",
            "reports/deep/more.rpt",
            "other/x.v",
        )
    }


def test_two_nodes_have_their_own_places(tmp_path):
    first, second = _run_dir(tmp_path, "d/yosys"), _run_dir(tmp_path, "d/arty/nextpnr")
    selected = select_fetch(tmp_path, [_node(first), _node(second)], fetch_all=False).files
    assert "d/yosys/results.json" in selected and "d/arty/nextpnr/results.json" in selected


def test_failed_node_fetches_only_what_it_wrote(tmp_path):
    run = _run_dir(tmp_path)  # every file is here, as an earlier successful run left it
    results = dict(artifacts={"bit": "out.bit", "stale": "other/x.v"}, outputs={"x": ["other/x.v"]})
    written = frozenset({"results.json", "flow.log", "out.bit", "notes.txt"})
    for fetch_all in (False, True):
        node = _node(run, named=True, success=False, written=written, **results)
        selected = select_fetch(tmp_path, [node], fetch_all=fetch_all).files
        expected = {"results.json", "flow.log", "out.bit"} | ({"notes.txt"} if fetch_all else set())
        assert set(selected) == {f"d/flow/{name}" for name in expected}
    # with no word from the remote about what the run wrote, nothing comes back
    node = _node(run, named=True, success=False, written=None, **results)
    assert select_fetch(tmp_path, [node], fetch_all=True).files == {}


def test_a_missing_file_of_a_passed_node_is_an_error_and_of_a_failed_node_is_not(tmp_path):
    run = _run_dir(tmp_path)
    artifacts = {"gone": "never.bit"}
    with pytest.raises(RemoteFetchError, match="never.bit"):
        select_fetch(tmp_path, [_node(run, named=True, artifacts=artifacts)], fetch_all=False).files
    failed = _node(
        run, named=True, success=False, written=frozenset({"never.bit"}), artifacts=artifacts
    )
    assert "d/flow/never.bit" not in select_fetch(tmp_path, [failed], fetch_all=False).files


@pytest.mark.parametrize("name", ["/etc/passwd", "../other.v", "a/../../b.v"])
def test_a_record_that_names_a_file_outside_the_run_directory_is_no_record(tmp_path, name):
    """Nothing is taken for the names of such a record, and the node counts as not passed."""
    run = _run_dir(tmp_path)
    node = _node(run, named=True, outputs={"x": [name]}, artifacts={"bit": "out.bit"})
    selected = select_fetch(tmp_path, [node], fetch_all=False).files
    assert "d/flow/out.bit" not in selected and "d/flow/results.json" not in selected


def test_a_node_without_a_record_comes_back_as_a_node_that_did_not_pass(tmp_path):
    run = _run_dir(tmp_path)
    old = NodeResult(run.name, run, {"success": True}, True, frozenset({"flow.log"}))
    assert set(select_fetch(tmp_path, [old], fetch_all=False).files) == {"d/flow/flow.log"}


def test_links_a_tool_left_are_not_followed(tmp_path):
    run = _run_dir(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n")
    (run / "reports" / "link.rpt").symlink_to(secret)
    (run / "reports" / "inner.rpt").symlink_to(run / "reports" / "timing.rpt")
    (run / "reports" / "dir").symlink_to(tmp_path)
    (run / "other" / "dirlink").symlink_to(run / "reports", target_is_directory=True)
    (run / "run.log").symlink_to(secret)
    for fetch_all in (False, True):
        selected = select_fetch(tmp_path, [_node(run)], fetch_all=fetch_all).files
        assert not any(name.startswith("d/flow/reports/dir/") for name in selected)
        assert not any(name.startswith("d/flow/other/dirlink/") for name in selected)
        for linked in ("reports/link.rpt", "reports/inner.rpt", "run.log"):
            assert f"d/flow/{linked}" not in selected, "a link is not a file to fetch"
        assert "d/flow/reports/timing.rpt" in selected
        assert all(not path.is_symlink() for path in selected.values())


@pytest.mark.parametrize("fetch_all", [False, True])
def test_a_linked_reports_directory_is_not_entered(tmp_path, fetch_all):
    run = tmp_path / "d" / "flow"
    run.mkdir(parents=True)
    (run / "results.json").write_text("{}\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.rpt").write_text("secret\n")
    (run / "reports").symlink_to(elsewhere, target_is_directory=True)
    selected = select_fetch(tmp_path, [_node(run)], fetch_all=fetch_all).files
    assert set(selected) == {"d/flow/results.json"}


@pytest.mark.parametrize("where", ["at the name", "on the way"])
def test_an_artifact_that_a_link_leads_out_of_the_run_directory_is_skipped(tmp_path, where):
    """A tool may leave a link, at the name of an artifact or on the way to it. What it leads to
    outside the run directory is not fetched: the artifact is listed as skipped, with where it
    leads, as a local delivery skips it."""
    run = _run_dir(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.bit").write_text("outside\n")
    if where == "at the name":
        name = "x.bit"
        (run / name).symlink_to(elsewhere / "x.bit")
    else:
        name = "build/x.bit"
        (run / "build").symlink_to(elsewhere, target_is_directory=True)
    selection = select_fetch(
        tmp_path, [_node(run, named=True, artifacts={"bit": name})], fetch_all=False
    )
    assert f"d/flow/{name}" not in selection.files
    assert not [path for path in selection.files.values() if path.is_relative_to(elsewhere)]
    assert selection.skipped == [
        SkippedSource("flow", name, str(run / name), os.path.realpath(elsewhere / "x.bit"))
    ]


@pytest.mark.parametrize("where", ["at the name", "on the way"])
def test_an_artifact_that_a_link_leads_to_a_file_of_the_run_directory_comes_back(tmp_path, where):
    """A link that stays in the run directory brings back the file it leads to, under its name."""
    run = _run_dir(tmp_path)
    if where == "at the name":
        name = "linked.bit"
        (run / name).symlink_to("out.bit")
        target = run / "out.bit"
    else:
        name = "alias/x.v"
        (run / "alias").symlink_to("other", target_is_directory=True)
        target = run / "other" / "x.v"
    selection = select_fetch(
        tmp_path, [_node(run, named=True, artifacts={"bit": name})], fetch_all=False
    )
    assert selection.files[f"d/flow/{name}"] == target.resolve()
    assert selection.skipped == []


def test_a_named_output_that_a_link_leads_out_of_the_run_directory_is_an_error(tmp_path):
    run = _run_dir(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n")
    (run / "linked.bit").symlink_to(secret)
    node = _node(run, named=True, outputs={"bit": ["linked.bit"]})
    with pytest.raises(RemoteFetchError, match="declared output.*outside its run directory"):
        select_fetch(tmp_path, [node], fetch_all=False)


def test_a_named_link_that_leads_nowhere_is_an_error(tmp_path):
    run = _run_dir(tmp_path)
    (run / "dangling.bit").symlink_to(tmp_path / "nowhere")
    with pytest.raises(RemoteFetchError, match="leads nowhere"):
        select_fetch(
            tmp_path, [_node(run, named=True, artifacts={"bit": "dangling.bit"})], fetch_all=False
        )


@pytest.mark.parametrize("inside", [False, True])
def test_a_failed_run_brings_back_no_link(tmp_path, inside):
    run = _run_dir(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n")
    (run / "linked.bit").symlink_to(run / "out.bit" if inside else secret)
    failed = _node(
        run,
        named=True,
        success=False,
        written=frozenset({"linked.bit"}),
        artifacts={"b": "linked.bit"},
    )
    selection = select_fetch(tmp_path, [failed], fetch_all=False)
    assert "d/flow/linked.bit" not in selection.files
    assert selection.skipped == []


def test_a_skipped_source_from_the_remote_is_checked():
    good = {"node": "f", "place": "x.bit", "source": "/r/x.bit", "target": "/etc/x"}
    assert SkippedSource.from_json_value(good).as_json_value() == good
    for bad in (
        {**good, "place": "../x.bit"},
        {**good, "place": "/x.bit"},
        {**good, "target": 3},
        {key: value for key, value in good.items() if key != "node"},
        {**good, "more": "x"},
        "x.bit",
    ):
        with pytest.raises(RemoteFetchError):
            SkippedSource.from_json_value(bad)


def test_rewritten_results_keep_the_place_of_a_skipped_artifact(tmp_path):
    """A skipped artifact keeps its place in the mirror, where no file lies: the next launch
    then asks about a file in the way of its delivery before it runs, as a local launch does."""
    from xeda.deliver import outputs_to_deliveries

    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "out.bit").write_text("x\n")
    results = {"success": True, "artifacts": {"bit": "out.bit", "log": "tool.log"}}
    rewritten = rewrite_results(results, PurePosixPath("/r/d/flow"), mirror, skipped={"tool.log"})
    assert rewritten["artifacts"] == {
        "bit": str(mirror / "out.bit"),
        "log": str(mirror / "tool.log"),
    }
    assert not os.path.lexists(mirror / "tool.log")
    predicted = outputs_to_deliveries(rewritten["artifacts"], mirror, tmp_path / "out")
    assert sorted(d.name.as_posix() for d in predicted) == ["out.bit", "tool.log"]
    with pytest.raises(RemoteFetchError, match="not in the mirror"):
        rewrite_results(results, PurePosixPath("/r/d/flow"), mirror)


def test_mirror_relative_keeps_a_place_in_the_run_directory_and_gives_the_rest_none():
    run = "/remote/xeda_run/d/flow"
    assert mirror_relative("reports/a.rpt", run) == PurePosixPath("reports/a.rpt")
    assert mirror_relative(f"{run}/out/b.bit", run) == PurePosixPath("out/b.bit")
    assert mirror_relative("_external/b.bit", run) == PurePosixPath("_external/b.bit")
    assert mirror_relative("/remote/other/b.bit", run) is None
    assert mirror_relative("../up/b.bit", run) is None
    assert mirror_relative(f"{run}/../b.bit", run) is None


# --------------------------------------------------------------------------------- the mirror


def _publish(run_root: Path, selected: dict[str, Path], mirror: Path, **kwargs) -> dict[str, Path]:
    entries = build_manifest(selected)
    stream = io.BytesIO()
    write_tar(entries, selected, {}, stream)
    return publish_mirror(
        mirror,
        kwargs.pop("entries", entries),
        io.BytesIO(stream.getvalue()),
        remote_run_root=PurePosixPath("/remote/xeda_run"),
    )


def test_the_mirror_gets_the_selected_files(tmp_path):
    remote_root = tmp_path / "remote"
    run = _run_dir(remote_root)
    (run / "out.bit").chmod(0o755)
    selected = select_fetch(
        remote_root, [_node(run, named=True, artifacts={"b": "out.bit"})], fetch_all=False
    ).files
    run_root = tmp_path / "local"
    run_root.mkdir()
    mirror = run_root / "@host"
    published = _publish(remote_root, selected, mirror)
    assert set(published) == {f"/remote/xeda_run/{name}" for name in selected}
    for name, source in selected.items():
        assert (mirror / name).read_text() == source.read_text()
        assert published[f"/remote/xeda_run/{name}"] == mirror / name
    assert os.access(mirror / "d/flow/out.bit", os.X_OK)
    assert not os.access(mirror / "d/flow/flow.log", os.X_OK)


def test_mirror_files_are_verified_and_published_atomically(tmp_path):
    remote_root = tmp_path / "remote"
    run = _run_dir(remote_root)
    selected = {"d/flow/results.json": run / "results.json", "d/flow/flow.log": run / "flow.log"}
    entries = build_manifest(selected)
    wrong = [
        (
            Entry(entry.path, entry.size, "0" * 64, entry.executable)
            if entry.path.endswith("flow.log")
            else entry
        )
        for entry in entries
    ]
    run_root = tmp_path / "local"
    run_root.mkdir()
    mirror = run_root / "@host"
    old = mirror / "d/flow/flow.log"
    old.parent.mkdir(parents=True)
    old.write_text("the earlier fetch\n")
    stream = io.BytesIO()
    write_tar(entries, selected, {}, stream)
    with pytest.raises(RemoteFetchError, match="does not match its digest"):
        publish_mirror(
            mirror, wrong, io.BytesIO(stream.getvalue()), remote_run_root=PurePosixPath("/r")
        )
    assert old.read_text() == "the earlier fetch\n", "a bad file replaces nothing"
    assert [p.name for p in old.parent.iterdir() if p.name.startswith(".")] == [], "no temporaries"


def test_a_file_in_the_stream_that_was_not_asked_for_is_refused(tmp_path):
    remote_root = tmp_path / "remote"
    run = _run_dir(remote_root)
    selected = {"d/flow/results.json": run / "results.json", "d/flow/flow.log": run / "flow.log"}
    stream = io.BytesIO()
    write_tar(build_manifest(selected), selected, {}, stream)
    run_root = tmp_path / "local"
    run_root.mkdir()
    asked = build_manifest({"d/flow/results.json": run / "results.json"})
    with pytest.raises(RemoteProtocolError):
        publish_mirror(
            run_root / "@host",
            asked,
            io.BytesIO(stream.getvalue()),
            remote_run_root=PurePosixPath("/r"),
        )
    assert not (run_root / "@host" / "d/flow/flow.log").exists()


def test_mirror_refuses_a_link_out_of_it(tmp_path):
    remote_root = tmp_path / "remote"
    run = _run_dir(remote_root)
    selected = select_fetch(
        remote_root, [_node(run, named=True, artifacts={"x": "out.bit"})], fetch_all=False
    ).files
    run_root = tmp_path / "local"
    run_root.mkdir()
    mirror = run_root / "@host"
    published = _publish(remote_root, selected, mirror)
    assert (mirror / "d/flow/out.bit").read_text() == "d/flow/out.bit\n"
    assert all(path.is_relative_to(mirror) for path in published.values())

    # a directory of the mirror that is a link out of it: nothing is written through it
    target = tmp_path / "victim"
    target.mkdir()
    fresh = run_root / "@other"
    (fresh / "d").mkdir(parents=True)
    (fresh / "d" / "flow").symlink_to(target, target_is_directory=True)
    with pytest.raises(RemoteFetchError, match="symbolic link"):
        _publish(remote_root, selected, fresh)
    assert list(target.iterdir()) == []


def test_the_mirror_replaces_a_link_at_the_name_of_a_file(tmp_path):
    remote_root = tmp_path / "remote"
    run = _run_dir(remote_root)
    selected = {"d/flow/results.json": run / "results.json"}
    run_root = tmp_path / "local"
    mirror = run_root / "@host"
    (mirror / "d/flow").mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n")
    (mirror / "d/flow/results.json").symlink_to(victim)
    _publish(remote_root, selected, mirror)
    assert victim.read_text() == "keep\n"
    assert not (mirror / "d/flow/results.json").is_symlink()
    assert (mirror / "d/flow/results.json").read_text() == (run / "results.json").read_text()


def test_the_mirror_must_lie_below_its_run_root(tmp_path):
    run_root = tmp_path / "local"
    run_root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (run_root / "@host").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(RemoteFetchError, match="@host is a symbolic link"):
        publish_mirror(run_root / "@host", [], io.BytesIO(), remote_run_root=PurePosixPath("/r"))
    assert list(elsewhere.iterdir()) == []


def test_a_mirror_that_is_a_link_inside_the_run_root_is_refused_too(tmp_path):
    run_root = tmp_path / "local"
    (run_root / "b").mkdir(parents=True)
    (run_root / "@host").symlink_to("b", target_is_directory=True)
    remote_root = tmp_path / "remote"
    run = _run_dir(remote_root)
    selected = {"d/flow/results.json": run / "results.json"}
    with pytest.raises(RemoteFetchError, match="@host is a symbolic link .to b.. Xeda neither"):
        _publish(remote_root, selected, run_root / "@host")
    assert list((run_root / "b").iterdir()) == []


@pytest.mark.parametrize("leads", ["inside", "outside"])
@pytest.mark.parametrize("level", ["mirror", "design"])
def test_the_mirror_of_an_unfinished_run_is_not_emptied_through_a_link(
    tmp_path, caplog, leads, level
):
    """`_discard_mirrors` locks before it clears, and the lock refuses a link on the way."""
    from types import SimpleNamespace

    from xeda.remote import client

    run_root = tmp_path / "xeda_run"
    (run_root / "b" / "yosys_fpga").mkdir(parents=True)
    (tmp_path / "outside" / "yosys_fpga").mkdir(parents=True)
    home = run_root / "b" if leads == "inside" else tmp_path / "outside"
    sentinel = home / "yosys_fpga" / "KEEP"
    sentinel.write_text("keep\n")
    if level == "mirror":
        link = run_root / "@host"
        link.symlink_to(home, target_is_directory=True)
        planned = run_root / "yosys_fpga"  # its mirror is @host/yosys_fpga
        (run_root / "yosys_fpga").mkdir()
    else:
        (run_root / "@host").mkdir()
        link = run_root / "@host" / "a"
        link.symlink_to(home, target_is_directory=True)
        planned = run_root / "a" / "yosys_fpga"
    local = SimpleNamespace(run_paths=[planned])
    with caplog.at_level("WARNING"):
        client._discard_mirrors(local, run_root / "@host", run_root)
    assert sentinel.read_text() == "keep\n"
    assert f"{link} is a symbolic link" in caplog.text


# --------------------------------------------------------------------------------- the results


def _mirror_with(tmp_path: Path, *names: str) -> Path:
    mirror_dir = tmp_path / "@host" / "d" / "flow"
    for name in names:
        (mirror_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (mirror_dir / name).write_text(f"{name}\n")
    return mirror_dir


def test_results_point_at_the_mirror(tmp_path, caplog):
    remote = PurePosixPath("/remote/xeda_run/d/flow")
    mirror_dir = _mirror_with(tmp_path, "other/x.v", "out.bit")
    output = mirror_dir / "other" / "x.v"
    results = {
        "success": True,
        "run_path": str(remote),
        "artifacts": {
            "netlist": "other/x.v",
            "bit": [f"{remote}/out.bit", "/remote/elsewhere/e.bin"],
            "none": None,
        },
        "outputs": {
            "netlist": {"path": f"{remote}/other/x.v", "sha": content_digest(output)},
        },
    }
    original = json.dumps(results)
    with caplog.at_level(logging.WARNING, logger="xeda.remote.fetch"):
        rewritten = rewrite_results(results, remote, mirror_dir)
    assert json.dumps(results) == original, "the argument stays as it is"
    assert rewritten["run_path"] == str(mirror_dir)
    # a file outside the run directory does not come back: it keeps its remote path
    assert rewritten["artifacts"] == {
        "netlist": str(mirror_dir / "other/x.v"),
        "bit": [str(mirror_dir / "out.bit"), "/remote/elsewhere/e.bin"],
        "none": None,
    }
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "/remote/elsewhere/e.bin" in warning and "never fetches" in warning
    assert rewritten["outputs"]["netlist"]["path"] == str(output)


def test_an_artifact_that_is_not_in_the_mirror_is_an_error(tmp_path):
    remote = PurePosixPath("/remote/xeda_run/d/flow")
    mirror_dir = _mirror_with(tmp_path, "out.bit")
    results = {"success": True, "artifacts": {"bit": "out.bit", "gone": "gone.bit"}}
    with pytest.raises(RemoteFetchError, match="gone.bit"):
        rewrite_results(results, remote, mirror_dir)


def test_a_failed_node_keeps_only_the_artifacts_its_run_wrote(tmp_path):
    remote = PurePosixPath("/remote/xeda_run/d/flow")
    mirror_dir = _mirror_with(tmp_path, "written.bit", "earlier.bit", "gone.bit")
    (mirror_dir / "gone.bit").unlink()
    results = {
        "success": False,
        "flow": "flow",
        "artifacts": {
            "written": "written.bit",
            "earlier": "earlier.bit",
            "gone": "gone.bit",
            "outside": "/remote/elsewhere/e.bin",
        },
    }
    rewritten = rewrite_results(
        results, remote, mirror_dir, written=frozenset({"written.bit", "gone.bit"})
    )
    assert rewritten["artifacts"] == {"written": str(mirror_dir / "written.bit")}
    # with no word from the remote about what the run wrote, none is kept
    assert rewrite_results(results, remote, mirror_dir)["artifacts"] == {}


def test_a_declared_output_that_changed_during_the_transfer_is_refused(tmp_path):
    remote = PurePosixPath("/remote/xeda_run/d/flow")
    mirror_dir = _mirror_with(tmp_path, "x.v")
    results = {
        "success": True,
        "outputs": {"n": {"path": f"{remote}/x.v", "sha": "0" * 32}},
    }
    with pytest.raises(DeliveryError, match="changed during transfer"):
        rewrite_results(results, remote, mirror_dir)
    (mirror_dir / "x.v").unlink()
    with pytest.raises(DeliveryError, match="not in the mirror"):
        rewrite_results(results, remote, mirror_dir)


def test_a_failed_node_has_no_declared_outputs(tmp_path):
    remote = PurePosixPath("/remote/xeda_run/d/flow")
    results = {"success": False, "outputs": {"n": {"path": f"{remote}/x.v", "sha": "0"}}}
    assert "outputs" not in rewrite_results(results, remote, tmp_path)


class _StubAgent:
    """Stands in for the agent of a session: it answers a fetch with `document`."""

    host = "somewhere"
    stdout = io.BytesIO()

    def __init__(self, document):
        self.document = document

    def send(self, document, step):
        assert document == {"fetch": {"fetch_all": False}}

    def message(self, step):
        return self.document


def test_the_client_reads_the_files_and_the_skipped_sources_of_a_fetch():
    from xeda.remote.client import _fetch

    skipped = {"node": "f", "place": "x.log", "source": "/r/x.log", "target": "/etc/x"}
    entries, sources, _reader = _fetch(_StubAgent({"files": [], "skipped": [skipped]}), False)
    assert entries == [] and sources == [SkippedSource("f", "x.log", "/r/x.log", "/etc/x")]
    for bad in ({"files": []}, {"files": [], "skipped": {}}, {"files": {}, "skipped": []}):
        with pytest.raises(RemoteProtocolError):
            _fetch(_StubAgent(bad), False)
    with pytest.raises(RemoteFetchError):
        _fetch(_StubAgent({"files": [], "skipped": [{**skipped, "place": "../x"}]}), False)


def test_the_delivered_node_lists_the_deliveries_made_from_the_mirror():
    """A local run lists its deliveries in its node; so does a remote one, in the node that this
    side delivered, skipped ones included."""
    from xeda.remote.client import _deliveries_of_the_node

    made = [{"setting": "--outputs-to", "from": "/r/x", "to": "/o/x", "state": "skipped"}]
    document = {"nodes": [{"node": "a", "deliveries": []}, {"node": "b", "deliveries": []}]}
    _deliveries_of_the_node(document, {"deliveries": made}, "b")
    assert document["nodes"] == [{"node": "a", "deliveries": []}, {"node": "b", "deliveries": made}]
    _deliveries_of_the_node(document, None, "a")
    assert document["nodes"][0]["deliveries"] == []
