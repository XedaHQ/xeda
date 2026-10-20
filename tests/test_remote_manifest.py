"""Digest-based sync: the manifest, the tar stream and the strict receiver."""

import hashlib
import io
import os
import tarfile
import tempfile
from pathlib import Path

import pytest

from xeda.remote.errors import RemoteError, RemoteFetchError, RemoteProtocolError
from xeda.remote.frames import DataReader, FrameWriter
from xeda.remote.manifest import (
    Entry,
    SyncPlan,
    apply_sync,
    build_manifest,
    plan_sync,
    safe_member_path,
    write_tar,
)

pytestmark = pytest.mark.python_compat


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sync(root: Path, tmp_path: Path, files: dict[str, bytes], executable=()) -> SyncPlan:
    """Sync `files` (path -> content) into `root` the way client and agent do."""
    sources = {}
    source_dir = Path(tempfile.mkdtemp(dir=tmp_path, prefix="sources"))
    for path, content in files.items():
        source = source_dir / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        source.chmod(0o755 if path in executable else 0o644)
        sources[path] = source
    entries = build_manifest(sources)
    plan = plan_sync(root, entries)
    wanted = {entry.path: entry for entry in entries}
    tar = io.BytesIO()
    write_tar([wanted[p] for p in plan.missing], sources, {}, tar)
    apply_sync(root, entries, plan, io.BytesIO(tar.getvalue()))
    return plan


def raw_tar(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> io.BytesIO:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w|") as archive:
        for info, content in members:
            archive.addfile(info, io.BytesIO(content) if content is not None else None)
    return io.BytesIO(out.getvalue())


def regular(name: str, content: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    return info, content


def entry_for(name: str, content: bytes, executable: bool = False) -> Entry:
    return Entry(name, len(content), sha(content), executable)


def tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): ("dir" if p.is_dir() else p.read_text())
        for p in sorted(root.rglob("*"))
    }


# --- names --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["/etc/passwd", "../x", "a/../../x", "", "a/..", ".", "a/./b"])
def test_unsafe_member_names_are_refused(name):
    with pytest.raises(RemoteProtocolError):
        safe_member_path(name)
    with pytest.raises(RemoteProtocolError):
        Entry(name, 0, sha(b""), False)


@pytest.mark.parametrize("name", ["a//b", "a/", "a\0b", "./a"])
def test_other_malformed_member_names_are_refused(name):
    with pytest.raises(RemoteProtocolError):
        safe_member_path(name)


def test_safe_names_are_kept():
    assert str(safe_member_path("src/a b/c.v")) == "src/a b/c.v"
    assert str(safe_member_path("..hidden/.x")) == "..hidden/.x"


@pytest.mark.parametrize("name", ["/etc/passwd", "../x", "a/../../x"])
def test_unsafe_names_in_a_tar_are_refused_and_nothing_is_written(tmp_path, name):
    root = tmp_path / "inputs"
    entry = entry_for("ok.v", b"x")
    tar = raw_tar([regular(name, b"x"), regular("ok.v", b"x")])
    with pytest.raises(RemoteProtocolError):
        apply_sync(root, [entry], SyncPlan(["ok.v"], []), tar)
    assert tree(root) == {}
    assert not (tmp_path / "x").exists()


# --- manifest ------------------------------------------------------------------------------


def test_manifest_describes_files_and_generated_content(tmp_path):
    (tmp_path / "a.v").write_bytes(b"module a;")
    script = tmp_path / "run.sh"
    script.write_bytes(b"#!/bin/sh\n")
    script.chmod(0o755)
    entries = build_manifest(
        {"z/a.v": tmp_path / "a.v", "run.sh": script}, {"design.yaml": b"x: 1"}
    )
    assert entries == [
        Entry("design.yaml", 4, sha(b"x: 1"), False),
        Entry("run.sh", 10, sha(b"#!/bin/sh\n"), True),
        Entry("z/a.v", 9, sha(b"module a;"), False),
    ]


def test_manifest_refuses_clashes_and_non_files(tmp_path):
    (tmp_path / "a.v").write_bytes(b"x")
    with pytest.raises(ValueError, match="both a file and generated"):
        build_manifest({"a.v": tmp_path / "a.v"}, {"a.v": b"y"})
    with pytest.raises(ValueError, match="not a regular file"):
        build_manifest({"d": tmp_path})
    with pytest.raises(RemoteProtocolError, match="as file and as directory"):
        build_manifest({"a": tmp_path / "a.v", "a/b": tmp_path / "a.v"})
    with pytest.raises(RemoteProtocolError):
        build_manifest({"../a": tmp_path / "a.v"})


