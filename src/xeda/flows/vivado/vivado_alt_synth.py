import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from ...dataclass import Field, field_validator
from ...design import SourceType
from ...flow import Flow, FpgaSynthFlow
from .vivado_synth import (
    CHECKPOINT_SYNTH,
    OUT_OF_CONTEXT,
    RunOptions,
    VivadoSynth,
    _VivadoSynthOutputs,
    constraint_files,
    out_of_context_conflicts,
    refuse_conflicts,
    synth_design_modes,
)

log = logging.getLogger(__name__)

# Options curated from experiments and the Vivado documentation, including
# https://www.xilinx.com/support/documentation/sw_manuals/xilinx2022_1/ug901-vivado-synthesis.pdf
strategies: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {
    "synth": {
        "Debug": {
            "synth": {
                "assert": True,
                "debug_log": True,
                "keep_equivalent_registers": True,
                "no_lc": True,
                "fsm_extraction": "off",
                "directive": "RuntimeOptimized",
            },
            "opt": {"directive": "RuntimeOptimized"},
        },
        "Runtime": {
            "synth": {"directive": "RuntimeOptimized"},
            "opt": {"directive": "RuntimeOptimized"},
        },
        "Default": {
            "synth": {"directive": "Default"},
            "opt": {"directive": "ExploreWithRemap"},
        },
        "Timing": {
            "synth": {
                # "retiming": True,
                "directive": "PerformanceOptimized",
                "fsm_extraction": "one_hot",
                "shreg_min_size": 5,
            },
            "opt": {"directive": "ExploreWithRemap"},
        },
        "ExtraTiming": {
            "synth": {
                "retiming": True,
                "directive": "PerformanceOptimized",
                "fsm_extraction": "one_hot",
                "resource_sharing": "off",
                "shreg_min_size": "10",
                "keep_equivalent_registers": True,
            },
            "opt": {"directive": "ExploreWithRemap"},
        },
        "ExtraTimingAlt": {
            "synth": {
                "retiming": True,
                "directive": "PerformanceOptimized",
                "fsm_extraction": "one_hot",
                "resource_sharing": "off",
                "shreg_min_size": 5,
                "no_lc": True,  # turns off LUT combining
                "keep_equivalent_registers": True,
            },
            "opt": {"directive": "ExploreWithRemap"},
        },
        "Area": {
            # In Vivado 2020.1, AreaOptimized_medium and AreaOptimized_high print the error message
            # "unexpected non-zero reference counts" but succeed, and the post-implementation
            # simulation passes.
            "synth": {
                "control_set_opt_threshold": 1,
                "shreg_min_size": 3,
                "resource_sharing": "auto",
                "directive": "AreaOptimized_medium",
            },
            "opt": {"directive": "ExploreArea"},
        },
        "AreaHigh": {
            # See the comment on "Area" about the AreaOptimized directives.
            "synth": {
                "control_set_opt_threshold": 1,
                "shreg_min_size": 3,
                "resource_sharing": "on",
                "directive": "AreaOptimized_high",
            },
            "opt": {"directive": "ExploreArea"},
        },
        "AreaPower": {
            # See the comment on "Area" about the AreaOptimized directives.
            "synth": {
                "control_set_opt_threshold": "1",
                "shreg_min_size": "3",
                "resource_sharing": "auto",
                "gated_clock_conversion": "auto",
                "directive": "AreaOptimized_medium",
            },
            "opt": {"directive": "ExploreArea"},
        },
        "AreaTiming": {
            "synth": {"retiming": None},
            "opt": {"directive": "ExploreWithRemap"},
        },
        "AreaExploreWithRemap": {
            "synth": {"retiming": None},
            "opt": {"directive": "ExploreWithRemap"},
        },
        "AreaExploreWithRemap2": {
            "synth": {"retiming": None},
            "opt": {"directive": "ExploreArea"},
        },
        "AreaExplore": {
            "synth": {},
            "opt": {"directive": "ExploreArea"},
        },
        "Power": {
            "synth": {
                "gated_clock_conversion": "auto",
                "control_set_opt_threshold": "1",
                "shreg_min_size": "3",
                "resource_sharing": "auto",
            },
            "opt": {"directive": "ExploreSequentialArea"},
            # "power_opt": {},
        },
    },
    # See https://www.xilinx.com/support/documentation/sw_manuals/xilinx2020_1/ug904-vivado-implementation.pdf
    "impl": {
        "Debug": {
            "place": {"directive": "RuntimeOptimized"},
            "place_opt": {},
            "route": {"directive": "RuntimeOptimized"},
            "phys_opt": {"directive": "RuntimeOptimized"},
        },
        "Runtime": {
            "place": {"directive": "RuntimeOptimized"},
            "place_opt": {},
            "route": {"directive": "RuntimeOptimized"},
            "phys_opt": {"directive": "RuntimeOptimized"},
        },
        "Default": {
            "place": {"directive": "Default"},
            "place_opt": {},
            "route": {"directive": "Default"},
            "phys_opt": {"directive": "Default"},
        },
        "Timing": {
            "place": {"directive": "ExtraPostPlacementOpt"},
            "place_opt": {
                "retarget": True,
                "propconst": True,
                "sweep": True,
                "aggressive_remap": True,
                "shift_register_opt": True,
            },
            "phys_opt": {"directive": "AggressiveExplore"},
            "place_opt2": {"directive": "Explore"},
            "route": {"directive": "AggressiveExplore"},
        },
        "ExtraTimingCongestion": {
            "place": {"directive": "AltSpreadLogic_high"},
            "place_opt": {
                "retarget": True,
                "propconst": True,
                "sweep": True,
                "remap": True,
                "muxf_remap": True,
                "aggressive_remap": True,
                "shift_register_opt": True,
            },
            "place_opt2": {"directive": "Explore"},
            "phys_opt": {"directive": "AggressiveExplore"},
            "route": {"directive": "AlternateCLBRouting"},
        },
        "ExtraTiming": {
            "place": {"directive": "ExtraTimingOpt"},
            "place_opt": {
                "retarget": True,
                "propconst": True,
                "sweep": True,
                "muxf_remap": True,
                "aggressive_remap": True,
                "shift_register_opt": True,
            },
            "place_opt2": {"directive": "Explore"},
            "phys_opt": {"directive": "AggressiveExplore"},
            "route": {"directive": "NoTimingRelaxation"},
        },
        "TimingAutoPlace1": {
            "place": {
                # Only available since Vivado 2022.1.
                "directive": "Auto_1",
                "timing_summary": True,
            },
            "place_opt": {
                "propconst": True,
                "sweep": True,
                "muxf_remap": True,
                "aggressive_remap": True,
                "shift_register_opt": True,
            },
            "place_opt2": {"directive": "Explore"},
            "phys_opt": {"directive": "AggressiveExplore"},
            "route": {"directive": "NoTimingRelaxation"},
        },
        "TimingAutoPlace2": {
            "place": {
                # Only available since Vivado 2022.1.
                "directive": "Auto_2",
                "timing_summary": True,
            },
            "place_opt": {
                "propconst": True,
                "sweep": True,
                "muxf_remap": True,
                "aggressive_remap": True,
                "shift_register_opt": True,
            },
            "place_opt2": {"directive": "Explore"},
            "phys_opt": {"directive": "AggressiveExplore"},
            "route": {"directive": "NoTimingRelaxation"},
        },
        "TimingAutoPlace3": {
            "place": {
                # Only available since Vivado 2022.1.
                "directive": "Auto_3",
                "timing_summary": True,
            },
            "place_opt": {
                "propconst": True,
                "sweep": True,
                "muxf_remap": True,
                "aggressive_remap": True,
                "shift_register_opt": True,
            },
            "place_opt2": {"directive": "Explore"},
            "phys_opt": {"directive": "AggressiveExplore"},
            "route": {"directive": "NoTimingRelaxation"},
        },
        "ExtraTimingAltRouting": {
            "place": {"directive": "ExtraTimingOpt"},
            "place_opt": {
                "retarget": True,
                "propconst": True,
                "sweep": True,
                "aggressive_remap": True,
                "shift_register_opt": True,
            },
            "phys_opt": {"directive": "AggressiveExplore"},
            "route": {"directive": "AlternateCLBRouting"},
        },
        "AreaPower": {
            "place": {"directive": "Default"},
            "place_opt": {
                "retarget": True,
                "propconst": True,
                "sweep": True,
                "aggressive_remap": True,
                "shift_register_opt": True,
                "dsp_register_opt": True,
                "resynth_seq_area": True,
                "merge_equivalent_drivers": True,
            },
            "place_opt2": {"directive": "ExploreArea"},
            # TODO: verify with a post-implementation timing simulation.
            "phys_opt": {"directive": "AggressiveExplore"},
            "route": {"directive": "Explore"},
        },
        "AreaTiming": {
            "place": {"directive": "ExtraPostPlacementOpt"},
            "place_opt": {
                "retarget": True,
                "propconst": True,
                "sweep": True,
                "aggressive_remap": True,
                "shift_register_opt": True,
                "dsp_register_opt": True,
                "resynth_seq_area": True,
                "merge_equivalent_drivers": True,
            },
            "place_opt2": {"directive": "ExploreArea"},
            "phys_opt": {"directive": "AggressiveExplore"},
            "route": {"directive": "Explore"},
            "post_route_phys_opt": [
                "-placement_opt",
                "-routing_opt",
                "-retime",
                "-critical_cell_opt",
                "-slr_crossing_opt",
                "-hold_fix",
                "-rewire",
            ],
        },
        "AreaExplore": {
            "place": {"directive": "Default"},
            "place_opt": {"directive": "ExploreArea"},
            "phys_opt": {"directive": "Explore"},
            "route": {"directive": "Explore"},
        },
        "AreaExploreWithRemap": {
            "place": {"directive": "Default"},
            "place_opt": {"directive": "ExploreWithRemap"},
            "phys_opt": {"directive": "Explore"},
            "route": {"directive": "Explore"},
        },
        "Power": {
            "place": {"directive": "Default"},
            "place_opt": {"directive": "ExploreSequentialArea"},
            # "power_opt": {},
            "phys_opt": {"directive": "Explore"},
            "route": {"directive": "Explore"},
        },
    },
}


