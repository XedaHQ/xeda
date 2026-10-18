- `liberty` is now a setting of the OpenROAD flows too, and it agrees along a chain with `yosys`
  like `platform` does. OpenROAD refuses a Liberty file with another time unit than the platform's,
  or a first one with another capacitance unit, and earlier OpenROAD results are not reused.
