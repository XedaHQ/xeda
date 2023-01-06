import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from importlib_resources import files, as_file
from pydantic import Field, validator

from ...flow import AsicSynthFlow
from ...flows.yosys import HiLoMap, YosysSynth
from ...tool import Tool
from ...utils import unique, first_value
from ...design import SourceType
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
    pattern = r"(^\s*cell\s*\(\s*([\"]*" + '["]*|["]*'.join(pattern_list) + r"[\"]*)\)\s*\{)"
    replace = r"\1\n    dont_use : true;"
    content, count = re.subn(pattern, replace, content, 0, re.M)
    log.info("Marked %d cells as dont_use", count)

    # Yosys-abc throws an error if original_pin is found within the liberty file.
    pattern = r"(.*original_pin.*)"
    replace = r"/* \1 */;"
    content, count = re.subn(pattern, replace, content)
    log.info("Commented %d lines containing 'original_pin", count)

    # Yosys, does not like properties that start with : !, without quotes
    pattern = r":\s+(!.*)\s+;"
    replace = r': "\1" ;'
    content, count = re.subn(pattern, replace, content)
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


def preproc_libs(in_files, new_lib_name, merged_file, dont_use_cells: List[str]):
    merged_content = ""
    proc_files = []
    for in_file in in_files:
        in_file = Path(in_file)
        log.info("Pre-processing liberty file: %s", in_file)
        with open(in_file, encoding="utf-8") as f:
            content = clean_ascii(f.read())
        merged_content += preproc_lib_content(content, dont_use_cells)
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


def replace_abs_paths(p: Dict[str, Any], root_dir: Path, p_path_fields=[], p_path_list_fields=[]):
    for f in p_path_fields:
        v = p.get(f)
        if v and isinstance(v, str) and not os.path.isabs(v):
            p[f] = str(root_dir / v)
    for f in p_path_list_fields:
        vl = p.get(f)
        if vl and isinstance(vl, (list, tuple)):
            new_vl = []
            for v in vl:
                if v and isinstance(v, str) and not os.path.isabs(v):
                    v = str(root_dir / v)
                new_vl.append(v)
            p[f] = new_vl
    return p


def dict_to_str(d, kv_sep=" ", sep=" ", val_fmt=lambda v: str(v)):
    return sep.join(f"{k}{kv_sep}{val_fmt(v)}" for k, v in d.items())


