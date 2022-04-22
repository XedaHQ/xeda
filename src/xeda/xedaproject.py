from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Union

import yaml

from .dataclass import XedaBaseModel
from .design import Design
from .utils import WorkingDirectory, toml_load


class XedaProject(XedaBaseModel):
    # validate to concrete Designs to verify the whole xedaproject
    designs: List[Design]
    # keep raw dict as flows are dynamically discovered
    flows: Dict[str, dict] = {}

    @classmethod
    def from_file(cls, file: Union[str, os.PathLike, Path]):
        """load xedaproject from file"""
        if not isinstance(file, Path):
            file = Path(file)
        ext = file.suffix.lower()
        if ext == ".toml":
            data = toml_load(file)
        else:
            with open(file) as f:
                if ext == ".json":
                    data = json.load(f)
                elif ext == ".yaml":
                    data = yaml.safe_load(f)
                else:
                    raise ValueError(
                        f"File {file} has unknown extension {ext}. Supported formats are TOML, JSON, and YAML."
                    )
        if not isinstance(data, dict) or not data:
            raise ValueError("Invalid xedaproject!")
        designs = data.get("design") or data.get("designs")
        if not designs:
            raise ValueError("No designs found in the xedaproject file!")

        flows = data.get("flow") or data.get("flows")
        with WorkingDirectory(file.parent):
            return cls(designs=designs, flows=flows)

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
