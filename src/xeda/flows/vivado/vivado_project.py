import logging

from ...dataclass import Field, input_names
from ...design import SourceType
from ...flow import Flow, FpgaSynthFlow
from ..vivado import Vivado
from ..vivado.vivado_sim import VivadoSim
from ..vivado.vivado_synth import (
    VivadoSynth,
    constraint_files,
    post_step_hooks,
    property_mode_conflicts,
    refuse_conflicts,
    run_steps,
    step_mode_conflicts,
    tcl_property_value,
    vivado_synth_generics,
)

log = logging.getLogger(__name__)


def _in_vivado_sim(replacement: str) -> str:
    """Return the replacement of a setting that `vivado_sim` removed, as it reads for a project:
    a project has no simulation, so a setting of the simulator is named in `flows.vivado_sim`."""
    field = replacement.strip("`")
    if replacement == f"`{field}`" and field in VivadoSim.Settings.model_fields:
        return f"`flows.vivado_sim.{field}`"
    return replacement


class VivadoProject(Vivado, FpgaSynthFlow):
    """Create an AMD-Xilinx Vivado project for the design, to work on in Vivado.

    The project holds the design's sources and testbench, its constraints, the synthesis and
    implementation strategies and step settings, and the hooks that write the reports of
    `vivado_synth` after each step. The flow runs no synthesis or implementation. Open the
    project (`artifacts.project`) in Vivado, or set `gui` to let the flow open it. For synthesis
    and implementation in batch, use `vivado_synth`.
    """

    # Creating a project reports nothing beyond the keys every flow reports.
    results_description = {}

    #: The project's simulation fileset holds the testbench, so the flow reads `tb`.
    design_parts = frozenset({"rtl", "tb"})
    reads_sources = frozenset(
        {
            SourceType.Verilog,
            SourceType.SystemVerilog,
            SourceType.Vhdl,
            SourceType.VerilogHeader,
            SourceType.SVHeader,
            SourceType.MemoryFile,
            SourceType.Xdc,
            SourceType.Sdc,
            SourceType.Tcl,
        }
    )

    class Settings(VivadoSynth.Settings):
        """Settings for a Vivado project: synthesis and implementation."""

        # A project runs no simulation. The simulation settings are refused, naming `vivado_sim`.
        removed_settings = {
            **VivadoSynth.Settings.removed_settings,
            **{
                spelling: f"`flows.vivado_sim.{field}`"
                for spelling, field in input_names(VivadoSim.Settings).items()
                if field not in VivadoSynth.Settings.model_fields
            },
            **{
                spelling: _in_vivado_sim(text)
                for spelling, text in VivadoSim.Settings.removed_setting_replacements().items()
                if spelling not in VivadoSynth.Settings.model_fields
            },
        }

        gui: bool = Field(
            False,
            description="Open the created project in the Vivado GUI, which needs a display.",
        )

    @classmethod
    def check_settings_supported(cls, settings: Flow.Settings) -> None:
        """Refuse a second `-mode` of `synth_design` beside `out_of_context` before the run."""
        super().check_settings_supported(settings)
        assert isinstance(settings, cls.Settings)
        refuse_conflicts(
            cls.Settings, step_mode_conflicts(settings) + property_mode_conflicts(settings)
        )

    def init(self):
        """Set up the Vivado tool, in GUI mode when `gui` is set."""
        super().init()
        assert isinstance(self.settings, self.Settings)
        self.add_template_global_func(tcl_property_value)
        if self.settings.gui:
            # `vivado -mode gui -source ...` runs the script in the GUI and leaves the project
            # open there, as the `gui` setting of the `dc` flow does with `dc_shell -gui`.
            args = list(self.vivado.default_args)
            args[args.index("-mode") + 1] = "gui"
            self.vivado.default_args = args

    def run(self):
        """Create the Vivado project and configure its synthesis and implementation runs."""
        assert isinstance(self.settings, self.Settings)
        settings = self.settings
        self.artifacts.project = f"{self.design.name}.xpr"

        synth_steps, impl_steps = run_steps(settings, out_of_context=settings.out_of_context)
        # Repeat the check of planning, because a flow built directly is not planned.
        refuse_conflicts(type(settings), property_mode_conflicts(settings))
        # The hooks write the reports after each major step, as `vivado_synth` does.
        tcl_files = [self.process_path(p, subs_vars=True) for p in settings.tcl_files]
        tcl_files += post_step_hooks(self, settings, synth_steps, impl_steps)

        script_path = self.copy_from_template(
            "vivado_project.tcl",
            # Constraints go to the constraint fileset, as `vivado_synth` reads them.
            sources=[
                src
                for src in self.sources_read()
                if src.type not in (SourceType.Xdc, SourceType.Sdc)
            ],
            xdc_files=constraint_files(self, settings),
            tcl_files=tcl_files,
            synth_steps=synth_steps,
            impl_steps=impl_steps,
            generics=" ".join(vivado_synth_generics(self.design.rtl.parameters)),
        )
        self.vivado.run("-source", script_path)

    def parse_reports(self) -> bool:
        """Check that this run created the Vivado project file.

        A project file that a previous run left does not count (`Flow.report_file`)."""
        return self.report_file(self.run_path / f"{self.design.name}.xpr") is not None