#: The steps of each run of the non-project Tcl scripts, in the order the script runs them. A step
#: that neither a strategy nor the user gives is `None`, and the script leaves it out.
RUN_STEPS = {
    "synth": ["synth", "opt", "power_opt"],
    "impl": [
        "place",
        "power_opt",
        "place_opt",
        "place_opt2",
        "phys_opt",  # post-placement
        "route",
        "post_route_phys_opt",
    ],
}


def expand_run_options(run: str, value: RunOptions) -> RunOptions:
    """Return the options of a run (`synth` or `impl`) with the steps of its strategy added.

    The steps that the user gave win over the steps of the strategy. Every step of the run gets an
    entry, `None` if neither gave it.

    The result is a copy. This function runs in a `mode="after"` validator, so `value` may be the
    caller's own `RunOptions` instance, and expanding the strategy in place would grow the
    caller's `steps` mapping. The function also copies the steps of the strategy. They belong to
    the module, and the caller owns the result. (The model happens to copy a mapping that it
    validates, but this function does not depend on that.)
    """
    value = value.model_copy(deep=True)
    if value.strategy:
        strategy_steps = strategies[run].get(value.strategy)
        if strategy_steps is None:
            raise ValueError(f"Unknown strategy: {value.strategy}")
        value.steps = {
            **deepcopy(strategy_steps),
            **value.steps,
        }
    for step in RUN_STEPS[run]:
        if step not in value.steps:
            value.steps[step] = None
    return value


