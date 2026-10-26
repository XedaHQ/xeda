"""The project-file spellings tests write: whatever discovery accepts, YAML first."""

from xeda.xedaproject import PROJECT_FILE_NAMES

#: the spelling tests write when they only need a project file (`xedaproject.yaml`)
PROJECT_FILE = PROJECT_FILE_NAMES[0]
#: the one spelling TOML content is written under
TOML_PROJECT_FILE = next(name for name in PROJECT_FILE_NAMES if name.endswith(".toml"))


def project_listing(design, flows: dict | None = None, **keys) -> str:
    """Write a project file beside the design file `design` that lists it, with the shared
    `flows` and any other keys; return the path of the project file as text.

    The project's `flows` apply to a design that the project selects, not to a design file given
    on its own. Select the design by its name (`--design-name`)."""
    import yaml

    path = design.parent / PROJECT_FILE
    path.write_text(
        yaml.safe_dump({"designs": [{"file": design.name}], "flows": flows or {}, **keys})
    )
    return str(path)
