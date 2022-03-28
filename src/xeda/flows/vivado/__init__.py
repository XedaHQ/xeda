from html import unescape
import logging
from functools import reduce
from pathlib import Path
from typing import Any, Dict, Optional
from xml.etree import ElementTree

from ...design import Design
from ...utils import try_convert
from ..flow import Flow
from ...tool import Tool
from ...debug import DebugLevel

log = logging.getLogger(__name__)


def vivado_generics(kvdict, sim=False):
    def supported_vivado_generic(k, v, sim):
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
            if supported_vivado_generic(k, v, sim)
        ]
    )


class Vivado(Flow):
    class Settings(Flow.Settings):
        pass

    def __init__(self, settings: Settings, design: Design, run_path: Path):
        super().__init__(settings, design, run_path)
        debug = self.settings.debug > DebugLevel.NONE
        default_args = ["-nojournal", "-mode", "tcl" if debug else "batch"]
        if not debug:
            default_args.append("-notrace")
        self.vivado = Tool(executable="vivado", default_args=default_args)
        self.jinja_env.filters["vivado_generics"] = vivado_generics

    @staticmethod
    def parse_xml_report(report_xml) -> Optional[Dict[str, Any]]:
        try:
            tree = ElementTree.parse(report_xml)
        except ElementTree.ParseError as e:
            log.critical("Parsing %s failed: %s", report_xml, e.msg)
            return None
        data = {}
        for section in tree.findall(f"./section"):
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
                        cell_key = cells[0]
                        if cell_data:
                            table_data[cell_key] = try_convert(cell_data, to_str=False)
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