#: The key of the settings that holds the options of `synth_design` in `vivado_alt_synth`.
SYNTH_STEP_KEY = "synth.steps.synth"


def synth_step_options(settings: Any) -> Dict[str, Any]:
    """Return a copy of the `synth` step of the settings as a mapping of options.

    A list of options becomes a mapping from each item to `None`."""
    steps = settings.synth.steps.get("synth")
    return {step: None for step in steps} if isinstance(steps, list) else deepcopy(steps or {})


def synth_design_options(settings: Any) -> Dict[str, Any]:
    """Return the options of `synth_design`.

    They are the `synth` step of the settings and what the settings derive (`-mode out_of_context`
    and `-flatten_hierarchy`). The result is a copy, computed when the script is rendered, so
    neither the settings nor the strategies of the module change."""
    options = synth_step_options(settings)
    if settings.out_of_context:
        # repeat the planning check, because a flow built directly is not planned
        text = flatten_options(options)
        refuse_conflicts(type(settings), out_of_context_conflicts(SYNTH_STEP_KEY, text))
        if not synth_design_modes(text):
            options["mode"] = OUT_OF_CONTEXT
    if settings.flatten_hierarchy:
        options["flatten_hierarchy"] = settings.flatten_hierarchy
    return options


def alt_mode_conflicts(settings: Any) -> List[Tuple[str, str]]:
    """Return the options of the `synth` step that give `synth_design` a mode besides
    `out_of_context`."""
    if not settings.out_of_context:
        return []
    text = flatten_options(synth_step_options(settings))
    return out_of_context_conflicts(SYNTH_STEP_KEY, text)


def flatten_options(d) -> str:
    if d is None:
        return ""
    if isinstance(d, (list, tuple)):
        return " ".join(flatten_options(s) for s in d)
    if isinstance(d, dict):
        return " ".join(
            [
                (
                    f"-{k} {flatten_options(v)}"
                    if v is not None and not isinstance(v, bool)
                    else f"-{k}"
                )
                for k, v in d.items()
                if v is not False
            ]
        )
    return str(d)


