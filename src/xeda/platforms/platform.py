import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar, Union

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

    @classmethod
    def from_setting(cls: Type[PlatformType], value: Any) -> Any:
        """Resolve a flow's `platform` setting: a bundled platform name or a `config.toml` path.

        Anything else (a mapping, an existing instance) is returned for pydantic to validate.
        A name or path that does not resolve is reported as a `ValueError`, so that it becomes a
        settings error naming the field -- `-s platform=asap8` used to escape as a bare
        `FileNotFoundError` traceback pointing into xeda's own package directory.
        """
        if not isinstance(value, (str, Path)):
            return value
        name = value if isinstance(value, str) and not value.endswith(".toml") else None
        try:
            return cls.from_resource(name) if name is not None else cls.from_toml(value)
        except FileNotFoundError:
            if name is not None:
                bundled = bundled_platform_names()
                raise ValueError(
                    f"Unknown platform {value!r}. "
                    + (
                        f"Bundled platforms: {', '.join(bundled)}."
                        if bundled
                        else "No platforms are bundled with this installation of xeda."
                    )
                    + " A path to a platform's config.toml is also accepted."
                ) from None
            raise ValueError(f"Platform file not found: {value}") from None

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

        return self.__class__(**convert_rec(self.model_dump(), exclude_keys=["root_dir"]))


def bundled_platform_names() -> List[str]:
    """Names of the platforms shipped in `xeda.platforms` (those with a `config.toml`)."""
    try:
        root = Path(str(files(__package__)))
    except (ModuleNotFoundError, TypeError):  # pragma: no cover
        return []
    return sorted(config.parent.name for config in root.glob("*/config.toml"))
