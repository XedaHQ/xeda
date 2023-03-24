from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

import attrs
import yaml

from .dataclass import model_with_allow_extra
from .design import Design
from .utils import WorkingDirectory, hierarchical_merge, tomllib

log = logging.getLogger(__name__)


@attrs.define
class XedaProject:
    # TODO: workspace options
    workspace: Dict[str, Any] = {}
    # validate to concrete Designs to verify the whole xedaproject
    designs: List[Design] = []
    # keep raw dict as flows are dynamically discovered
    flows: Dict[str, dict] = {}  # = attrs.field(default={}, validator=type_validator())

    @classmethod
    def from_file(
        cls: Type["XedaProject"],
        file: Union[str, Path],
        skip_designs: bool = False,
        design_overrides: Dict[str, Any] = {},
        design_allow_extra: bool = False,
        design_remove_extra: List[str] = [],
    ) -> "XedaProject":
        """load xedaproject from file"""
        if not isinstance(file, Path):
            file = Path(file)
        ext = file.suffix.lower()
        with open(file, "rb" if ext == ".toml" else "r") as f:
            if ext == ".toml":
                data = tomllib.load(f)
            elif ext == ".json":
                data = json.load(f)
            elif ext == ".yaml":
                data = yaml.safe_load(f)
            else:
                raise ValueError(
                    f"File {file} has unknown extension {ext}. Supported formats are TOML, JSON, and YAML."
                )
        if not isinstance(data, dict) or not data:
            raise ValueError("Invalid xedaproject!")
        designs = None
        if not skip_designs:
            designs = data.get("design") or data.get("designs")
        if designs:
            if not isinstance(designs, list):
                designs = [designs]

        flows = data.get("flow") or data.get("flows", {})
        assert isinstance(flows, dict)
        design_cls = Design

        if designs is not None:
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
                return cls(  # type: ignore[call-arg]
                    designs=[
                        design_cls(**hierarchical_merge(d, design_overrides))
                        for d in designs
                        if isinstance(d, dict)
                    ],
                    flows=flows,
                )
            except Exception as e:
                log.error("Error processing project file: %s", file.absolute())
                raise e from None

    @property
    def design_names(self) -> List[str]:
        return [d.name for d in self.designs]

    def get_design(self, name: Optional[str] = None) -> Optional[Design]:
        if name is None:
            return self.designs[0] if len(self.designs) == 1 else None
        try:
            return self.designs[self.design_names.index(name)]
        except ValueError:
            return None