def test_manifest_follows_a_local_source_link(tmp_path):
    (tmp_path / "real.v").write_bytes(b"real")
    (tmp_path / "link.v").symlink_to("real.v")
    assert build_manifest({"x.v": tmp_path / "link.v"}) == [Entry("x.v", 4, sha(b"real"), False)]


def test_entry_json_is_strict():
    entry = entry_for("a/b.v", b"x", True)
    assert Entry.from_json_value(entry.as_json_value()) == entry
    good = entry.as_json_value()
    for bad in (
        {**good, "extra": 1},
        {k: v for k, v in good.items() if k != "size"},
        {**good, "size": -1},
        {**good, "size": True},
        {**good, "size": "1"},
        {**good, "sha256": good["sha256"].upper()},
        {**good, "sha256": "ab"},
        {**good, "executable": 1},
        {**good, "path": "../a"},
        [],
        None,
    ):
        with pytest.raises(RemoteProtocolError):
            Entry.from_json_value(bad)


def test_duplicate_and_conflicting_entries_are_refused(tmp_path):
    a = entry_for("a", b"x")
    with pytest.raises(RemoteProtocolError, match="twice"):
        plan_sync(tmp_path, [a, a])
    with pytest.raises(RemoteProtocolError, match="file and as directory"):
        plan_sync(tmp_path, [a, entry_for("a/b", b"x")])


# --- the sync ------------------------------------------------------------------------------


def test_first_sync_writes_everything_with_modes(tmp_path):
    root = tmp_path / "inputs"
    plan = sync(root, tmp_path, {"a.v": b"A", "sub/b.v": b"B", "run.sh": b"#!"}, ["run.sh"])
    assert plan == SyncPlan(["a.v", "run.sh", "sub/b.v"], [])
    assert tree(root) == {"a.v": "A", "run.sh": "#!", "sub": "dir", "sub/b.v": "B"}
    assert os.access(root / "run.sh", os.X_OK)
    assert not os.access(root / "a.v", os.X_OK)
    assert (root / "a.v").stat().st_mode & 0o777 == 0o644
    assert (root / "run.sh").stat().st_mode & 0o777 == 0o755


def test_unchanged_file_keeps_its_inode_changed_file_gets_a_new_one(tmp_path):
    root = tmp_path / "inputs"
    sync(root, tmp_path, {"keep.v": b"K", "edit.v": b"1"})
    keep, edit = (root / "keep.v").stat(), (root / "edit.v").stat()
    plan = sync(root, tmp_path, {"keep.v": b"K", "edit.v": b"2"})
    assert plan == SyncPlan(["edit.v"], [])
    assert (root / "keep.v").stat().st_ino == keep.st_ino
    assert (root / "keep.v").stat().st_mtime_ns == keep.st_mtime_ns
    assert (root / "edit.v").read_bytes() == b"2"
    assert (root / "edit.v").stat().st_ino != edit.st_ino


def test_same_size_different_content_is_a_change(tmp_path):
    root = tmp_path / "inputs"
    sync(root, tmp_path, {"a.v": b"abc"})
    assert sync(root, tmp_path, {"a.v": b"abd"}).missing == ["a.v"]
    assert (root / "a.v").read_bytes() == b"abd"


def test_executable_bit_is_part_of_the_comparison(tmp_path):
    root = tmp_path / "inputs"
    sync(root, tmp_path, {"run.sh": b"x"})
    assert sync(root, tmp_path, {"run.sh": b"x"}, ["run.sh"]).missing == ["run.sh"]
    assert os.access(root / "run.sh", os.X_OK)
    assert sync(root, tmp_path, {"run.sh": b"x"}).missing == ["run.sh"]
    assert not os.access(root / "run.sh", os.X_OK)


def test_file_that_left_the_manifest_is_removed(tmp_path):
    root = tmp_path / "inputs"
    sync(root, tmp_path, {"a.v": b"A", "gone/x.v": b"X", "gone/deep/y.v": b"Y", "keep/z.v": b"Z"})
    (root / "empty").mkdir()
    plan = sync(root, tmp_path, {"a.v": b"A", "keep/z.v": b"Z"})
    assert plan == SyncPlan([], ["gone/deep/y.v", "gone/x.v"])
    assert tree(root) == {"a.v": "A", "keep": "dir", "keep/z.v": "Z"}


