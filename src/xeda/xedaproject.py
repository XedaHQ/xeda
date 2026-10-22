from __future__ import annotations

import difflib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Type, Union

import attrs

from .dataclass import model_with_allow_extra
from .design import DESIGN_FILE_FORMATS, Design, DesignValidationError
from .utils import (
    ConflictingKeys,
    WorkingDirectory,
    XedaException,
    expand_hierarchy,
    flows_table_problems,
    tomllib,
)
from .yaml_loader import load_yaml

log = logging.getLogger(__name__)


PROJECT_FILE_NAMES = ("xedaproject.yaml", "xedaproject.yml", "xedaproject.toml")


class ProjectFileError(XedaException):
    """The error for a project file (`xedaproject.yaml`, `.yml` or `.toml`) that cannot be found,
    opened, parsed or used.

    `code` is the code of the `xeda.errors.ProjectError` that it becomes."""

    def __init__(self, message: str, *, code: str = "invalid_project") -> None:
        super().__init__(message)
        self.code = code


def find_default_xedaproject(directory: str | Path = ".") -> Path | None:
    """Return the sole conventional project file in `directory`, if present.

    Raise `ProjectFileError` when `directory` holds more than one of `PROJECT_FILE_NAMES`.
    """
    directory = Path(directory)
    found = [directory / name for name in PROJECT_FILE_NAMES if (directory / name).exists()]
    if len(found) > 1:
        names = ", ".join(path.name for path in found)
        raise ProjectFileError(
            f"Multiple project files found in {directory}: {names}; keep one project file",
            code="several_project_files",
        )
    return found[0] if found else None


def resolve_project_file(
    given: str | Path | None = None, directory: str | Path = "."
) -> Path | None:
    """Return the project file that a launch reads, the same for a local and a remote launch.

    It is the file `given` (which must exist; `""` is no file given), else the sole conventional
    project file in `directory`, else `None`. A missing file `given` and an ambiguous directory
    raise `ProjectFileError`."""
    if given:
        path = Path(given)
        if not path.exists():
            raise ProjectFileError(
                f'Cannot open project file "{given}": no such file', code="project_file_not_found"
            )
        return path
    return find_default_xedaproject(directory)


#: The top-level keys of a project file: its designs and the settings of its flows. Each may be
#: spelled in the singular.
PROJECT_KEYS = ("designs", "design", "flows", "flow")
DESIGN_KEYS = ("designs", "design")


