import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...dataclass import Field, validator
from ...flow import Flow
from ...flows.ghdl import GhdlSynth
from ...utils import try_convert

log = logging.getLogger(__name__)


def append_flag(flag_list: List[str], flag: str) -> List[str]:
    if flag not in flag_list:
        flag_list.append(flag)
    return flag_list


class YosysBase(Flow):
    """Synthesize the design using Yosys Open SYnthesis Suite"""

    class Settings(Flow.Settings):
        log_file: Optional[str] = "yosys.log"
        flatten: bool = Field(True, description="flatten design")
        read_verilog_flags: List[str] = [
            "-noautowire",
            "-sv",
        ]
        read_systemverilog_flags: List[str] = []
        check_assert: bool = True
        rtl_verilog: Optional[str] = None  # "rtl.v"
        rtl_vhdl: Optional[str] = None  # "rtl.vhdl"
        rtl_json: Optional[str] = None  # "rtl.json"
        show_rtl: bool = False
        show_rtl_flags: List[str] = [
            "-stretch",
            "-enum",
            "-width",
        ]
        ghdl: GhdlSynth.Settings = GhdlSynth.Settings()  # pyright: ignore
        verilog_lib: List[str] = []
        splitnets: bool = True
        splitnets_driver: bool = False
        set_attribute: Dict[str, Any] = {}
        prep: Optional[List[str]] = None
        keep_hierarchy: List[str] = []
        defines: Dict[str, Any] = {}
        black_box: List[str] = []
        top_is_vhdl: Optional[bool] = Field(
            None,
            description="set to `true` to specify top module is VHDL, or `false` to override detection based on last source.",
        )

        @validator("verilog_lib", pre=True)
        def validate_verilog_lib(cls, value):
            if isinstance(value, str):
                value = [value]
            value = [str(Path(v).resolve(strict=True)) for v in value]
            return value

        @validator("set_attribute", pre=True, always=True)
        def validate_set_attributes(cls, value):
            if value:
                if isinstance(value, (str, Path)):
                    attr_file = Path(value)
                    if attr_file.suffix.endswith(".json"):
                        try:
                            log.info("Parsing %s as JSON file", attr_file)
                            with open(attr_file) as f:
                                value = {**json.load(f)}
                        except json.JSONDecodeError as e:
                            raise ValueError(
                                f"Decoding of JSON file {attr_file} failed: {e.args}"
                            ) from e
                        except TypeError as e:
                            raise ValueError(f"JSON TypeError: {e.args}") from e
                    else:
                        raise ValueError(f"Unsupported extension for JSON file: {value}")
                for attr, attr_val in value.items():
                    assert attr
                    if isinstance(attr_val, (dict)):
                        new_dict = {}
                        for (path, v) in attr_val.items():
                            assert path and v
                            if isinstance(path, list):
                                path = "/".join(path)
                            if isinstance(v, str):
                                v1 = try_convert(v, int)
                                if v1 is None:
                                    v = f'"{v}"'
                                else:
                                    v = v1
                            new_dict[path] = v
                        value[attr] = {**new_dict}
                    elif isinstance(attr_val, (str)):
                        v1 = try_convert(attr_val, int)
                        if v1 is None:
                            value[attr] = f'"{attr_val}"'
                        else:
                            value[attr] = v1
            return value


def process_parameters(parameters: Dict[str, Any]) -> Dict[str, str]:
    out = dict()
    for k, v in parameters.items():
        if isinstance(v, bool):
            v = f"1'b{int(v)}"
        elif isinstance(v, str) and not re.match(r"\d+'b[01]+", v):
            v = '\\"' + v + '\\"'
        out[k] = str(v)
    return out
