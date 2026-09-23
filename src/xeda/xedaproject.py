from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

import attrs
import yaml

from .dataclass import model_with_allow_extra
from .design import DESIGN_FILE_FORMATS, Design
from .utils import WorkingDirectory, hierarchical_merge, tomllib

log = logging.getLogger(__name__)


@attrs.define
class XedaProject:
    # TODO: workspace options
    workspace: Dict[str, Any] = {}
    designs: List[Dict[str, Any]] = []
    # keep raw dict as flows are dynamically discovered
    flows: Dict[str, dict] = {}  # = attrs.field(default={}, validator=type_validator())
    design_cls: Type[Design] = Design
    root_path: Path = attrs.field(factory=Path.cwd)

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
        with open(file, "rb" if fmt == "toml" else "r") as f:
            if fmt == "toml":
                data = tomllib.load(f)
            elif fmt == "json":
                data = json.load(f)
            else:
                data = yaml.safe_load(f)
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
        flows = data.get("flow", data.get("flows", {}))
        if not isinstance(flows, dict):
            raise ValueError(f"Project `flows` must be a mapping, got {type(flows).__name__}")

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
                )
            except Exception as e:
                log.error("Error processing project file: %s", file.absolute())
                raise e from None

    @property
    def design_names(self) -> List[str]:
        return [str(d.get("name")) for d in self.designs if "name" in d]

    def get_design(self, name_or_idx: Union[str, int, None] = None) -> Optional[Design]:
        if name_or_idx is None:
            return self.get_design(0)
        if isinstance(name_or_idx, int):
            if len(self.designs) <= name_or_idx:
                return None
            data = dict(self.designs[name_or_idx])
            data.setdefault("design_root", self.root_path)
            return self.design_cls(**data)
        try:
            data = dict(self.designs[self.design_names.index(name_or_idx)])
            data.setdefault("design_root", self.root_path)
            return self.design_cls(**data)
        except ValueError:
            return None
