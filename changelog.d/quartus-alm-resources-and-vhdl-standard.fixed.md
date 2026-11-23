- `quartus` reports `lut` and `ff` for Cyclone V and other ALM families, and leaves out a slack
  that the report gives as `N/A`. A `language.vhdl.standard` of `08` or `93` now gives a valid
  input version, and a standard that Quartus does not know is refused at planning.
