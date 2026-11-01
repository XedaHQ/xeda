"""Read a project file, take a design and a spec from it, and run it in the project's run root."""

from pathlib import Path
from typing import assert_type

import xeda

project = xeda.Project.load("xedaproject.yaml")
assert_type(project, xeda.Project)
assert_type(project.path, Path)
assert_type(project.run_root, Path)
assert_type(project.designs, tuple[str, ...])

design = project.design("riscv", target="artix7")
assert_type(design, xeda.Design)

spec = xeda.Spec("vivado_synth", design, settings={"vivado_synth.fpga": "xc7a35ticsg324-1L"})
result = xeda.Workspace(project.run_root).run(spec)
assert_type(result, xeda.Result)
assert_type(xeda.run(spec, run_root=project.run_root), xeda.Result)
