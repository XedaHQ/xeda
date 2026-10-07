from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Type, Union

import attrs

from .dataclass import model_with_allow_extra
from .design import DESIGN_FILE_FORMATS, Design
from .utils import (
    WorkingDirectory,
    XedaException,
    flows_table_problems,
    hierarchical_merge,
    tomllib,
)
from .yaml_loader import load_yaml

log = logging.getLogger(__name__)


PROJECT_FILE_NAMES = ("xedaproject.yaml", "xedaproject.yml", "xedaproject.toml")


class ProjectFileError(XedaException):
    """A project file (`xedaproject.yaml`, `.yml` or `.toml`) that cannot be found, opened,
    parsed or used."""


def find_default_xedaproject(directory: str | Path = ".") -> Path | None:
    """Return the sole conventional project file in *directory*, if present.

    Raises `ProjectFileError` when *directory* holds more than one of `PROJECT_FILE_NAMES`.
    """
    directory = Path(directory)
    found = [directory / name for name in PROJECT_FILE_NAMES if (directory / name).exists()]
    if len(found) > 1:
        names = ", ".join(path.name for path in found)
        raise ProjectFileError(
            f"Multiple project files found in {directory}: {names}; keep one project file"
        )
    return found[0] if found else None


def resolve_project_file(
    given: str | Path | None = None, directory: str | Path = "."
) -> Path | None:
    """The project file a launch reads, the same for a local and a remote one: the file *given*
    (which must exist; `""` is no file given), else the sole conventional project file in
    *directory*, else `None`. Raises `ProjectFileError` for a missing file given and for an
    ambiguous directory."""
    if given:
        path = Path(given)
        if not path.exists():
            raise ProjectFileError(f'Cannot open project file "{given}": no such file')
        return path
    return find_default_xedaproject(directory)


@attrs.define
class XedaProject:
    # TODO: workspace options
    workspace: Dict[str, Any] = {}
    designs: List[Dict[str, Any]] = []
    # keep raw dict as flows are dynamically discovered
    flows: Dict[str, dict] = {}  # = attrs.field(default={}, validator=type_validator())
    design_cls: Type[Design] = Design
    root_path: Path = attrs.field(factory=Path.cwd)
    # applied to each design when the project is read, and again over a selected target
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
        """load xedaproject from file"""
        if design_overrides is None:
            design_overrides = {}
        if design_remove_extra is None:
            design_remove_extra = []
        if not isinstance(file, Path):
            file = Path(file)
        # The table design files are read by: case-sensitive, `.yml` is YAML too.
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
            raise ValueError("Invalid xedaproject!")
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
            if design_allow_extra:
                design_cls = model_with_allow_extra(design_cls)
            else:
                for d in designs:
                    for k in design_remove_extra:
                        d.pop(k, None)
        else:
            designs = []

        with WorkingDirectory(file.parent):
            try:
                return cls(
                    designs=[hierarchical_merge(d, design_overrides) for d in designs],
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
        return [str(d.get("name")) for d in self.designs if "name" in d]

    def get_design(
        self, name_or_idx: Union[str, int, None] = None, target: str | None = None
    ) -> Design | None:
        """The project's design of that name or position (the first by default), with `target`
        selected among its `targets` as `Design.from_file` selects one; None if there is none."""
        if name_or_idx is None:
            name_or_idx = 0
        if isinstance(name_or_idx, str):
            if name_or_idx not in self.design_names:
                return None
            name_or_idx = self.design_names.index(name_or_idx)
        if len(self.designs) <= name_or_idx:
            return None
        data = self.design_cls.target_selected(
            self.designs[name_or_idx], target, self.design_overrides
        )
        data.setdefault("design_root", self.root_path)
        return self.design_cls(**data)
