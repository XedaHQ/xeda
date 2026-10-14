"""The residue of a platform: the small file that xeda ships for it (`platform.yaml`).

The residue holds what neither the PDK files nor the configuration of the PDK give:

* `providers`: where a PDK store keeps the files of the platform (see `xeda.platforms.providers`);
* `upstream`: the configuration file of the PDK or of OpenROAD-flow-scripts, and its adapter;
* `corners`: the default corner and the aliases of the corners;
* `files`: the role of each file (`liberty`, `tech_lef`, ...) with its path in the platform
  directory;
* `values`: leaves of the platform model (`xeda.platforms.model`) that no lower layer gives;
* `overrides`: leaves that a lower layer gives and that this platform sets differently, each with
  the reason.

A user gives the same schema in a file (`platform_file`). The file names a bundled platform in
`extends` and then changes only what it writes, or it has no `extends` and is a complete platform.
"""

import re
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional

import yaml
from pydantic import ValidationError

from xeda.dataclass import Field, XedaBaseModel, field_validator, model_validator
from xeda.platforms.model import PlatformValues, flatten_leaves, normalize_leaf
from xeda.yaml_loader import load_yaml

__all__ = [
    "Corners",
    "CielSource",
    "DirectorySource",
    "Files",
    "OrfsSource",
    "Override",
    "Providers",
    "Residue",
    "ResidueError",
    "ResidueValues",
    "Upstream",
    "bundled_platform_names",
    "effective_residue",
    "load_residue",
]

#: The name of a platform or of a bundled platform directory.
PLATFORM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
#: A commit of a Git repository, written in full.
COMMIT = re.compile(r"[0-9a-f]{40}")


class ResidueError(ValueError):
    """A residue file that cannot be loaded. The message names the file."""


def relative_posix_path(text: str) -> str:
    """Return `text` if it is a path relative to the platform directory, written with `/`."""
    path = PurePosixPath(text)
    if not text or "\\" in text or path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"`{text}` is not a path relative to the platform directory; write it with `/` "
            "and without `..`"
        )
    return text


def plain_component(text: str) -> str:
    """Return `text` if it is one plain component of a path: a name that cannot leave the
    directory it is joined to."""
    if not text or text in (".", "..") or "/" in text or "\\" in text or "\0" in text:
        raise ValueError(
            f"`{text}` is not a single path component: write one name without `/`, "
            "and not `.` or `..`"
        )
    return text


def _pin(value: str) -> str:
    if not COMMIT.fullmatch(value):
        raise ValueError(f"`{value}` is not a full commit hash (40 lowercase hexadecimal digits)")
    return value


class OrfsSource(XedaBaseModel):
    """A platform directory in the OpenROAD-flow-scripts repository."""

    version: str = Field(description="The commit of the repository that holds the files.")
    directory: str = Field(description="Path of the platform directory inside the repository.")
    download_mib: Optional[float] = Field(
        None, description="Size in MiB of the download, which the install command shows first."
    )

    @field_validator("version", mode="after")
    @classmethod
    def _version_is_a_commit(cls, value: str) -> str:
        return _pin(value)

    @field_validator("directory", mode="after")
    @classmethod
    def _directory_is_relative(cls, value: str) -> str:
        return relative_posix_path(value)


class CielSource(XedaBaseModel):
    """A library of a PDK build that `ciel` installs."""

    family: str = Field(description="The PDK family of ciel, such as `sky130`.")
    variant: str = Field(description="The variant directory of the build, such as `sky130A`.")
    library: str = Field(description="The standard cell library, such as `sky130_fd_sc_hd`.")
    version: str = Field(description="The ciel version of the PDK build: the commit hash.")
    download_mib: Optional[float] = Field(
        None, description="Size in MiB of the download, which the install command shows first."
    )

    @field_validator("version", mode="after")
    @classmethod
    def _version_is_a_commit(cls, value: str) -> str:
        return _pin(value)

    @field_validator("family", "variant", "library", mode="after")
    @classmethod
    def _names_are_path_components(cls, value: str) -> str:
        return plain_component(value)


