"""Helpers for tests that launch or plan a design from a file."""

from __future__ import annotations

from pathlib import Path


def empty_project(directory: Path) -> str:
    """Write a project file without designs or settings in `directory`; return its path.

    Xeda reads the one `xedaproject.*` file of the current directory unless a request names a
    project file. A test that plans or launches a design would otherwise change with the
    directory pytest starts in, because the project files of the examples set clocks. Pass the
    result as `xedaproject` (`--xedaproject` on the command line)."""
    file = directory / "empty_project.yaml"
    file.write_text("flows: {}\n")
    return str(file)
