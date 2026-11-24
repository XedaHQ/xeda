- `ScrubResult` is now a frozen dataclass with keyword-only fields, not a named tuple. Read its
  fields by name: it no longer unpacks or indexes.