class OpenROAD(AsicSynthFlow):
    merged_lib_file = "merged.lib"

    class Settings(AsicSynthFlow.Settings):
        platform: Platform
        core_utilization: float = Field(40.0, description="Core utilization in percent (0..100)")
        core_aspect_ratio: float = Field(1.0, description="Core height / core width")
        core_margin: float = Field(2.0, description="Core margin in um")
        core_area: List[Union[int, float]] = []
        die_area: List[Union[int, float]] = []
        sdc_files: List[str] = []
        floorplan_def: Optional[str] = None
        floorplan_tcl: Optional[str] = None
        io_constraints: Optional[str] = None
        place_density: Union[None, float, str] = None
        log_file: Optional[str] = Field("openroad.log", description="write log")
        optimize: Literal["speed", "area"] = Field("area", description="Optimization target")
        write_metrics: Optional[str] = Field(
            "metrics.json", description="write metrics in file in JSON format"
        )
        exit: bool = Field(True, description="exit after completion")
        gui: bool = Field(False, description="start in gui mode")
        place_density_lb_addon: Optional[float] = None
        nthreads: Optional[int] = Field(None, description="Number of threads to use. If none/0, use max available.")  # type: ignore
        dont_use_cells: List[str] = []
        place_pins_args: List[str] = []
        blocks: List[str] = []
        use_fill: bool = False
        copy_platform_files: bool = False

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
        yosys_settings = YosysSynth.Settings()  # pyright: ignore

        if not ss.copy_platform_files:
            p_path_fields = [
                "tech_lef",
                "merged_lef",
                "latch_map_file",
                "clkgate_map_file",
                "adder_map_file",
                "pdn_tcl",
                "tapcell_tcl",
                "fastroute_tcl",
                "klayout_tech_file",
                "klayout_display_file",
                "fill_config",
                "template_pga_cfg",
                "gds_layer_map",
                "setrc_tcl",
                "derate_tcl",
            ]
            p_path_list_fields = ["additional_lef_files", "gds_files"]
            root_dir = ss.platform.root_dir

            p = ss.platform.dict()
            p = replace_abs_paths(p, root_dir, p_path_fields, p_path_list_fields)

            for corer_name, corner in ss.platform.corner.items():
                new_corner = corner.dict()
                pc_path_fields = ["rcx_rules", "dff_lib_file"]
                pc_path_list_fields = ["lib_files"]
                new_corner = replace_abs_paths(
                    new_corner, root_dir, pc_path_fields, pc_path_list_fields
                )
                p["corner"][corer_name] = new_corner

            ss.platform = Platform(**p)
        else:
            files_to_copy: List[str] = []
            for _, corner in ss.platform.corner.items():
                files_to_copy += corner.lib_files
                if corner.dff_lib_file:
                    files_to_copy.append(corner.dff_lib_file)
            files_to_copy.append(ss.platform.tech_lef)
            if ss.platform.merged_lef:
                files_to_copy.append(ss.platform.merged_lef)
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
        default_corner = (
            ss.platform.corner.get(ss.platform.default_corner)
            if ss.platform.default_corner
            else first_value(ss.platform.corner)
        )
        assert default_corner
        dff_lib_file = default_corner.dff_lib_file
        orig_libs = unique(orig_libs)
        for lib in orig_libs:
            src = ss.platform.root_dir / lib
            dst = my_lib_dir / src.name
            shutil.copy(src, dst)
        preproc_libs(
            orig_libs,
            f"{ss.platform.name}_merged",
            self.merged_lib_file,
            ss.platform.dont_use_cells,
        )
        yosys_libs = [self.merged_lib_file]
        if dff_lib_file:
            yosys_settings.dff_liberty = os.path.join(YosysSynth.copied_resources_dir, dff_lib_file)
        copy_resources += yosys_libs
        yosys_settings.liberty = [
            str(os.path.join(YosysSynth.copied_resources_dir, os.path.basename(res)))
            for res in yosys_libs
        ]
        yosys_settings.clocks = ss.clocks
        yosys_settings.abc_constr = [
            f"set_driving_cell {ss.platform.abc_driver_cell}",
            f"set_load {ss.platform.abc_load_in_ff}",
        ]
        yosys_settings.abc_script = abc_opt_script(ss.optimize)
        ###
        yosys_settings.adder_map = ss.platform.adder_map_file
        yosys_settings.clockgate_map = ss.platform.clkgate_map_file
        yosys_settings.other_maps = [ss.platform.latch_map_file]
        yosys_settings.flatten = True
        yosys_settings.hilomap = HiLoMap(
            hi=(ss.platform.tiehi_cell, ss.platform.tiehi_port),
            lo=(ss.platform.tielo_cell, ss.platform.tielo_port),
        )
        yosys_settings.insbuf = (
            ss.platform.min_buf_cell,
            ss.platform.min_buf_ports[0],
            ss.platform.min_buf_ports[1],
        )
        yosys_settings.black_box = ss.blocks
        yosys_settings.post_synth_opt = False
        self.add_dependency(YosysSynth, yosys_settings, copy_resources)

    def run(self):
        assert isinstance(self.settings, self.Settings)
        ss = self.settings
        jobs_dir = Path("results")
        jobs_dir.mkdir(exist_ok=True, parents=True)

        yosys_dep = self.pop_dependency(YosysSynth)
        netlist = yosys_dep.artifacts.netlist_verilog
        if not os.path.isabs(netlist):
            netlist = os.path.join(yosys_dep.run_path, netlist)
        synth_netlist = jobs_dir / "1_synth.v"
        shutil.copy(netlist, synth_netlist)

        # yosys doesn't support SDC so we generate it here
        clocks_sdc = self.copy_from_template("clocks.sdc")
        sdc_files = []
        sdc_files += [str(s) for s in self.design.sources_of_type(SourceType.Sdc)]
        sdc_files.append(str(clocks_sdc))
        sdc_files += ss.sdc_files
        merged_sdc_file = jobs_dir / "1_synth.sdc"
        merge_files(sdc_files, merged_sdc_file)

        openroad = Tool("openroad", self, version_arg="-version")

        if not self.design.rtl.clocks:
            log.critical(
                "No clocks specified for top RTL design. Continuing with synthesis anyways."
            )
        all_lib_files: List[str] = []
        for _, corner in ss.platform.corner.items():
            all_lib_files += corner.lib_files
        default_corner = (
            ss.platform.corner.get(ss.platform.default_corner)
            if ss.platform.default_corner
            else first_value(ss.platform.corner)
        )
        assert default_corner

        sd = files(__package__).joinpath("openroad_scripts")
        with as_file(sd) as scripts_dir:
            utils_dir = scripts_dir / "utils"
            assert self.design.rtl.top, "rtl.top must be set"
            rc_corner_name = ss.platform.rcx_rc_corner or ""

            env = dict(
                SCRIPTS_DIR=scripts_dir,
                RESULTS_DIR=jobs_dir,
                OBJECTS_DIR=jobs_dir,
                REPORTS_DIR=jobs_dir,
                UTILS_DIR=utils_dir,
                DESIGN_NAME=self.design.rtl.top or self.design.name,
                CLOCK_PORT=self.design.rtl.clock_port or "",
                CLOCK_PERIOD=f"{ss.main_clock.period_ps:.1f}" if ss.main_clock else "",
                PLATFORM=ss.platform.name,
                PLATFORM_DIR=ss.platform.root_dir,
                PLACE_SITE=ss.platform.place_site or "",
                SC_LEF=ss.platform.merged_lef or "",
                TECH_LEF=ss.platform.tech_lef,
                ADDITIONAL_LEFS=" ".join(ss.platform.additional_lef_files),
                LIB_FILES=" ".join(all_lib_files),
                DONT_USE_SC_LIB=self.merged_lib_file,
                DONT_USE_CELLS=" ".join(ss.platform.dont_use_cells + ss.dont_use_cells),
                CLKGATE_MAP_FILE=ss.platform.clkgate_map_file,
                LATCH_MAP_FILE=ss.platform.latch_map_file,
                GDS_FILES=" ".join(sorted(ss.platform.gds_files)),
                IO_PLACER_H=ss.platform.io_placer_h,
                IO_PLACER_V=ss.platform.io_placer_v,
                SDC_FILE=merged_sdc_file,  # expects a single SDC file
                CORE_UTILIZATION=f"{ss.core_utilization:.1f}",
                CORE_MARGIN=f"{ss.core_margin:.3}",
                CORE_ASPECT_RATIO=f"{ss.core_aspect_ratio:.1f}",
                PLACE_PINS_ARGS=" ".join(ss.place_pins_args),
                GPL_ROUTABILITY_DRIVEN=1,
                GPL_TIMING_DRIVEN=1,
                GDS_LAYER_MAP=ss.platform.gds_layer_map or "",
                ABC_AREA=0,  # TODO ???
                NUM_CORES=ss.nthreads or openroad.nproc,
                PDN_TCL=ss.platform.pdn_tcl,
                MIN_ROUTING_LAYER=ss.platform.min_routing_layer,
                MAX_ROUTING_LAYER=ss.platform.max_routing_layer,
                PLACE_DENSITY=ss.place_density or f"{ss.platform.place_density:.2f}",
                CELL_PAD_IN_SITES_GLOBAL_PLACEMENT=ss.platform.cell_pad_in_sites_global_placement,
                CELL_PAD_IN_SITES_DETAIL_PLACEMENT=ss.platform.cell_pad_in_sites_detail_placement,
                CELL_PAD_IN_SITES=ss.platform.cell_pad_in_sites,
                TIEHI_CELL_AND_PORT=f"{ss.platform.tiehi_cell} {ss.platform.tiehi_port}",
                TIELO_CELL_AND_PORT=f"{ss.platform.tielo_cell} {ss.platform.tielo_port}",
                MIN_BUF_CELL_AND_PORTS=f"{ss.platform.min_buf_cell} {' '.join(ss.platform.min_buf_ports)}",
                CTS_BUF_CELL=ss.platform.cts_buf_cell,
                USE_FILL=ss.use_fill or "",
                FILL_CELLS=" ".join(ss.platform.fill_cells),
                FILL_CONFIG=ss.platform.fill_config,
                TAPCELL_TCL=ss.platform.tapcell_tcl,
                MACRO_PLACE_HALO=" ".join(str(e) for e in ss.platform.macro_place_halo),
                MACRO_PLACE_CHANNEL=" ".join(str(e) for e in ss.platform.macro_place_channel),
                PWR_NETS_VOLTAGES=dict_to_str(
                    ss.platform.pwr_nets_voltages, val_fmt=lambda v: f"{v:0.1}"
                ),
                GND_NETS_VOLTAGES=dict_to_str(
                    ss.platform.gnd_nets_voltages, val_fmt=lambda v: f"{v:0.1}"
                ),
                RCX_RC_CORNER=rc_corner_name,
                RCX_RULES=(
                    ss.platform.corner[rc_corner_name] if rc_corner_name else default_corner
                ).rcx_rules,
                FASTROUTE_TCL=ss.platform.fastroute_tcl,
                KLAYOUT_TECH_FILE=ss.platform.klayout_tech_file or "",
                KLAYOUT_DISPLAY_FILE=ss.platform.klayout_display_file or "",
                MAKE_TRACKS=ss.platform.make_tracks_tcl,
                CORE_AREA=" ".join(
                    f"{a:.3}" if isinstance(a, float) else str(a) for a in ss.core_area
                ),
                DIE_AREA=" ".join(
                    f"{a:.3}" if isinstance(a, float) else str(a) for a in ss.die_area
                ),
                FLOORPLAN_DEF=ss.floorplan_def,
                FLOORPLAN_TCL=ss.floorplan_tcl,
                PLACE_DENSITY_LB_ADDON=f"{ss.place_density_lb_addon:.3f}"
                if ss.place_density_lb_addon
                else None,
                HAS_IO_CONSTRAINTS=1 if ss.io_constraints is not None else 0,
                IO_CONSTRAINTS=ss.io_constraints,
            )
            if len(ss.platform.corner) > 1:
                env["CORNERS"] = " ".join(ss.platform.corner.keys())
            # env["FOOTPRINT_TCL"] =
            # env["FOOTPRINT"] =
            # env["SIG_MAP_FILE"] =
            # env["IO_CONSTRAINTS"] =
            # env["MACRO_PLACEMENT"] =
            # env["RTLMP_CONFIG_FILE"] =
            # env["POST_FLOORPLAN_TCL"] =
            # env["RESYNTH_TIMING_RECOVER"] =
            # env["RESYNTH_AREA_RECOVER"] =

            cmd_file = self.copy_from_template(
                "orflow.tcl",
                lstrip_blocks=True,
                trim_blocks=True,
                sdc_files=sdc_files,
                netlist=synth_netlist,
                platform=ss.platform,
            )

            args = ["-no_splash", "-no_init", "-threads", ss.nthreads or "max"]
            if ss.exit:
                args.append("-exit")
            if ss.gui:
                args.append("-gui")
            if ss.log_file:
                args += ["-log", ss.log_file]

            openroad.run(*args, cmd_file, env=env)
