"""Settings items for the command line, which addresses every setting by its node."""

from collections.abc import Iterable, Mapping

from xeda.flow import registered_flows


def scoped(node: str, items: Iterable[str]) -> list[str]:
    """Return `items` with `node.` before each key that does not start with a flow name. The old
    `flows.` prefix of an item is dropped."""
    out = []
    for item in items:
        item = item.removeprefix("flows.")
        out.append(item if item.partition(".")[0] in registered_flows else f"{node}.{item}")
    return out


def node_settings(flow, settings):
    """Return `settings`, the settings of the requested flow of `flow` (a flow class, a flow name
    or the text of a chain), as the node-scoped layer that a launcher takes. A settings instance,
    an empty layer, and a layer that names its nodes already stay as they are."""
    from xeda.flow import Flow
    from xeda.flow_runner.chains import parse_request

    if not settings or isinstance(settings, Flow.Settings):
        return settings
    if isinstance(flow, type):
        name = flow.name
    elif isinstance(flow, str):
        name = parse_request(flow).requested.name
    else:
        name = flow.requested.name
    if isinstance(settings, Mapping):
        # a key that names a flow (`flows.` before it or not) stays a node key; any other key is a
        # setting of the requested flow. Every key is dotted from the top, so tables and dotted
        # keys never meet.
        out: dict = {}
        for key, value in settings.items():
            if key == "flows" and isinstance(value, Mapping):
                out.update(value)
                continue
            bare = key.removeprefix("flows.")
            head, dot, _rest = bare.partition(".")
            if head in registered_flows and (dot or isinstance(value, Mapping)):
                out[bare] = value
            else:
                out[f"{name}.{key}"] = value
        return out
    if any(isinstance(item, dict) for item in settings):
        from xeda.utils import settings_to_dict

        table = settings_to_dict(scoped(name, [item for item in settings if isinstance(item, str)]))
        for item in settings:
            if isinstance(item, dict):
                table.setdefault(name, {}).update(item)
        return table
    return scoped(name, settings)
