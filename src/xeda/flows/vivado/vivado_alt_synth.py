import logging
from typing import Any, Dict

from ..flow import FpgaSynthFlow
from ...dataclass import validator
from . import Vivado
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
    # "ExtraTimingCongestion": {
    #     "synth": ["-flatten_hierarchy": "full",
    #               "-retiming",
    #               "-directive": "PerformanceOptimized",
    #               "-fsm_extraction": "one_hot",
    #               "-resource_sharing off",
    #               "-shreg_min_size 10",
    #               "-keep_equivalent_registers",
    #               ],
    #     "opt": ["-directive ExploreWithRemap"],
    #     "place": ["-directive AltSpreadLogic_high"],
    #     "place_opt": ['-retarget', '-propconst', '-sweep', '-remap', '-muxf_remap', '-aggressive_remap', '-shift_register_opt'],
    #     "place_opt2": ["-directive Explore"],
    #     "phys_opt": ["-directive AggressiveExplore"],
    #     "route": ["-directive AlternateCLBRouting"],
    # },
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
    # "ExtraTimingAltRouting": {
    #     # -mode: default, out_of_context
    #     # -flatten_hierarchy: rebuilt, full; equivalent in terms of QoR?
    #     # -no_lc: When checked, this option turns off LUT combining
    #     # -keep_equivalent_registers -no_lc
    #     "synth": ["-flatten_hierarchy full",
    #               "-retiming",
    #               "-directive PerformanceOptimized",
    #               "-fsm_extraction one_hot",
    #               #   "-resource_sharing off",
    #               #   "-no_lc",
    #               "-shreg_min_size 5",
    #               "-keep_equivalent_registers "
    #               ],
    #     "opt": ["-directive ExploreWithRemap"],
    #     "place": ["-directive ExtraTimingOpt"],
    #     "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt'],
    #     "phys_opt": ["-directive AggressiveExplore"],
    #     # "route": "-directive NoTimingRelaxation",
    #     "route": ["-directive AlternateCLBRouting"],
    # },
    # "Area": {
    #     # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
    #     "synth": ["-flatten_hierarchy full", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing auto", "-directive AreaOptimized_medium"],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreArea",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreArea",
    #     # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # },
    # "AreaHigh": {
    #     # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
    #     "synth": ["-flatten_hierarchy full", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing on", "-directive AreaOptimized_high"],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreArea",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreArea",
    #     # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # },
    # "AreaPower": {
    #     # AreaOptimized_medium or _high prints error messages in Vivado 2020.1: "unexpected non-zero reference counts", but succeeeds and post-impl sim is OK too
    #     "synth": ["-flatten_hierarchy full", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing auto", "-gated_clock_conversion auto", "-directive AreaOptimized_medium"],
    #     "opt": ["-directive ExploreArea"],
    #     "place": "-directive Default",
    #     "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt', '-dsp_register_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     "place_opt2": ["-directive ExploreArea"],
    #     # FIXME!!! This is the only option that results in correct post-impl timing sim! Why??!
    #     "phys_opt": ["-directive AggressiveExplore"],
    #     "route": ["-directive Explore"],
    # },
    # "AreaTiming": {
    #     "synth": ["-flatten_hierarchy rebuilt", "-retiming"],
    #     # if no directive: -resynth_seq_area
    #     "opt": ["-directive ExploreWithRemap"],
    #     "place": ["-directive ExtraPostPlacementOpt"],
    #     # "place_opt": ["-directive ExploreArea"],
    #     "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt', '-dsp_register_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     "place_opt2": ["-directive ExploreArea"],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive AggressiveExplore",
    #     "route": "-directive Explore",
    # },
    # "AreaExploreWithRemap": {
    #     "synth": ["-flatten_hierarchy full", "-retiming"],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreWithRemap",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreWithRemap",
    #     # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # },
    # "AreaExploreWithRemap2": {
    #     "synth": [],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreArea",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreWithRemap",
    #     # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # },
    # "AreaExplore": {
    #     "synth": ["-flatten_hierarchy full"],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreArea",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreArea",
    #     # "place_opt": ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #               '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # },
    # "Power": {
    #     "synth": ["-flatten_hierarchy full", "-gated_clock_conversion auto", "-control_set_opt_threshold 1", "-shreg_min_size 3", "-resource_sharing auto"],
    #     # if no directive: -resynth_seq_area
    #     "opt": "-directive ExploreSequentialArea",
    #     "place": "-directive Default",
    #     "place_opt": "-directive ExploreSequentialArea",
    #     # ['-retarget', '-propconst', '-sweep', '-aggressive_remap', '-shift_register_opt',
    #     #   '-dsp_register_opt', '-bram_power_opt', '-resynth_seq_area', '-merge_equivalent_drivers'],
    #     # if no directive: -placement_opt
    #     "phys_opt": "-directive Explore",
    #     "route": "-directive Explore",
    # }
}


def _vivado_steps(strategy: str, s: str):
    if s == "synth":
        steps = ["synth", "opt"]
    else:
        steps = [
            "place",
            "place_opt",
            "place_opt2",
            "phys_opt",
            "phys_opt2",
            "route",
        ]
    return {step: xeda_strategies[strategy].get(step) for step in steps}


class VivadoAltSynth(Vivado, FpgaSynthFlow):
    """Synthesize with Xilinx Vivado using an alternative TCL-based flow"""

    class Settings(VivadoSynth.Settings):
        synth: RunOptions = RunOptions(
            strategy="Default", steps=_vivado_steps("Default", "synth")
        )
        impl: RunOptions = RunOptions(
            strategy="Default", steps=_vivado_steps("Default", "impl")
        )

        # pylint: disable=no-self-argument,no-self-use
        @validator("synth")
        def validate_synth(cls, v: RunOptions):
            if v.strategy and not v.steps:
                v.steps = _vivado_steps(v.strategy, "synth")
            return v

        # pylint: disable=no-self-argument,no-self-use
        @validator("impl", always=True)
        def validate_impl(cls, v: RunOptions, values):
            if not v.strategy:
                v.strategy = values.get("synth", {}).get("strategy")
            if v.strategy and not v.steps:
                v.steps = _vivado_steps(v.strategy, "impl")
            return v

    def run(self):
        clock_xdc_path = self.copy_from_template("clock.xdc")
        ss = self.settings
        assert isinstance(ss, self.Settings)

        if ss.out_of_context:
            if "synth" in ss.synth.steps and ss.synth.steps["synth"] is not None:
                ss.synth.steps["synth"]["-mode"] = "out_of_context"

        blacklisted_resources = ss.blacklisted_resources
        if "bram" in blacklisted_resources and "bram_tile" not in blacklisted_resources:
            blacklisted_resources.append("bram_tile")

        log.info("blacklisted_resources: %s", blacklisted_resources)

        # if 'bram_tile' in blacklisted_resources:
        #     # FIXME also add -max_uram 0 for ultrascale+
        #     settings.synth.steps['synth'].append('-max_bram 0')
        # if 'dsp' in blacklisted_resources:
        #     settings.synth.steps['synth'].append('-max_dsp 0')

        self.add_template_filter(
            "flatten_dict",
            lambda d: " ".join([f"{k} {v}" if v else k for k, v in d.items()]),
        )

        script_path = self.copy_from_template(
            "vivado_alt_synth.tcl",
            xdc_files=[clock_xdc_path],
        )
        self.vivado.run("-source", script_path)
