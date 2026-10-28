"""Find the project file: `xedaproject.yaml`, `xedaproject.yml` or `xedaproject.toml`.

`xeda.project.Project` reads the file."""

from __future__ import annotations

from pathlib import Path

from .errors import ProjectError

PROJECT_FILE_NAMES = ("xedaproject.yaml", "xedaproject.yml", "xedaproject.toml")


def find_default_xedaproject(directory: str | Path = ".") -> Path | None:
    """Return the sole conventional project file in `directory`, if present.

    Raise `ProjectError` (`several_project_files`) when `directory` holds more than one of
    `PROJECT_FILE_NAMES`.
    """
    directory = Path(directory)
    found = [directory / name for name in PROJECT_FILE_NAMES if (directory / name).exists()]
    if len(found) > 1:
        names = ", ".join(path.name for path in found)
        raise ProjectError(
            f"Multiple project files found in {directory}: {names}; keep one project file",
            code="several_project_files",
            path=directory,
        )
    return found[0] if found else None


def resolve_project_file(
    given: str | Path | None = None, directory: str | Path = "."
) -> Path | None:
    """Return the project file that a launch reads, the same for a local and a remote launch.

    It is the file `given` (which must exist; `""` is no file given), else the sole conventional
    project file in `directory`, else `None`. A missing file `given` and an ambiguous directory
    raise `ProjectError`."""
    if given:
        path = Path(given)
        if not path.exists():
            raise ProjectError(
                f'Cannot open project file "{given}": no such file',
                code="project_file_not_found",
                path=path,
            )
        return path
    return find_default_xedaproject(directory)
