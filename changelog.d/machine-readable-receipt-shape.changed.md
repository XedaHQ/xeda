- In `xeda pdk install --json` and `xeda pdk path --json`, `receipt` is the receipt object of the
  install, or `null` if the store has none, as in `xeda tools`. It was the path of the receipt file.
  The receipt file has `schema_version` and `installed_at` (it was `time`).
