import json
import logging
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Annotated, List, Literal, Optional, Union

from importlib_resources import as_file, files

from ...dataclass import Field, field_validator, model_validator
from ...design import SourceType
from ...flow import AsicSynthFlow, describe_results
from ...flows.yosys import HiLoMap, Yosys, preproc_libs
from ...platforms import AsicsPlatform
from ...tool import ExecutableNotFound, Tool
from ...units import convert_unit
from ...utils import try_convert, unique

log = logging.getLogger(__name__)


def abc_opt_script(opt: Optional[str]) -> Optional[str]:
    """The abc mapping script for `optimize`, as the `abc_script` setting of `Yosys` takes it.

    The scripts are OpenROAD-flow-scripts' `abc_area.script` and `abc_speed.script`, written
    inline (``+cmd;cmd;...``, which `Yosys.Settings` adapts for `abc -script`). `None` keeps
    yosys's own default mapping script.
    """
    if opt is None:
        return None
    if opt == "area":
        scr = ["strash", "dch", "map -B 0.9", "topo", "stime -c", "buffer -c"]
    else:
        scr = [
            # fmt: off
            "&get -n", "&st", "&dch", "&nf", "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf",
            "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf", "&put", "&get -n", "&st", "&syn2",
            "&if -g -K 6", "&synch2", "&nf", "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf",
            "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf", "&put", "buffer -c", "topo", "stime -c",
            # fmt: on
        ]
    return "+" + ";".join([*scr, "upsize -c", "dnsize -c"])


def format_value(v, precision=3) -> Optional[str]:
    if v is None:
        return None
    return f"{round(v, precision):.{precision}}" if isinstance(v, float) else str(v)


def join_to_str(*args, sep=" ", val_fmt=None):
    if val_fmt is None:
        val_fmt = lambda x: str(x)  # noqa: E731
    return sep.join(val_fmt(e) for e in args if e is not None)


def dict_to_str(
    d,
    kv_sep=" ",
    sep=" ",
    val_fmt=format_value,
):
    return sep.join(f"{k}{kv_sep}{val_fmt(v)}" for k, v in d.items())


def embrace(s):
    return "{" + str(s) + "}"


