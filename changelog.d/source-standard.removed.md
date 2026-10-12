- A source table no longer takes `standard` or `variant`, which no flow read: set the VHDL
  standard of the design with `language.vhdl.standard`. A source table with any other unknown key
  is refused too. Every design gets a new identity, so flows run once more.
