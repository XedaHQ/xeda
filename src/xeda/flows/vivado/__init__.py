import logging
from abc import ABCMeta
from functools import reduce
from html import unescape
from pathlib import Path
from typing import Any, Dict, Optional
from xml.etree import ElementTree


from ...dataclass import Field
from ...design import Design
from ...tool import Docker, Tool
from ..flow import Flow

log = logging.getLogger(__name__)


def vivado_generics(kvdict, sim=False):
    def supported_vivado_generic(v, sim):
        if sim:
            return True
        if isinstance(v, int):
            return True
        if isinstance(v, bool):
            return True
        v = str(v)
        return v.isnumeric() or (v.strip().lower() in {"true", "false"})

    def vivado_gen_convert(k, x, sim):
        if sim:
            return x
        xl = str(x).strip().lower()
        if xl == "false":
            return "1\\'b0"
        if xl == "true":
            return "1\\'b1"
        return x

    return " ".join(
        [
            f"-generic{'_top' if sim else ''} {k}={vivado_gen_convert(k, v, sim)}"
            for k, v in kvdict.items()
            if supported_vivado_generic(v, sim)
        ]
    )


class Vivado(Flow, metaclass=ABCMeta):
    """Xilinx (AMD) Vivado FPGA synthesis and simulation flows"""

    class Settings(Flow.Settings):
        tcl_shell: bool = Field(
            False,
            description="Drop to interactive TCL shell after Vivado finishes running a flow script",
        )

    def __init__(self, settings: Settings, design: Design, run_path: Path):
        super().__init__(settings, design, run_path)
        default_args = [
            "-nojournal",
            "-mode",
            "tcl" if settings.tcl_shell else "batch",
        ]
        if not self.settings.debug:
            default_args.append("-notrace")
        self.vivado = Tool(
            "vivado",
            default_args=default_args,
            docker=Docker(
                image="siliconbootcamp/xilinx-vivado",
                command=["/tools/Xilinx/Vivado/2021.1/bin/vivado"],
                tag="stable",
                enabled=self.settings.dockerized,
            ),  # type: ignore
            design_root=self.design_root,
        )
        self.add_template_filter("vivado_generics", vivado_generics)

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
                    title = (
                        section_title + ":" + table_title
                        if table_title
                        else section_title
                    )
                    data[title] = table_data
        return data

    @staticmethod
    def get_from_path(dct: dict, path):
        if isinstance(path, str):
            path = path.split(".")
        return reduce(dict.__getitem__, path, dct)
