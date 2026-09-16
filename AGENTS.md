# Notes for coding agents

Xeda ships a skill that explains how to drive it. Read it before running flows or writing design
files - it is shorter than rediscovering the CLI, and it documents the traps.

- **[.claude/skills/xeda/SKILL.md](.claude/skills/xeda/SKILL.md)** - the CLI contract, the design
  file shape, how settings and flow dependencies work, and how to read results.
- [.claude/skills/xeda/references/flows.md](.claude/skills/xeda/references/flows.md) - catalog of
  every flow, generated from the installed version.
- [.claude/skills/xeda/references/design-file.md](.claude/skills/xeda/references/design-file.md) -
  full design-file reference.
- [.claude/skills/xeda/references/troubleshooting.md](.claude/skills/xeda/references/troubleshooting.md)
  - failure modes and what to check.

Working in another repository? Install the same files there, regenerated against your installed
version:

```bash
xeda skill install                 # writes ./.claude/skills/xeda/
xeda skill install --dir <dir>     # or somewhere else
xeda skill reference               # just print the flow catalog
```

## The short version

`--json` always means "a parseable result on stdout".

```bash
xeda list-flows --json                            # what flows exist
xeda list-settings <flow> --json                  # what you can set on one
xeda list-results <flow> --json                   # what comes back
xeda design-schema                                # JSON Schema of a design file
xeda run <flow> <design> -s key=value --json      # run it; tool output goes to stderr
```

Discover, do not guess: an unknown setting is a hard error, not a warning. Flow names tolerate
dashes, underscores and CamelCase; setting names do not.

## Contributing to Xeda itself

See [CLAUDE.md](CLAUDE.md) for the repository's own conventions, `mypy src`
and `black --check src` must stay clean, and `tests/test_documentation.py` requires every new flow
to have its own docstring, a `description=` on every setting, and a `results_description`.