class Openroad(AsicSynthFlow):
    """OpenROAD open-source ASIC synthesis flow"""

    merged_lib_file = "merged.lib"  # used by Yosys and floorplan (restructure)

    results_description = describe_results(
        "Fmax",
        "wns",
        "tns",
        "worst_slack",
        "setup_violations",
        "hold_violations",
        "design_area",
        "design_area_unit",
        "utilization_percent",
    )

    class Settings(AsicSynthFlow.Settings):
        platform: AsicsPlatform = Field(
            description="ASIC platform (PDK) to target: a bundled platform name (see "
            "`xeda list-platforms`) or the path to a platform config.toml.",
        )
        corner: Optional[Union[str, List]] = Field(
            None,
            description='Process corner(s) from the platform\'s Liberty set to analyze, e.g. "tt". '
            "Defaults to the platform's own default corner.",
        )
        multi_corner: bool = Field(
            False,
            description="Analyze timing across all of the platform's corners rather than only the "
            "default one. Forced off when the platform defines fewer than two corners.",
        )
        input_delay_frac: Optional[float] = Field(
            0.20, description="Input delay as fraction of clock period [0.0..1.0)"
        )
        output_delay_frac: Optional[float] = Field(
            0.20, description="Output delay as fraction of clock period [0.0..1.0)"
        )
        input_delay: Optional[float] = Field(None, description="Input delay in picoseconds")
        output_delay: Optional[float] = Field(None, description="Output delay in picoseconds")
        core_utilization: Optional[float] = Field(
            40.0, description="Core utilization in percent (0..100)"
        )
        core_aspect_ratio: float = Field(1.0, description="Core height / core width")
        core_margin: float = Field(2.0, description="Core margin in um")
        core_area: List[Union[int, float]] = Field(
            [],
            description="Explicit core area as [x1, y1, x2, y2] in microns. Overrides "
            "`core_utilization`.",
        )
        die_area: List[Union[int, float]] = Field(
            [],
            description="Explicit die area as [x1, y1, x2, y2] in microns. Overrides "
            "`core_utilization`.",
        )
        sdc_files: List[Path] = Field(
            [],
            description="Additional SDC constraint files, read on top of the constraints generated "
            "from `clocks`.",
        )
        io_constraints: Optional[Path] = Field(
            None, description="TCL script that places the IO pins, sourced during pin placement."
        )
        results_dir: Path = Field(
            Path("results"),
            description="Directory, relative to the run directory, where OpenROAD writes its "
            "output files.",
        )
        optimize: Optional[Literal["speed", "area"]] = Field(
            "area",
            description="Optimization target of synthesis: selects OpenROAD-flow-scripts' abc "
            "mapping script for it. `null` keeps yosys's default script and skips post-synthesis "
            "optimization.",
        )
        abc_load_in_ff: Optional[float] = Field(
            None,
            description="Wire load ABC assumes, in units of flip-flop input capacitance. Overrides "
            "the platform value.",
        )
        abc_driver_cell: Optional[int] = Field(
            None,
            description="Cell ABC assumes drives the primary inputs. Overrides the platform value.",
        )
        write_metrics: Optional[Path] = Field(
            Path("metrics.json"), description="write metrics in file in JSON format"
        )
        exit: bool = Field(True, description="exit after completion")
        gui: bool = Field(False, description="start in GUI mode")
        copy_platform_files: bool = Field(
            False,
            description="Copy the platform's LEF/Liberty/technology files into the run directory "
            "instead of referencing them where they are installed.",
        )
        extra_liberty_files: List[Path] = Field(
            [], description="Additional Liberty (.lib) files to read, e.g. for hard macros."
        )
        # floorplan
        floorplan_def: Optional[Path] = Field(
            None,
            description="DEF file with a ready-made floorplan, used instead of generating one.",
        )
        floorplan_tcl: Optional[Path] = Field(
            None, description="TCL script sourced at the end of floorplanning, for custom tweaks."
        )
        resynth_for_timing: bool = Field(
            False,
            description="Run an extra resynthesis pass after floorplanning, optimizing for timing.",
        )
        resynth_for_area: bool = Field(
            False,
            description="Run an extra resynthesis pass after floorplanning, optimizing for area.",
        )
        # pre_place
        rtlmp_flow: bool = Field(
            False,
            description="Use the RTL-aware hierarchical macro placer (RTL-MP) instead of the "
            "default macro placement. Configured by `rtlmp_config_file`.",
        )
        # io_place
        io_place_random: bool = Field(
            False,
            description="Place IO pins randomly instead of running the pin placer. Only useful for "
            "quick experiments.",
        )
        # place
        place_density: Union[None, float, str] = Field(
            None,
            description="Target placement density (0.0..1.0). Defaults to the platform value. "
            "Higher is denser and harder to route.",
        )
        place_density_lb_addon: Optional[Annotated[float, Field(ge=0.0, lt=1)]] = Field(  # type: ignore
            None,
            description="Amount added to the automatically determined lower bound of the "
            "placement density [0.0..1.0).",
        )
        dont_use_cells: List[str] = Field(
            [],
            description="Standard cells the tools must not use, in addition to the platform's own "
            "dont-use list.",
        )
        place_pins_args: List[str] = Field(
            [], description="Extra arguments passed to OpenROAD's `place_pins`."
        )
        blocks: List[str] = Field(
            [],
            description="Sub-blocks that are hardened separately; treated as black boxes during "
            "synthesis.",
        )
        global_placement_args: List[str] = Field(
            [], description="Extra arguments passed to OpenROAD's `global_placement`."
        )
        min_phi_coef: Optional[Annotated[float, Field(ge=0.95, le=1.05)]] = Field(  # type: ignore
            None,
            description="Lower bound for the minimum placement-density coefficient (phi). "
            "Defaults to 0.95; allowed values are 0.95 through 1.05.",
        )
        max_phi_coef: Optional[Annotated[float, Field(ge=1.00, le=1.20)]] = Field(  # type: ignore
            None,
            description="Upper bound for the minimum placement-density coefficient (phi). "
            "Defaults to 1.05; allowed values are 1.00 through 1.20.",
        )
        # global_route
        congestion_iterations: int = Field(
            100,
            description="Maximum number of global-routing iterations spent resolving congestion.",
        )
        global_routing_layer_adjustment: float = Field(
            0.5,
            description="Fraction by which each layer's routing resources are derated during "
            "global routing (0.0..1.0). Higher values reserve more room for detailed routing.",
        )
        repair_antennas: bool = Field(
            False, description="Run antenna-violation repair after detailed routing."
        )
        update_sdc_margin: Optional[Annotated[float, Field(gt=0.0, lt=1.0)]] = Field(0.05, description="If set, write an SDC file with clock periods that result in slightly (value * clock_period) negative slack (failing).")  # type: ignore
        # detailed_route
        detailed_route_or_seed: Optional[int] = Field(
            None, description="Random seed for the detailed router's optimization ordering."
        )
        detailed_route_or_k: Optional[int] = Field(
            None, description="Number of candidate orderings the detailed router evaluates."
        )
        db_process_node: Optional[str] = Field(
            None,
            description="Process node name recorded in the OpenDB database; used by some "
            "via-generation rules.",
        )
        detailed_route_additional_args: List[str] = Field(
            [], description="Extra arguments passed to OpenROAD's `detailed_route`."
        )
        post_detailed_route_tcl: Optional[Path] = Field(
            None, description="TCL script sourced after detailed routing completes."
        )
        #
        gpl_routability_driven: bool = Field(
            True, description="Let global placement optimize for routability."
        )
        gpl_timing_driven: bool = Field(
            True, description="Let global placement optimize for timing."
        )
        macro_placement_file: Optional[Path] = Field(
            None,
            description="File with fixed macro placements, applied instead of automatic macro "
            "placement.",
        )
        post_pdn_tcl: Optional[Path] = Field(
            None,
            description="TCL script sourced after the power distribution network is generated.",
        )
        make_tracks_tcl: Optional[Path] = Field(
            None,
            description="TCL script that creates the routing tracks, overriding the platform "
            "default.",
        )
        footprint: Optional[Path] = Field(
            None, description="Pad-ring footprint strategy file, for designs with an IO ring."
        )
        footprint_def: Optional[Path] = Field(
            None, description="DEF file describing the pad-ring footprint."
        )
        footprint_tcl: Optional[Path] = Field(
            None, description="TCL script that builds the pad-ring footprint."
        )
        gds_seal_file: Optional[Path] = Field(
            None, description="GDS file containing a seal ring, merged into the final layout."
        )
        sig_map_file: Optional[Path] = Field(
            None, description="Signal-mapping file used when building the pad ring."
        )
        density_fill: bool = Field(
            False,
            description="Insert metal fill to meet density rules before writing the final layout.",
        )
        rtlmp_config_file: Optional[Path] = Field(
            None, description="Configuration file for the RTL-MP macro placer. See `rtlmp_flow`."
        )
        macro_wrappers: Optional[Path] = Field(
            None, description="File describing wrapper cells placed around macros."
        )
        cdl_masters_file: Optional[Path] = Field(
            None,
            description="CDL netlists of the cell/macro masters, merged into the written CDL. "
            "See `write_cdl`.",
        )
        hold_slack_margin: Optional[float] = Field(
            None,
            description="Extra hold slack (ns) targeted when repairing timing, so the result "
            "keeps margin.",
        )
        setup_slack_margin: Optional[float] = Field(
            None,
            description="Extra setup slack (ns) targeted when repairing timing, so the result "
            "keeps margin.",
        )
        cts_buf_distance: Optional[int] = Field(
            None,
            description="Maximum distance (um) between clock-tree buffers. Defaults to the "
            "platform value.",
        )
        cts_cluster_size: int = Field(
            30, description="Maximum number of sinks in a clock-tree cluster."
        )
        cts_cluster_diameter: int = Field(
            100, description="Maximum diameter (um) of a clock-tree cluster."
        )
        tie_separation: int = Field(
            0, description="Minimum distance (um) between a tie-high/tie-low cell and its load."
        )
        final_irdrop_analysis: bool = Field(
            True, description="Run static IR-drop analysis on the final routed design."
        )
        disable_via_gen: bool = Field(
            False,
            description="Disable automatic via generation and use only the vias the platform "
            "defines.",
        )
        repair_pdn_via_layer: Optional[str] = Field(
            None, description="Metal layer on which power-grid via violations are repaired."
        )
        # final
        write_cdl: bool = Field(
            False, description="Write a CDL netlist of the final design. See `cdl_masters_file`."
        )
        save_images: bool = Field(
            True, description="Render PNG images of the layout at each major stage."
        )
        generate_gds: bool = Field(True, description="Stream the final layout out to GDSII.")

        @field_validator("platform", mode="before")
        @classmethod
        def _validate_platform(cls, value):
            value = AsicsPlatform.from_setting(value)
            if isinstance(value, AsicsPlatform):
                # The selected corner is applied after all settings fields are available. Keep
                # that normalization isolated from a caller-owned platform instance.
                value = value.model_copy(deep=True)
            return value

        @model_validator(mode="after")
        def _select_platform_corner(self):
            if self.corner:
                corner = self.corner[0] if isinstance(self.corner, list) else self.corner
                self.platform.select_corner(corner)
            return self

        @field_validator("input_delay", "output_delay", mode="before")
        @classmethod
        def _validate_values_units_to_ps(cls, value):
            if isinstance(value, str):
                value = convert_unit(value, "picoseconds")
            return value

    def init(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        if len(ss.platform.corner) < 2:
            ss.multi_corner = False

        yosys_settings = Yosys.Settings(
            clocks=ss.clocks,
            flatten=True,
            black_box=ss.blocks,
            # `optimize` is OpenROAD's own setting: it reaches yosys as the abc mapping script
            # it selects, plus post-synthesis optimization. Yosys has no such setting of its own.
            post_synth_opt=ss.optimize is not None,
            abc_script=abc_opt_script(ss.optimize),
            abc_constr=[
                f"set_driving_cell {ss.abc_driver_cell or ss.platform.abc_driver_cell}",
                f"set_load {ss.abc_load_in_ff if ss.abc_load_in_ff is not None else ss.platform.abc_load_in_ff}",
            ],
            netlist_attrs=False,
            netlist_hex=False,
            netlist_dec=False,
            netlist_blackboxes=False,
            # systemverilog="slang",
        )  # pyright: ignore

        if not ss.copy_platform_files:
            ss.platform = ss.platform.with_absolute_paths()
        else:
            files_to_copy: List[Path] = []
            for _, corner in ss.platform.corner.items():
                files_to_copy += corner.lib_files
                if corner.dff_lib_file:
                    files_to_copy.append(corner.dff_lib_file)
            if ss.platform.tech_lef:
                files_to_copy.append(ss.platform.tech_lef)
            if ss.platform.std_cell_lef:
                files_to_copy.append(ss.platform.std_cell_lef)
            files_to_copy += ss.platform.gds_files
            if ss.platform.setrc_tcl:
                files_to_copy.append(ss.platform.setrc_tcl)
            if ss.platform.derate_tcl:
                files_to_copy.append(ss.platform.derate_tcl)
            for file in unique(files_to_copy):
                if not os.path.isabs(file):
                    src = ss.platform.root_dir / file
                    dst = Path(file)
                    if not dst.parent.exists():
                        dst.parent.mkdir(parents=True)
                    shutil.copy(src, dst)

        my_lib_dir = Path("lib")
        my_lib_dir.mkdir(exist_ok=True)
        copy_resources = []
        orig_libs = []
        dff_lib_file = None
        corner = ss.platform.default_corner_settings
        orig_libs += corner.lib_files
        if corner.dff_lib_file:
            src = ss.platform.root_dir / corner.dff_lib_file
            dst = my_lib_dir / src.name
            shutil.copy(src, dst)
            copy_resources.append(str(dst))
        assert ss.platform.default_corner_settings
        dff_lib_file = ss.platform.default_corner_settings.dff_lib_file
        orig_libs = unique(orig_libs)
        for lib in orig_libs:
            src = ss.platform.root_dir / lib
            dst = my_lib_dir / src.name
            shutil.copy(src, dst)
        ss.dont_use_cells = unique(ss.platform.dont_use_cells + ss.dont_use_cells)
        preproc_libs(
            orig_libs,
            self.merged_lib_file,
            ss.dont_use_cells,
            f"{ss.platform.name}_merged",
            use_temp_folder=not ss.debug,
        )
        yosys_libs = [self.merged_lib_file]
        if dff_lib_file:
            yosys_settings.dff_liberty = Path(dff_lib_file).absolute()
        copy_resources += yosys_libs
        yosys_settings.liberty = [Path(lib).absolute() for lib in yosys_libs]
        yosys_settings.adder_map = str(ss.platform.adder_map_file)
        yosys_settings.clockgate_map = str(ss.platform.clkgate_map_file)
        yosys_settings.other_maps = [str(ss.platform.latch_map_file)]
        if (
            ss.platform.tiehi_cell
            and ss.platform.tiehi_port
            and ss.platform.tielo_cell
            and ss.platform.tielo_port
        ):
            yosys_settings.hilomap = HiLoMap(
                hi=(ss.platform.tiehi_cell, ss.platform.tiehi_port),
                lo=(ss.platform.tielo_cell, ss.platform.tielo_port),
            )
        if ss.platform.min_buf_cell:
            yosys_settings.insbuf = (
                ss.platform.min_buf_cell,
                ss.platform.min_buf_ports[0],
                ss.platform.min_buf_ports[1],
            )
        self.add_dependency(Yosys, yosys_settings, copy_resources)

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        ss.results_dir.mkdir(exist_ok=True, parents=True)
        ss.checkpoints_dir.mkdir(exist_ok=True, parents=True)

        yosys_dep = self.pop_dependency(Yosys)
        netlist = yosys_dep.artifacts.netlist_verilog
        if not os.path.isabs(netlist):
            netlist = os.path.join(yosys_dep.run_path, netlist)
        synth_netlist = ss.results_dir / "1_synth.v"
        shutil.copy(netlist, synth_netlist)

        # yosys doesn't support SDC so we generate it here
        clocks_sdc = self.copy_from_template("clocks.sdc")
        sdc_files = [
            s.path for s in self.design.sources_of_type(SourceType.Sdc, rtl=True, tb=False)
        ]
        sdc_files.append(clocks_sdc)
        sdc_files += ss.sdc_files
        ss.sdc_files = sdc_files

        openroad = Tool("openroad", self, version_flag="-version")

        if not self.design.rtl.clocks:
            log.critical(
                "No clocks specified for top RTL design. Continuing with synthesis anyways."
            )

        assert self.design.rtl.top, "design.rtl.top must be set"

        if not ss.footprint_def and ss.footprint:
            assert ss.sig_map_file
        env = dict(
            MIN_ROUTING_LAYER=ss.platform.min_routing_layer,  # needed by platform.fastroute
            MAX_ROUTING_LAYER=ss.platform.max_routing_layer,  # needed by platform.fastroute
            TAP_CELL_NAME=ss.platform.tapcell_name,  # needed by platform.tapcell_tcl
        )

        flow_steps = [
            "load",
            "floorplan",
            "resynth",
            "pre_place",
            "io_place",
            "global_place",
            "resize",
            "detailed_place",
            "cts",
            "filler",
            "global_route",
            "detailed_route",
            "finalize",
        ]
        one_shot = True  # TODO

        def get_step_index(step_name):
            return flow_steps.index(step_name)

        def get_step_id(step_name):
            return f"{get_step_index(step_name)}_{step_name}"

        def get_prev_step_id(step_name):
            idx = get_step_index(step_name)
            if idx <= 0:
                return None
            return get_step_id(flow_steps[idx - 1])

        write_checkpoint_steps = ["cts", "detailed_route", "finalize"]
        load_checkpoint_steps = ["finalize"]

        def should_write_checkpoint(step_name: str):
            return (not one_shot) or step_name in write_checkpoint_steps

        def should_load_checkpoint(step_name: str):
            return (not one_shot) or step_name in load_checkpoint_steps

        self.add_template_filter_func(embrace)
        self.add_template_filter_func(join_to_str)
        self.add_template_global_func(get_step_id)
        self.add_template_global_func(get_step_index)
        self.add_template_global_func(get_prev_step_id)
        self.add_template_global_func(should_write_checkpoint)
        self.add_template_global_func(should_load_checkpoint)

        args = ["-no_splash", "-no_init", "-threads", ss.nthreads or "max"]
        if ss.exit:
            args.append("-exit")
        if ss.gui:
            args.append("-gui")
        if ss.write_metrics:
            args.extend(["-metrics", str(ss.write_metrics)])

        defines = dict(
            platform=ss.platform,  # for easier reference
            netlist=synth_netlist,
            merged_lib_file=self.merged_lib_file,  # used by resynth
            total_steps=len(flow_steps),
        )
        self.artifacts.logs = []

        def run_steps(start_idx, end_idx):
            steps_to_run = flow_steps[start_idx:end_idx]
            log_file = f"openroad_{start_idx}_{end_idx-1}.log"
            log_args = ["-log", log_file]
            self.artifacts.logs.append(log_file)
            tcl_script = self.copy_from_template(
                "orflow.tcl",
                lstrip_blocks=False,
                trim_blocks=True,
                script_filename=f"orflow_{start_idx}_{end_idx-1}.tcl",
                **defines,
                steps_to_run=steps_to_run,
                starting_index=start_idx,
            )
            openroad.run(*args, *log_args, tcl_script, env=env)

        run_steps(0, len(flow_steps) - 1)
        run_steps(len(flow_steps) - 1, len(flow_steps))

        if ss.generate_gds:
            klayout = openroad.derive("klayout")  # TODO ?
            platform_lyt = ss.platform.klayout_tech_file
            assert platform_lyt
            xml_tree = ET.parse(platform_lyt).getroot()
            base_path = xml_tree.find("base-path")
            assert base_path is not None
            base_path.text = str(ss.platform.root_dir.absolute())
            lef_files = xml_tree.find("reader-options/lefdef/lef-files")
            assert lef_files is not None
            lef_files.text = str(ss.platform.std_cell_lef)
            properties_file = xml_tree.find("layer-properties_file")
            if properties_file is None:
                properties_file = xml_tree.find("layer-properties-file")
            lyp = ss.platform.klayout_layer_prop_file
            if lyp and properties_file is not None:
                properties_file.text = str(lyp)

            lyt = platform_lyt.name
            with open(lyt, "wb") as f:
                f.write(ET.tostring(xml_tree))
            out_file = ss.results_dir / "final.gds"
            res = files(__package__).joinpath("openroad_scripts", "utils", "def2stream.py")
            with as_file(res) as def2stream:
                args = [
                    "-zz",
                    "-rd",
                    f"design_name={self.design.rtl.top}",
                    "-rd",
                    f'in_def={ss.results_dir / f"{get_step_id(flow_steps[-1])}.def"!s}',
                    "-rd",
                    f"in_files={join_to_str(*ss.platform.gds_files)}",
                    "-rd",
                    f"config_file={join_to_str(ss.platform.fill_config)}",
                    "-rd",
                    f"seal_file={join_to_str(ss.gds_seal_file)}",
                    "-rd",
                    f"out_file={out_file!s}",
                    "-rd",
                    f"tech_file={lyt!s}",
                    "-rd",
                    f"layer_map={join_to_str(ss.platform.gds_layer_map)}",
                    "-rm",
                    str(def2stream),
                ]
                try:
                    klayout.run(*args)
                    self.artifacts.gds = out_file
                    log.info("GDS file saved in %s", str(out_file.absolute()))
                except ExecutableNotFound:
                    log.warning("`%s` was not found. Skipping GDS generation.", klayout.executable)

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        last_log = self.artifacts.logs[-1]
        time_unit = ss.platform.time_unit
        results = self.parse_regex(
            last_log,
            r"tns\s+(?P<tns>\-?\d+(?:\.\d+)?)",
            r"wns\s+(?P<wns>\-?\d+(?:\.\d+)?)",
            r"worst slack\s+(?P<worst_slack>\-?\d+(?:\.\d+)?)",
            r"setup violation count +(?P<setup_violations>\d+)",
            r"hold violation count +(?P<hold_violations>\d+)",
            r"Design area\s+(?P<design_area>\d+(?:\.\d+)?)\s+(?P<design_area_unit>[a-zA-Z]+\^2)\s+(?P<utilization_percent>\d+(?:\.\d+)?)% utilization.",
            required=False,
        )

        if results:
            u = try_convert(results.pop("utilization_percent"), float)
            if u is not None:
                results["utilization"] = u / 100
        else:
            if not ss.write_metrics or not ss.write_metrics.exists():
                log.error("No results found!")
                return False
            results = dict()
        if ss.write_metrics:
            # metrics JSON should contain more accurate and more reliable values, therefore overwrite results from parsing the log
            with open(ss.write_metrics) as mf:
                metrics = dict(json.load(mf))
            metrics_name_map = {
                "timing__setup__tns": "tns",
                "timing__setup__ws": "worst_slack",
                "timing__setup__wns": None,
                # "clock__skew__setup": ,
                # "clock__skew__hold": ,
                # "timing__drv__max_slew_limit": ,
                # "timing__drv__max_slew": ,
                # "timing__drv__max_cap_limit": ,
                # "timing__drv__max_cap": ,
                # "timing__drv__max_fanout_limit":,
                # "timing__drv__max_fanout": 0,
                "timing__drv__setup_violation_count": "setup_violations",
                "timing__drv__hold_violation_count": "hold_violations",
                "power__internal__total": "_power_internal",
                "power__switching__total": "_power_switching",
                "power__leakage__total": "_power_leakage",
                "power__total": "power",
                "design__io": "_num_io",
                "design__die__area": "die_area",
                "design__core__area": "core_area",
                # "design__instance__count": ,
                "design__instance__area": "design_area",
                # "design__instance__count__stdcell": ,
                "design__instance__area__stdcell": "_design_area_stdcell",
                # "design__instance__count__macros": ,
                "design__instance__area__macros": "_design_area_macros",
                "design__instance__utilization": "utilization",
                # "design__instance__utilization__stdcell": ,
            }
            for metr_name, res_name in metrics_name_map.items():
                v = metrics.get(metr_name)
                if v is not None:
                    results[res_name] = v
        wns = results.get("wns")
        worst_slack = results.get("worst_slack")
        self.results.update(**results, success=True)
        if wns is not None and worst_slack is not None and len(ss.clocks) == 1:
            worst_slack = try_convert(worst_slack, float)
            assert worst_slack
            assert ss.main_clock
            # Fmax in MHz
            self.results["Fmax"] = 1000.0 / convert_unit(
                ss.main_clock.period_unit(time_unit) - worst_slack,
                to_unit="nanoseconds",
                from_unit=time_unit,
            )
        success = True
        violations = try_convert(self.results.get("setup_violations"), int)
        if violations:
            log.error("%d setup violations.", violations)
            success = False
        violations = try_convert(self.results.get("hold_violations"), int)
        if violations:
            log.error("%d hold violations.", violations)
            success = False
        return success
