- The `sby` flow now fails a `prove` whose assumptions no trace meets within a bound, as `bmc` does.
  It no longer reports such a proof as a pass. `prove` now also needs `yosys-smtbmc` and Bitwuzla.
