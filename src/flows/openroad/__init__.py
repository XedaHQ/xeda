import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Literal, Optional, Union

from importlib_resources import as_file, files
from pydantic import Field, confloat, validator

from ...design import SourceType
from ...flow import AsicSynthFlow
from ...flows.yosys import HiLoMap, YosysSynth
from ...tool import Tool
from ...utils import unique, try_convert
from .platforms import Platform

log = logging.getLogger(__name__)


def merge_files(in_files, out_file, add_newline=False):
    with open(out_file, "w") as out_f:
        for in_file in in_files:
            with open(in_file, "r") as in_f:
                out_f.write(in_f.read())
            if add_newline:
                out_f.write("\n")


def clean_ascii(s: str) -> str:
    return s.encode("ascii", "ignore").decode("ascii")


def preproc_lib_content(content: str, pattern_list: List[str]) -> str:
    # Pattern to match a cell header
    pattern1 = r"(^\s*cell\s*\(\s*([\"]*" + '["]*|["]*'.join(pattern_list) + r"[\"]*)\)\s*\{)"
    replace = r"\1\n    dont_use : true;"
    content, count = re.subn(pattern1, replace, content, flags=re.MULTILINE)
    if count:
        log.info("Marked %d cells as dont_use", count)

    # Yosys-abc throws an error if original_pin is found within the liberty file.
    pattern2 = re.compile(r"(.*original_pin.*)")
    replace = r"/* \1 */;"
    content, count = re.subn(pattern2, replace, content)
    if count:
        log.info("Commented %d lines containing 'original_pin", count)

    # Yosys, does not like properties that start with : !, without quotes
    pattern3 = re.compile(r":\s+(!.*)\s+;")
    replace = r': "\1" ;'
    content, count = re.subn(pattern3, replace, content)
    if count:
        log.info("Replaced %d malformed functions", count)
    return content


def merge_libs(in_files, new_lib_name, out_file):
    cell_regex = re.compile(r"^\s*cell\s*\(")

    log.info(
        "Merging libraries %s into library %s (%s)",
        ", ".join(str(f) for f in in_files),
        new_lib_name,
        out_file,
    )

    with open(out_file, "w") as out:
        with open(in_files[0]) as hf:
            for line in hf.readlines():
                if re.search(r"^\s*library\s*\(", line):
                    out.write(f"library ({new_lib_name}) {{\n")
                elif re.search(cell_regex, line):
                    break
                else:
                    out.write(line)
        for f in in_files:
            with open(f, "r") as f:
                flag = 0
                for line in f.readlines():
                    if re.search(cell_regex, line):
                        if flag != 0:
                            raise Exception("Error! new cell before finishing previous one.")
                        flag = 1
                        out.write("\n" + line)
                    elif flag > 0:
                        flag += len(re.findall(r"\{", line))
                        flag -= len(re.findall(r"\}", line))
                        out.write(line)
        out.write("\n}\n")


def preproc_libs(
    in_files, new_lib_name, merged_file, dont_use_cells: List[str], needs_preproc=False
):
    merged_content = ""
    proc_files = []
    for in_file in in_files:
        in_file = Path(in_file)
        log.info("Pre-processing liberty file: %s", in_file)
        with open(in_file, encoding="utf-8") as f:
            content = clean_ascii(f.read())
        if needs_preproc:
            merged_content += preproc_lib_content(content, dont_use_cells)
        else:
            merged_content += content
        out_file = in_file.with_stem(in_file.stem + "-mod")
        log.info("Writing pre-processed file: %s", out_file)
        with open(out_file, "w") as f:
            f.write(merged_content)
        proc_files.append(out_file)
    merge_libs(proc_files, new_lib_name, merged_file)


