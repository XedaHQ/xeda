"""The generated sources that a generator writes into its design tree: the scan of the tree, the
rule that tells the files a run wrote, and the copy of a file into the storage.

The scan matches by the rule of `generation.matches_in`, because a launch judges the copies in the
storage with `matches_in`. Any other rule would make every judgment fail.
"""

import os
import sys
from pathlib import Path

import pytest

from xeda import tree_outputs
from xeda.design import TREE_DIRECTORY
from xeda.generation import (
    GenerationError,
    delivered_name,
    matches_in,
    output_name_of,
    stored_pattern,
)
from xeda.tree_outputs import copy_tree_file, observe, part_matches, written_names

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="tree entries need POSIX")

#: The files of the tree that the matcher tests scan.
NAMES = [
    "a.v",
    "b.sv",
    ".hidden.v",
    "fifo[1].v",
    "x?.v",
    "gen/a.v",
    "gen/.h.v",
    "gen/a\\b.v",
    "gen/sub/c.v",
    "gen2/a.v",
    ".dot/d.v",
]

#: Names, patterns, nested patterns, dot names and the literal characters `?`, `[` and `\`.
PATTERNS = [
    "a.v",
    "A.V",
    "*.v",
    "*.V",
    "*",
    ".*",
    "fifo[1].v",
    "fifo*",
    "x?.v",
    "gen",
    "gen/*.v",
    "gen/*",
    "gen/.*",
    "gen/a\\b.v",
    "*/a.v",
    "gen*/a.v",
    "*/*/c.v",
    ".dot/*.v",
    ".*/d.v",
    "*/d.v",
    "missing/*.v",
    "a.v/x",
]


def _tree(root: Path) -> Path:
    for name in NAMES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    return root


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------------- matching


@pytest.mark.parametrize("pattern", PATTERNS)
def test_the_scan_of_a_tree_matches_what_matches_in_matches(tmp_path, pattern):
    root = _tree(tmp_path / "root")
    assert sorted(observe(root, [pattern])) == matches_in(root, pattern)


@pytest.mark.parametrize("pattern", PATTERNS)
def test_the_copies_match_their_declaration_again_in_the_storage(tmp_path, pattern):
    root = _tree(tmp_path / "root")
    storage = tmp_path / "storage"
    seen = observe(root, [pattern])
    for name, observed in seen.items():
        destination = storage / TREE_DIRECTORY / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy_tree_file(root, name, observed, destination)
    found = matches_in(storage, stored_pattern(pattern)) if storage.is_dir() else []
    assert found == [f"{TREE_DIRECTORY}/{name}" for name in sorted(seen)]
    assert [output_name_of(name) for name in found] == sorted(seen)


@pytest.mark.parametrize(
    "pattern", ["a.v", "*.v", "*", ".*", "fifo[1].v", "fifo*", "x?.v", "*.V", "b*", "*v"]
)
def test_a_pattern_part_fits_the_names_that_matches_in_finds(tmp_path, pattern):
    root = _tree(tmp_path / "root")
    files = [name for name in NAMES if "/" not in name]
    found = matches_in(root, pattern)
    assert [name for name in files if part_matches(pattern, name)] == [
        name for name in files if name in found
    ]


def test_a_backslash_is_part_of_a_name(tmp_path):
    """On POSIX a backslash is a character of a name, never a separator: the copy of a tree
    file `gen/a\\b.v` is found again in the storage."""
    root = _tree(tmp_path / "root")
    assert matches_in(root, "gen/a\\b.v") == ["gen/a\\b.v"]
    assert matches_in(root, "gen/*b.v") == ["gen/a\\b.v"]


def test_the_names_of_the_storage_translate_both_ways():
    assert stored_pattern("$XEDA_GENERATED/gen/*.v") == "gen/*.v"
    assert stored_pattern("gen_rtl/*.sv") == ".xeda-tree/gen_rtl/*.sv"
    assert output_name_of(".xeda-tree/gen_rtl/x.sv") == "gen_rtl/x.sv"
    assert output_name_of("gen/top.v") == "$XEDA_GENERATED/gen/top.v"
    assert delivered_name(".xeda-tree/gen_rtl/x.sv") == "gen_rtl/x.sv"
    assert delivered_name("gen/top.v") == "gen/top.v"


# ------------------------------------------------------------------------------------ links


def test_a_link_at_a_matched_name_is_refused(tmp_path):
    root = tmp_path / "root"
    _write(tmp_path / "elsewhere.v", "x")
    (root / "gen").mkdir(parents=True)
    (root / "gen" / "top.v").symlink_to(tmp_path / "elsewhere.v")
    for pattern in ("gen/top.v", "gen/*.v"):
        with pytest.raises(GenerationError, match="gen/top.v` is a symbolic link"):
            observe(root, [pattern])


def test_a_link_at_a_literal_directory_on_the_way_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _write(tmp_path / "real" / "top.v", "x")
    (root / "gen").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(GenerationError, match="root/gen` is a symbolic link"):
        observe(root, ["gen/top.v"])


def test_a_link_at_a_directory_that_a_pattern_matches_is_refused(tmp_path):
    root = tmp_path / "root"
    _write(root / "b" / "top.v", "x")
    _write(tmp_path / "real" / "top.v", "x")
    (root / "a").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(GenerationError, match="root/a` is a symbolic link"):
        observe(root, ["*/top.v"])


