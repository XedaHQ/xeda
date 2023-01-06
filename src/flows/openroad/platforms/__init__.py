import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Type, TypeVar, Union

from importlib_resources import as_file, files

from ....dataclass import XedaBaseModelAllowExtra
from ....utils import toml_load

log = logging.getLogger(__name__)

PlatformType = TypeVar("PlatformType", bound="Platform")


class CornerSettings(XedaBaseModelAllowExtra):
    lib_files: List[str]
    dff_lib_file: Optional[str] = None
    voltage: float
    temperature: Optional[str] = None
    rcx_rules: Optional[str] = None


class Platform(XedaBaseModelAllowExtra):
    root_dir: Path
    name: str
    process: int
    corner: Dict[str, CornerSettings]
    default_corner: Optional[str]
    rcx_rc_corner: Optional[str] = None
    stackup: str
    tech_lef: str
    additional_lef_files: List[str] = []
    merged_lef: str
    gds_files: List[str] = []
    dont_use_cells: List[str] = []
    fill_cells: List[str]
    tiehi_cell: str
    tiehi_port: str
    tielo_cell: str
    tielo_port: str
    min_buf_cell: str
    min_buf_ports: List[str]
    latch_map_file: str
    clkgate_map_file: str
    adder_map_file: str
    abc_driver_cell: str
    abc_load_in_ff: int
    #
    place_site: str
    io_placer_h: str
    io_placer_v: str
    macro_place_halo: List[int]
    macro_place_channel: List[int]
    cell_pad_in_sites_global_placement: int
    cell_pad_in_sites_detail_placement: int
    place_density: float
    cell_pad_in_sites: int
    cts_buf_cell: str
    min_routing_layer: str
    max_routing_layer: str
    ir_drop_layer: str
    pwr_nets_voltages: Dict[str, float] = {}
    gnd_nets_voltages: Dict[str, float] = {}
    klayout_tech_file: str
    klayout_display_file: str
    fill_config: str
    template_pga_cfg: Optional[str]
    tapcell_tcl: Optional[str]
    pdn_tcl: str
    fastroute_tcl: Optional[str]
    derate_tcl: Optional[str]
    setrc_tcl: Optional[str]
    make_tracks_tcl: Optional[str]
    gds_layer_map: Optional[str]

    @classmethod
    def from_toml(cls: Type[PlatformType], platform_toml: Union[str, os.PathLike]) -> PlatformType:
        path = Path(platform_toml)
        kv = toml_load(path)
        return cls(root_dir=path.parent, **kv)

    @classmethod
    def from_resource(cls: Type[PlatformType], name: str) -> PlatformType:
        res = files(__package__).joinpath(name, "config.toml")
        with as_file(res) as path:
            return cls.from_toml(path)
