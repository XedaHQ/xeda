"""The settings and the flow mixin that every ASIC flow shares.

An ASIC flow works on one platform: a standard cell library with its technology files. The
platform is a name (`platform`) of a platform that Xeda ships, or a residue file of the user
(`platform_file`). The PDK files of the platform live in a store (`pdk_root`) that one provider
serves (`pdk_provider`) at one version (`pdk_version`). Together these settings select one PDK, so
they agree along every declared edge of a chain (`flow_runner.resolver.SHARED_SETTINGS`), and the
resolver fills in the provider and the version from the residue when the user gives none. Then
the pin is part of the identity of every node, and the place of the store is not (`LOCATOR`).

The settings read the residue files and find the PDK files with `stat`. They start no process
and write nothing, so planning and `--dry-run` can use them.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Literal, Mapping, Optional

from ..dataclass import LOCATOR, Field, field_validator
from .flow import Flow, FlowSettingsException

if TYPE_CHECKING:
    from ..platforms import Located, Residue

    _FlowBase = Flow
else:
    _FlowBase = object

__all__ = [
    "AsicPlatformFlow",
    "AsicPlatformSettings",
    "DERIVATION_ROLES",
    "check_platform_settings",
    "platform_residues",
]

#: The roles of the files that the derivation of a platform model reads (the derived layer: the
#: site, the tie cells, the supply pins, the tracks and the units).
DERIVATION_ROLES = ("liberty", "tech_lef", "cell_lef")

_NO_TOML = (
    "`platform` no longer takes the path of a config.toml: give a bundled name (see `xeda "
    "list-platforms`), or a residue file in `platform_file`"
)


def _bundled_residue(name: str) -> "Residue":
    from ..platforms.residue import ResidueError, bundled_platform_names, load_residue

    names = bundled_platform_names()
    if name not in names:
        raise FlowSettingsException(
            f"no platform '{name}' is bundled; the bundled platforms are: {', '.join(names)}. "
            "Give the residue file of another platform in `platform_file`."
        )
    try:
        return load_residue(name)
    except ResidueError as error:
        raise FlowSettingsException(str(error)) from None


def platform_residues(
    platform: Optional[str], platform_file: Optional[Path]
) -> "tuple[Optional[Residue], Optional[Residue]]":
    """Read the residue files of a platform: the bundled one and the one of the user.

    Return `(bundled, user)`. The bundled residue is that of `platform`, or the one that the user
    file extends, and it is `None` for a user file that extends none. The user residue is `None`
    without `platform_file`. Raise `FlowSettingsException` for a file that cannot be read, for
    a name that no bundled platform has, and for a `platform` that is not the `name` of the
    user file. The function reads nothing else."""
    from ..platforms.residue import ResidueError, load_residue

    if platform is None and platform_file is None:
        raise FlowSettingsException(
            "no platform: give `platform` (a bundled name, see `xeda list-platforms`), or a "
            "residue file in `platform_file`"
        )
    user: Optional["Residue"] = None
    if platform_file is not None:
        try:
            user = load_residue(Path(platform_file))
        except ResidueError as error:
            raise FlowSettingsException(f"`platform_file` is not usable: {error}") from None
        if platform is not None and platform != user.name:
            raise FlowSettingsException(
                f"`platform` is '{platform}', but `platform_file` {platform_file} names the "
                f"platform '{user.name}': give one platform, or make the names equal"
            )
        base = user.extends
    else:
        base = platform
    return (None if base is None else _bundled_residue(base)), user


def check_platform_settings(settings: "AsicPlatformSettings") -> None:
    """Refuse a platform that the settings cannot select. Pure: it reads the residue files only.

    That is a platform that is unknown or unreadable, a `platform` that disagrees with the file,
    an unknown corner, a provider that does not serve the platform, and a version that is no
    pin of that provider. Raise `FlowSettingsException`. Settings without a platform pass."""
    if settings.platform is None and settings.platform_file is None:
        return
    from ..platforms.providers import platform_directory

    residue = settings.residue()
    try:
        residue.resolve_corner(settings.corner)
        platform_directory(
            residue, settings.pdk_provider, settings.pdk_version, settings.store_root()
        )
    except ValueError as error:
        raise FlowSettingsException(str(error)) from None


class AsicPlatformSettings(Flow.Settings):
    """The settings that select the platform and the PDK of an ASIC flow."""

    platform: Optional[str] = Field(
        None,
        description="Name of a bundled ASIC platform (see `xeda list-platforms`). It names the "
        "standard cell library and the technology files that the flow works on. Give "
        "`platform_file` for a platform that Xeda does not ship; then `platform` can be left out, "
        "and if given it must be the `name` of that file.",
    )
    platform_file: Optional[Path] = Field(
        None,
        description="Residue file of a platform (YAML). A file that sets `extends` to a bundled "
        "platform changes only what it gives. A file without `extends` is a complete platform. "
        "A relative path is relative to the design root.",
    )
    corner: Optional[str] = Field(
        None,
        description="Name of the corner of the platform, or one of its aliases. The default is "
        "the default corner of the platform. A flow works on one corner: a list of corners is "
        "refused.",
    )
    dont_use_cells: List[str] = Field(
        [],
        description="Standard cells that the flow must not use, in addition to the cells that "
        "the platform lists. The default is none.",
    )
    pdk_provider: Optional[Literal["orfs", "ciel", "directory"]] = Field(
        None,
        description="Where the PDK files of the platform come from. `orfs`: a sparse checkout "
        "of OpenROAD-flow-scripts. `ciel`: a PDK build that `ciel` installs. `directory`: a "
        "directory that holds the files in a known layout. The default is the provider that the "
        "platform names; the platform must have an entry for the provider.",
    )
    pdk_version: Optional[str] = Field(
        None,
        description="Version of the PDK files: the commit (`orfs`) or the hash (`ciel`). For "
        "`directory`, a version that Xeda checks against the directory when it can, and "
        "`unpinned` when you give none. The default is the version that the platform pins for "
        "`pdk_provider`. The version is part of the identity of the run.",
    )
    pdk_root: Optional[Path] = Field(
        None,
        json_schema_extra=LOCATOR,
        description="Directory of the PDK store. `None` means `~/.ciel`. Variables such as "
        "`$HOME` expand in it, and a relative path is relative to the design root. It tells "
        "where the files are, so it is no part of the identity of the run: the run depends on "
        "the content of the files it reads.",
    )

    @field_validator("platform", mode="after")
    @classmethod
    def _platform_is_a_name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value.lower().endswith(".toml"):
            raise ValueError(_NO_TOML)
        return value

    @field_validator("corner", mode="before")
    @classmethod
    def _corner_is_one_name(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            raise ValueError(
                "`corner` takes one corner name: multi-corner analysis is not supported"
            )
        return value

    # --- where things are ---------------------------------------------------------------------

    def _in_design(self, path: Path) -> Path:
        """Return `path`, made relative to the design root as the other path settings are."""
        root = self.context.get("design_root")
        return path if path.is_absolute() or root is None else Path(root) / path

    def store_root(self) -> Path:
        """Return the root of the PDK store: `pdk_root`, or `~/.ciel` (`~` is `HOME`)."""
        from ..platforms.providers import default_root

        return default_root() if self.pdk_root is None else self._in_design(self.pdk_root)

    def user_platform_file(self) -> Optional[Path]:
        """Return the residue file of the user, or `None`."""
        return None if self.platform_file is None else self._in_design(self.platform_file)

    # --- the residue and the files ------------------------------------------------------------

    def residue_layers(self) -> "tuple[Optional[Residue], Optional[Residue]]":
        """Return the bundled residue and the residue of the user (see `platform_residues`)."""
        return platform_residues(self.platform, self.user_platform_file())

    def residue(self) -> "Residue":
        """Return the effective residue: the bundled one of `platform`, or the user file of
        `platform_file` over the bundled one that it extends. This reads the residue files only."""
        from ..platforms.residue import effective_residue

        return effective_residue(*self.residue_layers())

    def locate_platform(self) -> "Located":
        """Find the PDK files of the platform in the store, with `stat` only.

        The provider, the version, the root and the corner are those of the settings. Raise
        `PdkNotInstalled` if a file is missing, and `FlowSettingsException` for a provider or a
        corner that the platform does not have."""
        from ..platforms import locate

        try:
            return locate(
                self.residue(),
                self.pdk_provider,
                self.pdk_version,
                self.store_root(),
                self.corner,
            )
        except ValueError as error:
            raise FlowSettingsException(str(error)) from None

    def platform_files(self, roles: Iterable[str]) -> List[Path]:
        """Return every file that a flow may open for the platform, once, in a fixed order.

        First the files of `roles` (the role names of the residue, such as `liberty`, `gds`), in
        the order given. Then the files that the derivation of the platform model reads
        (`DERIVATION_ROLES`) and that are not listed. Then the files of the upstream
        configuration, and last the residue file of the user. A role that the platform has no
        files for adds none: the flow that needs it refuses when it prepares its inputs."""
        located = self.locate_platform()
        listed: dict[Path, None] = {}
        for role in (*roles, *DERIVATION_ROLES):
            listed.update(dict.fromkeys(located.files.get(role, [])))
        listed.update(dict.fromkeys(located.upstream_files))
        user = self.user_platform_file()
        if user is not None:
            listed.setdefault(user)
        return list(listed)


class AsicPlatformFlow(_FlowBase):
    """A mixin for a flow that works on an ASIC platform. List it before the flow base class,
    and give the flow settings that inherit `AsicPlatformSettings`.

    A flow that requires `platform` (`Flow.required_settings`) also runs when only
    `platform_file` is given, since the residue file names its platform. The flow refuses the
    platform that its settings cannot select when it is planned (`check_platform_settings`)."""

    @classmethod
    def required_settings_for(cls, settings: Flow.Settings) -> Mapping[str, str]:
        required = super().required_settings_for(settings)
        if getattr(settings, "platform_file", None) is not None:
            return {name: how for name, how in required.items() if name != "platform"}
        return required

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        super().check_settings_supported(settings)
        if isinstance(settings, AsicPlatformSettings):
            check_platform_settings(settings)
