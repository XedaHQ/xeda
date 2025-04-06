import logging
import re
from abc import ABCMeta
from functools import cached_property, reduce
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from xml.etree import ElementTree

import colorama

from ...dataclass import Field
from ...design import Design
from ...flow import Flow, FpgaSynthFlow, SynthFlow
from ...tool import Docker, Tool

log = logging.getLogger(__name__)

all = [
    "Vivado",
    "VivadoSynthSettings",
    "VivadoTool",
]


def vivado_generics(is_sim_flow: bool):
    def vivado_generics_to_str(kvdict) -> str:
        def supported_vivado_generic(v):
            return v is not None

        def value_to_str(v):
            if is_sim_flow:
                return v
            if v is False:
                return "1\\'b0"
            if v is True:
                return "1\\'b1"
            if isinstance(v, str):
                return f'\\"{v}\\"'
            return str(v).strip()

        return " ".join(
            [
                f"-generic{'_top' if is_sim_flow else ''} {{{k}={value_to_str(v)}}}"
                for k, v in kvdict.items()
                if supported_vivado_generic(v)
            ]
        )

    return vivado_generics_to_str


def vivado_defines(is_sim_flow: bool):
    def defines_to_str(mapping) -> str:
        def value_to_str(v):
            if is_sim_flow:
                return v
            if v is False:
                return "1\\'b0"
            if v is True:
                return "1\\'b1"
            if isinstance(v, str):
                return f'\\"{v}\\"'
            return str(v).strip()

        return " ".join(
            [
                f"-define {k}" + ("" if v is None else f"={value_to_str(v)}")
                for k, v in mapping.items()
            ]
        )

    return defines_to_str


class VivadoTool(Tool):
    executable: str = "vivado"
    docker: Optional[Docker] = Docker(
        image="siliconbootcamp/xilinx-vivado",
        command=["/tools/Xilinx/Vivado/2021.1/bin/vivado"],
        tag="stable",
    )  # pyright: ignore
    highlight_rules: Optional[Dict[str, str]] = {
        r"^(ERROR:)(.+)$": colorama.Fore.RED + colorama.Style.BRIGHT + r"\g<0>",
        r"^(CRITICAL WARNING:)(.+)$": colorama.Fore.RED + r"\g<1>" + r"\g<2>",
        r"^(WARNING:)(.+)$": colorama.Fore.YELLOW
        + colorama.Style.BRIGHT
        + r"\g<1>"
        + colorama.Style.NORMAL
        + r"\g<2>",
        r"^(INFO:)(.+)$": colorama.Fore.GREEN
        + colorama.Style.BRIGHT
        + r"\g<1>"
        + colorama.Style.NORMAL
        + r"\g<2>",
        r"^(====[=]+\()(.*)(\)[=]+====)$": colorama.Fore.BLUE
        + r"\g<1>"
        + colorama.Fore.CYAN
        + r"\g<2>"
        + colorama.Fore.BLUE
        + r"\g<3>",
    }

    @cached_property
    def version(self) -> Tuple[str, ...]:
        out = self.run_get_stdout(
            "-version",
        )
        assert isinstance(out, str)
        so = re.split(r"\s+", out)
        version_string = so[1] if len(so) > 1 else so[0] if len(so) > 0 else ""
        return tuple(version_string.split("."))


class Vivado(Flow, metaclass=ABCMeta):
    """Xilinx (AMD) Vivado FPGA synthesis and simulation flows"""

    class Settings(Flow.Settings):
        tcl_shell: bool = Field(
            False,
            description="Drop to interactive TCL shell after Vivado finishes running a flow script",
        )
        no_log: bool = False
        suppress_msgs: List[str] = [
            "Vivado 12-7122",  # Auto Incremental Compile: No reference checkpoint was found in run
        ]

    def __init__(self, settings: Settings, design: Design, run_path: Path):
        super().__init__(settings, design, run_path)
        assert isinstance(self.settings, self.Settings)
        default_args = [
            "-nojournal",
            "-mode",
            "tcl" if self.settings.tcl_shell else "batch",
        ]
        if not self.settings.debug:
            default_args.append("-notrace")
        if self.settings.verbose:
            default_args.append("-verbose")
        if self.settings.no_log:
            default_args.append("-nolog")
        self.vivado = VivadoTool(
            default_args=default_args,
            design_root=self.design_root,
        )  # pyright: ignore
        if self.settings.redirect_stdout:
            self.vivado.redirect_stdout = Path(f"{self.name}_stdout.log")
        self.add_template_filter(
            "vivado_generics", vivado_generics(not isinstance(self, SynthFlow))
        )

        self.add_template_filter(  # defines to an option string
            "vivado_defines", vivado_defines(not isinstance(self, SynthFlow))
        )

    @staticmethod
    def parse_xml_report(report_xml) -> Optional[Dict[str, Any]]:
        try:
            tree = ElementTree.parse(report_xml)
        except FileNotFoundError:
            log.critical("File %s not found.", report_xml)
            return None
        except ElementTree.ParseError as e:
            log.critical("Parsing %s failed: %s", report_xml, e.msg)
            return None
        data = {}
        for section in tree.findall("./section"):
            section_title = section.get("title", "<section>")
            for table in section.findall("./table"):
                table_data = {}
                header = [
                    unescape(col.attrib["contents"]).strip()
                    for col in table.findall("./tablerow/tableheader")
                ]
                for tablerow in table.findall("./tablerow"):
                    cells = [
                        unescape(cell.attrib["contents"]).strip()
                        for cell in tablerow.findall("./tablecell")
                    ]
                    if cells:
                        # choose 0th element as "index data" (distinct key)
                        cell_data = {h: c for h, c in zip(header[1:], cells[1:]) if c}
                        if cell_data:
                            table_data[cells[0]] = cell_data
                if table_data:
                    table_title = table.get("title")
                    title = section_title + ":" + table_title if table_title else section_title
                    data[title] = table_data
        return data

    @staticmethod
    def get_from_path(dct: dict, path):
        if isinstance(path, str):
            path = path.split(".")
        return reduce(dict.__getitem__, path, dct)
