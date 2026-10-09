"""Place and route of a netlist in Vivado, in non-project mode."""

import logging
from pathlib import Path
from typing import Optional

from ...dataclass import Field, field_validator
from ...design import Design, SourceType
from ...edif import modules_used_as_library_cells_in_file
from ...flow import Flow, FlowFatalError, FlowSettingsException, In, Out
from ...utils import replacing_copy
from .vivado_alt_synth import expand_run_options, flatten_options
from .vivado_synth import RunOptions, VivadoImplementation, VivadoSynth, declare_outputs

__all__ = ["VivadoImpl"]

log = logging.getLogger(__name__)


class VivadoImpl(VivadoImplementation):
    """Place and route a netlist with AMD-Xilinx Vivado, in non-project mode.

    Its `netlist` input is an `Edif` design source or, by default, the EDIF netlist that
    `yosys_fpga` writes for a flattened Xilinx synthesis, so `xeda run yosys_fpga+vivado_impl
    design.yaml` implements the open-source synthesis with Vivado. Vivado does no synthesis here.
    A generated TCL script reads the constraints and the netlist, links the design, and runs
    `opt_design`, `place_design` and `route_design` on it in memory, with the optimizations that
    `impl` asks for (the default strategy adds `opt_design` and `phys_opt_design` after
    placement); it reports timing and utilization, and writes a bitstream when `bitstream` is
    set or a consumer needs it. The results are those of `vivado_synth`.

    Vivado finds the top module of an EDIF netlist by the name of its file, so the flow copies
    the netlist to `<top>.edif` in its run directory, whatever the file was called. The design
    needs `rtl.top` for that, and `fpga` and a clock as for any Vivado flow. Pin constraints
    are `Xdc` design sources or `xdc_files`.

    A netlist from another source has to be flat, and written with its buses' ranges: yosys's
    `write_edif -pvector bra` after `synth_xilinx -flatten`. The flow reads the netlist before it
    starts Vivado, and stops for one that is not flat, naming its modules. It cannot see the
    ranges. Without them Vivado links the design with every bus reversed and says nothing.
    """

    # Vivado's runs, whose status `vivado_synth` reports, are project mode's
    results_description = {
        key: text for key, text in VivadoSynth.results_description.items() if key != "status"
    }

    class Settings(VivadoImplementation.Settings):
        """Vivado implementation settings"""

        impl: RunOptions = Field(
            RunOptions(strategy="Default"),
            description="Implementation run options: a named `strategy` (the ones `impl.strategy` "
            "of `vivado_alt_synth` takes) and options for single steps (`impl.steps`: `place`, "
            "`place_opt`, `place_opt2`, `phys_opt`, `route`, `post_route_phys_opt`, "
            "`power_opt`), which replace the strategy's.",
        )

        @field_validator("impl")
        @classmethod
        def _expand_impl(cls, value):
            return expand_run_options("impl", value)

    class Inputs(VivadoImplementation.Inputs):
        netlist: Path = In(
            SourceType.Edif,
            producer="yosys_fpga",
            output="netlist_edif",
            description="The EDIF netlist to implement: a design source or yosys_fpga's.",
        )
        constraints: list[Path] = In(
            (SourceType.Xdc, SourceType.Sdc),
            optional=True,
            description="XDC and SDC constraints, in design-source order.",
        )

    class Outputs(VivadoImplementation.Outputs):
        bitstream: Path | None = Out(
            SourceType.Bitstream,
            enabled_by="bitstream",
            description="The FPGA bitstream, at `bitstream`.",
        )

    @classmethod
    def check_design_supported(cls, design: Design) -> None:
        """Vivado looks the top of an EDIF netlist up by its file name (`run`), so the design
        has to name one that is a file name."""
        super().check_design_supported(design)
        top = design.rtl.top
        if not top:
            raise FlowSettingsException(
                "vivado_impl needs the design's top module (`rtl.top`): Vivado finds the top of "
                "an EDIF netlist by the name of its file, which the flow names for it"
            )
        if "/" in top or "\\" in top or "\0" in top:
            raise FlowSettingsException(
                f"vivado_impl names the netlist's file for the top module, {top!r}, which is "
                "no name a file can have"
            )

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Vivado implements Xilinx devices only."""
        assert isinstance(settings, cls.Settings)
        fpga = settings.fpga
        assert fpga is not None, "checked at launch (`required_settings`)"
        vendor = (fpga.vendor or "").lower()
        if vendor != "xilinx":
            raise FlowSettingsException(
                "vivado_impl implements Xilinx devices, and fpga.part="
                f"{fpga.part!r} is not one (fpga.vendor is {vendor or None!r})"
            )

    def init(self) -> None:
        super().init()
        self.add_template_filter("flatten_options", flatten_options)

    @staticmethod
    def refuse_a_hierarchy(netlist: Path) -> None:
        """Stop for a netlist that Vivado would take apart into black boxes. The settings show
        only some of the ways to a hierarchy (`YosysFpga.Settings.edif_problem`): the HDL asks for
        one too (`(* keep_hierarchy *)`), and a netlist from elsewhere may hold one. The netlist
        shows them all, and is read in chunks: the file of a large design is not held. Vivado
        reports the cause in its log, and xeda only its exit status."""
        modules = modules_used_as_library_cells_in_file(netlist)
        if modules:
            raise FlowFatalError(
                f"The EDIF netlist {netlist} is not flat. Its instances refer to modules that it "
                f"defines as library cells: {', '.join(modules)}. Vivado looks a library cell up "
                "among its primitives, takes each of these modules for a black box, and stops. "
                "Write a flat netlist. With `yosys_fpga`, set `flatten: true`, and remove every "
                "`keep_hierarchy`: the setting, the attribute in `set_mod_attribute`, "
                "`set_attribute` and `rtl.attributes`, and the attribute in the HDL."
            )

    def run(self) -> None:
        """Place and route the netlist handed over as the input `netlist`."""
        ss = self.settings
        assert isinstance(ss, self.Settings)
        inputs, declared = self.inputs, self.outputs
        assert isinstance(inputs, self.Inputs) and isinstance(declared, self.Outputs)
        top = self.design.rtl.top
        assert top, "checked at launch (`check_design_supported`)"
        if not self.design.rtl.clocks:
            log.warning("No clocks specified for top RTL design.")
        self.refuse_a_hierarchy(inputs.netlist)

        # Vivado finds the top by the name of the file (`[Project 1-68] No files found to match
        # top module`): so whatever the netlist is called, the script reads this copy of it
        netlist = self.run_directory.writable(f"{top}.edif")
        replacing_copy(inputs.netlist, netlist)

        bitstream: Optional[Path] = None
        if ss.bitstream is not None:
            # never through a link at that name (`RunDirectory.writable`)
            bitstream = self.run_directory.writable(self.run_path / ss.bitstream)
            bitstream.parent.mkdir(parents=True, exist_ok=True)
            declare_outputs(self, {"bitstream": ss.bitstream})

        xdc_files = [
            self.copy_from_template("clock.xdc"),
            *inputs.constraints,
            *(self.normalize_path_to_design_root(p) for p in ss.xdc_files),
        ]
        script_path = self.copy_from_template(
            "vivado_impl.tcl",
            xdc_files=xdc_files,
            netlist=netlist.relative_to(self.run_directory.path),
        )
        self.vivado.run("-source", script_path)
        if bitstream is not None and not self.wrote_output(bitstream):
            raise FlowFatalError(
                f"Vivado completed the implementation, but it wrote no bitstream at {bitstream}"
                + (": the file there is from before the run." if bitstream.exists() else ".")
            )