@attrs.define
class XedaProject:
    # TODO: workspace options
    workspace: Dict[str, Any] = {}
    designs: List[Dict[str, Any]] = []
    # Keep the raw dict: flows are discovered dynamically.
    flows: Dict[str, dict] = {}  # = attrs.field(default={}, validator=type_validator())
    design_cls: Type[Design] = Design
    root_path: Path = attrs.field(factory=Path.cwd)
    # Applied to a design once, when it is selected (`Design.target_selected`), as for a design
    # file. The entries of `designs` stay as written.
    design_overrides: dict[str, Any] = {}

    @classmethod
    def from_file(
        cls: Type[XedaProject],
        file: Union[str, Path],
        skip_designs: bool = False,
        design_overrides: Union[Dict[str, Any], None] = None,
        design_allow_extra: bool = False,
        design_remove_extra: Union[List[str], None] = None,
    ) -> XedaProject:
        """Load a project file."""
        if design_overrides is None:
            design_overrides = {}
        if design_remove_extra is None:
            design_remove_extra = []
        if not isinstance(file, Path):
            file = Path(file)
        # The suffix table of design files: case-sensitive, and `.yml` is YAML too.
        fmt = DESIGN_FILE_FORMATS.get(file.suffix)
        if fmt is None:
            hint = (
                f"suffixes are case-sensitive, did you mean {file.suffix.lower()!r}?"
                if file.suffix.lower() in DESIGN_FILE_FORMATS
                else "a project file is TOML, JSON or YAML "
                f"({', '.join(repr(s) for s in DESIGN_FILE_FORMATS)})"
            )
            raise ValueError(f"project file {file}: file suffix {file.suffix!r}: {hint}")
        if fmt == "yaml":
            data = load_yaml(file)
        else:
            with open(file, "rb" if fmt == "toml" else "r") as f:
                if fmt == "toml":
                    data = tomllib.load(f)
                else:
                    data = json.load(f)
        if not isinstance(data, dict) or not data:
            raise ValueError(f"Invalid project file {file}: it must hold a nonempty mapping")
        # The design entries are expanded where they are used, one by one, whether `design` is a
        # table or a list of tables.
        entries = {key: data.pop(key) for key in DESIGN_KEYS if key in data}
        try:
            data = expand_hierarchy(data)
        except ConflictingKeys as e:
            raise ValueError(f"project file {file}: {e}") from e
        if any(key in data for key in DESIGN_KEYS):
            raise ValueError(
                f"project file {file}: a dotted key cannot reach into the design entries: "
                "write each one under `designs:`"
            )
        data.update(entries)
        unknown = [key for key in data if key not in PROJECT_KEYS]
        if unknown:
            hints = [
                f"{key!r}: did you mean {close[0]!r}?"
                for key in unknown
                if (close := difflib.get_close_matches(str(key), PROJECT_KEYS, n=1))
            ]
            raise ValueError(
                f"project file {file}: unknown key(s) "
                f"{', '.join(repr(key) for key in unknown)}; a project has "
                f"{', '.join(repr(key) for key in PROJECT_KEYS)}"
                + (f" ({'; '.join(hints)})" if hints else "")
                + ". Write a setting of a flow as flows.<flow>.<setting>."
            )
        designs = None
        if not skip_designs:
            if "design" in data and "designs" in data:
                raise ValueError(
                    "Specify project designs once, as `designs` (or `design`), not both"
                )
            designs = data.get("design", data.get("designs"))
        if designs:
            if not isinstance(designs, list):
                designs = [designs]

        if "flow" in data and "flows" in data:
            raise ValueError("Specify project flow settings once, as `flows` (or `flow`), not both")
        flows = data.get("flow", data.get("flows"))
        problems = flows_table_problems(flows)
        if problems:
            raise ValueError(
                "Project " + "; ".join(f"`{path}` {problem}" for path, problem in problems)
            )
        flows = flows or {}

        design_cls = Design
        if designs is not None:
            for i, d in enumerate(designs):
                if not isinstance(d, dict):
                    raise ValueError(
                        f"Design entry {i} must be a mapping (table), got "
                        f"{type(d).__name__}: {d!r}"
                    )
            # A design entry is a table of dotted keys, as in a design file. After expansion a key
            # to remove has its field name.
            expanded = []
            for i, d in enumerate(designs):
                try:
                    expanded.append(design_cls.expanded(d))
                except DesignValidationError as e:
                    raise ValueError(
                        f"project file {file}: design entry {i}"
                        + (f" ({d['name']})" if "name" in d else "")
                        + ": "
                        + "; ".join(str(message) for _, message, *_ in e.errors)
                    ) from e
            designs = expanded
            if design_allow_extra:
                design_cls = model_with_allow_extra(design_cls)
            else:
                for d in designs:
                    design_cls.remove_keys(d, design_remove_extra)
        else:
            designs = []

        with WorkingDirectory(file.parent):
            try:
                return cls(
                    designs=designs,
                    flows=flows,
                    design_cls=design_cls,
                    root_path=file.parent.resolve(),
                    design_overrides=dict(design_overrides),
                )
            except Exception as e:
                log.error("Error processing project file: %s", file.absolute())
                raise e from None

    @property
    def design_names(self) -> List[str]:
        """Return the names of the designs as loaded. An override of `name` renames each."""
        renamed = "name" in self.design_overrides
        return [
            str(self.design_overrides["name"] if renamed else d.get("name"))
            for d in self.designs
            if renamed or "name" in d
        ]

    def design_index(self, name_or_idx: Union[str, int, None] = None) -> int | None:
        """Return the position of the design with that name (the first, if several have it) or at
        that position (the first by default); None if there is none."""
        if name_or_idx is None:
            name_or_idx = 0
        if isinstance(name_or_idx, str):
            if name_or_idx not in self.design_names:
                return None
            name_or_idx = self.design_names.index(name_or_idx)
        if len(self.designs) <= name_or_idx:
            return None
        return name_or_idx

    def get_design(
        self, name_or_idx: Union[str, int, None] = None, target: str | None = None
    ) -> Design | None:
        """Return the project's design with that name or at that position (the first by default).

        `target` is selected among its `targets` as `Design.from_file` selects one. Return None if
        there is no such design."""
        index = self.design_index(name_or_idx)
        if index is None:
            return None
        data = self.design_cls.target_selected(self.designs[index], target, self.design_overrides)
        data.setdefault("design_root", self.root_path)
        return self.design_cls(**data)
