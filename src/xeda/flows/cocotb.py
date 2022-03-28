import logging
import os
from typing import Any, Dict, List, Literal, Optional

from junitparser import JUnitXml

from ..dataclass import Field, XedaBaseModel, validator
from ..design import Design
from ..tool import Tool

log = logging.getLogger(__name__)


class CocotbSettings(XedaBaseModel):
    coverage: bool = Field(
        True, description="Collect coverage data if supported by simulation tool."
    )
    reduced_log_fmt: bool = Field(
        True, description="Display shorter log lines in the terminal."
    )
    results_xml: str = Field(
        "results.xml",
        description="xUnit-compatible cocotb result file.",
        hidden_from_schema=True,
    )
    resolve_x: Literal["VALUE_ERROR", "ZEROS", "ONES", "RANDOM"] = Field(
        "VALUE_ERROR",
        description="how to resolve bits with a value of X, Z, U or W when being converted to integer.",
    )
    testcase: List[str] = Field(
        [],
        description="List of test-cases to run. Can also be specified as a comma-separated string. Currently used for cocotb testbenches only.",
    )
    random_seed: Optional[int] = Field(
        None,
        description="Seed the Python random module to recreate a previous test stimulus.",
    )
    gpi_extra: List[str] = Field(
        [],
        description="A comma-separated list of extra libraries that are dynamically loaded at runtime.",
    )

    @validator("testcase", "gpi_extra", pre=True, always=True)
    def str_to_list(cls, value):
        if isinstance(value, str):
            value = [s.strip() for s in value.split(",")]
        return value


class Cocotb(CocotbSettings, Tool):
    """Cocotb support for a SimFlow"""

    executable = "cocotb-config"
    sim_name: str

    """Not a stand-alone tool, but is used from a SimFlow"""

    def vpi_path(self) -> str:
        so_ext = "so"  # TODO windows?
        if self.version_gte(1, 6):
            so_path = self.run_get_stdout(
                ["--lib-name-path", "vpi", self.sim_name],
            )
        else:
            so_path = (
                self.run_get_stdout(["--prefix"])
                + f"/cocotb/libs/libcocotbvpi_{self.sim_name}.{so_ext}"
            )

        log.info(f"cocotb.vpi_path = {so_path}")
        return so_path

    def env(self, design: Design) -> Dict[str, Any]:
        ret = {}
        if design.tb and design.tb.cocotb:
            assert design.tb.top, "tb.top was not set by the parent SimFlow"
            coco_module = design.tb.sources[0].file.stem
            tb_top_path = design.tb.sources[0].file.parent
            ppath = []
            current_ppath = os.environ.get("PYTHONPATH")
            if current_ppath:
                ppath = current_ppath.split(os.pathsep)
            ppath.append(str(tb_top_path))
            top: str = (
                design.tb.top if isinstance(design.tb.top, str) else design.tb.top[0]
            )
            ret = {
                "MODULE": coco_module,
                "TOPLEVEL": top,  # TODO
                "TOPLEVEL_LANG": "vhdl",
                "COCOTB_REDUCED_LOG_FMT": int(self.reduced_log_fmt),
                "PYTHONPATH": os.pathsep.join(ppath),
                "COCOTB_RESULTS_FILE": self.results_xml,
                "COVERAGE": self.coverage,
                "COCOTB_RESOLVE_X": self.resolve_x,
            }
            if self.testcase:
                ret["TESTCASE"] = ",".join(self.testcase)
            if self.random_seed is not None:
                ret["RANDOM_SEED"] = self.random_seed
            if self.gpi_extra:
                ret["GPI_EXTRA"] = ",".join(self.gpi_extra)
        return ret

    @property
    def results(self):
        results_xml = self.results_xml
        return JUnitXml.fromfile(results_xml)

    def parse_results(self) -> bool:
        xml = self.results
        failed = False
        if xml.errors:
            failed = True
            log.error(f"Cocotb: {xml.errors} error(s)")
        if xml.failures:
            failed = True
            log.critical(f"Cocotb: {xml.failures} failure(s)")

        return not failed
