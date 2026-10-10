"""Generate the built-in flow transition catalog used by lightweight completion."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xeda.flow.io import declared_outputs  # noqa: E402
from xeda.flow_runner.chains import complete_request  # noqa: E402
from xeda.introspect import all_flow_classes  # noqa: E402


def render() -> str:
    """Return deterministic JSON generated from the runtime chain completion rules."""
    classes = sorted(
        (cls for cls in all_flow_classes() if cls.__module__.startswith("xeda.flows.")),
        key=lambda cls: cls.name,
    )
    catalog: dict[str, dict[str, object]] = {}
    for flow_class in classes:
        canonical = flow_class.name
        followers: dict[str, list[str]] = {}
        for output in ("", *declared_outputs(flow_class)):
            qualifier = f".{output}" if output else ""
            request = f"{canonical}{qualifier}+"
            followers[output] = [
                candidate[len(request) :] for candidate in complete_request(request, classes)
            ]
        catalog[canonical] = {
            "aliases": list(flow_class.aliases),
            "class_name": flow_class.__name__,
            "followers": followers,
        }
    return json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="fail if the checked-in catalog is stale"
    )
    args = parser.parse_args()
    target = ROOT / "src" / "xeda" / "flow_catalog.json"
    generated = render()
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != generated:
            print(
                f"{target.relative_to(ROOT)} is stale; run python tools/generate_flow_catalog.py",
                file=sys.stderr,
            )
            return 1
        return 0
    target.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
