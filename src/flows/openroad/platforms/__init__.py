import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

from importlib_resources import as_file, files

if __name__ == "__main__":
    __package__ = "xeda.flows.openroad.platforms"

from ....dataclass import XedaBaseModel
from ....utils import toml_load, first_value

log = logging.getLogger(__name__)


class CornerSettings(XedaBaseModel):
    lib_files: List[Path]
    dff_lib_file: Optional[Path] = None
    rcx_rules: Optional[Path] = None
    voltage: float
    temperature: Optional[str] = None


class Platform(XedaBaseModel):
    root_dir: Path
    name: str
    process: int
    default_corner: Optional[str]
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
    # files:
    corner: Dict[str, CornerSettings]
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

    @classmethod
    def create(cls: Type["Platform"], **kwargs) -> "Platform":
        if "corner" not in kwargs:
            corner_settings = {}
            for k in CornerSettings.__fields__.keys():
                if k in kwargs:
                    corner_settings[k] = kwargs.pop(k)
            default_corner = kwargs.get("default_corner", "tt")
            kwargs["default_corner"] = default_corner
            kwargs["corner"] = {default_corner: CornerSettings(**corner_settings)}
        return cls(**kwargs)

    @classmethod
    def from_toml(
        cls: Type["Platform"], platform_toml: Union[str, os.PathLike], overrides={}
    ) -> "Platform":
        path = Path(platform_toml)
        kv = {**toml_load(path), **overrides}
        return cls.create(root_dir=path.parent, **kv)

    @classmethod
    def from_resource(cls: Type["Platform"], name: str, overrides={}) -> "Platform":
        res = files(__package__).joinpath(name, "config.toml")
        with as_file(res) as path:
            return cls.from_toml(path, overrides)

    def with_absolute_paths(self) -> "Platform":
        rd = self.root_dir.absolute()

        def to_abs(v):
            if v and isinstance(v, Path) and not os.path.isabs(v):
                return rd / v
            return v

        def convert_rec(kv: dict[str, Any], exclude_keys=[]):
            for k, v in kv.items():
                if k in exclude_keys:
                    continue
                if isinstance(v, dict):
                    v = convert_rec(v)
                elif isinstance(v, (list, tuple)):
                    v = [to_abs(ve) for ve in v]
                else:
                    v = to_abs(v)
                kv[k] = v
            return kv

        return Platform(**convert_rec(self.dict(), exclude_keys=["root_dir"]))

    @property
    def default_corner_settings(self):
        if self.default_corner:
            return self.corner.get(self.default_corner)
        return first_value(self.corner)


if __name__ == "__main__":
    print(Platform.schema_json(indent=2))
