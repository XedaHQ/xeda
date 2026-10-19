"""`pytest --shard K/N` splits the suite into disjoint parts that together are the whole suite.

CI runs the full suite as one job per shard (`.github/workflows/ci.yml`). A test that no shard
selects would never run, and a test that two shards select would run twice.
"""

import argparse
import configparser
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from .sharding import parse_shard, shard_of

ROOT = Path(__file__).parent.parent

#: Shapes of node ids: a plain test, a parametrized one with brackets and slashes, a class method.
NODE_IDS = [
    f"tests/test_{module}.py::{'Test' + str(c) + '::' if c else ''}test_{name}[{case}]"
    for module in range(40)
    for c in range(3)
    for name in range(5)
    for case in ("a/b", "x y", "1-2-3", "naïve", "")
]


@pytest.mark.parametrize("text, expected", [("1/1", (1, 1)), ("2/4", (2, 4)), ("12/12", (12, 12))])
def test_a_shard_is_a_number_and_a_count(text, expected):
    assert parse_shard(text) == expected


@pytest.mark.parametrize(
    "text", ["", "4", "0/4", "5/4", "1/0", "-1/4", "1/-4", "a/b", "1/4/4", "1 / 4", "01/4", "2/4 "]
)
def test_anything_else_is_refused(text):
    with pytest.raises(argparse.ArgumentTypeError, match="K/N"):
        parse_shard(text)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 7, 16])
def test_the_shards_partition_the_tests(count):
    """Disjoint, and their union is every test."""
    shards = [{n for n in NODE_IDS if shard_of(n, count) == k} for k in range(1, count + 1)]
    assert sum(len(shard) for shard in shards) == len(NODE_IDS)
    assert set().union(*shards) == set(NODE_IDS)


@pytest.mark.parametrize("count", [2, 4, 8])
def test_the_shards_are_about_equal_in_size(count):
    sizes = [sum(shard_of(n, count) == k for n in NODE_IDS) for k in range(1, count + 1)]
    assert min(sizes) > 0.8 * len(NODE_IDS) / count
    assert max(sizes) < 1.2 * len(NODE_IDS) / count


def test_the_shard_of_a_test_is_the_same_in_every_process():
    """A fixed value, not Python's `hash`, which changes per process (a worker of pytest-xdist
    and the controller of another job must agree)."""
    assert [shard_of("tests/test_a.py::test_b[c]", count) for count in (2, 3, 4)] == [2, 2, 2]
    assert [shard_of("tests/test_a.py::test_b[d]", count) for count in (2, 3, 4)] == [1, 2, 3]


def collected(*arguments: str) -> list[str]:
    """The node ids that `pytest --collect-only` lists for this file, in a new process."""
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
            "--no-header",
            __file__,
            *arguments,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if "::" in line]


def test_the_option_partitions_the_collected_tests():
    count = 3
    everything = collected()
    assert len(everything) > 2 * count
    shards = [collected("--shard", f"{k}/{count}") for k in range(1, count + 1)]
    assert sorted(node for shard in shards for node in shard) == sorted(everything)
    assert all(shard_of(node, count) == k for k, shard in enumerate(shards, 1) for node in shard)


def test_a_malformed_shard_is_a_usage_error():
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--shard", "5/4", __file__],
        cwd=ROOT,
        env=dict(environment, PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 4
    assert "K/N" in result.stderr


def test_the_workflow_runs_every_shard_of_the_full_suite_once():
    """The jobs of the full Python version are shards 1 to N of one N, and tox hands the option
    on to the full suite."""
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["tox"]
    full = job["env"]["FULL_PYTHON"]
    shards = [entry["shard"] for entry in job["strategy"]["matrix"]["include"]]
    assert {entry["python-version"] for entry in job["strategy"]["matrix"]["include"]} == {full}
    count = int(shards[0].split("/")[1])
    assert shards == [f"{k}/{count}" for k in range(1, count + 1)]

    tox = configparser.ConfigParser(interpolation=None)
    tox.read(ROOT / "tox.ini")
    commands = [line.strip() for line in tox["testenv"]["commands"].splitlines()]
    full_suite = [c for c in commands if c.startswith("!compat:")]
    assert len(full_suite) == 1 and "{posargs}" in full_suite[0]
