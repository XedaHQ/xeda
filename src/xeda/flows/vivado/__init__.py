import logging
import re
from abc import ABCMeta
from functools import cached_property, reduce
from html import unescape
from typing import Any, Dict, Optional, Tuple
from xml.etree import ElementTree

import colorama

from ...dataclass import Field
from ...design import Source, SourceType
from ...flow import (
    Flow,
    SynthFlow,
    describe_artifacts,
)
from ...tool import Docker, Tool
from ...utils import tcl_word

log = logging.getLogger(__name__)

SOURCE_FILE_TYPES = {
    SourceType.Verilog: "Verilog",
    SourceType.SystemVerilog: "SystemVerilog",
    SourceType.Vhdl: "VHDL",
    SourceType.VerilogHeader: "Verilog Header",
    SourceType.SVHeader: "Verilog Header",
    SourceType.MemoryFile: "Memory File",
    SourceType.Xdc: "XDC",
    SourceType.Sdc: "SDC",
    SourceType.Tcl: "TCL",
}


def vivado_file_type(source: Source) -> str:
    """Return the project file type of a source, whatever its file name extension."""
    return SOURCE_FILE_TYPES[SourceType(source.type)]


__all__ = [
    "Vivado",
    "VivadoTool",
]


def _vivado_value(v: Any, is_sim_flow: bool) -> str:
    """Return a parameter or define value as the tool reads it.

    For synthesis, a boolean becomes a one-bit literal and text gets double quotes
    (`G_STR="abc"`)."""
    if is_sim_flow:
        return str(v)
    if v is False:
        return "1'b0"
    if v is True:
        return "1'b1"
    if isinstance(v, str):
        return f'"{v}"'
    return str(v).strip()


def vivado_generics(is_sim_flow: bool):
    def vivado_generics_to_str(kvdict) -> str:
        """Render `-generic NAME=VALUE` for each parameter.
        The assignment is one literal Tcl word."""
        option = "-generic_top" if is_sim_flow else "-generic"
        return " ".join(
            f"{option} {tcl_word(f'{k}={_vivado_value(v, is_sim_flow)}')}"
            for k, v in kvdict.items()
            if v is not None
        )

    return vivado_generics_to_str


def vivado_defines(is_sim_flow: bool):
    def defines_to_str(mapping) -> str:
        """Render `-define NAME[=VALUE]` for each define.
        The definition is one literal Tcl word."""
        return " ".join(
            "-define " + tcl_word(k if v is None else f"{k}={_vivado_value(v, is_sim_flow)}")
            for k, v in mapping.items()
        )

    return defines_to_str


class VivadoTool(Tool):
    executable: str = "vivado"
    docker: Optional[Docker] = Docker(
        image="axemsolutions/vivado:2024.2",
        command=["vivado"],
        tag="stable",
        default_env={
            "LD_PRELOAD": "/lib/x86_64-linux-gnu/libudev.so.1:/lib/x86_64-linux-gnu/libz.so.1",
            "DISPLAY": "host.docker.internal:0",
        },
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
        r"^====[=]+\(\s*(WARN|WARNING):\s+(.*)\s*\)[=]+====$": colorama.Fore.YELLOW
        + colorama.Style.BRIGHT
        + r"XEDA WARNING: \g<2>",
        r"^====[=]+\(\s*ERROR:\s+(.*)\s*\)[=]+====$": colorama.Fore.RED
        + colorama.Style.BRIGHT
        + r"XEDA ERROR: \g<1>",
        r"^====[=]+\(\s*(.*)\s*\)[=]+====$": colorama.Fore.BLUE
        + colorama.Style.BRIGHT
        + "XEDA: "
        + colorama.Style.NORMAL
        + colorama.Fore.CYAN
        + r"\g<1>",
    }

    @cached_property
    def version(self) -> Tuple[str, ...]:
        # in a temporary directory: `vivado -version` writes vivado.jou and vivado.log
        out = self.probe_stdout("-version")
        assert isinstance(out, str)
        so = re.split(r"\s+", out)
        version_string = so[1] if len(so) > 1 else so[0] if len(so) > 0 else ""
        return tuple(version_string.split("."))


class Vivado(Flow, metaclass=ABCMeta):
    """Base class of the AMD Vivado synthesis and simulation flows."""

    executables = ("vivado",)
    locator_variables = ("XILINX_VIVADO", "XILINXD_LICENSE_FILE", "LM_LICENSE_FILE")

    artifacts_description = describe_artifacts("log")

    class Settings(Flow.Settings):
        tcl_shell: bool = Field(
            False,
            description="Open an interactive Tcl shell after Vivado runs the flow script.",
        )
        no_log: bool = Field(
            False, description="Pass -nolog to Vivado, so it does not write a vivado.log file."
        )

    def init(self):
        super().init()
        self.add_template_global_func(vivado_file_type)
        assert isinstance(self.settings, self.Settings)
        default_args = [
            "-nojournal",
            "-mode",
            "tcl" if self.settings.tcl_shell else "batch",
        ]
        if self.settings.verbosity != "debug":
            default_args.append("-notrace")
        if self.settings.verbosity == "verbose":
            default_args.append("-verbose")
        if self.settings.no_log:
            default_args.append("-nolog")
        self.vivado = VivadoTool(
            default_args=default_args,
            design_root_=self.design_root,
        )  # pyright: ignore
        self.add_template_filter(
            "vivado_generics", vivado_generics(not isinstance(self, SynthFlow))
        )

        self.add_template_filter(  # defines to an option string
            "vivado_defines", vivado_defines(not isinstance(self, SynthFlow))
        )

    def parse_xml_report(self, report_xml, *, optional: bool = False) -> Optional[Dict[str, Any]]:
        """Return the tables of a Vivado XML report that this run wrote (`Flow.report_file`).

        `optional` is the argument of `Flow.report_file`."""
        report = self.report_file(report_xml, optional=optional)
        if report is None:
            return None
        try:
            tree = ElementTree.parse(report)
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
                        # the first cell of a row is its key
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
