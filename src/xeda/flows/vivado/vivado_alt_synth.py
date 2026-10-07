import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...dataclass import Field, field_validator
from ...design import SourceType
from ...flow import FpgaSynthFlow
from .vivado_synth import (
    CHECKPOINT_PLACE,
    CHECKPOINT_ROUTE,
    CHECKPOINT_SYNTH,
    NETLIST,
    NETLIST_TIMING,
    SDF,
    XDC_EXPORTED,
    RunOptions,
    StepsValType,
    VivadoSynth,
    _VivadoSynthOutputs,
    constraint_files,
    declare_outputs,
)

log = logging.getLogger(__name__)

# curated options based on experiments and Vivado documentations
# and https://www.xilinx.com/support/documentation/sw_manuals/xilinx2022_1/ug901-vivado-synthesis.pdf
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
            # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeds and post-impl sim is OK too
            "synth": {
                "control_set_opt_threshold": 1,
                "shreg_min_size": 3,
                "resource_sharing": "auto",
                "directive": "AreaOptimized_medium",
            },
            "opt": {"directive": "ExploreArea"},
        },
        "AreaHigh": {
            # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts",
            # but succeeds and post-impl sim is OK too!
            "synth": {
                "control_set_opt_threshold": 1,
                "shreg_min_size": 3,
                "resource_sharing": "on",
                "directive": "AreaOptimized_high",
            },
            "opt": {"directive": "ExploreArea"},
        },
        "AreaPower": {
            # see the comment above for AreaOptimized_medium directive
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
    # see https://www.xilinx.com/support/documentation/sw_manuals/xilinx2020_1/ug904-vivado-implementation.pdf
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
                # Only available since Vivado 2022.1
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
                # Only available since Vivado 2022.1
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
                # Only available since Vivado 2022.1
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
            # TODO: verify through post-impl timing simulation
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


#: The steps of each run of the non-project TCL scripts, in the order the script runs them. A step
#: a strategy or the user does not give is `None`: the script leaves it out.
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
    """The options of the `run` (`synth` or `impl`) of a non-project TCL script, with the steps of
    its strategy under the ones the user gave, and every step of the run named.

    A copy: this runs in a `mode="after"` validator, so `value` may be the caller's own
    `RunOptions` instance, and expanding the strategy in place would grow the caller's `steps`
    mapping.
    """
    value = value.model_copy(deep=True)
    if value.strategy:
        strategy_steps = strategies[run].get(value.strategy)
        if strategy_steps is None:
            raise ValueError(f"Unknown strategy: {value.strategy}")
        value.steps = {
            **strategy_steps,
            **value.steps,
        }
    for step in RUN_STEPS[run]:
        if step not in value.steps:
            value.steps[step] = None
    return value


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
    """FPGA synthesis and implementation with AMD-Xilinx Vivado, in non-project mode.

    A generated TCL script reads the design and runs synth_design through route_design on it in
    memory, each step with the options its `synth`/`impl` strategy and steps give it, and reports
    utilization and timing. See `vivado_synth` for the same in project mode.

    The outputs are switched as in `vivado_synth`, but this flow writes one SDF corner (the slow
    one) and so declares no `sdf_min`: `write_netlist` writes the functional netlist
    `impl_funcsim.v` (`netlist`) and the constraints `impl.xdc` (`xdc_exported`);
    `write_timing_netlist` writes the timing netlist `impl_timesim.v` (`netlist_timing`) and
    `impl_timesim.sdf` (`sdf`); `write_checkpoint` the three checkpoints (`checkpoint_synth`,
    `checkpoint_place`, `checkpoint_route`); `bitstream` the bitstream.
    """

    # Vivado's runs, whose status `vivado_synth` reports, are project mode's
    results_description = {
        key: text for key, text in VivadoSynth.results_description.items() if key != "status"
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
        """One SDF corner: no `sdf_min`, which `VivadoSynth.Outputs` adds."""

    class Settings(VivadoSynth.Settings):
        synth: RunOptions = Field(
            RunOptions(strategy="Default"),
            description="Synthesis run options for the alternative TCL flow.",
        )
        impl: RunOptions = Field(
            RunOptions(strategy="Default"),
            description="Implementation run options for the alternative TCL flow.",
        )

        @field_validator("tcl_files")
        @classmethod
        def _no_tcl_files(cls, value):
            """Reject project-only Tcl hooks for non-project synthesis."""
            if value:
                raise ValueError(
                    "vivado_alt_synth runs Vivado in non-project mode, which has no fileset for "
                    "TCL hooks to be added to: `tcl_files` is a setting of vivado_synth"
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
            description='Vivado message IDs to suppress, e.g. "Synth 8-7080". Suppressed '
            "messages are not printed and never trigger `fail_critical_warning`.",
        )

    def run(self):
        """Render and execute the non-project Vivado synthesis steps."""
        ss = self.settings
        assert isinstance(ss, self.Settings)

        synth_steps: Optional[StepsValType] = ss.synth.steps.get("synth")
        if synth_steps is None:
            synth_steps = {}
        if isinstance(synth_steps, list):
            synth_steps = {s: None for s in synth_steps}
        assert isinstance(synth_steps, dict), f"synth_steps: {synth_steps} is not a dict"

        if ss.out_of_context:
            if "synth" in ss.synth.steps and ss.synth.steps["synth"] is not None:
                if isinstance(ss.synth.steps["synth"], dict):
                    ss.synth.steps["synth"]["mode"] = "out_of_context"
                else:
                    ss.synth.steps["synth"].append("-mode out_of_context")

            # always need a synth step?
            ss.synth.steps["synth"] = synth_steps
        if ss.flatten_hierarchy:
            synth_steps["flatten_hierarchy"] = ss.flatten_hierarchy
        ss.synth.steps["synth"] = synth_steps

        def steps_to_str(steps):
            return "\n " + "\n ".join(
                f"{name}: {flatten_options(step)}" for name, step in steps.items() if step
            )

        log.debug("Synthesis steps:%s", steps_to_str(ss.synth.steps))
        log.debug("Implementation steps:%s", steps_to_str(ss.impl.steps))
        self.add_template_filter(
            "flatten_options",
            flatten_options,
        )
        script_path = self.copy_from_template(
            "vivado_alt_synth.tcl",
            xdc_files=constraint_files(self, ss),
        )
        # These are written by `vivado_alt_synth.tcl` whenever the setting that enables them is
        # on (`write_checkpoint`/`write_netlist`/`write_timing_netlist`); record them here, since
        # `VivadoAltSynth` does not override `parse_reports` (it inherits `VivadoSynth`'s, which
        # only tracks `bitstream` and the reports/log globs). Each is also a declared output.
        paths: Dict[str, Path] = {}
        if ss.write_checkpoint:
            paths[CHECKPOINT_SYNTH] = ss.checkpoints_dir / "post_synth.dcp"
            paths[CHECKPOINT_PLACE] = ss.checkpoints_dir / "post_place.dcp"
            paths[CHECKPOINT_ROUTE] = ss.checkpoints_dir / "post_route.dcp"
        if ss.write_netlist:
            paths[NETLIST] = ss.outputs_dir / "impl_funcsim.v"
            paths[XDC_EXPORTED] = ss.outputs_dir / "impl.xdc"
        if ss.write_timing_netlist:
            paths[NETLIST_TIMING] = ss.outputs_dir / "impl_timesim.v"
            paths[SDF] = ss.outputs_dir / "impl_timesim.sdf"
        self.artifacts.update(paths)
        declare_outputs(
            self,
            {
                "netlist": paths.get(NETLIST),
                "netlist_timing": paths.get(NETLIST_TIMING),
                "sdf": paths.get(SDF),
                "checkpoint_synth": paths.get(CHECKPOINT_SYNTH),
                "checkpoint_route": paths.get(CHECKPOINT_ROUTE),
                "bitstream": ss.bitstream,
            },
        )
        self.vivado.run("-source", script_path)
