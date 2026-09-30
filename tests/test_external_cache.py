"""The external-repository cache is safe to fill from several processes at once.

`pytest -n` gives every worker its own session, so each asks `test_bsc_external` for the same
pinned checkout at the same time. Nothing here needs the network or `XEDA_TESTS_EXTERNAL`: the
"remote" is a local git repository.
"""

import multiprocessing
import os
import subprocess
from pathlib import Path

import pytest

from .test_bsc_external import _checked_out_sha, _fetch_pinned_commit

WORKERS = 6


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fetch(args):
    cache, url, sha = args
    os.environ["XEDA_TESTS_EXTERNAL_CACHE"] = cache
    return str(_fetch_pinned_commit("repo", url, sha))


@pytest.fixture
def origin(tmp_path):
    src = tmp_path / "origin"
    src.mkdir()
    _git(src, "init", "-q")
    for i in range(200):  # enough files that a fetch takes visible time
        (src / f"f{i}.txt").write_text(str(i) * 1000)
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "one")
    return src, _git(src, "rev-parse", "HEAD")


def test_concurrent_fetches_of_one_commit_all_get_the_finished_checkout(tmp_path, origin):
    src, sha = origin
    cache = tmp_path / "cache"
    job = (str(cache), str(src), sha)
    with multiprocessing.get_context("spawn").Pool(WORKERS) as pool:
        dests = pool.map(_fetch, [job] * WORKERS, chunksize=1)
    assert len(set(dests)) == 1
    assert _checked_out_sha(Path(dests[0])) == sha
    assert (Path(dests[0]) / "f199.txt").is_file()


def test_a_finished_checkout_is_reused_not_fetched_again(tmp_path, origin, monkeypatch):
    src, sha = origin
    monkeypatch.setenv("XEDA_TESTS_EXTERNAL_CACHE", str(tmp_path / "cache"))
    first = _fetch_pinned_commit("repo", str(src), sha)
    (first / "marker").write_text("kept")
    assert _fetch_pinned_commit("repo", str(src), sha) == first
    assert (first / "marker").read_text() == "kept"
