import logging
import os
from typing import Any, Dict, List, Literal, Optional
from junitparser import JUnitXml
from pydantic import validator, Field

from ..tool import Tool
from .design import Design


log = logging.getLogger(__name__)


class Cocotb(Tool):
    """Cocotb support for a SimFlow"""

    """Not a stand-alone tool, but is used from a SimFlow"""

    class Settings(Tool.Settings):
        coverage: bool = Field(
            True, description="Collect coverage data if supported by simulation tool."
        )
        reduced_log_fmt: bool = Field(
            True, description="Display shorter log lines in the terminal."
        )
        results: str = Field(
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

    def __init__(
        self, settings: "Cocotb.Settings", cocotb_sim_name: str, run_path: os.PathLike
    ):
        super().__init__(settings, run_path)
        self.settings: "Cocotb.Settings" = settings
        self.cocotb_name = cocotb_sim_name

    def get_version(self):
        return self.run_tool("cocotb-config", ["--version"], stdout=True)

    def vpi_path(self):
        so_ext = "so"  # on Linux and macOS
        if self.version_minor >= 5:
            so_path = self.run_tool(
                "cocotb-config",
                ["--lib-name-path", "vpi", self.cocotb_name],
                stdout=True,
            )
        else:
            so_path = (
                self.run_tool("cocotb-config", ["--prefix"], stdout=True, check=True)
                + f"/cocotb/libs/libcocotbvpi_{self.cocotb_name}.{so_ext}"
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
            ss = self.settings
            ret = {
                "MODULE": coco_module,
                "TOPLEVEL": top,  # TODO
                "TOPLEVEL_LANG": "vhdl",
                "COCOTB_REDUCED_LOG_FMT": int(ss.reduced_log_fmt),
                "PYTHONPATH": os.pathsep.join(ppath),
                "COCOTB_RESULTS_FILE": ss.results,
                "COVERAGE": ss.coverage,
                "COCOTB_RESOLVE_X": ss.resolve_x,
            }
            if ss.testcase:
                ret["TESTCASE"] = ",".join(ss.testcase)
            if ss.random_seed is not None:
                ret["RANDOM_SEED"] = ss.random_seed
            if ss.gpi_extra:
                ret["GPI_EXTRA"] = ",".join(ss.gpi_extra)
        return ret

    def parse_results(self) -> bool:
        xml = JUnitXml.fromfile(self.run_path / self.settings.results)
        failed = False
        if xml.failures:
            failed = True
            log.critical(f"Cocotb: {xml.failures} failure(s)")
        if xml.errors:
            failed = True
            log.error(f"Cocotb: {xml.errors} error(s)")

        return not failed