class VivadoAltSynth(VivadoSynth, FpgaSynthFlow):
    """Synthesize and implement a design for an FPGA with Vivado in non-project mode.

    A generated Tcl script reads the design and runs the steps from `synth_design` through
    `route_design` in memory. Each step gets the options of its `synth` or `impl` strategy and
    steps. The script reports utilization and timing. `vivado_synth` does the same in project
    mode.

    Settings switch the outputs on as in `vivado_synth`. This flow writes one SDF corner (the
    slow one), so it declares no `sdf_min`. `write_netlist` writes the functional netlist
    `impl_funcsim.v` (`netlist`) and the constraints `impl.xdc` (`xdc_exported`).
    `write_timing_netlist` writes the timing netlist `impl_timesim.v` (`netlist_timing`) and
    `impl_timesim.sdf` (`sdf`). `write_checkpoint` writes the three checkpoints
    (`checkpoint_synth`, `checkpoint_place`, `checkpoint_route`). `bitstream` writes the
    bitstream.
    """

    # Only project mode has Vivado runs, so there is no run `status`.
    results_description = {
        key: spec for key, spec in VivadoSynth.results_description.items() if key != "status"
    }

    reads_sources = frozenset(
        {
            SourceType.Verilog,
            SourceType.SystemVerilog,
            SourceType.Vhdl,
            SourceType.VerilogHeader,
            SourceType.SVHeader,
            SourceType.Xdc,
            SourceType.Sdc,
        }
    )

    class Outputs(_VivadoSynthOutputs):
        """The declared outputs, with one SDF corner and no `sdf_min` (which `VivadoSynth.Outputs`
        adds)."""

    class Settings(VivadoSynth.Settings):
        synth: RunOptions = Field(
            RunOptions(strategy="Default"),
            description="Strategy and per-step options of the synthesis part of the Tcl script.",
        )
        impl: RunOptions = Field(
            RunOptions(strategy="Default"),
            description="Strategy and per-step options of the implementation part of the Tcl "
            "script.",
        )

        @field_validator("tcl_files")
        @classmethod
        def _no_tcl_files(cls, value):
            """Reject project-only Tcl hooks for non-project synthesis."""
            if value:
                raise ValueError(
                    "vivado_alt_synth runs Vivado in non-project mode, which has no fileset for "
                    "Tcl hooks: `tcl_files` is a setting of vivado_synth"
                )
            return value

        @field_validator("set_synth_properties", "set_impl_properties", "show_available_strategies")
        @classmethod
        def _no_runs(cls, value, info):
            """Reject the settings of a project's runs, because non-project mode has none."""
            if value:
                raise ValueError(
                    "vivado_alt_synth runs Vivado in non-project mode, which has no runs to use "
                    f"`{info.field_name}` on: it is a setting of vivado_synth"
                )
            return value

        @field_validator("synth", "impl")
        @classmethod
        def validate_synth(cls, value, info):
            return expand_run_options(info.field_name, value)

        suppress_msgs: List[str] = Field(
            [
                "Vivado 12-7122",  # Auto Incremental Compile: No reference checkpoint was found
                "Synth 8-7080",  # "Parallel synthesis criteria is not met"
                "Synth 8-350",  # warning partial connection
                "Synth 8-256",  # info do synthesis
                "Synth 8-638",
                # "Synth 8-3969", # BRAM mapped to LUT due to optimization
                # "Synth 8-4480", # BRAM with no output register
                # "Drc 23-20",  # DSP without input pipelining
                # "Netlist 29-345",  # Update IP version
            ],
            description='Vivado message IDs to suppress, for example "Synth 8-7080". Vivado does '
            "not print suppressed messages, and they never trigger `fail_critical_warning`.",
        )

    @classmethod
    def mode_conflicts(cls, settings: Flow.Settings) -> List[Tuple[str, str]]:
        """Return the mode conflicts of the `synth` step, which holds the `synth_design` options."""
        return alt_mode_conflicts(settings)

    def run(self):
        """Render the non-project Tcl script and run it in Vivado."""
        ss = self.settings
        assert isinstance(ss, self.Settings)

        synth_options = synth_design_options(ss)

        def steps_to_str(steps):
            return "\n " + "\n ".join(
                f"{name}: {flatten_options(step)}" for name, step in steps.items() if step
            )

        log.debug("Synthesis steps:%s", steps_to_str({**ss.synth.steps, "synth": synth_options}))
        log.debug("Implementation steps:%s", steps_to_str(ss.impl.steps))
        self.add_template_filter(
            "flatten_options",
            flatten_options,
        )
        script_path = self.copy_from_template(
            "vivado_alt_synth.tcl",
            xdc_files=constraint_files(self, ss),
            synth_options=synth_options,
        )
        # `vivado_alt_synth.tcl` writes these files whenever their enabling setting is on
        # (`write_checkpoint`, `write_netlist`, `write_timing_netlist`). Record them here,
        # because `VivadoAltSynth` inherits `parse_reports` from `VivadoSynth`, which tracks only
        # `bitstream` and the globs of the reports and logs. Each is also a declared output.
        paths = self.non_project_outputs()
        if ss.write_checkpoint:
            paths[CHECKPOINT_SYNTH] = ss.checkpoints_dir / "post_synth.dcp"
        self.record_outputs(paths)
        self.vivado.run("-source", script_path)
