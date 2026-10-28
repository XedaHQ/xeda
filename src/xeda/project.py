"""The project file: its designs, the `flows` table they share, its run root and default target.

A project is a checked-in file of declarations. `Project.load` reads it, and `Project.design`
selects one of its designs. A project does not run anything.
"""

from __future__ import annotations

import difflib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from .dataclass import SecondSpelling, removed_inputs_in, removed_message
from .design import (
    DESIGN_FILE_FORMATS,
    Design,
    DesignFileParseError,
    DesignValidationError,
    _read_design_file,
    target_name_problem,
)
from .errors import DesignError, ProjectError
from .frozen import freeze, thaw
from .types import UNSET, PathLike, Unset
from .utils import ConflictingKeys, expand_hierarchy, flows_table_problems, tomllib
from .yaml_loader import load_yaml

__all__ = ["PROJECT_KEYS", "REMOVED_PROJECT_KEYS", "Project"]

#: The top-level keys of a project file.
PROJECT_KEYS = ("designs", "flows", "run_root", "default_target")
#: The singular spellings that a project file refuses, with the key to use.
REMOVED_PROJECT_KEYS = {
    "design": SecondSpelling("`designs`"),
    "flow": SecondSpelling("`flows`"),
}


@dataclass(frozen=True)
class _Entry:
    """One entry of `designs`: a design written in the project file, or a design file."""

    name: str
    #: The design as written (dotted keys expanded), for an entry that is a table.
    table: Mapping[str, Any] | None = None
    #: The design file, for an entry `{file: <path>}`.
    file: Path | None = None


def _invalid(file: Path, problem: str, *, code: str = "invalid_project") -> ProjectError:
    return ProjectError(f'Cannot load project file "{file}": {problem}', code=code, path=file)