def test_leftover_temporary_files_are_stale(tmp_path):
    root = tmp_path / "inputs"
    sync(root, tmp_path, {"a.v": b"A"})
    (root / ".xeda-0123456789abcdef.tmp").write_bytes(b"partial")
    assert sync(root, tmp_path, {"a.v": b"A"}).stale == [".xeda-0123456789abcdef.tmp"]
    assert tree(root) == {"a.v": "A"}


def test_directory_where_a_file_belongs_and_file_where_a_directory_belongs(tmp_path):
    root = tmp_path / "inputs"
    sync(root, tmp_path, {"a/b.v": b"1", "c": b"file"})
    plan = sync(root, tmp_path, {"a": b"now a file", "c/d.v": b"2"})
    assert plan.missing == ["a", "c/d.v"]
    assert tree(root) == {"a": "now a file", "c": "dir", "c/d.v": "2"}


def test_empty_sync_with_an_empty_archive_changes_nothing(tmp_path):
    root = tmp_path / "inputs"
    sync(root, tmp_path, {"a.v": b"A"})
    apply_sync(root, [entry_for("a.v", b"A")], SyncPlan([], []), raw_tar([]))
    assert tree(root) == {"a.v": "A"}


def test_plan_for_a_root_that_does_not_exist(tmp_path):
    entries = [entry_for("a.v", b"A")]
    assert plan_sync(tmp_path / "nope", entries) == SyncPlan(["a.v"], [])
    assert not (tmp_path / "nope").exists()


def test_sync_through_frames(tmp_path):
    """The tar goes in DATA frames; after the sync the next frame is the one after END."""
    sources = {"a.v": tmp_path / "a.v", "b/c.v": tmp_path / "c.v"}
    (tmp_path / "a.v").write_bytes(b"A" * 100_000)
    (tmp_path / "c.v").write_bytes(b"C")
    entries = build_manifest(sources)
    tar = io.BytesIO()
    write_tar(entries, sources, {}, tar)
    wire = io.BytesIO()
    writer = FrameWriter(wire)
    writer.send_stream(io.BytesIO(tar.getvalue()), chunk=4096)
    writer.send_json({"done": True})
    stream = io.BytesIO(wire.getvalue())
    root = tmp_path / "inputs"
    apply_sync(root, entries, plan_sync(root, entries), io.BufferedReader(DataReader(stream)))
    assert (root / "a.v").read_bytes() == b"A" * 100_000
    from xeda.remote.frames import read_frame

    assert read_frame(stream) == (b"J", b'{"done":true}')


def test_generated_content_and_changed_source_in_write_tar(tmp_path):
    source = tmp_path / "a.v"
    source.write_bytes(b"one")
    entries = build_manifest({"a.v": source}, {"design.yaml": b"x: 1"})
    tar = io.BytesIO()
    write_tar(entries, {"a.v": source}, {"design.yaml": b"x: 1"}, tar)
    with tarfile.open(fileobj=io.BytesIO(tar.getvalue())) as archive:
        assert archive.getnames() == ["a.v", "design.yaml"]
        member = archive.getmember("a.v")
        assert (member.uid, member.gid, member.mtime, member.mode) == (0, 0, 0, 0o644)
    source.write_bytes(b"two")  # changed after build_manifest
    with pytest.raises(RemoteError, match="changed while it was packed"):
        write_tar(entries, {"a.v": source}, {"design.yaml": b"x: 1"}, io.BytesIO())
    source.write_bytes(b"t")  # shorter than at build_manifest
    with pytest.raises(RemoteError, match="changed while it was packed"):
        write_tar(entries, {"a.v": source}, {"design.yaml": b"x: 1"}, io.BytesIO())
    source.write_bytes(b"three")  # longer
    with pytest.raises(RemoteError, match="changed while it was packed"):
        write_tar(entries, {"a.v": source}, {"design.yaml": b"x: 1"}, io.BytesIO())
    with pytest.raises(ValueError, match="no file or content"):
        write_tar(entries, {}, {}, io.BytesIO())


def test_tar_is_deterministic(tmp_path):
    source = tmp_path / "a.v"
    source.write_bytes(b"one")
    entries = build_manifest({"a.v": source})
    first, second = io.BytesIO(), io.BytesIO()
    write_tar(entries, {"a.v": source}, {}, first)
    os.utime(source, (12345, 12345))
    write_tar(entries, {"a.v": source}, {}, second)
    assert first.getvalue() == second.getvalue()


