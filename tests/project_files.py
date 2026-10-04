"""The project-file spellings tests write: whatever discovery accepts, YAML first."""

from xeda.xedaproject import PROJECT_FILE_NAMES

#: the spelling tests write when they only need a project file (`xedaproject.yaml`)
PROJECT_FILE = PROJECT_FILE_NAMES[0]
#: the one spelling TOML content is written under
TOML_PROJECT_FILE = next(name for name in PROJECT_FILE_NAMES if name.endswith(".toml"))
