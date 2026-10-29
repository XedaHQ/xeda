"""Settings items for the command line, which addresses every setting by its node."""

from collections.abc import Iterable

from xeda.flow import registered_flows


def scoped(node: str, items: Iterable[str]) -> list[str]:
    """Return `items` with `node.` before each key that does not start with a flow name. The old
    `flows.` prefix of an item is dropped."""
    out = []
    for item in items:
        item = item.removeprefix("flows.")
        out.append(item if item.partition(".")[0] in registered_flows else f"{node}.{item}")
    return out
