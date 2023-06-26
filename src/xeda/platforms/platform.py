import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Type, TypeVar, Union

from importlib_resources import as_file, files

from ..dataclass import XedaBaseModel
from ..utils import toml_load

log = logging.getLogger(__name__)


PlatformType = TypeVar("PlatformType", bound="Platform")


class Platform(XedaBaseModel):
    root_dir: Path
    name: Optional[str] = None
    version: Optional[str] = None
    description: Optional[str] = None

    @classmethod
    def create(cls: Type[PlatformType], **kwargs) -> PlatformType:
        log.debug("create: kwargs=%s", str(kwargs))
        return cls(**kwargs)

    @classmethod
    def from_toml(
        cls: Type[PlatformType], platform_toml: Union[str, os.PathLike], overrides={}
    ) -> PlatformType:
        path = Path(platform_toml)
        kv = {**toml_load(path), **overrides}
        return cls.create(root_dir=path.parent, **kv).with_absolute_paths()

    @classmethod
    def from_resource(cls: Type[PlatformType], name: str, overrides={}) -> PlatformType:
        res = files(__package__).joinpath(name, "config.toml")
        with as_file(res) as path:
            d = cls.from_toml(path, overrides)
        if not d.name:
            d.name = name
        return d

    def with_absolute_paths(self: PlatformType) -> PlatformType:
        rd = self.root_dir.absolute()

        def to_abs(v):
            if v and isinstance(v, Path) and not os.path.isabs(v):
                return rd / v
            return v

        def convert_rec(kv: Dict[str, Any], exclude_keys=[]):
            for k, v in kv.items():
                if k in exclude_keys:
                    continue
                if isinstance(v, dict):
                    v = convert_rec(v)
                elif isinstance(v, (list, tuple)):
                    v = [to_abs(ve) for ve in v]
                else:
                    v = to_abs(v)
                kv[k] = v
            return kv

        return self.__class__(**convert_rec(self.dict(), exclude_keys=["root_dir"]))
