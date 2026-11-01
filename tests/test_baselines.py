"""A release baseline never changes after its release.

Each baseline directory has a manifest of the digest of every file in it, and this file pins the
digest of each manifest. An edit to a baseline, or to its manifest, fails here.
"""

import hashlib
from pathlib import Path

BASELINES = Path(__file__).parent / "baselines"
MANIFEST = "MANIFEST.sha256"

#: The digest of `tests/baselines/<release>/MANIFEST.sha256`. A new release adds a row; no row
#: changes after the release.
BASELINE_MANIFESTS = {"1.0": "7f9235f5847abe4bd0a2e076c2458e361713517714998ef0008ff79263d5d320"}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _baselines() -> list[Path]:
    return sorted(p for p in BASELINES.iterdir() if p.is_dir())


def test_every_baseline_matches_its_manifest() -> None:
    assert _baselines()
    for baseline in _baselines():
        listed = {}
        for line in (baseline / MANIFEST).read_text(encoding="utf-8").splitlines():
            digest, name = line.split("  ", 1)
            listed[name] = digest
        files = {
            p.relative_to(baseline).as_posix(): p
            for p in baseline.rglob("*")
            if p.is_file() and p.name != MANIFEST
        }
        assert set(listed) == set(files), baseline.name
        assert {name: _digest(path) for name, path in files.items()} == listed, baseline.name


def test_every_manifest_is_pinned() -> None:
    assert {p.name: _digest(p / MANIFEST) for p in _baselines()} == BASELINE_MANIFESTS
