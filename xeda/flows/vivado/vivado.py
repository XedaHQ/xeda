# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)
import html
import logging
from xeda.utils import try_convert
from xml.etree import ElementTree
from ..flow import Flow, DebugLevel
from functools import reduce

logger = logging.getLogger()


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


def vivado_generics(kvdict, sim):
    return ' '.join([f"-generic{'_top' if sim else ''} {k}={vivado_gen_convert(k, v, sim)}" for k, v in kvdict.items() if supported_vivado_generic(k, v, sim)])


class Vivado(Flow):
    reports_subdir_name = 'reports'

    def run_vivado(self, script_path, stdout_logfile=None):
        if stdout_logfile is None:
            stdout_logfile = f'{self.name}_stdout.log'
        debug = self.args.debug > DebugLevel.NONE
        vivado_args = ['-nojournal', '-mode', 'tcl' if debug >=
                       DebugLevel.HIGHEST else 'batch', '-source', str(script_path)]
        # if not debug:
        #     vivado_args.append('-notrace')
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
                header = [html.unescape(col.attrib['contents']).strip() for col in table.findall("./tablerow/tableheader")]
                for tablerow in table.findall("./tablerow"):
                    cells = [html.unescape(cell.attrib['contents']).strip() for cell in tablerow.findall("./tablecell")]
                    if cells:
                        # choose 0th element as "index data" (distinct key)
                        cell_data = {h:c for h,c in zip(header[1:],cells[1:]) if c}
                        cell_key = cells[0]
                        if cell_data:
                            table_data[cell_key] = try_convert(cell_data, to_str=False)
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