def abc_opt_script(opt):
    if opt == "speed":
        scr = [
            # fmt: off
            "&get -n", "&st", "&dch", "&nf", "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf",
            "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf", "&put", "&get -n", "&st", "&syn2",
            "&if -g -K 6", "&synch2", "&nf", "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf",
            "&put", "&get -n", "&st", "&syn2", "&if -g -K 6", "&synch2", "&nf", "&put", "buffer -c", "topo", "stime -c",
            # fmt: on
        ]
    else:
        scr = ["strash", "dch", "map -B 0.9", "topo", "stime -c", "buffer -c"]
    scr += ["upsize -c", "dnsize -c"]


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

    class Settings(AsicSynthFlow.Settings):
        platform: Platform
        input_delay: Optional[float] = Field(
            0.20, description="Input delay as fraction of clock period (0.0..1.0)"
        )
        output_delay: Optional[float] = Field(
            0.20, description="Output delay as fraction of clock period (0.0..1.0)"
        )
        core_utilization: Optional[float] = Field(
            40.0, description="Core utilization in percent (0..100)"
        )
        core_aspect_ratio: float = Field(1.0, description="Core height / core width")
        core_margin: float = Field(2.0, description="Core margin in um")
        core_area: List[Union[int, float]] = []
        die_area: List[Union[int, float]] = []
        sdc_files: List[Path] = []
        io_constraints: Optional[Path] = None
        place_density: Union[None, float, str] = None
        results_dir: Path = Path("results")
        optimize: Optional[Literal["speed", "area"]] = Field(
            "area", description="Optimization target"
        )
        abc_load_in_ff: Optional[int] = None  # to override platform value
        abc_driver_cell: Optional[int] = None  # to override platform value
        write_metrics: Optional[Path] = Field(
            "metrics.json", description="write metrics in file in JSON format"
        )
        exit: bool = Field(True, description="exit after completion")
        gui: bool = Field(False, description="start in gui mode")
        copy_platform_files: bool = False
        nthreads: Optional[int] = Field(None, description="Number of threads to use. If none/0, use max available.")  # type: ignore
        extra_liberty_files: List[Path] = []
        # floorplan
        floorplan_def: Optional[Path] = None
        floorplan_tcl: Optional[Path] = None
        resynth_for_timing: bool = False
        resynth_for_area: bool = False
        # pre_place
        rtlmp_flow: bool = False
        # place
        place_density_lb_addon: Optional[confloat(ge=0.0, lt=1)] = None  # type: ignore
        dont_use_cells: List[str] = []
        place_pins_args: List[str] = []
        blocks: List[str] = []
        global_placement_args: List[str] = []
        # global_route
        congestion_iterations: int = 100
        global_routing_layer_adjustment: float = 0.5
        update_sdc_margin: Optional[confloat(gt=0.0, lt=1.0)] = Field(0.05, description="If set, write an SDC file with clock periods that result in slightly (value * clock_period) negative slack (failing).")  # type: ignore
        # detailed_route
        detailed_route_or_seed: Optional[int] = None
        detailed_route_or_k: Optional[int] = None
        db_process_node: Optional[str] = None
        detailed_route_additional_args: List[str] = []
        post_detailed_route_tcl: Optional[Path] = None
        #
        gpl_routability_driven: bool = True
        gpl_timing_driven: bool = True
        macro_placement_file: Optional[Path] = None
        post_pdn_tcl: Optional[Path] = None
        make_tracks_tcl: Optional[Path] = None
        footprint: Optional[Path] = None  # footprint strategy file
        footprint_def: Optional[Path] = None  # floorplan, resize
        footprint_tcl: Optional[Path] = None  # floorplan, resize
        gds_seal_file: Optional[Path] = None
        sig_map_file: Optional[Path] = None
        density_fill: bool = False
        rtlmp_config_file: Optional[Path] = None
        macro_wrappers: Optional[Path] = None
        cdl_masters_file: Optional[Path] = None  # cts
        hold_slack_margin: Optional[float] = None
        setup_slack_margin: Optional[float] = None
        cts_buf_distance: Optional[int] = None
        cts_cluster_size: int = 30
        cts_cluster_diameter: int = 100
        tie_separation: int = 0
        final_irdrop_analysis: bool = True
        disable_via_gen: bool = False
        repair_pdn_via_layer: Optional[str] = None
        # final
        write_cdl: bool = False
        save_images: bool = True
        generate_gds: bool = True

        @validator("platform", pre=True, always=True)
        def _validate_platform(cls, value):
            if isinstance(value, str) and not value.endswith(".toml"):
                return Platform.from_resource(value)
            elif isinstance(value, (str, Path)):
                return Platform.from_toml(value)
            return value

    def init(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings

        yosys_settings = YosysSynth.Settings(
            clocks=ss.clocks,
            flatten=True,
            black_box=ss.blocks,
            optimize=ss.optimize,
            post_synth_opt=ss.optimize is not None,
            abc_script=abc_opt_script(ss.optimize),
            abc_constr=[
                f"set_driving_cell {ss.abc_driver_cell or ss.platform.abc_driver_cell}",
                f"set_load {ss.abc_load_in_ff if ss.abc_load_in_ff is not None else ss.platform.abc_load_in_ff}",
            ],
        )  # pyright: ignore

        if not ss.copy_platform_files:
            ss.platform = ss.platform.with_absolute_paths()
        else:
            files_to_copy: List[Path] = []
            for _, corner in ss.platform.corner.items():
                files_to_copy += corner.lib_files
                if corner.dff_lib_file:
                    files_to_copy.append(corner.dff_lib_file)
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
        for _, corner in ss.platform.corner.items():
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
            f"{ss.platform.name}_merged",
            self.merged_lib_file,
            ss.dont_use_cells,
        )
        yosys_libs = [self.merged_lib_file]
        if dff_lib_file:
            yosys_settings.dff_liberty = os.path.join(YosysSynth.copied_resources_dir, dff_lib_file)
        copy_resources += yosys_libs
        yosys_settings.liberty = [
            str(os.path.join(YosysSynth.copied_resources_dir, os.path.basename(res)))
            for res in yosys_libs
        ]
        yosys_settings.adder_map = str(ss.platform.adder_map_file)
        yosys_settings.clockgate_map = str(ss.platform.clkgate_map_file)
        yosys_settings.other_maps = [str(ss.platform.latch_map_file)]
        yosys_settings.hilomap = HiLoMap(
            hi=(ss.platform.tiehi_cell, ss.platform.tiehi_port),
            lo=(ss.platform.tielo_cell, ss.platform.tielo_port),
        )
        yosys_settings.insbuf = (
            ss.platform.min_buf_cell,
            ss.platform.min_buf_ports[0],
            ss.platform.min_buf_ports[1],
        )
        self.add_dependency(YosysSynth, yosys_settings, copy_resources)

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        ss.results_dir.mkdir(exist_ok=True, parents=True)
        ss.checkpoints_dir.mkdir(exist_ok=True, parents=True)

        yosys_dep = self.pop_dependency(YosysSynth)
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

        openroad = Tool("openroad", self, version_arg="-version")

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

        defines = dict(
            platform=ss.platform,  # for easier reference
            netlist=synth_netlist,
            merged_lib_file=self.merged_lib_file,  # used by resynth
            num_cores=ss.nthreads or openroad.nproc,
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
                    f'in_def={str(ss.results_dir / f"{get_step_id(flow_steps[-1])}.def")}',
                    "-rd",
                    f"in_files={join_to_str(*ss.platform.gds_files)}",
                    "-rd",
                    f"config_file={join_to_str(ss.platform.fill_config)}",
                    "-rd",
                    f"seal_file={join_to_str(ss.gds_seal_file)}",
                    "-rd",
                    f"out_file={str(out_file)}",
                    "-rd",
                    f"tech_file={str(lyt)}",
                    "-rd",
                    f"layer_map={join_to_str(ss.platform.gds_layer_map)}",
                    "-rm",
                    str(def2stream),
                ]
                klayout.run(*args)
                log.info("GDS file saved in %s", str(out_file.absolute()))
                self.artifacts.gds = out_file

    def parse_reports(self) -> bool:
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        last_log = self.artifacts.logs[-1]
        print(last_log)
        success = self.parse_report_regex(
            last_log,
            r"tns\s+(?P<tns>\-?\d+(?:\.\d+)?)",
            r"wns\s+(?P<wns>\-?\d+(?:\.\d+)?)",
            r"worst slack\s+(?P<worst_slack>\-?\d+(?:\.\d+)?)",
            r"setup violation count +(?P<setup_violations>\d+)",
            r"hold violation count +(?P<hold_violations>\d+)",
            r"Design area\s+(?P<design_area>\d+(?:\.\d+)?)\s+(?P<design_area_unit>[a-zA-Z]+\^2)\s+(?P<utilization_percent>\d+(?:\.\d+)?)% utilization.",
            required=False,
        )
        wns = self.results.get("wns")
        worst_slack = self.results.get("worst_slack")
        if wns is not None and worst_slack is not None and len(ss.clocks) == 1:
            worst_slack = try_convert(worst_slack, float)
            assert worst_slack
            assert ss.main_clock
            self.results["Fmax"] = 1000.0 / (ss.main_clock.period - worst_slack)
        violations = try_convert(self.results.get("setup_violations"), int)
        if violations:
            log.error("%d setup violations.", violations)
            success = False
        violations = try_convert(self.results.get("hold_violations"), int)
        if violations:
            log.error("%d hold violations.", violations)
            success = False
        return success
