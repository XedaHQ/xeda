- A project file has the keys `designs` (a list), `flows`, `run_root` and `default_target`. Select
  its design with `--design-name` and its target with `--target` or `--no-target`. A design file
  given as `DESIGN` never gets the project's `flows`, and a design entry cannot set `design_root`:
  use a `file` entry.