class DirectorySource(XedaBaseModel):
    """A directory of the user that holds the PDK files in a known layout."""

    layout: Literal["orfs", "open_pdks"] = Field(
        description="The layout of the directory: an OpenROAD-flow-scripts platform directory or "
        "a build of `open_pdks`."
    )
    variant: Optional[str] = Field(
        None, description="For the `open_pdks` layout: the variant directory, such as `sky130A`."
    )
    library: Optional[str] = Field(
        None, description="For the `open_pdks` layout: the standard cell library."
    )

    @field_validator("variant", "library", mode="after")
    @classmethod
    def _names_are_path_components(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else plain_component(value)

    @model_validator(mode="after")
    def _open_pdks_names_its_variant_and_library(self) -> "DirectorySource":
        given = {"variant": self.variant, "library": self.library}
        if self.layout == "open_pdks":
            missing = [name for name, value in given.items() if value is None]
            if missing:
                raise ValueError(f"the layout `open_pdks` needs {' and '.join(missing)}")
        else:
            extra = [name for name, value in given.items() if value is not None]
            if extra:
                raise ValueError(f"the layout `orfs` takes no {' and no '.join(extra)}")
        return self


class Providers(XedaBaseModel):
    """The providers that serve the platform. A provider without an entry does not serve it."""

    default: Optional[Literal["orfs", "ciel", "directory"]] = Field(
        None, description="The provider that serves the platform unless the user names another."
    )
    orfs: Optional[OrfsSource] = Field(None, description="The ORFS provider of the platform.")
    ciel: Optional[CielSource] = Field(None, description="The ciel provider of the platform.")
    directory: Optional[DirectorySource] = Field(
        None, description="The directory provider of the platform."
    )

    def entry(self, provider: str) -> Any:
        """Return the entry of the provider, or `None` if the provider does not serve."""
        return getattr(self, provider)


class Upstream(XedaBaseModel):
    """A configuration file of the PDK, which an adapter reads."""

    adapter: Literal["orfs_config_mk", "open_pdks_config"] = Field(
        description="The reader of the files."
    )
    files: list[str] = Field(description="Paths of the files in the platform directory.")

    @field_validator("files", mode="after")
    @classmethod
    def _files_are_relative(cls, value: list[str]) -> list[str]:
        return [relative_posix_path(item) for item in value]


class Corners(XedaBaseModel):
    """The corners of the platform. A corner is named by the token of the PDK, which is a key of
    `files.liberty`."""

    default: Optional[str] = Field(
        None, description="The corner that a run uses unless the user names another."
    )
    aliases: dict[str, str] = Field(
        default_factory=dict, description="Other names of a corner: alias to token."
    )


class Files(XedaBaseModel):
    """The files of the platform by role, with paths relative to the platform directory. A role
    that has one file takes text, and a role that can have several takes a list."""

    liberty: dict[str, list[str]] = Field(
        default_factory=dict, description="Liberty files by corner token."
    )
    tech_lef: Optional[str] = Field(None, description="The technology LEF file.")
    cell_lef: list[str] = Field(default_factory=list, description="LEF files of the cells.")
    gds: list[str] = Field(default_factory=list, description="GDS files of the cells.")
    rcx_rules: Optional[str] = Field(None, description="The OpenRCX rules file.")
    klayout_tech: Optional[str] = Field(None, description="The technology file of KLayout.")
    klayout_layer_props: Optional[str] = Field(
        None, description="The layer properties file of KLayout."
    )
    latch_map: Optional[str] = Field(None, description="The Yosys techmap file for latches.")
    clkgate_map: Optional[str] = Field(None, description="The Yosys techmap file for clock gates.")
    adder_map: Optional[str] = Field(None, description="The Yosys techmap file for adders.")

    @model_validator(mode="after")
    def _paths_are_relative(self) -> "Files":
        for paths in self.liberty.values():
            if not paths:
                raise ValueError("a corner of `liberty` needs at least one file")
        for path in self.all_paths():
            relative_posix_path(path)
        return self

    def all_paths(self) -> list[str]:
        """Return every path of every role, for every corner."""
        paths = [path for files in self.liberty.values() for path in files]
        for name in type(self).model_fields:
            value = getattr(self, name)
            if name == "liberty":
                continue
            paths.extend(value if isinstance(value, list) else [] if value is None else [value])
        return paths

    def role_paths(self, corner: str) -> dict[str, list[str]]:
        """Return the paths of the roles that have files, by role, with the Liberty files of the
        corner. The paths are relative to the platform directory."""
        roles: dict[str, list[str]] = {}
        for name in type(self).model_fields:
            value = self.liberty.get(corner) if name == "liberty" else getattr(self, name)
            if value:
                roles[name] = list(value) if isinstance(value, list) else [value]
        return roles


class ResidueValues(PlatformValues):
    """The leaves a residue gives under `values`, as nested tables that mirror the platform
    model. Every field is optional. The name, the corner, the files and the sources are no
    leaves of a residue: the residue gives the first three in its own keys."""


class Override(XedaBaseModel):
    """A leaf that a lower layer gives and that this platform sets differently."""

    value: Any = Field(description="The value of the leaf, in the type of the leaf.")
    reason: str = Field(description="Why this platform does not take the value of the lower layer.")

    @field_validator("reason", mode="after")
    @classmethod
    def _reason_is_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("an override needs a reason: say why the lower layer is not right")
        return value


class Residue(XedaBaseModel):
    """The residue file of a platform."""

    name: str = Field(description="Name of the platform.")
    description: Optional[str] = Field(None, description="One line that says what the platform is.")
    extends: Optional[str] = Field(
        None,
        description="Name of a bundled platform that this file changes. Only a user file has it.",
    )
    providers: Providers = Field(
        default_factory=Providers, description="The providers that serve the platform."
    )
    upstream: Optional[Upstream] = Field(
        None, description="The configuration file of the PDK and its adapter."
    )
    corners: Corners = Field(default_factory=Corners, description="The corners of the platform.")
    files: Files = Field(default_factory=Files, description="The files of the platform by role.")
    values: ResidueValues = Field(
        default_factory=ResidueValues,
        description="Leaves that neither the PDK files nor the upstream configuration give.",
    )
    overrides: dict[str, Override] = Field(
        default_factory=dict,
        description="Leaves that a lower layer gives and that this platform sets differently.",
    )

    @field_validator("name", "extends", mode="after")
    @classmethod
    def _name_is_a_plain_name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not PLATFORM_NAME.fullmatch(value):
            raise ValueError(f"`{value}` is not a platform name: use letters, digits, `_` and `-`")
        return value

    @model_validator(mode="after")
    def _is_consistent(self) -> "Residue":
        for key, override in self.overrides.items():
            if override.value is None:
                raise ValueError(f"overrides.{key}: the value is null; an override needs a value")
            try:
                normalize_leaf(key, override.value)
            except ValueError as error:
                raise ValueError(f"overrides: {error}") from None
        given = self._given_value_leaves()
        for key, value in given.items():
            if value is None:
                raise ValueError(f"values.{key} is null, which gives nothing; delete the line")
        both = sorted(set(given) & set(self.overrides))
        if both:
            raise ValueError(
                f"{', '.join(both)}: given in both `values` and `overrides`; keep the override"
            )
        if self.extends is None:
            self._is_complete()
        return self

    def _is_complete(self) -> None:
        """Check what a platform needs when no bundled platform supplies the rest."""
        provider = self.providers.default
        if provider is None:
            raise ValueError("providers.default is missing: name the provider that serves it")
        if self.providers.entry(provider) is None:
            raise ValueError(
                f"providers.default is `{provider}`, but `providers.{provider}` is missing"
            )
        if not self.files.liberty:
            raise ValueError("files.liberty is missing: the platform needs a Liberty file")
        if self.corners.default is None:
            raise ValueError("corners.default is missing: name the default corner")
        for alias, token in self.corners.aliases.items():
            if alias in self.files.liberty:
                raise ValueError(f"corners.aliases.{alias}: a corner of this name exists already")
            if token not in self.files.liberty:
                raise ValueError(
                    f"corners.aliases.{alias}: `{token}` is not a corner of files.liberty"
                    f" ({', '.join(self.files.liberty)})"
                )
        self.resolve_corner(None)

    def _given_value_leaves(self) -> dict[str, Any]:
        dumped = self.values.model_dump(mode="json", exclude_unset=True)
        return flatten_leaves(dumped)

    def value_leaves(self) -> dict[str, Any]:
        """Return the leaves of `values`, by leaf key in sorted order, in their normal form (see
        `xeda.platforms.model.normalize_leaf`)."""
        given = self._given_value_leaves()
        return {key: normalize_leaf(key, given[key]) for key in sorted(given)}

    def override_leaves(self) -> dict[str, Any]:
        """Return the values of `overrides`, by leaf key, in their normal form."""
        return {
            key: normalize_leaf(key, self.overrides[key].value) for key in sorted(self.overrides)
        }

    def resolve_corner(self, corner: Optional[str]) -> str:
        """Return the token of the corner. `None` gives the default corner, an alias gives its
        token, and a token gives itself. Raise `ValueError` for any other name."""
        known = list(self.files.liberty)
        wanted = self.corners.default if corner is None else corner
        if wanted is None:
            raise ValueError(f"platform '{self.name}' has no default corner: name one")
        token = self.corners.aliases.get(wanted, wanted)
        if token not in known:
            aliases = ", ".join(f"{a} ({t})" for a, t in self.corners.aliases.items()) or "none"
            raise ValueError(
                f"unknown corner '{wanted}' of platform '{self.name}'; "
                f"the corners are: {', '.join(known)}; the aliases are: {aliases}"
            )
        return token


# ---------------------------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------------------------


def bundled_platform_names() -> list[str]:
    """Return the names of the platforms that xeda ships a residue for, sorted."""
    root = resources.files("xeda.platforms")
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and entry.joinpath("platform.yaml").is_file()
    )


