"""The installable agent skill: instructions plus a reference generated from the installed version.

The prose lives in `xeda/data/agent/` and ships with the package. The flow catalog is generated
from live introspection at install time, so it always matches the installed version.
"""

from __future__ import annotations

import logging
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, cast

from importlib_resources import as_file, files

from .flow_runner import get_flow_class
from .introspect import flows_info, results_info, settings_info

log = logging.getLogger(__name__)

__all__ = [
    "SKILL_NAME",
    "generate_flows_reference",
    "install_skill",
    "skill_source_dir",
]

SKILL_NAME = "xeda"

#: The order in which the catalog presents the categories, most commonly used first.
_CATEGORY_ORDER = ["simulation", "fpga_synthesis", "asic_synthesis", "synthesis", "other"]
_CATEGORY_TITLES = {
    "simulation": "Simulation",
    "fpga_synthesis": "FPGA synthesis / implementation",
    "asic_synthesis": "ASIC synthesis",
    "synthesis": "Synthesis (generic)",
    "other": "Other",
}


@contextmanager
def skill_source_dir() -> Iterator[Path]:
    """Yield the directory that holds the packaged skill sources, valid only inside the context.

    This is a context manager and not a plain path. When Xeda is imported from a zip file (or
    from any other loader that is not a file system), `as_file` extracts the resources to a
    temporary directory and deletes it on exit. A path returned from inside the `with` would then
    name something that no longer exists. The caller must keep the context open for as long as it
    reads the files.
    """
    with as_file(files("xeda.data").joinpath("agent")) as path:
        yield Path(path)


def _edge_text(edge: dict[str, object], name: str, key: str) -> str:
    """Return one chain relation as text: the other flow, qualified when the request must name an
    output, followed by the flows its link passes through, and marked when the target decides the
    relation."""
    other = str(edge["flow"])
    text = f"`{other}`"
    if edge.get("output"):
        text = (
            f"`{name}.{edge['output']}+{other}`"
            if key == "can_precede"
            else f"`{other}.{edge['output']}`"
        )
    through = cast(List[str], edge["through"])
    if through:
        text += f" (through {', '.join(f'`{flow}`' for flow in through)})"
    return text + (" (depends on the target)" if edge.get("target_dependent") else "")


def _group_lines(flow: str, fields: List[Dict[str, object]]) -> List[str]:
    """Return the lines about the settings groups of a flow: for each top-level group, its
    `common` settings by dotted name and the number of the other settings it has. `fields` are
    the entries of `settings_info` that are specific to the flow."""
    lines: List[str] = []
    for group in dict.fromkeys(str(f["group"]) for f in fields if f.get("group")):
        members = [f for f in fields if f.get("group") == group]
        names = {str(f["name"]) for f in members}
        # an entry that has entries below it is a sub-group and holds no setting of its own
        leaves = [
            f for f in members if not any(other.startswith(f"{f['name']}.") for other in names)
        ]
        common = [f"`{f['name']}`" for f in leaves if f.get("tier") == "common"]
        more = len(leaves) - len(common)
        line = f"Group `{group}`: " + (", ".join(common) if common else "no common settings")
        if more:
            line += f"; {more} more: `xeda list-settings {flow} --tier expert --json`"
        lines += [line, ""]
    return lines


def _flow_section(flow: Dict[str, object]) -> str:
    name = str(flow["name"])
    lines = [f"### `{name}`", ""]

    facts: List[str] = []
    aliases = flow.get("aliases") or []
    if isinstance(aliases, list) and aliases:
        facts.append("aliases: " + ", ".join(f"`{a}`" for a in aliases))
    deps = flow.get("dependencies") or []
    if isinstance(deps, list) and deps:
        facts.append("runs first: " + " -> ".join(f"`{d}`" for d in deps))
    if flow.get("supports_cocotb"):
        facts.append("supports cocotb testbenches")
    for label, key in (("can follow", "can_follow"), ("can be followed by", "can_precede")):
        edges = flow.get(key) or []
        if isinstance(edges, list) and edges:
            facts.append(f"{label}: " + ", ".join(_edge_text(edge, name, key) for edge in edges))
    reason = flow.get("action_reason")
    if reason:
        facts.append(f"{reason} and can only end a chain".replace("it programs", "programs", 1))
    if facts:
        lines += ["*" + " | ".join(facts) + "*", ""]

    description = flow.get("description")
    if description:
        lines += [" ".join(str(description).split()), ""]

    settings = settings_info(name)
    flow_specific = [f for f in settings["fields"] if not f.get("common")]
    # the fields of a settings group are listed by `xeda list-settings` and do not count here
    specific = [f for f in flow_specific if f.get("group") is None]
    required = [f["name"] for f in specific if f.get("required")]
    # Add what the flow cannot run without although its model leaves it optional (`fpga` of
    # `nextpnr`, `target_libraries` of `dc`). The launch checks these settings and the model
    # does not.
    required += [r for r in get_flow_class(name).required_settings if r not in required]
    if required:
        lines += ["Required settings: " + ", ".join(f"`{r}`" for r in required), ""]
    lines += [
        f"{len(specific)} flow-specific settings "
        f"(plus the common ones): `xeda list-settings {name} --json`",
        "",
    ]
    lines += _group_lines(name, flow_specific)

    results = results_info(name)
    paths = [m["path"] for m in results["metrics"]]
    if paths:
        lines += ["Reports: " + ", ".join(f"`{p}`" for p in paths), ""]
    else:
        lines += ["Reports no metrics beyond the keys every flow reports.", ""]
    return "\n".join(lines)


