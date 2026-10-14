"""The base of the OpenROAD flows."""

from ...flow import Flow
from ...flow.asic import AsicPlatformFlow, AsicPlatformSettings

__all__ = ["OpenroadStage"]


class OpenroadStage(AsicPlatformFlow, Flow):
    """The base class of the OpenROAD flows. Each stage works on a platform and reads only its
    declared inputs."""

    reads_sources = None
    design_parts = frozenset({"rtl"})
    required_settings = {
        "platform": "the ASIC platform: give a bundled platform's name with `-s platform=<name>` "
        "(see `xeda list-platforms`), or a residue file with `platform_file`; in the design "
        "file, give it as `flows.{flow}.platform`",
    }

    class Settings(AsicPlatformSettings):
        """The settings that every OpenROAD flow shares."""
