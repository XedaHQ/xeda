"""Place and route of a netlist in Vivado, in non-project mode."""

import logging
from pathlib import Path
from typing import Optional

from ...dataclass import Field, field_validator
from ...design import Design, SourceType
from ...edif import modules_used_as_library_cells_in_file
from ...flow import Flow, FlowFatalError, FlowSettingsException, In
from ...utils import replacing_copy
from .vivado_alt_synth import expand_run_options, flatten_options
from .vivado_synth import RunOptions, VivadoImplementation, VivadoSynth

__all__ = ["VivadoImpl"]

log = logging.getLogger(__name__)


class VivadoImpl(VivadoImplementation):
    """Place and route a netlist with AMD-Xilinx Vivado, in non-project mode.

    The `netlist` input is an `Edif` design source or, by default, the EDIF netlist that
    `yosys_fpga` writes for a flat Xilinx synthesis. So `xeda run yosys_fpga+vivado_impl
    design.yaml` implements the open-source synthesis with Vivado. Vivado does no synthesis here.
    A generated TCL script reads the constraints and the netlist, links the design, and runs
    `opt_design`, `place_design` and `route_design` on it in memory, with the optimizations that
    `impl` asks for (the default strategy adds `opt_design` and `phys_opt_design` after
    placement). The flow reports timing and utilization. It writes a bitstream when `bitstream`
    is set or a consumer needs it. The results are those of `vivado_synth`.

    The outputs follow `vivado_alt_synth`, which also runs Vivado in non-project mode, and the
    settings of the same names switch them on. `write_checkpoint` writes the routed checkpoint
    `checkpoints/post_route.dcp` (`checkpoint_route`) and the one after placement beside it.
    `write_netlist` writes the functional netlist `outputs/impl_funcsim.v` (`netlist`) and the
    constraints `outputs/impl.xdc` (`xdc_exported`). `write_timing_netlist` writes the timing
    netlist `outputs/impl_timesim.v` (`netlist_timing`) and its slow-corner SDF
    `outputs/impl_timesim.sdf` (`sdf`). `bitstream` writes the bitstream. The flow does no
    synthesis, so it has no `checkpoint_synth`.

    Vivado finds the top module of an EDIF netlist by the name of its file. The flow therefore
    copies the netlist to `<top>.edif` in its run directory, whatever the file was called. For
    that, the design needs `rtl.top`. It also needs `fpga` and a clock, as for any Vivado flow.
    Pin constraints are `Xdc` design sources or `xdc_files`.

    A netlist from another source must be flat and must carry the ranges of its buses. Use
    yosys's `write_edif -pvector bra` after `synth_xilinx -flatten`. The flow reads the netlist
    before it starts Vivado. It stops for a netlist that is not flat, and it names the modules.
    It cannot check the ranges. Without them, Vivado links the design with every bus reversed and
    gives no message.
    """

    # This flow has no project-mode Vivado runs, so it drops the `status` result of
    # `vivado_synth`.
    results_description = {
        key: spec for key, spec in VivadoSynth.results_description.items() if key != "status"
    }

    class Settings(VivadoImplementation.Settings):
        """Vivado implementation settings."""

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

    #: The design sources it can take as its inputs: the netlist and the constraints.
    design_source_types = frozenset({SourceType.Edif, SourceType.Xdc, SourceType.Sdc})

    class Inputs(VivadoImplementation.Inputs):
        netlist: Path = In(
            SourceType.Edif,
            producer="yosys_fpga",
            output="netlist_edif",
            description="The EDIF netlist to implement: a design source or the one from "
            "`yosys_fpga`.",
        )
        constraints: list[Path] = In(
            (SourceType.Xdc, SourceType.Sdc),
            optional=True,
            description="XDC and SDC constraints, in design-source order.",
        )

    @classmethod
    def check_design_supported(cls, design: Design, settings: Flow.Settings | None = None) -> None:
        """Refuse a design with no top module, or with a top module that is no file name.

        Vivado looks up the top of an EDIF netlist by the name of its file (`run`)."""
        super().check_design_supported(design, settings)
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
        """Refuse a device that is not a Xilinx device."""
        super().check_settings_supported(settings)
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
        """Refuse a netlist with a hierarchy, which Vivado would take for black boxes.

        The settings show only some of the ways to a hierarchy
        (`YosysFpga.Settings.edif_problem`). The HDL can ask for one too
        (`(* keep_hierarchy *)`), and a netlist from another source can hold one. The netlist
        shows all of them. The function reads the netlist in chunks, so it does not hold the
        file of a large design in memory. Vivado reports the cause in its log, but Xeda sees
        only its exit status."""
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

        # Vivado finds the top module by the name of the file (`[Project 1-68] No files found
        # to match top module`). The script therefore reads this copy, whatever the netlist was
        # called.
        netlist = self.run_directory.writable(f"{top}.edif")
        replacing_copy(inputs.netlist, netlist)

        bitstream: Optional[Path] = None
        if ss.bitstream is not None:
            # Never write through a link at that name (`RunDirectory.writable`).
            bitstream = self.run_directory.writable(self.run_path / ss.bitstream)
            bitstream.parent.mkdir(parents=True, exist_ok=True)
        self.record_outputs(self.non_project_outputs())

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