def _problems(error: ValidationError) -> str:
    return "; ".join(
        (".".join(str(part) for part in problem["loc"]) + ": " if problem["loc"] else "")
        + str(problem["msg"]).removeprefix("Value error, ")
        for problem in error.errors()
    )


def _read(path: Path, label: str) -> Residue:
    try:
        table = load_yaml(path)
    except (OSError, yaml.YAMLError) as error:
        raise ResidueError(f"{label}: {error}") from error
    if not isinstance(table, dict):
        raise ResidueError(f"{label}: the file must hold a table, not {type(table).__name__}")
    try:
        return Residue.model_validate(table)
    except ValidationError as error:
        raise ResidueError(f"{label}: {_problems(error)}") from None


def load_residue(name_or_path: str | Path) -> Residue:
    """Load a residue. A name (`str`) is a bundled platform, and a `Path` is a residue file.
    Raise `ResidueError`, which names the file, if the file is not a valid residue."""
    if isinstance(name_or_path, Path):
        return _read(name_or_path, str(name_or_path))
    names = bundled_platform_names()
    if name_or_path not in names:
        raise ResidueError(
            f"no platform '{name_or_path}' is bundled; the bundled platforms are: {', '.join(names)}"
        )
    label = f"xeda/platforms/{name_or_path}/platform.yaml"
    with resources.as_file(resources.files("xeda.platforms") / name_or_path / "platform.yaml") as p:
        residue = _read(p, label)
    if residue.name != name_or_path:
        raise ResidueError(f"{label}: the name is '{residue.name}', not '{name_or_path}'")
    if residue.extends is not None:
        raise ResidueError(f"{label}: only a user file extends a platform")
    return residue


