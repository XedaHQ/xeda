import logging
from typing import Any, Dict, List, Optional

from ...dataclass import validator
from ...flow import FpgaSynthFlow
from .vivado_synth import RunOptions, VivadoSynth

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
        "AreaExploreWithRemap": {
            "place": {"directive": "Default"},
            "place_opt": {"directive": "ExploreWithRemap"},
            "phys_opt": {"directive": "Explore"},
            "route": {"directive": "Explore"},
        },
        "AreaExploreWithRemap2": {
            "place": {"directive": "Default"},
            "place_opt": {"directive": "ExploreWithRemap"},
            "phys_opt": {"directive": "Explore"},
            "route": {"directive": "Explore"},
        },
        "AreaExplore": {
            "place": {"directive": "Default"},
            "place_opt": {"directive": "ExploreArea"},
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


def flatten_options(d) -> str:
    if d is None:
        return ""
    if isinstance(d, (list, tuple)):
        return " ".join(flatten_options(s) for s in d)
    if isinstance(d, dict):
        return " ".join(
            [
                f"-{k} {flatten_options(v)}"
                if v is not None and not isinstance(v, bool)
                else f"-{k}"
                for k, v in d.items()
                if v is not False
            ]
        )
    return str(d)


class VivadoAltSynth(VivadoSynth, FpgaSynthFlow):
    """Synthesize with Xilinx Vivado using an alternative TCL-based flow"""

    class Settings(VivadoSynth.Settings):
        synth: RunOptions = RunOptions(strategy="Default")
        impl: RunOptions = RunOptions(strategy="Default")
        out_of_context: bool = False

        @validator("synth", "impl", always=True)
        def validate_synth(cls, value, values, field):
            if value.strategy:
                strategy_steps = strategies[field.name].get(value.strategy)
                if strategy_steps is None:
                    raise ValueError(f"Unknown strategy: {value.strategy}")
                value.steps = {
                    **strategy_steps,
                    **value.steps,
                }
            if field.name == "synth":
                steps = ["synth", "opt", "power_opt"]
            else:
                steps = [
                    "place",
                    "power_opt",
                    "place_opt",
                    "place_opt2",
                    "phys_opt",  # post-placement
                    "route",
                    "post_route_phys_opt",
                ]
            for step in steps:
                if step not in value.steps:
                    value.steps[step] = None
            return value

        suppress_msgs: List[str] = [
            "Vivado 12-7122",  # Auto Incremental Compile:: No reference checkpoint was found in run
            "Synth 8-7080",  # "Parallel synthesis criteria is not met"
            "Synth 8-350",  # warning partial connection
            "Synth 8-256",  # info do synthesis
            "Synth 8-638",
            # "Synth 8-3969", # BRAM mapped to LUT due to optimization
            # "Synth 8-4480", # BRAM with no output register
            # "Drc 23-20",  # DSP without input pipelining
            # "Netlist 29-345",  # Update IP version
        ]

    def run(self):
        ss = self.settings
        assert isinstance(ss, self.Settings)

        synth_steps = ss.synth.steps.get("synth")
        if synth_steps is None:
            synth_steps = {}
        if not isinstance(synth_steps, dict):
            synth_steps = {s: None for s in synth_steps}

        if ss.out_of_context:
            if "synth" in ss.synth.steps and ss.synth.steps["synth"] is not None:
                if isinstance(ss.synth.steps["synth"], dict):
                    ss.synth.steps["synth"]["mode"] = "out_of_context"
                else:
                    ss.synth.steps["synth"].append("-mode out_of_context")

            # always need a synth step?
            ss.synth.steps["synth"] = synth_steps
        if any(x in ss.blacklisted_resources for x in ("bram_tile", "bram")):
            # FIXME also add "max_uram 0", only for UltraScale+ devices
            synth_steps["max_bram"] = 0
        if "dsp" in ss.blacklisted_resources:
            synth_steps["max_dsp"] = 0
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
        clock_xdc_path = self.copy_from_template("clock.xdc")
        script_path = self.copy_from_template(
            "vivado_alt_synth.tcl",
            xdc_files=[clock_xdc_path],
        )

        self.vivado.run("-source", script_path)