@dataclass(frozen=True, init=False, eq=False)
class Project:
    """The view of a project file.

    There is no constructor: `Project.load` reads the file. In Python, `Design.load` and
    `Spec.settings` do what a project file does for a design and its settings."""

    #: The project file (absolute).
    path: Path
    #: The directory of the project file. Every relative path in the file resolves against it.
    root: Path
    #: The names of the designs, in the order of the file.
    designs: tuple[str, ...]
    #: The `run_root` of the file, relative to the project file, else `<root>/xeda_run`.
    run_root: Path
    default_target: str | None
    #: The `flows` table of the file.
    flows: Mapping[str, Any]
    _entries: tuple[_Entry, ...]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("a Project has no constructor: read a project file with Project.load")

    def __repr__(self) -> str:
        return f"Project({str(self.path)!r}, designs={self.designs!r})"

    @classmethod
    def load(cls, path: PathLike) -> Project:
        """Read and check the project file `path`.

        A relative `path` resolves against the current directory now, once. Raises
        `ProjectError` (`project_file_not_found`, `invalid_project`, `removed_alias`)."""
        file = Path(path).absolute()
        if not file.is_file():
            raise ProjectError(
                f'Cannot open project file "{path}": no such file',
                code="project_file_not_found",
                path=file,
            )
        data = _read(file)
        root = file.parent.resolve()
        # The design entries are expanded where they are used, one by one.
        entries = {"designs": data.pop("designs")} if "designs" in data else {}
        try:
            data = expand_hierarchy(data)
        except ConflictingKeys as e:
            raise _invalid(file, str(e)) from e
        # After the expansion: `flow.yosys_fpga.flatten: true` is a `flow` key too.
        for key, replacement in REMOVED_PROJECT_KEYS.items():
            if key in data:
                raise _invalid(file, removed_message(key, replacement, None), code="removed_alias")
        if "designs" in data:
            raise _invalid(
                file,
                "a dotted key cannot reach into the design entries: write each one under "
                "`designs:`",
            )
        data.update(entries)
        unknown = [key for key in data if key not in PROJECT_KEYS]
        if unknown:
            hints = [
                f"{key!r}: did you mean {close[0]!r}?"
                for key in unknown
                if (close := difflib.get_close_matches(str(key), PROJECT_KEYS, n=1))
            ]
            raise _invalid(
                file,
                f"unknown key(s) {', '.join(repr(key) for key in unknown)}; a project has "
                f"{', '.join(repr(key) for key in PROJECT_KEYS)}"
                + (f" ({'; '.join(hints)})" if hints else "")
                + ". Write a setting of a flow as flows.<flow>.<setting>.",
            )
        flows = data.get("flows")
        problems = flows_table_problems(flows)
        if problems:
            raise _invalid(file, "; ".join(f"`{key}` {problem}" for key, problem in problems))
        default_target = data.get("default_target")
        if default_target is not None and (problem := target_name_problem(default_target)):
            raise _invalid(file, f"`default_target`: {problem}")
        run_root = _run_root(file, root, data.get("run_root"))
        parsed = _entries(file, root, data)
        return _make(
            cls,
            path=file,
            root=root,
            designs=tuple(entry.name for entry in parsed),
            run_root=run_root,
            default_target=default_target,
            flows=freeze(flows or {}),
            _entries=parsed,
        )

    def design(
        self,
        name: str | Literal[Unset.UNSET] = UNSET,
        *,
        target: str | None | Literal[Unset.UNSET] = UNSET,
    ) -> Design:
        """Return the design `name` with the project's `flows` table as its project layer.

        Without `name`, the design is the only one of the project. `target` selects one of the
        targets of the design; `UNSET` selects `default_target`, and `None` selects no target.

        Raises `ProjectError` (`invalid_project` for a project without designs,
        `design_name_required`, `unknown_design`) and `DesignError` (`target_required` when the
        default target is no target of the design and no target was given; `unknown_target`)."""
        entry = self._entry(name)
        layer = (str(self.path), self.flows)
        wanted = self.default_target if target is UNSET else target
        assert wanted is None or isinstance(wanted, str)

        def build(chosen: str | None) -> Design:
            if entry.file is not None:
                return Design._load_file(entry.file, target=chosen, project_layer=layer)
            assert entry.table is not None
            return Design._built(self.root, thaw(entry.table), target=chosen, project_layer=layer)

        # The design is built once, with its target: a source that only the target's generator
        # writes is no missing file of the target.
        try:
            return build(wanted)
        except DesignValidationError as e:
            if target is not UNSET or not any(error[3] == "unknown_target" for error in e.errors):
                raise
        design = build(None)
        raise DesignError(
            f'The default target {wanted!r} of the project file "{self.path}" is not a '
            f"target of the design {design.name!r}"
            + (f" (its targets are: {', '.join(design.targets)})" if design.targets else "")
            + ". Select a target, or select no target.",
            code="target_required",
            field="targets",
            path=self.path,
        )

    def _entry(self, name: str | Literal[Unset.UNSET]) -> _Entry:
        if not self._entries:
            raise ProjectError(
                f'The project file "{self.path}" has no designs. Give a design file instead.',
                code="invalid_project",
                path=self.path,
            )
        names = ", ".join(self.designs)
        if name is UNSET:
            if len(self._entries) == 1:
                return self._entries[0]
            raise ProjectError(
                f'The project file "{self.path}" has several designs: {names}. Name the one to '
                "use.",
                code="design_name_required",
                path=self.path,
            )
        for entry in self._entries:
            if entry.name == name:
                return entry
        close = difflib.get_close_matches(str(name), self.designs, n=1)
        raise ProjectError(
            f'Design {name!r} is not in the project file "{self.path}"'
            + (f" (did you mean {close[0]!r}?)" if close else "")
            + f". Its designs are: {names}.",
            code="unknown_design",
            path=self.path,
        )

    def referenced_name(self, file: PathLike) -> str | None:
        """Return the name of the design whose entry is the design file `file`, or None.

        Two paths name the same file when they resolve to one path."""
        resolved = Path(file).resolve()
        for entry in self._entries:
            if entry.file is not None and entry.file.resolve() == resolved:
                return entry.name
        return None


def _make(cls: type[Project], **fields: Any) -> Project:
    project = object.__new__(cls)
    for key, value in fields.items():
        object.__setattr__(project, key, value)
    return project


