import html
import logging
from functools import reduce
from typing import List
from xml.etree import ElementTree
from pathlib import Path

from ...flows.design import Design
from ...utils import try_convert
from ..flow import Flow, DebugLevel

logger = logging.getLogger()


def vivado_generics(kvdict, sim=False):
    def supported_vivado_generic(k, v, sim):
        if sim:
            return True
        if isinstance(v, int):
            return True
        if isinstance(v, bool):
            return True
        v = str(v)
        return (v.isnumeric() or (v.strip().lower() in {'true', 'false'}))

    def vivado_gen_convert(k, x, sim):
        if sim:
            return x
        xl = str(x).strip().lower()
        if xl == 'false':
            return "1\\'b0"
        if xl == 'true':
            return "1\\'b1"
        return x

    return ' '.join([f"-generic{'_top' if sim else ''} {k}={vivado_gen_convert(k, v, sim)}" for k, v in kvdict.items() if supported_vivado_generic(k, v, sim)])


class Vivado(Flow):
    reports_subdir_name = 'reports'

    def __init__(self, flow_settings: Flow.Settings, design: Design, run_path: Path, completed_dependencies: List[Flow]):
        super().__init__(flow_settings, design, run_path,
                         completed_dependencies=completed_dependencies)
        self.jinja_env.filters['vivado_generics'] = vivado_generics

    def run_vivado(self, script_path, stdout_logfile=None):
        if stdout_logfile is None:
            stdout_logfile = f'{self.name}_stdout.log'
        debug = self.settings.debug  # > DebugLevel.NONE
        vivado_args = ['-nojournal', '-mode',
                       'tcl' if debug else 'batch', '-source', str(script_path)]
        if not debug:
            vivado_args.append('-notrace')
        return self.run_process('vivado', vivado_args, initial_step='Starting vivado',
                                stdout_logfile=stdout_logfile)

    @staticmethod
    def parse_xml_report(report_xml):
        tree = ElementTree.parse(report_xml)

        data = {}
        # components = {}

        for section in tree.findall(f"./section"):
            section_title = section.get("title")
            for table in section.findall("./table"):
                table_data = {}
                header = [html.unescape(col.attrib['contents']).strip(
                ) for col in table.findall("./tablerow/tableheader")]
                for tablerow in table.findall("./tablerow"):
                    cells = [html.unescape(cell.attrib['contents']).strip(
                    ) for cell in tablerow.findall("./tablecell")]
                    if cells:
                        # choose 0th element as "index data" (distinct key)
                        cell_data = {h: c for h, c in zip(
                            header[1:], cells[1:]) if c}
                        cell_key = cells[0]
                        if cell_data:
                            table_data[cell_key] = try_convert(
                                cell_data, to_str=False)
                if table_data:
                    table_title = table.get("title")
                    title = section_title + ":" + table_title if table_title else section_title
                    data[title] = table_data
        return data

    @staticmethod
    def get_from_path(dct: dict, path):
        if isinstance(path, str):
            path = path.split('.')
        return reduce(dict.__getitem__, path, dct)
