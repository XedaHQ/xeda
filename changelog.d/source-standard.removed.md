- A source table no longer takes `standard` or `variant`, which no flow read: set the VHDL
  standard of the design with `language.vhdl.standard`. A source table with any other unknown key
  is refused too. A design with sources gets a new identity, so its flows run once more.
