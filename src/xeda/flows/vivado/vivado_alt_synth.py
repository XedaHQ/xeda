import logging
from typing import Any, Dict, List

from ...dataclass import validator
from ..flow import FpgaSynthFlow
from .vivado_synth import RunOptions, VivadoSynth

log = logging.getLogger(__name__)

# curated options based on experiments and:
# see https://www.xilinx.com/support/documentation/sw_manuals/xilinx2020_1/ug904-vivado-implementation.pdf
# and https://www.xilinx.com/support/documentation/sw_manuals/xilinx2020_1/ug901-vivado-synthesis.pdf
xeda_strategies: Dict[str, Dict[str, Any]] = {
    "Debug": {
        "synth": {
            "-assert": None,
            "-debug_log": None,
            "-flatten_hierarchy": "none",
            "-keep_equivalent_registers": None,
            "-no_lc": None,
            "-fsm_extraction": "off",
            "-directive": "RuntimeOptimized",
        },
        "opt": {"-directive": "RuntimeOptimized"},
        "place": {"-directive": "RuntimeOptimized"},
        "place_opt": {},
        "route": {"-directive": "RuntimeOptimized"},
        "phys_opt": {"-directive": "RuntimeOptimized"},
    },
    "Runtime": {
        "synth": {"-directive": "RuntimeOptimized"},
        "opt": {"-directive": "RuntimeOptimized"},
        "place": {"-directive": "RuntimeOptimized"},
        "place_opt": {},
        # with -ultrathreads results are not reproducible!
        # OR "-no_timing_driven -ultrathreads",
        "route": {"-directive": "RuntimeOptimized"},
        "phys_opt": {"-directive": "RuntimeOptimized"},
    },
    "Default": {
        "synth": {"-flatten_hierarchy": "rebuilt", "-directive": "Default"},
        "opt": {"-directive": "ExploreWithRemap"},
        "place": {"-directive": "Default"},
        "place_opt": {},
        "route": {"-directive": "Default"},
        "phys_opt": {"-directive": "Default"},
    },
    "Timing": {
        # -mode: default, out_of_context
        # -flatten_hierarchy: rebuilt, full; equivalent in terms of QoR?
        # -no_lc: When checked, this option turns off LUT combining
        # -keep_equivalent_registers -no_lc
        "synth": {
            "-flatten_hierarchy": "rebuilt",
            "-retiming": None,
            "-directive": "PerformanceOptimized",
            "-fsm_extraction": "one_hot",
            #   "-resource_sharing off",
            #   "-no_lc",
            "-shreg_min_size": "5",
            #   "-keep_equivalent_registers "
        },
        "opt": {"-directive": "ExploreWithRemap"},
        "place": {"-directive": "ExtraPostPlacementOpt"},
        "place_opt": {
            "-retarget": None,
            "-propconst": None,
            "-sweep": None,
            "-aggressive_remap": None,
            "-shift_register_opt": None,
        },
        "phys_opt": {"-directive": "AggressiveExplore"},
        "place_opt2": {"-directive": "Explore"},
        # "route": "-directive NoTimingRelaxation",
        "route": {"-directive": "AggressiveExplore"},
    },
    "ExtraTimingCongestion": {
        "synth": {
            "-flatten_hierarchy": "full",
            "-retiming": None,
            "-directive": "PerformanceOptimized",
            "-fsm_extraction": "one_hot",
            "-resource_sharing": "off",
            "-shreg_min_size": "10",
            "-keep_equivalent_registers": None,
        },
        "opt": {"-directive": "ExploreWithRemap"},
        "place": {"-directive": "AltSpreadLogic_high"},
        "place_opt": {
            "-retarget": None,
            "-propconst": None,
            "-sweep": None,
            "-remap": None,
            "-muxf_remap": None,
            "-aggressive_remap": None,
            "-shift_register_opt": None,
        },
        "place_opt2": {"-directive": "Explore"},
        "phys_opt": {"-directive": "AggressiveExplore"},
        "route": {"-directive": "AlternateCLBRouting"},
    },
    "ExtraTiming": {
        "synth": {
            "-flatten_hierarchy": "full",
            "-retiming": None,
            "-directive": "PerformanceOptimized",
            "-fsm_extraction": "one_hot",
            "-resource_sharing": "off",
            #   "-no_lc",
            "-shreg_min_size": "10",
            "-keep_equivalent_registers": None,
        },
        "opt": {"-directive": "ExploreWithRemap"},
        "place": {"-directive": "ExtraTimingOpt"},
        "place_opt": {
            "-retarget": None,
            "-propconst": None,
            "-sweep": None,
            "-muxf_remap": None,
            "-aggressive_remap": None,
            "-shift_register_opt": None,
        },
        "place_opt2": {"-directive": "Explore"},
        "phys_opt": {"-directive": "AggressiveExplore"},
        "route": {"-directive": "NoTimingRelaxation"},
    },
    "ExtraTimingAltRouting": {
        # -mode: default, out_of_context
        # -flatten_hierarchy: rebuilt, full; equivalent in terms of QoR?
        # -no_lc: When checked, this option turns off LUT combining
        # -keep_equivalent_registers -no_lc
        "synth": {
            "-flatten_hierarchy": "full",
            "-retiming": None,
            "-directive": "PerformanceOptimized",
            "-fsm_extraction": "one_hot",
            #   "-resource_sharing off",
            #   "-no_lc",
            "-shreg_min_size": "5",
            "-keep_equivalent_registers": None,
        },
        "opt": {"-directive": "ExploreWithRemap"},
        "place": {"-directive": "ExtraTimingOpt"},
        "place_opt": {
            "-retarget": None,
            "-propconst": None,
            "-sweep": None,
            "-aggressive_remap": None,
            "-shift_register_opt": None,
        },
        "phys_opt": {"-directive": "AggressiveExplore"},
        # "route": "-directive NoTimingRelaxation",
        "route": {"-directive": "AlternateCLBRouting"},
    },
    "Area": {
        # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
        "synth": {
            "-flatten_hierarchy": "full",
            "-control_set_opt_threshold": "1",
            "-shreg_min_size": "3",
            "-resource_sharing": "auto",
            "-directive": "AreaOptimized_medium",
        },
        # if no directive: -resynth_seq_area
        "opt": {"-directive": "ExploreArea"},
        "place": {"-directive": "Default"},
        "place_opt": {"-directive": "ExploreArea"},
        # "place_opt": {'-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
        #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'},
        # if no directive: -placement_opt
        "phys_opt": {"-directive": "Explore"},
        "route": {"-directive": "Explore"},
    },
    "AreaHigh": {
        # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
        "synth": {
            "-flatten_hierarchy": "full",
            "-control_set_opt_threshold": "1",
            "-shreg_min_size": "3",
            "-resource_sharvng": "on",
            "-directive": "AreaOptimized_high",
        },
        # if no directive: -resynth_seq_area
        "opt": {"-directive": "ExploreArea"},
        "place": {"-directive": "Default"},
        "place_opt": {"-directive": "ExploreArea"},
        # "place_opt": {'-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
        #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'},
        # if no directive: -placement_opt
        "phys_opt": {"-directive": "Explore"},
        "route": {"-directive": "Explore"},
    },
    "AreaPower": {
        # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
        "synth": {
            "-flatten_hierarchy": "full",
            "-control_set_opt_threshold": "1",
            "-shreg_min_size": "3",
            "-resource_sharing": "auto",
            "-gated_clock_conversion": "auto",
            "-directive": "AreaOptimized_medium",
        },
        "opt": {"-directive": "ExploreArea"},
        "place": {"-directive": "Default"},
        "place_opt": {
            "-retarget": None,
            "-propconst": None,
            "-sweep": None,
            "-aggressive_remap": None,
            "-shift_register_opt": None,
            "-dsp_register_opt": None,
            "-resynth_seq_area": None,
            "-merge_equivalent_drivers": None,
        },
        "place_opt2": {"-directive": "ExploreArea"},
        # FIXME!!! This is the only option that results in correct post-impl timing sim! Why??!
        "phys_opt": {"-directive": "AggressiveExplore"},
        "route": {"-directive": "Explore"},
    },
    "AreaTiming": {
        "synth": {"-flatten_hierarchy": "rebuilt", "-retiming": None},
        # if no directive: -resynth_seq_area
        "opt": {"-directive": "ExploreWithRemap"},
        "place": {"-directive": "ExtraPostPlacementOpt"},
        # "place_opt": {"-directive ExploreArea"},
        "place_opt": {
            "-retarget": None,
            "-propconst": None,
            "-sweep": None,
            "-aggressive_remap": None,
            "-shift_register_opt": None,
            "-dsp_register_opt": None,
            "-resynth_seq_area": None,
            "-merge_equivalent_drivers": None,
        },
        "place_opt2": {"-directive": "ExploreArea"},
        # if no directive: -placement_opt
        "phys_opt": {"-directive": "AggressiveExplore"},
        "route": {"-directive": "Explore"},
    },
    "AreaExploreWithRemap": {
        "synth": {"-flatten_hierarchy": "full", "-retiming": None},
        # if no directive: -resynth_seq_area
        "opt": {"-directive": "ExploreWithRemap"},
        "place": {"-directive": "Default"},
        "place_opt": {"-directive": "ExploreWithRemap"},
        # "place_opt": {'-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
        #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'},
        # if no directive: -placement_opt
        "phys_opt": {"-directive": "Explore"},
        "route": {"-directive": "Explore"},
    },
    "AreaExploreWithRemap2": {
        "synth": {"-flatten_hierarchy": "full", "-retiming": None},
        # if no directive: -resynth_seq_area
        "opt": {"-directive": "ExploreArea"},
        "place": {"-directive": "Default"},
        "place_opt": {"-directive": "ExploreWithRemap"},
        # "place_opt": {'-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
        #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'},
        # if no directive: -placement_opt
        "phys_opt": {"-directive": "Explore"},
        "route": {"-directive": "Explore"},
    },
    "AreaExplore": {
        "synth": {"-flatten_hierarchy": "full"},
        # if no directive: -resynth_seq_area
        "opt": {"-directive": "ExploreArea"},
        "place": {"-directive": "Default"},
        "place_opt": {"-directive": "ExploreArea"},
        # "place_opt": {'-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
        #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'},
        # if no directive: -placement_opt
        "phys_opt": {"-directive": "Explore"},
        "route": {"-directive": "Explore"},
    },
    "Power": {
        "synth": {
            "-flatten_hierarchy": "full",
            "-gated_clock_conversion": "auto",
            "-control_set_opt_threshold": "1",
            "-shreg_min_size": "3",
            "-resource_sharing": "auto",
        },
        # if no directive: -resynth_seq_area
        "opt": {"-directive": "ExploreSequentialArea"},
        "place": {"-directive": "Default"},
        "place_opt": {"-directive": "ExploreSequentialArea"},
        # {'-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
        #   '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'},
        # if no directive: -placement_opt
        "phys_opt": {"-directive": "Explore"},
        "route": {"-directive": "Explore"},
    },
}