def test_a_link_that_no_entry_matches_is_left_alone(tmp_path):
    root = tmp_path / "root"
    _write(root / "gen" / "top.v", "x")
    (root / "gen" / "notes.txt").symlink_to(tmp_path / "nowhere")
    assert sorted(observe(root, ["gen/*.v"])) == ["gen/top.v"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
def test_a_directory_that_cannot_be_read_is_refused(tmp_path):
    root = tmp_path / "root"
    _write(root / "gen" / "top.v", "x")
    (root / "gen").chmod(0)
    try:
        with pytest.raises(GenerationError, match="cannot read"):
            observe(root, ["gen/*.v"])
    finally:
        (root / "gen").chmod(0o755)


def test_a_system_without_directory_descriptors_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(GenerationError, match="needs a POSIX system"):
        observe(tmp_path, ["gen/top.v"])


# ---------------------------------------------------------------------------- written files


def test_a_new_file_is_written(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    before = observe(root, ["gen/*.v"])
    _write(root / "gen" / "top.v", "new")
    assert written_names(before, observe(root, ["gen/*.v"])) == ["gen/top.v"]


def test_a_file_rewritten_in_place_is_written(tmp_path):
    root = tmp_path / "root"
    path = _write(root / "gen" / "top.v", "old")
    before = observe(root, ["gen/top.v"])
    with open(path, "r+") as stream:
        stream.write("new")
    assert written_names(before, observe(root, ["gen/top.v"])) == ["gen/top.v"]


def test_a_file_replaced_by_a_rename_is_written(tmp_path):
    root = tmp_path / "root"
    _write(root / "gen" / "top.v", "same")
    before = observe(root, ["gen/top.v"])
    os.replace(_write(root / "gen" / "top.v.tmp", "same"), root / "gen" / "top.v")
    assert written_names(before, observe(root, ["gen/top.v"])) == ["gen/top.v"]


def test_an_untouched_file_is_not_written(tmp_path):
    root = tmp_path / "root"
    _write(root / "gen" / "top.v", "old")
    _write(root / "gen" / "other.v", "old")
    before = observe(root, ["gen/*.v"])
    _write(root / "gen" / "other.v", "new")
    assert written_names(before, observe(root, ["gen/*.v"])) == ["gen/other.v"]


def test_a_file_with_the_same_state_and_new_bytes_is_written():
    """The state of a file can stay as it was across a rewrite of the same size within one tick
    of a coarse clock: the digest of the content tells the rewrite."""
    state = (1, 2, 3, 4, 5)
    assert written_names({"gen/top.v": (state, "old")}, {"gen/top.v": (state, "new")}) == [
        "gen/top.v"
    ]
    assert written_names({"gen/top.v": (state, "old")}, {"gen/top.v": (state, "old")}) == []


def test_a_file_with_the_same_state_and_new_bytes_is_written_by_the_scan(tmp_path, monkeypatch):
    root = tmp_path / "root"
    path = _write(root / "gen" / "top.v", "old")
    monkeypatch.setattr(tree_outputs, "_state", lambda found: (0, 0, 0, 0, 0))
    before = observe(root, ["gen/top.v"])
    path.write_text("new")
    assert written_names(before, observe(root, ["gen/top.v"])) == ["gen/top.v"]


def test_a_removed_file_is_not_written(tmp_path):
    root = tmp_path / "root"
    _write(root / "gen" / "top.v", "old")
    before = observe(root, ["gen/*.v"])
    (root / "gen" / "top.v").unlink()
    assert written_names(before, observe(root, ["gen/*.v"])) == []


# ------------------------------------------------------------------------------------- copy


def test_a_copy_holds_the_content_of_the_tree_file(tmp_path):
    root = tmp_path / "root"
    _write(root / "gen" / "top.v", "module top; endmodule\n")
    (observed,) = observe(root, ["gen/top.v"]).values()
    destination = tmp_path / "storage" / "top.v"
    destination.parent.mkdir()
    copy_tree_file(root, "gen/top.v", observed, destination)
    assert destination.read_text() == "module top; endmodule\n"
    assert not destination.is_symlink()


def test_a_file_changed_after_the_scan_is_not_copied(tmp_path):
    root = tmp_path / "root"
    path = _write(root / "gen" / "top.v", "old")
    (observed,) = observe(root, ["gen/top.v"]).values()
    path.write_text("changed")
    destination = tmp_path / "top.v"
    with pytest.raises(GenerationError, match="changed while Xeda copied it"):
        copy_tree_file(root, "gen/top.v", observed, destination)
    assert not destination.exists()


def test_a_file_changed_while_it_is_copied_is_not_committed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    path = _write(root / "gen" / "top.v", "old")
    (observed,) = observe(root, ["gen/top.v"]).values()
    monkeypatch.setattr(tree_outputs, "copy_hook", lambda _path: path.write_text("changed"))
    destination = tmp_path / "top.v"
    with pytest.raises(GenerationError, match="changed while Xeda copied it"):
        copy_tree_file(root, "gen/top.v", observed, destination)
    assert not destination.exists()
    assert list(tmp_path.glob(".top.v.*")) == [], "no temporary file stays"


def test_a_copy_whose_bytes_differ_from_the_scan_is_not_committed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    _write(root / "gen" / "top.v", "old")
    (observed,) = observe(root, ["gen/top.v"]).values()
    destination = tmp_path / "top.v"
    with pytest.raises(GenerationError, match="changed while Xeda copied it"):
        copy_tree_file(root, "gen/top.v", (observed[0], "another digest"), destination)
    assert not destination.exists()


def test_a_copy_follows_no_link_that_appeared_after_the_scan(tmp_path):
    root = tmp_path / "root"
    _write(root / "gen" / "top.v", "old")
    (observed,) = observe(root, ["gen/top.v"]).values()
    _write(tmp_path / "real" / "top.v", "old")
    (root / "gen" / "top.v").unlink()
    (root / "gen").rmdir()
    (root / "gen").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(GenerationError, match="symbolic link"):
        copy_tree_file(root, "gen/top.v", observed, tmp_path / "top.v")
    assert not (tmp_path / "top.v").exists()
