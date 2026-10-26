- A design generator runs in the launch, in its own storage `<run root>/.generated/<design>/<key>/`
  (`$XEDA_GENERATED`), so name a script as `$DESIGN_ROOT/gen.py`; a `rtl.sources` entry is wholly
  generated or wholly written (see `generated_sources`), and a dependency's generator runs too.