def _vivado_steps(strategy: str, s: str) -> Dict[str, Any]:
    if s == "synth":
        steps = ["synth", "opt", "power_opt"]
    else:
        steps = [
            "place",
            "power_opt",
            "place_opt",
            "place_opt2",
            "phys_opt",
            "phys_opt2",
            "route",
        ]

    recipe = xeda_strategies.get(strategy)
    assert recipe is not None, f"Unknown strategy: {strategy}"
    return {step: recipe.get(step) for step in steps if step is not None}


class VivadoAltSynth(VivadoSynth, FpgaSynthFlow):
    """Synthesize with Xilinx Vivado using an alternative TCL-based flow"""

    class Settings(VivadoSynth.Settings):
        synth: RunOptions = RunOptions(strategy="Default")
        impl: RunOptions = RunOptions(strategy="Default")

        @validator("synth", "impl", always=True)
        def validate_synth(cls, value, values, field):
            if field.name == "impl" and not value.strategy and not value.steps:
                synth = values.get("synth")
                value.strategy = synth.strategy
            if value.strategy:
                value.steps = {
                    **_vivado_steps(value.strategy, field.name),
                    **value.steps,
                }
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

        if ss.out_of_context:
            if "synth" in ss.synth.steps and ss.synth.steps["synth"] is not None:
                ss.synth.steps["synth"]["-mode"] = "out_of_context"

        # always need a synth step?
        synth_steps = ss.synth.steps.get("synth")
        if synth_steps is None:
            synth_steps = {}
        if any(x in ss.blacklisted_resources for x in ("bram_tile", "bram")):
            # FIXME also add "-max_uram 0", only for UltraScale+ devices
            synth_steps["-max_bram"] = 0
        if "dsp" in ss.blacklisted_resources:
            synth_steps["-max_dsp"] = 0
        ss.synth.steps["synth"] = synth_steps

        def flatten_dict(d):
            return " ".join([f"{k} {v}" if v is not None else k for k, v in d.items()])

        def steps_to_str(steps):
            return "\n " + "\n ".join(
                f"{name}: {flatten_dict(step)}" for name, step in steps.items() if step
            )

        log.info("Synthesis steps:%s", steps_to_str(ss.synth.steps))
        log.info("Implementation steps:%s", steps_to_str(ss.impl.steps))

        self.add_template_filter(
            "flatten_dict",
            flatten_dict,
        )
        clock_xdc_path = self.copy_from_template("clock.xdc")
        script_path = self.copy_from_template(
            "vivado_alt_synth.tcl",
            xdc_files=[clock_xdc_path],
        )

        self.vivado.run("-source", script_path)
