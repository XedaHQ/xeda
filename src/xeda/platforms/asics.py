import logging
from pathlib import Path
import re
from typing import Dict, List, Optional

from pydantic import Field, validator
from simpleeval import simple_eval

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
    default_corner: str = DEFAULT_CORNER_FALLBACK
    time_unit: str = "1ns"
    rcx_rc_corner: Optional[str] = None
    stackup: Optional[str] = None
    dont_use_cells: List[str] = []
    fill_cells: List[str] = []
    tiehi_cell_and_port: List[str] = []
    tielo_cell_and_port: List[str] = []
    tiehi_cell: Optional[str] = None
    tiehi_port: Optional[str] = None
    tielo_cell: Optional[str] = None
    tielo_port: Optional[str] = None
    min_buf_cell_and_ports: List[str] = []
    min_buf_cell: Optional[str] = None
    min_buf_ports: List[str] = []
    abc_driver_cell: Optional[str] = None
    abc_load_in_ff: Optional[int] = None
    max_ungroup_size: Optional[int] = None
    place_site: Optional[str] = None
    io_placer_h: Optional[str] = None
    io_placer_v: Optional[str] = None
    macro_place_halo: List[int] = []
    macro_place_channel: List[int] = []
    cell_pad_in_sites_global_placement: Optional[int] = None
    cell_pad_in_sites_detail_placement: Optional[int] = None
    cell_pad_in_sites: Optional[int] = None
    place_density: Optional[float] = None
    cts_buf_cell: Optional[str] = None
    cts_buf_distance: Optional[float] = None
    min_routing_layer: Optional[str] = None
    max_routing_layer: Optional[str] = None
    via_in_pin_min_layer: Optional[str] = None
    via_in_pin_max_layer: Optional[str] = None
    ir_drop_layer: Optional[str] = None
    pwr_nets_voltages: Dict[str, float] = {}
    gnd_nets_voltages: Dict[str, float] = {}
    latch_map_file: Optional[Path] = None
    clkgate_map_file: Optional[Path] = None
    adder_map_file: Optional[Path] = None
    tech_lef: Optional[Path] = None
    additional_lef_files: List[Path] = []
    std_cell_lef: Path = Field(alias="sc_lef")
    derate_tcl: Optional[Path] = None
    setrc_tcl: Optional[Path] = Field(default=None, alias="set_rc_tcl")
    fill_config: Optional[Path] = None
    tapcell_tcl: Optional[Path] = None
    tapcell_name: Optional[str] = Field(None, alias="tap_cell_name")
    pdn_tcl: Optional[Path] = None
    fastroute_tcl: Optional[Path] = None
    make_tracks_tcl: Optional[Path] = Field(alias="make_tracks")
    cdl_file: Optional[Path] = None
    template_pga_cfg: Optional[Path] = None
    gds_files: List[Path] = []
    gds_layer_map: Optional[Path] = None
    gds_allow_empty: List[str] = []
    klayout_tech_file: Optional[Path] = None
    klayout_drc_file: Optional[Path] = None
    klayout_lvs_file: Optional[Path] = None
    klayout_layer_prop_file: Optional[Path] = None

    @validator(
        "tiehi_cell",
        "tielo_cell",
        "tiehi_port",
        "tielo_port",
        "min_buf_cell",
        "min_buf_ports",
        pre=True,
        always=True,
    )
    def _validate_cell_port(cls, value, values, field):
        sp = field.name.rsplit("_", 1)
        suffix = "_cell_and_port"
        if sp[0] == "min_buf":
            suffix = "_cell_and_ports"
        cnp = sp[0] + suffix
        cnp_val = values.get(cnp)
        if not value and cnp_val:
            if sp[1] == "cell":
                return cnp_val[0]
            if len(cnp_val) > 2:
                return cnp_val[1:]
            return cnp_val[1]
        return value

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

    @validator("pwr_nets_voltages", "gnd_nets_voltages", always=True, pre=True)
    def _validate_nets_voltages(cls, value, values, field):
        if isinstance(value, str):
            sp = value.split()
            value = {sp[2 * i]: float(sp[2 * i + 1]) for i in range(len(sp) // 2)}

        if field.name.startswith("pwr"):
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
                        print(selected_corner.dict())
                        print(v)
                        value[k] = float(simple_eval(v, names=selected_corner.dict()))
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
