- `xeda run --remote` no longer fetches an artifact that lies outside the run directory on the
  host into `_external/` of the mirror: the results keep its path on the host, and a warning
  names it.
