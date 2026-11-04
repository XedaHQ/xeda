- Remove the loader hooks, hash and fingerprint helpers of `Design`, `Rtl` and `Source`, and
  `Project.referenced_name`, from the public API. `Source` takes only a path and a type, and
  `Plan` takes no private fields in its constructor.
