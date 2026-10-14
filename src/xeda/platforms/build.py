"""Build a platform model from its layers, and check that the residue stays minimal.

Both functions are pure: they read no file. A provider finds the files (`Located`), the
derivation and the adapters read them, and the caller passes the results in.
"""

import json
from typing import Any, Mapping, Optional

from xeda.platforms.model import Layer, Located, PlatformModel, nest_leaves, normalize_leaf
from xeda.platforms.residue import Residue, effective_residue

__all__ = ["build_platform", "residue_problems"]


def _given(layer: str, leaves: Mapping[str, Any]) -> dict[str, Any]:
    """Return the leaves of a lower layer that have a value, in their normal form. A leaf that is
    `None` gives nothing."""
    given: dict[str, Any] = {}
    for key, value in leaves.items():
        if value is None:
            continue
        try:
            given[key] = normalize_leaf(key, value)
        except ValueError as error:
            raise ValueError(f"the {layer} layer gives a bad leaf: {error}") from None
    return given


def _check_bases(residue: Optional[Residue], user: Optional[Residue]) -> None:
    if user is None:
        if residue is None:
            raise ValueError("a platform needs a bundled residue, a user file, or both")
        return
    if user.extends is None:
        if residue is not None:
            raise ValueError(
                f"the user file '{user.name}' extends no platform, so it is complete: "
                f"pass no residue ('{residue.name}')"
            )
    elif residue is None:
        raise ValueError(f"the user file '{user.name}' extends '{user.extends}': pass its residue")
    elif residue.name != user.extends:
        raise ValueError(
            f"the user file '{user.name}' extends '{user.extends}', not '{residue.name}'"
        )


def _user_selected(
    corner: str, structure: Residue, residue: Optional[Residue], user: Optional[Residue]
) -> bool:
    """Whether the user file gave the corner `corner`: it is the only source of the platform, it
    names the corner, an alias of the user file points to it, or the default corner of the user
    file selects it."""
    if user is None:
        return False
    if residue is None or corner in user.files.liberty:
        return True
    if corner in user.corners.aliases.values():
        return True
    default = user.corners.default
    return default is not None and structure.corners.aliases.get(default, default) == corner


def build_platform(
    located: Located,
    residue: Optional[Residue],
    user: Optional[Residue],
    derived: Mapping[str, Any],
    upstream: Mapping[str, Any],
) -> PlatformModel:
    """Return the platform model of `located`.

    The layers merge leaf by leaf, weakest first: `derived`, `upstream`, `residue` (its values,
    then its overrides) and `user` (the same). A later layer wins. `derived` and `upstream` are
    tables of plain values by leaf key. `residue` is the bundled platform and `user` the file of
    the user. A user file that extends a platform comes with that platform's residue, and a
    complete user file comes alone. The name, the corner and the files come from `located` and
    from the residue, never from a derivation. `sources` says which layer gave each leaf.
    """
    _check_bases(residue, user)
    structure = effective_residue(residue, user)
    corner = structure.resolve_corner(located.corner)

    stack: list[tuple[Layer, dict[str, Any]]] = [
        ("derived", _given("derived", derived)),
        ("upstream", _given("upstream", upstream)),
    ]
    if residue is not None:
        stack.append(("residue", {**residue.value_leaves(), **residue.override_leaves()}))
    if user is not None:
        stack.append(("user", {**user.value_leaves(), **user.override_leaves()}))

    merged: dict[str, Any] = {}
    sources: dict[str, Layer] = {}
    for layer, leaves in stack:
        for key, value in leaves.items():
            merged[key] = value
            sources[key] = layer

    own_roles = set(user.files.role_paths(corner)) if user is not None else set()
    for role in located.files:
        own = user is not None and (residue is None or role in own_roles)
        sources[f"files.{role}"] = "user" if own else "residue"
    sources["name"] = "user" if user is not None else "residue"
    sources["corner"] = "user" if _user_selected(corner, structure, residue, user) else "residue"

    return PlatformModel.model_validate(
        {
            **nest_leaves(merged),
            "name": structure.name,
            "corner": corner,
            "files": {role: list(paths) for role, paths in located.files.items()},
            "sources": dict(sorted(sources.items())),
        }
    )


def _shown(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def residue_problems(
    residue: Residue, derived: Mapping[str, Any], upstream: Mapping[str, Any]
) -> list[str]:
    """Return the lines of `residue` that a lower layer makes useless or wrong, one message each.

    A value that a lower layer gives again is redundant. A value that a lower layer gives
    differently is an override and needs a reason. An override that the lower layer agrees with,
    or that no lower layer has, is in the wrong place. The messages come in the order of the keys,
    values first. The check keeps the residue minimal."""
    lower: dict[str, tuple[str, Any]] = {}
    for layer, leaves in (("derived", derived), ("upstream", upstream)):
        lower.update({key: (layer, value) for key, value in _given(layer, leaves).items()})
    problems: list[str] = []
    for key, value in residue.value_leaves().items():
        if key not in lower:
            continue
        layer, given = lower[key]
        if given == value:
            problems.append(f"{key}: {layer} gives it; delete this line")
        else:
            problems.append(
                f"{key}: {layer} gives {_shown(given)}; move it to overrides with a reason"
            )
    for key, value in residue.override_leaves().items():
        if key not in lower:
            problems.append(f"{key}: no lower layer gives it; move it to values")
        elif lower[key][1] == value:
            problems.append(f"{key}: {lower[key][0]} gives the same value; delete this override")
    return problems
