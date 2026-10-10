"""Keep the published Python range and CI test matrix in sync."""

import configparser
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent


def _supported_minors(project):
    classifiers = project["project"]["classifiers"]
    minors = sorted(
        int(match.group(1))
        for classifier in classifiers
        if (match := re.fullmatch(r"Programming Language :: Python :: 3\.(\d+)", classifier))
    )
    minimum = re.fullmatch(r">=3\.(\d+)", project["project"]["requires-python"])
    assert minimum, "requires-python must declare the Python 3 minor floor"
    assert minors and minors[0] == int(minimum.group(1))
    assert minors == list(range(minors[0], minors[-1] + 1))
    return minors


def test_project_python_support_matches_tox_and_github_actions():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    supported = _supported_minors(project)
    tox = configparser.ConfigParser(interpolation=None)
    tox.read(ROOT / "tox.ini")

    default_envs = [name.strip() for name in tox["tox"]["envlist"].split(",")]
    python_envs = {
        int(match.group(1)): (match.group(2) or "")
        for env in default_envs
        if (match := re.fullmatch(r"py3(\d+)(-compat)?", env))
    }
    assert python_envs == {minor: "-compat" if minor < supported[-1] else "" for minor in supported}

    tox_mapping = {
        int(minor): env
        for minor, env in re.findall(
            r"^\s*3\.(\d+):\s*(py\d+)(?:-compat)?\s*$", tox["gh-actions"]["python"], re.M
        )
    }
    assert tox_mapping == {minor: f"py3{minor}" for minor in supported}

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    matrix = workflow["jobs"]["tox"]["strategy"]["matrix"]["python-version"]
    assert matrix == [f"3.{minor}" for minor in supported]
