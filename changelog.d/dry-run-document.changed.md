- `xeda run --dry-run --json` prints a new document: `schema_version`, `spec`, `roots`, `options`
  and `nodes`, with `run_dir`, `identity`, `outputs_enabled` and `deliveries` for each node. A node
  that reuses a rejected run has the state `rejected` (it was `rejected again`), and `--rebuild-all`
  and `--clean` give the reasons `rebuild_all` and `clean`. A dry run also refuses a delivery that
  no answer could allow.
