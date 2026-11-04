- The JSON failure document of the query, `pdk` and `remote` commands now has an `error` with the
  keys `code`, `message`, `node`, `exit_status` and `log`, like a run. Read `error.code`, not
  `error.type`: the Python class name is gone, and `pdk` adds four codes (`pdk_not_installed`,
  `pdk_version_mismatch`, `pdk_install_busy`, `pdk_install_failed`).
