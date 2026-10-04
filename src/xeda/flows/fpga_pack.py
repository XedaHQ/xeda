"""Packing a routed FPGA configuration into a bitstream, all of it or none."""

import logging
from pathlib import Path, PurePath
from shutil import which
from tempfile import TemporaryDirectory

from ..board import FPGA_OR_BOARD_REQUIRED, WithFpgaBoardSettings
from ..dataclass import Field, deliverable
from ..design import SourceType
from ..flow import Flow, FlowFatalError, FlowSettingsException, FpgaSynthFlow, In, Out
from ..tool import Tool
from ..utils import replacing_copy
from .nextpnr import Nextpnr
from .xilinx import find_prjxray_database, select_xilinx_part

__all__ = ["FpgaPack"]

log = logging.getLogger(__name__)

#: The bitstream packer of each family this flow packs, and its bitstream's file extension.
#: openFPGALoader reads a file by its extension: `ecppack` and `fpga-as` write `.bit`,
#: `icepack` a raw `.bin`.
PACKERS = {"ecp5": ("ecppack", ".bit"), "ice40": ("icepack", ".bin"), "xilinx": ("fpga-as", ".bit")}


class FpgaPack(FpgaSynthFlow):
    """Pack a routed FPGA design into a bitstream.

    Its `config` input is the routed configuration of the target's family: a typed design
    source (`EcpConfig`, `IceAsc` or `Fasm`) or, by default, the one `nextpnr` records after
    `yosys_fpga` -> `nextpnr`. It is packed with `ecppack` (ECP5), `icepack` (iCE40) or
    openXC7's `fpga-as` (Xilinx 7-series) into the declared `bitstream` output. The packer
    writes to scratch space in the run directory, and the bitstream is published only once the
    packer has exited successfully with a nonempty file: a failed packing never leaves a
    partial bitstream, nor replaces an earlier one. Nothing is programmed; `openfpgaloader`
    programs a bitstream.
    """

    required_settings = {"fpga": FPGA_OR_BOARD_REQUIRED}

    # Nothing beyond the keys every flow reports.
    results_description: dict = {}

    class Settings(WithFpgaBoardSettings):
        packer_args: list[str] = Field(
            [], description="Extra arguments to the packer (`ecppack`, `icepack` or `fpga-as`)."
        )
        prjxray_db: Path | None = Field(
            None,
            description="Xilinx 7-series: Project X-Ray database root override; `nextpnr` "
            "routes with the same one. By default, the database installed with `fpga-as`.",
        )
        bitstream: Path | None = Field(
            None,
            description="The packed bitstream: a name in the run directory, or a location it "
            "is delivered to. By default `outputs/<design>.bit` (`.bin` for iCE40).",
            json_schema_extra=deliverable("outputs/{design}.bit"),
        )

        def conventional_output(self, field: str, design: str) -> PurePath | None:
            """The bitstream is named for its packer's format, which the family decides."""
            if field == "bitstream":
                _, suffix = PACKERS.get(Nextpnr.io_family(self), ("", ".bit"))
                return PurePath("outputs", design + suffix)
            return super().conventional_output(field, design)

    class Inputs(FpgaSynthFlow.Inputs):
        config: Path = In(
            (SourceType.EcpConfig, SourceType.IceAsc, SourceType.Fasm),
            producer="nextpnr",
            output="config",
            description="The routed configuration to pack: a design source or nextpnr's.",
        )

    class Outputs(FpgaSynthFlow.Outputs):
        bitstream: Path = Out(SourceType.Bitstream, description="The packed FPGA bitstream.")

    @classmethod
    def input_types(cls, settings: Flow.Settings, name: str) -> tuple[SourceType, ...]:
        if name == "config":  # the format nextpnr writes for this family
            return Nextpnr.output_types(settings, name)
        return super().input_types(settings, name)

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Reject a target that cannot be packed before anything is synthesized or placed."""
        assert isinstance(settings, cls.Settings)
        fpga = settings.fpga
        assert fpga is not None, "checked at launch (`required_settings`)"
        family = Nextpnr.io_family(settings)
        if family not in PACKERS:
            raise FlowSettingsException(
                f"fpga_pack has no bitstream packer for fpga.family={fpga.family or None!r} "
                f"(packers: {', '.join(f'{k}: {v[0]}' for k, v in PACKERS.items())}); run "
                "nextpnr on its own for other families."
            )
        if family != "xilinx":
            if settings.prjxray_db is not None:
                raise FlowSettingsException(f"{PACKERS[family][0]} does not take prjxray_db.")
        elif not (fpga.part and fpga.device and fpga.package and fpga.pins and fpga.speed):
            raise FlowSettingsException(
                "fpga-as needs the full part in fpga.part, with its package and speed grade, "
                f"e.g. xc7a100tcsg324-1, not {fpga.part or fpga.device or None!r}."
            )

    def run(self) -> None:
        """Pack the configuration handed over as the input `config`."""
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        inputs, declared = self.inputs, self.outputs
        assert isinstance(inputs, self.Inputs) and isinstance(declared, self.Outputs)
        assert ss.fpga is not None
        family = Nextpnr.io_family(ss)
        executable, _ = PACKERS[family]
        config = inputs.config
        name = ss.bitstream or ss.conventional_output("bitstream", self.design.name)
        assert name is not None
        # never through a link at that name (`RunDirectory.writable`)
        bitstream = self.run_directory.writable(self.run_path / name)
        # `fpga-as --version` prints its name alone: there is no version to read.
        packer = (
            Tool(executable=executable, version_flag=None)
            if family == "xilinx"
            else Tool(executable=executable)
        )
        # Every packer is given its options before its operands: `icepack`'s usage is
        # `[options] [input-file [output-file]]`, and a strict POSIX `getopt` (BSD, musl) stops
        # at the first operand, where an option after it would be read as one. `ecppack` and
        # `fpga-as` accept options in any position.
        fixed: list[str | Path] = []
        if family == "xilinx":
            resolved = which(executable)
            if resolved is None:
                raise FlowFatalError("fpga-as is missing on PATH; install openXC7 1.0.")
            database = find_prjxray_database(
                Path(resolved),
                self.normalize_path_to_design_root(ss.prjxray_db) if ss.prjxray_db else None,
            )
            selection = select_xilinx_part(ss.fpga.part or "", database)
            fixed = [f"--prjxray_db_path={database / selection.family}", f"--part={selection.name}"]
        # The packer writes scratch space only: an exit of zero may still leave no file (or an
        # empty one), and fpga-as writes its bitstream to standard output, so a failure there
        # leaves part of one. The bitstream's own name is replaced only by a whole new one.
        # ... and one a killed run left is removed here: nothing else would, and every entry
        # of a run directory after a run is recorded as that run's output
        self.run_directory.remove(*self.run_path.glob(".xeda-pack-*"))
        with TemporaryDirectory(prefix=".xeda-pack-", dir=self.run_path) as scratch:
            packed = Path(scratch) / bitstream.name
            if family == "xilinx":
                packer.run(*fixed, *ss.packer_args, config, stdout=packed)
            else:
                packer.run(*ss.packer_args, config, packed)
            if not packed.is_file() or packed.stat().st_size == 0:
                raise FlowFatalError(
                    f"{executable} exited successfully but wrote no bitstream for {config}."
                )
            bitstream.parent.mkdir(parents=True, exist_ok=True)
            replacing_copy(packed, bitstream)
        declared.bitstream = bitstream
        self.artifacts["bitstream"] = bitstream
