import logging
from pathlib import Path
import re
from typing import Dict, List, Optional

from pydantic import Field, validator

if __name__ == "__main__":
    import xeda

    __package__ = "xeda.platforms"

from ..dataclass import XedaBaseModel, root_validator
from ..utils import first_key, first_value
from .platform import Platform

log = logging.getLogger(__name__)

DEFAULT_CORNER_FALLBACK = "tt"


class CornerSettings(XedaBaseModel):
    lib_files: List[Path]
    dff_lib_file: Optional[Path] = None
    rcx_rules: Optional[Path] = None
    voltage: Optional[float] = None
    temperature: Optional[str] = None


class AsicsPlatform(Platform):
    process: Optional[float] = None
    corner: Dict[str, CornerSettings]
    default_corner: str
    time_unit: str = "1ns"
    rcx_rc_corner: Optional[str] = None
    stackup: Optional[str] = None
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
    cts_buf_distance: Optional[float] = None
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
    std_cell_lef: Path = Field(alias="sc_lef")
    derate_tcl: Optional[Path]
    setrc_tcl: Path = Field(alias="set_rc_tcl")
    fill_config: Optional[Path] = None
    tapcell_tcl: Optional[Path] = None
    tapcell_name: Optional[str] = Field(None, alias="tap_cell_name")
    pdn_tcl: Path
    fastroute_tcl: Optional[Path]
    make_tracks_tcl: Optional[Path] = Field(alias="make_tracks")
    template_pga_cfg: Optional[Path]
    gds_files: List[Path] = []
    gds_layer_map: Optional[Path]
    gds_allow_empty: List[str] = []
    klayout_tech_file: Optional[Path]
    klayout_layer_prop_file: Optional[Path]

    @root_validator(pre=True)
    def _root_validator(cls, values):
        # log.debug("AsicsPlatform.root_validator: values=%s", str(values))
        for k in ["gds_files"]:
            v = values.get(k)
            if v is not None and not isinstance(v, (list)):
                values[k] = [v]

        # migrate corner values **from root** to the default corner
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
        else:
            # copy CornerSettings values in root to each corner
            if not default_corner:
                default_corner = first_key(corners)
            for k in CornerSettings.__fields__.keys():
                if k in values:
                    v = values.pop(k)
                    assert isinstance(corners, dict), "expecting ``corners to be a dict"
                    for corner_name in corners.keys():
                        cs = corners[corner_name]
                        if k not in cs:
                            cs[k] = v

        values["default_corner"] = default_corner
        return values

    @validator("pwr_nets_voltages", always=True, pre=True)
    def _validate_nets_voltages(cls, value, values):
        corners = values.get("corner")
        if corners and value and isinstance(value, dict):
            default_corner = values.get("default_corner")
            selected_corner = (
                corners.get(default_corner) if default_corner else first_value(corners)
            )
            assert selected_corner, "no corner selected"
            if not isinstance(selected_corner, CornerSettings):
                assert isinstance(selected_corner, dict)
                selected_corner = CornerSettings(**selected_corner)
            for k in value.keys():
                v = value[k]
                if isinstance(v, str):
                    if selected_corner.voltage is not None:
                        v = re.sub(r"\$\(?(\w*)\)?", lambda pat: pat.group(1).lower(), v)
                    value[k] = float(eval(v, selected_corner.dict()))
        return value

    @property
    def default_corner_settings(self):
        selected_corner = None
        if self.default_corner:
            selected_corner = self.corner.get(self.default_corner)
        if selected_corner is None:
            selected_corner = first_value(self.corner)
        assert selected_corner is not None
        return selected_corner


if __name__ == "__main__":
    print(AsicsPlatform.schema_json(indent=2))
