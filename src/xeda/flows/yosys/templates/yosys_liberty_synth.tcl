{#- The mapping to a standard cell library: the commands of `yosys_liberty_synth.ys`, in the same
    order. Tcl has its own `proc` and `rename`, so `yosys` names those two passes. A `$` in a
    Tcl word starts a variable, so the words that hold one are in braces. The cells that the
    flow must not use (`dont_use`) are `tcl_word`s, so a `[`, `$` or `\` in a name reaches yosys
    as it is.

    Two modes need the Tcl interface of yosys, so only this template has them:
    * Hierarchical synthesis (`hierarchy.keep_min_cost`) keeps each module that costs more than
      the given number of NAND2 gate equivalents and flattens the others. `proc` stays before the
      RTL outputs, since `write_json` refuses a module with processes.
    * Operator wrapping (`arithmetic.wrap_operators`) replaces the second `synth` with a procedure
      that wraps each wide `$alu` and `$macc` in a module per architecture. Its full-adder map
      runs only if no cell implements an operator yet, as in OpenROAD-flow-scripts.
    `abc.mapper: abc_new` maps with `abc_new`, which takes its script from a scratchpad variable.
    The hierarchical mode and `abc_new` read the Liberty files again with their models
    (whiteboxes): `convert_liberty_areas` takes the areas from them and `abc_new` the functions. -#}
{%- set liberty_read = true -%}
{%- set keep_min_cost = settings.hierarchy.keep_min_cost -%}
{%- set wrap_operators = settings.arithmetic.wrap_operators -%}
{%- set abc_new = settings.abc.mapper == "abc_new" -%}
{%- set clockgate_map = asic.clockgate_map -%}
{%- set extra_map -%}
{%- set architecture = {"kogge_stone": "kogge-stone", "han_carlson": "han-carlson", "sklansky": "sklansky"}.get(settings.arithmetic.adder_architecture) -%}
{%- if architecture %} -extra-map +/choices/{{architecture}}.v{% endif -%}
{%- endset -%}
{%- set dont_use_words -%}
{%- for cell in dont_use %} -dont_use {{cell|tcl_word}}{% endfor -%}
{%- endset -%}
{% for lib in settings.liberty -%}
yosys read_liberty -overwrite -setattr liberty_cell -lib {{lib|read_path}}
{% endfor -%}
{% if asic.dff_liberty and asic.dff_liberty not in settings.liberty -%}
yosys read_liberty -overwrite -setattr liberty_cell -lib {{asic.dff_liberty|read_path}}
{% endif -%}
{% if keep_min_cost is not none or abc_new -%}
{% for lib in settings.liberty -%}
yosys read_liberty -overwrite -setattr liberty_cell -unit_delay -wb -ignore_miss_func -ignore_buses {{lib|read_path}}
{% endfor -%}
{% if asic.dff_liberty and asic.dff_liberty not in settings.liberty -%}
yosys read_liberty -overwrite -setattr liberty_cell -unit_delay -wb -ignore_miss_func -ignore_buses {{asic.dff_liberty|read_path}}
{% endif -%}
{% endif -%}
{% include 'read_files.tcl' %}
yosys proc
{%- if settings.flatten and keep_min_cost is none %}
flatten
{%- endif %}

{% include "post_rtl.tcl" %}
{#- as in the `.ys` script, `stop_after == "rtl"` omits the rest #}
{%- if settings.stop_after != "rtl" %}
{%- if keep_min_cost is not none %}
# The procedure `convert_liberty_areas` comes from `flow/scripts/synth_preamble.tcl` of
# OpenROAD-flow-scripts at commit 6b305a712966be72d8908695c04398acdbe273ca
# (https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/blob/6b305a712966be72d8908695c04398acdbe273ca/flow/scripts/synth_preamble.tcl),
# which has the license BSD-3-Clause (see `data/asic/NOTICE.md` of Xeda). Xeda left out its last
# part, which sets the cost of the macros that the memory generator of that flow makes.
# `keep_hierarchy -min_cost` reads the cost that it gives to each Liberty cell.
proc convert_liberty_areas { } {
  cellmatch -derive_luts =A:liberty_cell
  # find a reference nand2 gate
  set found_cell ""
  set found_cell_area ""
  # iterate over all cells with a nand2 signature
  foreach cell [tee -q -s result.string select -list-mod =*/a:lut=4'b0111 %m] {
    if { ![rtlil::has_attr -mod $cell area] } {
      puts "Cell $cell missing area information"
      continue
    }
    set area [rtlil::get_attr -string -mod $cell area]
    if { $found_cell == "" || $area < $found_cell_area } {
      set found_cell $cell
      set found_cell_area $area
    }
  }
  if { $found_cell == "" } {
    error "reference nand2 cell not found"
  }

  # convert the area on all Liberty cells to a gate number equivalent
  foreach box [tee -q -s result.string select -list-mod =A:area =A:liberty_cell %i] {
    set area [rtlil::get_attr -mod -string $box area]
    set gate_eq [expr int($area / $found_cell_area)]
    rtlil::set_attr -mod -uint $box gate_cost_equivalent $gate_eq
  }
}
synth -run :fine{{extra_map}}{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
convert_liberty_areas
keep_hierarchy -min_cost {{keep_min_cost}}
synth -flatten -run coarse:fine{{extra_map}}{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
{%- else %}
synth{% if settings.flatten %} -flatten{% endif %} -run :fine{{extra_map}}{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
{%- endif %}
{%- if wrap_operators %}
source {{asic.wrap_script|tcl_word}}
wrap_operators {{asic.booth_map|tcl_word}} {} {}
{%- else %}
synth{% if design.rtl.top %} -top {{design.rtl.top}}{% endif %} -run fine: -noabc{{extra_map}}{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
{%- endif %}
chformal -remove
delete {t:$print}
yosys rename -wire
opt -purge
{%- if asic.adder_map and wrap_operators %}
if {![design_has_extracted_operators]} {
  extract_fa
  techmap -map {{asic.adder_map|read_path}}
}
{%- elif asic.adder_map %}
extract_fa
techmap -map {{asic.adder_map|read_path}}
{%- endif %}
techmap
opt -fast -purge
{%- if asic.latch_map %}
dfflegalize -cell {$_DLATCH_P_} x -cell {$_DLATCH_N_} x {t:$_DLATCH_*}
techmap -map {{asic.latch_map|read_path}}
{%- endif %}
{%- for map in asic.techmap_files %}
techmap -map {{map|read_path}}
{%- endfor %}
dfflibmap{% for lib in ([asic.dff_liberty] if asic.dff_liberty else settings.liberty) %} -liberty {{lib|read_path}}{% endfor %}{{dont_use_words}}
opt
{%- if settings.finish.tie_undefined == "zero" %}
setundef -zero
{%- elif settings.finish.tie_undefined == "one" %}
setundef -one
{%- endif %}
{%- if abc_new %}
scratchpad -set abc9.script {{asic.abc_new_script|tcl_word}}
abc_new
{%- else %}
abc{% if asic.abc_script %} -script {{asic.abc_script|path}}{% endif %}
{%- endif %}{% for lib in settings.liberty %} -liberty {{lib|path}}{% endfor %}{{dont_use_words}}
{%- if abc_constr_file %} -constr {{abc_constr_file|path}}{% endif %}
{%- if asic.delay_target_ps is not none %} -D {{"%.3f"|format(asic.delay_target_ps)}}{% endif %}
{%- for flag in settings.abc_flags %} {{flag}}{% endfor %}
{%- if abc_new %}
delete {t:$specify*}
{%- endif %}
{%- if settings.splitnets %}
splitnets {%- if settings.splitnets_driver %} -driver {%- endif %} {%- if settings.splitnets_ports %} -ports {%- endif %}
{%- endif %}
opt_clean -purge
{%- if asic.tie_cells %}
hilomap{% if asic.tie_cells.singleton %} -singleton{% endif %} -hicell {{asic.tie_cells.hi|join(" ")}} -locell {{asic.tie_cells.lo|join(" ")}}
{%- endif %}
{%- if asic.buffer_cell %}
insbuf -buf {{asic.buffer_cell|join(" ")}}
{%- endif %}
{%- if settings.rmports %}
rmports
{%- endif %}
{#- the wrapped modules leave `$buf` cells, which `check -mapped` refuses #}
check{% if settings.check_assert %} -assert{% endif %}{% if not wrap_operators %} -mapped{% endif %}

{#- A Verilog library read with `-lib` keeps its `lib_whitebox` models with their `always` blocks,
    which `write_json` refuses to write. A liberty library holds blackboxes only. #}
{%- if settings.verilog_lib %}
blackbox =A:whitebox
{%- endif %}
{% include "write_netlist.tcl" %}

log -stdout "***** Synthesis Completed *****"
{%- endif %}
