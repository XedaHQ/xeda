import logging
import os
from functools import cached_property
from pathlib import Path
import sys
from typing import Any, Dict, List, Literal, Optional

from junitparser import JUnitXml  # type: ignore[import-untyped, attr-defined]

from .dataclass import Field, XedaBaseModel, validator
from .design import Design, SourceType
from .tool import Tool

log = logging.getLogger(__name__)


class CocotbSettings(XedaBaseModel):
    coverage: bool = Field(
        False, description="Collect coverage data if supported by simulation tool."
    )
    reduced_log_fmt: bool = Field(True, description="Display shorter log lines in the terminal.")
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

    executable: str = "cocotb-config"
    sim_name: str

    """Not a stand-alone tool, but is used from a SimFlow"""

    @cached_property
    def prefix(self) -> Optional[str]:
        return self.run_get_stdout("--prefix")

    @cached_property
    def share_dir(self) -> Optional[str]:
        return self.run_get_stdout("--share")

    @cached_property
    def lib_dir(self) -> Optional[str]:
        if self.version_gte(1, 6):
            return self.run_get_stdout("--lib-dir")
        else:
            if self.prefix is None:
                return None
            return os.path.join(
                self.prefix,
                "cocotb",
                "libs",
            )

    @cached_property
    def vpi_lib_name(self) -> Optional[str]:
        return self.get_lib_name(self.sim_name)

    def get_lib_name(self, simulator, interface="vpi") -> Optional[str]:
        return self.run_get_stdout("--lib_name", interface, simulator)

    def lib_path(self, interface: str = "vpi", sim_name=None) -> Optional[str]:
        so_ext = "so"  # TODO windows?
        if sim_name is None:
            sim_name = self.sim_name
        if self.version_gte(1, 6):
            so_path = self.run_get_stdout(
                "--lib-name-path",
                interface,
                sim_name,
            )
            if not so_path:
                log.error("[cocotb] %s failed!", self.executable)
                return None
        else:
            if self.prefix is None:
                so_path = None
            else:
                so_path = os.path.join(
                    self.prefix,
                    "cocotb",
                    "libs",
                    f"libcocotb{interface}_{sim_name}.{so_ext}",
                )
        log.info("cocotb.lib_path: %s", so_path)
        if so_path:
            if not Path(so_path).exists():
                log.error("[cocotb] shared library %s does not exist.", so_path)
                return None
        else:
            log.error("[cocotb] %s library for %s is not available.", interface.upper(), sim_name)
        return so_path

    def env(self, design: Design) -> Dict[str, Any]:
        environ = {}
        if design.tb.cocotb:
            if design.tb is None or not design.tb.sources:
                raise ValueError("'design.tb.cocotb' is set, but 'design.tb.sources' is empty.")
            if not design.tb.top:
                if not design.rtl.top:
                    raise ValueError(
                        f"[cocotb] In design {design.name}: Either `tb.top` or `rtl.top` must be specified."
                    )
                design.tb.top = (design.rtl.top,)
            cocotb_sources = design.sources_of_type(SourceType.Cocotb, rtl=False, tb=True)
            if cocotb_sources:
                top_cocotb_source = cocotb_sources[-1].file
            else:
                top_cocotb_source = None

            py_path = []
            current_py_path = os.environ.get("PYTHONPATH")
            if current_py_path:
                py_path += current_py_path.split(os.pathsep)
            coco_module = None
            if design.tb.cocotb.module:
                design_module_split = design.tb.cocotb.module.split("/")
                coco_module = design_module_split[-1]
                if len(design_module_split) > 1:
                    module_path = Path(os.sep.join(design_module_split[:-1]))
                    if not os.path.isabs(module_path):
                        module_path = os.path.join(design.root_path, module_path)
                else:
                    module_path = design.root_path
                py_path.append(str(module_path))
            elif top_cocotb_source:
                coco_module = top_cocotb_source.stem
            if top_cocotb_source:
                py_path.append(str(top_cocotb_source.parent))
            toplevel = design.tb.cocotb.toplevel
            if not toplevel and design.tb.top:
                toplevel = design.tb.top if isinstance(design.tb.top, str) else design.tb.top[0]
            environ = {
                "MODULE": coco_module,
                "COCOTB_TEST_MODULES": coco_module,
                "TOPLEVEL": toplevel,
                "COCOTB_REDUCED_LOG_FMT": os.environ.get(
                    "COCOTB_REDUCED_LOG_FMT", "1" if self.reduced_log_fmt else "0"
                ),
                "PYTHONPATH": os.pathsep.join(py_path),
                "COCOTB_RESULTS_FILE": self.results_xml,
                "COCOTB_RESOLVE_X": self.resolve_x,
                "PYGPI_PYTHON_BIN": os.environ.get(
                    # Use the current Python executable if not set in the environment
                    "PYGPI_PYTHON_BIN",
                    os.path.abspath(sys.executable),
                ),
            }
            if self.coverage:
                environ["COVERAGE"] = 1
            testcases = self.testcase or design.tb.cocotb.testcase
            if testcases:
                environ["TESTCASE"] = ",".join(testcases)
            if self.random_seed is not None:
                environ["RANDOM_SEED"] = self.random_seed
            if self.gpi_extra:
                environ["GPI_EXTRA"] = ",".join(self.gpi_extra)
            log.debug("Cocotb env: %s", environ)
        return environ

    @cached_property
    def _results(self):
        results_xml = self.results_xml
        if not Path(results_xml).exists():
            return JUnitXml()
        return JUnitXml.fromfile(results_xml)

    @property
    def results(self):
        return self._results

    @property
    def result_testcases(self):
        for ts in self.results:
            if ts is not None:
                for tc in ts:
                    if tc is not None:
                        yield {
                            "name": tc.name,
                            "result": str(tc),
                            "classname": tc.classname,
                            "time": None if tc.time is None else round(tc.time, 3),
                        }

    def add_results(self, flow_results: Dict[str, Any], prefix: str = "cocotb.") -> bool:
        """adds cocotb results to parent flow's results. returns success status"""
        xml = self.results
        flow_results[prefix + "tests"] = xml.tests
        flow_results[prefix + "errors"] = xml.errors
        flow_results[prefix + "failures"] = xml.failures
        flow_results[prefix + "skipped"] = xml.skipped
        flow_results[prefix + "time"] = xml.time
        failed = False
        if not xml.tests:
            failed = True
            log.error("No tests were discovered")
        if xml.errors:
            failed = True
            log.error("Cocotb: %d error(s)", xml.errors)
        if xml.failures:
            failed = True
            log.critical("Cocotb: %d failure(s)", xml.failures)
        if failed:
            flow_results["success"] = False

        return not failed