# --- links and special members -------------------------------------------------------------


def test_link_in_the_destination_is_never_followed(tmp_path):
    root = tmp_path / "inputs"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "canary.txt").write_text("outside")
    root.mkdir()
    (root / "sub").symlink_to(outside, target_is_directory=True)
    plan = sync(root, tmp_path, {"sub/a.v": b"A"})
    assert plan.missing == ["sub/a.v"]
    assert not (root / "sub").is_symlink()
    assert (root / "sub" / "a.v").read_bytes() == b"A"
    assert tree(outside) == {"canary.txt": "outside"}


def test_link_to_a_file_where_a_directory_belongs_is_replaced(tmp_path):
    root = tmp_path / "inputs"
    root.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("keep")
    (root / "sub").symlink_to(target)
    sync(root, tmp_path, {"sub/a.v": b"A"})
    assert (root / "sub" / "a.v").read_bytes() == b"A"
    assert target.read_text() == "keep"


def test_link_at_a_file_name_is_replaced_not_written_through(tmp_path):
    root = tmp_path / "inputs"
    root.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("keep")
    (root / "a.v").symlink_to(target)
    plan = sync(root, tmp_path, {"a.v": b"new"})
    assert plan.missing == ["a.v"]
    assert target.read_text() == "keep"
    assert not (root / "a.v").is_symlink()
    assert (root / "a.v").read_bytes() == b"new"


def test_a_link_to_the_right_content_is_not_a_match(tmp_path):
    root = tmp_path / "inputs"
    root.mkdir()
    target = tmp_path / "target.txt"
    target.write_bytes(b"A")
    (root / "a.v").symlink_to(target)
    assert plan_sync(root, [entry_for("a.v", b"A")]).missing == ["a.v"]


def test_stale_links_are_removed_as_links_and_never_entered(tmp_path):
    root = tmp_path / "inputs"
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "f.txt").write_text("outside")
    sync(root, tmp_path, {"a.v": b"A"})
    (root / "dirlink").symlink_to(outside, target_is_directory=True)
    (root / "filelink").symlink_to(outside / "deep" / "f.txt")
    (root / "dangling").symlink_to("nowhere")
    plan = sync(root, tmp_path, {"a.v": b"A"})
    assert plan.stale == ["dangling", "dirlink", "filelink"]
    assert tree(root) == {"a.v": "A"}
    assert tree(outside) == {"deep": "dir", "deep/f.txt": "outside"}


def test_link_at_depth_two_is_never_followed(tmp_path):
    root = tmp_path / "inputs"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "canary.txt").write_text("outside")
    (root / "a").mkdir(parents=True)
    (root / "a" / "b").symlink_to(outside, target_is_directory=True)
    plan = sync(root, tmp_path, {"a/b/c.v": b"C"})
    assert plan.missing == ["a/b/c.v"]
    assert not (root / "a" / "b").is_symlink()
    assert (root / "a" / "b" / "c.v").read_bytes() == b"C"
    assert tree(outside) == {"canary.txt": "outside"}
    # planning does not follow it either
    (root / "a" / "b").rename(tmp_path / "moved")
    (root / "a" / "b").symlink_to(outside, target_is_directory=True)
    assert plan_sync(root, [entry_for("a/b/canary.txt", b"outside")]).missing == ["a/b/canary.txt"]


def test_the_root_itself_may_not_be_a_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "inputs"
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RemoteError, match="not a real directory"):
        plan_sync(root, [entry_for("a.v", b"A")])
    with pytest.raises(RemoteError, match="not a real directory"):
        apply_sync(root, [], SyncPlan([], []), raw_tar([]))
    assert tree(outside) == {}


@pytest.mark.parametrize(
    "kind",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.DIRTYPE,
        tarfile.FIFOTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
    ],
)
def test_link_and_device_members_are_refused(tmp_path, kind):
    root = tmp_path / "inputs"
    info = tarfile.TarInfo("a.v")
    info.type = kind
    info.linkname = "/etc/passwd" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
    with pytest.raises(RemoteProtocolError, match="not a regular file"):
        apply_sync(root, [entry_for("a.v", b"")], SyncPlan(["a.v"], []), raw_tar([(info, None)]))
    assert tree(root) == {}