def generate_flows_reference() -> str:
    """Return the Markdown catalog of every flow in the installed version of Xeda."""
    flows = flows_info()
    out = [
        "# Flow catalog",
        "",
        "Generated by `xeda skill install` from the installed version of xeda. Regenerate after "
        "upgrading xeda, or when a flow is added.",
        "",
        "Settings are deliberately not listed here (there are several hundred). For any flow:",
        "",
        "```bash",
        "xeda list-settings <flow> --json     # every setting: name, type, default, meaning",
        "xeda list-results <flow> --json      # every metric: path, type, unit, meaning",
        "```",
        "",
        "Flows with declared file I/O chain with `+`: `xeda run yosys_fpga+nextpnr+fpga_pack "
        "design.yaml` binds each flow's declared outputs to the next flow's required inputs. "
        '"can follow" and "can be followed by" below are the declared relations; '
        "`(depends on the target)` marks one the target's family decides, and `flow.output+flow` "
        "one that needs the output named. `(through a, b)` marks a relation whose link passes "
        "through the flows `a` and `b` between the two: Xeda runs them as it does for the second flow "
        "alone. A refused chain says which chain would be valid, if any. The relations are "
        "declarations only: planning can still refuse a chain that "
        "passes (related inputs). For example, `yosys_fpga+vivado_impl+vivado_power` also needs "
        "`-s flows.vivado_postsynth_sim.inputs.<input>=vivado_impl.<input>` for each of "
        "`netlist`, `netlist_timing` and `sdf`; the refusal lists the bindings to add. "
        "A flow with no declared I/O runs alone.",
        "",
    ]

    by_category: Dict[str, List[Dict[str, object]]] = {}
    for flow in flows:
        by_category.setdefault(str(flow["category"]), []).append(flow)

    for category in _CATEGORY_ORDER + sorted(set(by_category) - set(_CATEGORY_ORDER)):
        group = by_category.get(category)
        if not group:
            continue
        out += [f"## {_CATEGORY_TITLES.get(category, category)}", ""]
        out += [_flow_section(flow) for flow in group]
    return "\n".join(out).rstrip() + "\n"


def install_skill(dest_dir: Path, force: bool = False) -> List[Path]:
    """Write the skill into `dest_dir/xeda/`, regenerating the flow catalog, and return the paths
    written. Refuse to overwrite an existing installation unless `force` is true.
    """
    target = Path(dest_dir) / SKILL_NAME
    if target.exists() and not force:
        raise FileExistsError(
            f"{target} already exists. Pass --force to overwrite it, or choose another --dir."
        )
    written: List[Path] = []

    (target / "references").mkdir(parents=True, exist_ok=True)
    # Copy while the resource context is open: the sources may live in a temporary directory
    # that only exists for the duration of the `with`.
    with skill_source_dir() as source:
        shutil.copyfile(source / "SKILL.md", target / "SKILL.md")
        written.append(target / "SKILL.md")
        for reference in sorted((source / "references").glob("*.md")):
            shutil.copyfile(reference, target / "references" / reference.name)
            written.append(target / "references" / reference.name)

    flows_md = target / "references" / "flows.md"
    flows_md.write_text(generate_flows_reference(), encoding="utf-8")
    written.append(flows_md)
    return written


def default_skill_dir(start: Optional[Path] = None) -> Path:
    """Return the default install directory: `.claude/skills` under the current directory."""
    return (start or Path.cwd()) / ".claude" / "skills"
