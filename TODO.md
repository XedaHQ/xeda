# Documentation
- [ ] Everything
- [ ] tutorials on readthedocs
- [ ] Developer docs for adding new plugins
- [ ] Developer docs for adding new flows

# Main code

- [ ] Idea: Some code in Suite should be refactored to a FlowRunner class, suites/flows? should provide a run method.
- [ ] parallel runs
- [ ] Flow chaining
- [ ] move DSE code to a plugin
- [ ] Installation / set-up of tools
- [ ] Run in docker

## Flows

- [ ] Modelsim
- [ ] ghdl + yosys + nextpnr-ecp5
- [ ] Synopsys VCS
- [ ] Synopsys DC
- [ ] Synopsys ICC2
- [ ] OpenROAD/OpenLANE

# Plugins

## LWC plugins

- [ ] LwcSim post_run should actually do the `regexp` on stdout and/or check results.txt
- [ ] LwcSim replicate settings 
- [ ] LwcSynth post_results: 1. check for usage of forbidden resources 2. only keep relevant data 