def test_a_regular_member_after_a_link_member_is_not_reached(tmp_path):
    root = tmp_path / "inputs"
    link = tarfile.TarInfo("a.v")
    link.type = tarfile.SYMTYPE
    link.linkname = str(tmp_path)
    tar = raw_tar([(link, None), regular("a.v", b"A")])
    with pytest.raises(RemoteProtocolError):
        apply_sync(root, [entry_for("a.v", b"A")], SyncPlan(["a.v"], []), tar)
    assert tree(root) == {}


# --- the tar must be what was asked for ----------------------------------------------------


def test_digest_mismatch_after_transfer_is_refused(tmp_path):
    root = tmp_path / "inputs"
    sync(root, tmp_path, {"a.v": b"old"})
    entries = [entry_for("a.v", b"new"), entry_for("b.v", b"B")]
    tar = raw_tar([regular("a.v", b"NEW"), regular("b.v", b"B")])  # same size, other content
    with pytest.raises(RemoteFetchError, match="a.v does not match its digest"):
        apply_sync(root, entries, SyncPlan(["a.v", "b.v"], []), tar)
    assert tree(root) == {"a.v": "old"}  # the old file stays, no temporary file is left


@pytest.mark.parametrize(
    "members, message",
    [
        ([regular("a.v", b"AA")], "wrong size"),
        ([regular("other.v", b"A")], "was not asked for"),
        ([regular("a.v", b"A"), regular("a.v", b"A")], "comes twice"),
        ([], "lacks"),
    ],
)
def test_the_archive_must_hold_exactly_what_was_asked_for(tmp_path, members, message):
    root = tmp_path / "inputs"
    with pytest.raises(RemoteProtocolError, match=message):
        apply_sync(root, [entry_for("a.v", b"A")], SyncPlan(["a.v"], []), raw_tar(members))
    assert tree(root) == ({"a.v": "A"} if message == "comes twice" else {})  # the first was valid


def test_a_truncated_archive_is_refused(tmp_path):
    root = tmp_path / "inputs"
    content = b"A" * 5000
    data = raw_tar([regular("a.v", content)]).getvalue()
    for cut in (100, 600, 3000):
        with pytest.raises(RemoteProtocolError):
            apply_sync(
                root,
                [entry_for("a.v", content)],
                SyncPlan(["a.v"], []),
                io.BytesIO(data[:cut]),
            )
        assert tree(root) == {}


def test_data_that_is_not_a_tar_archive_is_refused(tmp_path):
    with pytest.raises(RemoteProtocolError):
        apply_sync(
            tmp_path / "inputs",
            [entry_for("a.v", b"A")],
            SyncPlan(["a.v"], []),
            io.BytesIO(b"this is not a tar archive" * 100),
        )


def test_a_bad_plan_is_refused(tmp_path):
    entries = [entry_for("a.v", b"A")]
    for plan in (
        SyncPlan(["b.v"], []),
        SyncPlan(["a.v", "a.v"], []),
        SyncPlan([], ["a.v"]),
        SyncPlan([], ["../x"]),
    ):
        with pytest.raises(RemoteProtocolError):
            apply_sync(tmp_path / "inputs", entries, plan, raw_tar([]))


def test_a_plan_cannot_remove_a_directory_of_the_manifest(tmp_path):
    root = tmp_path / "inputs"
    (root / "d" / "sub").mkdir(parents=True)
    (root / "d" / "sub" / "keep.txt").write_text("keep")
    entries = [entry_for("d/new.v", b"N"), entry_for("e/f/g.v", b"G")]
    for stale in ("d", "e", "e/f"):
        tar = raw_tar([regular("d/new.v", b"N")])
        with pytest.raises(RemoteProtocolError, match="directory of it"):
            apply_sync(root, entries, SyncPlan(["d/new.v"], [stale]), tar)
    assert tree(root) == {"d": "dir", "d/sub": "dir", "d/sub/keep.txt": "keep"}


def test_a_stale_entry_is_removed_only_if_it_is_no_directory(tmp_path):
    root = tmp_path / "inputs"
    (root / "dir").mkdir(parents=True)
    (root / "dir" / "inner.txt").write_text("inner")
    (root / "file.txt").write_text("file")
    apply_sync(root, [], SyncPlan([], ["dir", "file.txt"]), raw_tar([]))
    assert tree(root) == {"dir": "dir", "dir/inner.txt": "inner"}  # a directory is not unlinked