def _dumped(model: XedaBaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_unset=True)


def effective_residue(residue: Optional[Residue], user: Optional[Residue]) -> Residue:
    """Return the residue that finds the files of a platform: `residue` (the bundled platform)
    with the structure that the user file gives on top. A key that the user file gives wins
    (a provider entry, the upstream files, a file role, the default corner), and
    `files.liberty` and the aliases merge per key. A key that the user file writes as `null`
    clears what the platform gives. The result has no `values` and no `overrides`,
    since those stay in their own layers."""
    if residue is None and user is None:
        raise ValueError("a platform needs a bundled residue, a user file, or both")
    if user is None:
        assert residue is not None
        return residue
    if residue is None:
        return user
    files = {**_dumped(residue.files), **_dumped(user.files)}
    files["liberty"] = {**residue.files.liberty, **user.files.liberty}
    corners = {**_dumped(residue.corners), **_dumped(user.corners)}
    corners["aliases"] = {**residue.corners.aliases, **user.corners.aliases}
    # A key the user file writes wins, even `upstream: null`, which clears the inherited one.
    upstream = user.upstream if "upstream" in user.model_fields_set else residue.upstream
    description = (
        user.description if "description" in user.model_fields_set else residue.description
    )
    return Residue.model_validate(
        {
            "name": user.name,
            "description": description,
            "providers": {**_dumped(residue.providers), **_dumped(user.providers)},
            "upstream": None if upstream is None else _dumped(upstream),
            "corners": corners,
            "files": files,
        }
    )
