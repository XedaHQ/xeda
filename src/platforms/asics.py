import logging
from pathlib import Path
from typing import Dict, List, Optional

if __name__ == "__main__":
    __package__ = "xeda.flows.openroad.platforms"

from ..dataclass import XedaBaseModel, root_validator
from ..utils import first_key, first_value
from .platform import Platform

log = logging.getLogger(__name__)

DEFAULT_CORNER_FALLBACK = "tt"


class CornerSettings(XedaBaseModel):
    lib_files: List[Path]
    dff_lib_file: Optional[Path] = None
    rcx_rules: Optional[Path] = None
    voltage: float
    temperature: Optional[str] = None


class AsicsPlatform(Platform):
    root_dir: Path
    name: str
    process: int
    corner: Dict[str, CornerSettings]
    default_corner: str
    rcx_rc_corner: Optional[str] = None
    stackup: str
    dont_use_cells: List[str] = []
    fill_cells: List[str]
    tiehi_cell: str
    tiehi_port: str
    tielo_cell: str
    tielo_port: str
    min_buf_cell: str
    min_buf_ports: List[str]
    abc_driver_cell: str
    abc_load_in_ff: int
    place_site: str
    io_placer_h: str
    io_placer_v: str
    macro_place_halo: List[int]
    macro_place_channel: List[int]
    cell_pad_in_sites_global_placement: int
    cell_pad_in_sites_detail_placement: int
    cell_pad_in_sites: Optional[int] = None
    place_density: float
    cts_buf_cell: str
    min_routing_layer: str
    max_routing_layer: str
    via_in_pin_min_layer: Optional[str]
    via_in_pin_max_layer: Optional[str]
    ir_drop_layer: str
    pwr_nets_voltages: Dict[str, float] = {}
    gnd_nets_voltages: Dict[str, float] = {}
    latch_map_file: Path
    clkgate_map_file: Path
    adder_map_file: Path
    tech_lef: Path
    additional_lef_files: List[Path] = []
    std_cell_lef: Path
    gds_files: List[Path] = []
    derate_tcl: Optional[Path]
    setrc_tcl: Path
    fill_config: Path
    tapcell_tcl: Optional[Path]
    pdn_tcl: Path
    fastroute_tcl: Optional[Path]
    make_tracks_tcl: Optional[Path]
    template_pga_cfg: Optional[Path]
    gds_layer_map: Optional[Path]
    klayout_tech_file: Optional[Path]
    klayout_layer_prop_file: Optional[Path]

    @root_validator(pre=True)
    def _root_validator(cls, values):
        """
        migrate corner values from root to default corner
        """
        log.debug("AsicsPlatform.root_validator: values=%s", str(values))
        corners = values.get("corner")
        default_corner = values.get("default_corner")
        if not corners:
            corner_settings = {}
            for k in CornerSettings.__fields__.keys():
                if k in values:
                    corner_settings[k] = values.pop(k)
            if not default_corner:
                default_corner = DEFAULT_CORNER_FALLBACK
            values["corner"] = {default_corner: corner_settings}
        elif not default_corner:
            default_corner = first_key(corners)
        values["default_corner"] = default_corner
        return values

    @property
    def default_corner_settings(self):
        if self.default_corner:
            return self.corner.get(self.default_corner)
        return first_value(self.corner)


if __name__ == "__main__":
    print(AsicsPlatform.schema_json(indent=2))