def _read(file: Path) -> dict[str, Any]:
    """Return the table in the project file, or raise `invalid_project`."""
    fmt = DESIGN_FILE_FORMATS.get(file.suffix)
    if fmt is None:
        hint = (
            f"suffixes are case-sensitive, did you mean {file.suffix.lower()!r}?"
            if file.suffix.lower() in DESIGN_FILE_FORMATS
            else "a project file is TOML, JSON or YAML "
            f"({', '.join(repr(s) for s in DESIGN_FILE_FORMATS)})"
        )
        raise _invalid(file, f"file suffix {file.suffix!r}: {hint}")
    try:
        if fmt == "yaml":
            data = load_yaml(file)
        elif fmt == "toml":
            with open(file, "rb") as f:
                data = tomllib.load(f)
        else:
            with open(file, "r") as f:
                data = json.load(f)
    except (OSError, ValueError, yaml.YAMLError) as e:
        # Unreadable, not TOML/JSON/YAML (the decode errors are `ValueError`s).
        raise _invalid(file, str(e)) from e
    if not isinstance(data, dict) or not data:
        raise _invalid(file, "it must hold a nonempty mapping")
    return data


def _run_root(file: Path, root: Path, given: Any) -> Path:
    if given is None:
        return root / "xeda_run"
    if not isinstance(given, str) or not given:
        raise _invalid(file, f"`run_root` is a path, not {given!r:.60}")
    return Path(os.path.normpath(root / given))


def _entries(file: Path, root: Path, data: Mapping[str, Any]) -> tuple[_Entry, ...]:
    designs = data.get("designs")
    if designs is None:
        return ()
    if not isinstance(designs, list):
        raise _invalid(
            file,
            "`designs` is a list. Write each design as an item, for example "
            "`designs: [{file: hw/fifo.yaml}]` or `designs: [{name: fifo, rtl: {...}}]`, "
            f"not {type(designs).__name__} {designs!r:.60}",
        )
    entries: list[_Entry] = []
    for i, written in enumerate(designs):
        if not isinstance(written, dict):
            raise _invalid(
                file,
                f"design entry {i} must be a mapping (table), got "
                f"{type(written).__name__}: {written!r}",
            )
        # A design entry is a table of dotted keys, as in a design file.
        try:
            table = Design.expanded(written)
        except DesignValidationError as e:
            raise _invalid(
                file,
                f"design entry {i}"
                + (f" ({written['name']})" if "name" in written else "")
                + ": "
                + "; ".join(str(message) for _, message, *_ in e.errors),
            ) from e
        entries.append(_entry(file, root, i, table))
    seen: set[str] = set()
    for entry in entries:
        if entry.name in seen:
            raise _invalid(file, f"two design entries are named {entry.name!r}")
        seen.add(entry.name)
    return tuple(entries)


def _entry(file: Path, root: Path, index: int, table: dict[str, Any]) -> _Entry:
    if "file" in table:
        if set(table) != {"file"}:
            raise _invalid(
                file,
                f"design entry {index}: an entry with `file` holds nothing else, but it has "
                f"{', '.join(repr(key) for key in table if key != 'file')}",
            )
        given = table["file"]
        if not isinstance(given, str) or not given:
            raise _invalid(file, f"design entry {index}: `file` is a path, not {given!r:.60}")
        design_file = root / given
        try:
            declared = _read_design_file(design_file)
        except DesignFileParseError as e:
            raise _invalid(file, f"design entry {index}: {e}") from e
        name = declared.get("name")
        if name is None:
            stem = design_file.stem
            name = stem.removesuffix(".xeda")
        if not isinstance(name, str) or not name:
            raise _invalid(file, f"design entry {index} ({given}): `name` is not a name")
        return _Entry(name, file=design_file)
    name = table.get("name")
    if not isinstance(name, str) or not name:
        raise _invalid(file, f"design entry {index} needs a `name`, a text")
    # The keys that a design file refuses are refused here too. The root of an entry is the
    # directory of the project file; a design with another root is a `file` entry.
    for path, message, kind in removed_inputs_in(Design, table):
        raise _invalid(file, f"design entry {index} ({name}): {message}", code=kind)
    return _Entry(name, table=freeze(table))
