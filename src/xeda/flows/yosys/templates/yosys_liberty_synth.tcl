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
    The memory limit (`memory.max_bits`, with `memory.mock_large`) is a Tcl-only mode too: the
    procedure `check_memory_sizes` reads the size of each memory from the design.
    `abc.mapper: abc_new` maps with `abc_new`, which takes its script from a scratchpad variable.
    The hierarchical mode and `abc_new` read the Liberty files again with their models
    (whiteboxes): `convert_liberty_areas` takes the areas from them and `abc_new` the functions.
    The maps of the user (`techmap_files`) run at the point that each `when` names: before the
    first part of `synth`, after the coarse part (and the memory mapping and the retiming), before
    the flip-flop mapping, and after it. The finishing options of the group `finish` switch the
    removal of formal cells, the renaming, the single-bit vectors, the extra checks, and the tie
    and buffer cells. -#}
{%- set liberty_read = true -%}
{%- set keep_min_cost = settings.hierarchy.keep_min_cost -%}
{%- set wrap_operators = settings.arithmetic.wrap_operators -%}
{%- set abc_new = settings.abc.mapper == "abc_new" -%}
{%- set clockgate_map = asic.clockgate_map -%}
{#- `-extra-map` acts in the fine part of `synth`, which operator wrapping replaces -#}
{%- set extra_map -%}
{%- set architecture = {"kogge_stone": "kogge-stone", "han_carlson": "han-carlson", "sklansky": "sklansky"}.get(settings.arithmetic.adder_architecture) -%}
{%- if architecture and not wrap_operators %} -extra-map +/choices/{{architecture}}.v{% endif -%}
{%- endset -%}
{%- set dont_use_words -%}
{%- for cell in dont_use %} -dont_use {{cell|tcl_word}}{% endfor -%}
{%- endset -%}
{%- set coarse_words -%}
{%- if not settings.coarse.fsm %} -nofsm{% endif -%}
{%- if not settings.coarse.share %} -noshare{% endif -%}
{%- if not settings.arithmetic.alumacc %} -noalumacc{% endif -%}
{%- if settings.arithmetic.multiplier == "booth" %} -booth{% endif -%}
{%- if settings.arithmetic.arith_tree %} -arith_tree{% endif -%}
{%- if not settings.memory.merge_ff %} -nordff{% endif -%}
{%- if not settings.memory.rw_check %} -no-rw-check{% endif -%}
{%- endset -%}
{%- set hieropt = " -hieropt" if settings.hierarchy.cross_boundary_opt else "" -%}
{#- `dfflegalize` takes the limits of the sequential group (`-mince`, `-minsrst`) as flags. -#}
{%- set legalize_limits = render_flags(settings.sequential) -%}
{#- the flags of `abc` that the group `abc` owns (`-dress`, `-liberty_args`) -#}
{%- set abc_options = render_flags(settings.abc) -%}
{%- set fa = settings.arithmetic.extract_fa -%}
{#- `extract_fa` refuses a limit (`-d`, `-b`) as its last word, so the kinds of adders come after
    the limits, and a run with limits names both kinds when it extracts both. -#}
{%- set extract_fa_words -%}
{%- set fa_flags = render_flags(fa) -%}
{%- if fa_flags %} {{fa_flags}}{% endif -%}
{%- if fa.full and fa.half %}{% if fa_flags %} -fa -ha{% endif %}
{%- elif fa.full %} -fa
{%- else %} -ha
{%- endif -%}
{%- endset -%}
{%- set max_bits = settings.memory.max_bits -%}
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
yosys proc{% if settings.coarse.latches %} -latches {{settings.coarse.latches}}{% endif %}
{%- for instance in settings.hierarchy.keep_instances %}
setattr -set keep_hierarchy 1 {{instance|path}}
{%- endfor %}
{%- if settings.hierarchy.separator %}
scratchpad -set flatten.separator {{settings.hierarchy.separator|path}}
{%- endif %}
{%- if settings.flatten and keep_min_cost is none %}
flatten
{%- endif %}

{% include "post_rtl.tcl" %}
{#- as in the `.ys` script, `stop_after == "rtl"` omits the rest #}
{%- if settings.stop_after != "rtl" %}
{%- if settings.finish.check_pre %}
check
{%- endif %}
{%- for map in asic.techmaps("pre_synth") %}
techmap -map {{map|read_path}}
{%- endfor %}
{%- if max_bits is not none %}
# The procedure `check_memory_sizes` follows two parts of OpenROAD-flow-scripts at commit
# 6b305a712966be72d8908695c04398acdbe273ca, which has the license BSD-3-Clause (see
# `data/asic/NOTICE.md` of Xeda): the block that mocks the large memories in `flow/scripts/synth.tcl`
# and the size test of `flow/scripts/mem_dump.py`. Xeda lists the memories in the log of yosys and
# writes no report file. A memory above `max_bits` shrinks to one row if `mock` is 1, and stops the run
# otherwise. `keep` is 1 to give the module of a shrunk memory the `keep_hierarchy` attribute.
proc check_memory_sizes { max_bits mock keep } {
  memory_collect
  set select [tee -q -s result.string select -list t:\$mem_v2]
  set too_large [list]
  foreach path [split [string trim $select] "\n"] {
    if { $path eq "" } {
      continue
    }
    set index [string first "/" $path]
    set module [string range $path 0 [expr { $index - 1 }]]
    set instance [string range $path [expr { $index + 1 }] end]
    set width [rtlil::get_param -uint $module $instance WIDTH]
    set size [rtlil::get_param -uint $module $instance SIZE]
    set nbits [expr { $width * $size }]
    log "Memory $path has dimensions $size x $width = $nbits"
    if { $nbits <= $max_bits } {
      continue
    }
    if { !$mock } {
      lappend too_large "$path ($nbits bits)"
      continue
    }
    rtlil::set_param -uint $module $instance SIZE 1
    log -stdout "Shrunk memory $path from $size rows to 1"
    if { $keep } {
      select -module $module
      setattr -mod -set keep_hierarchy 1
      select -clear
    }
  }
  if { [llength $too_large] > 0 } {
    error "memory above memory.max_bits ($max_bits bits): [join $too_large {, }]. Raise\
      memory.max_bits, or set memory.mock_large to shrink the memory to one row"
  }
}
{%- endif %}
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
synth -run :fine{{coarse_words}}{{extra_map}}{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
convert_liberty_areas
keep_hierarchy -min_cost {{keep_min_cost}}
synth -flatten -run coarse:fine{{coarse_words}}{{hieropt}}{{extra_map}}{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
{%- else %}
synth{% if settings.flatten %} -flatten{% endif %} -run :fine{{coarse_words}}{{hieropt}}{{extra_map}}{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
{%- endif %}
{%- if settings.sequential.tristate == "logic" %}
tribuf -logic
opt_clean
{%- endif %}
{%- if asic.memory_libmap %}
memory_libmap{% for lib in asic.memory_libmap %} -lib {{lib|path}}{% endfor %}
{%- endif %}
{%- if asic.memory_techmap %}
techmap -map {{asic.memory_techmap|read_path}}
{%- endif %}
{%- if max_bits is not none %}
check_memory_sizes {{max_bits}} {{1 if settings.memory.mock_large else 0}} {{1 if settings.memory.keep_mocked else 0}}
{%- endif %}
{%- if settings.sequential.retime_modules %}
select{% for name in settings.sequential.retime_modules %} {{name|path}}{% endfor %}
opt -fast -full
memory_map
opt -full
techmap
abc -dff{% if settings.abc.keep_ff %} -keepff{% endif %} -script {{asic.retime_script|path}}
select -clear
{%- endif %}
{%- for map in asic.techmaps("post_coarse") %}
techmap -map {{map|read_path}}
{%- endfor %}
{%- if wrap_operators %}
source {{asic.wrap_script|tcl_word}}
wrap_operators {{asic.booth_map|tcl_word}} {{asic.wrapped_adders|tcl_list}} {{asic.wrapped_multipliers|tcl_list}}
{%- else %}
synth{% if design.rtl.top %} -top {{design.rtl.top}}{% endif %} -run fine: -noabc{{hieropt}}{{extra_map}}{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
{%- endif %}
{%- if settings.finish.drop_formal %}
chformal -remove
delete {t:$print}
{%- endif %}
{%- if settings.finish.names == "rename_wire" %}
yosys rename -wire
{%- endif %}
opt -purge{% if settings.coarse.opt_dff_sat %} -sat{% endif %}
{%- if asic.adder_map and wrap_operators %}
if {![design_has_extracted_operators]} {
  extract_fa{{extract_fa_words}}
  techmap -map {{asic.adder_map|read_path}}
}
{%- elif asic.adder_map %}
extract_fa{{extract_fa_words}}
techmap -map {{asic.adder_map|read_path}}
{%- endif %}
techmap
opt -fast -purge
{%- if asic.latch_map %}
dfflegalize -cell {$_DLATCH_P_} x -cell {$_DLATCH_N_} x {t:$_DLATCH_*}
techmap -map {{asic.latch_map|read_path}}
{%- endif %}
{%- for map in asic.techmaps("pre_dff") %}
techmap -map {{map|read_path}}
{%- endfor %}
{%- if settings.sequential.clock_gating.enabled %}
{%- set gating = settings.sequential.clock_gating %}
clockgate
{%- if gating.pos_icg or gating.neg_icg -%}
{%- if gating.pos_icg %} -pos {{gating.pos_icg[0]|path}} {{(gating.pos_icg[1] ~ ":" ~ gating.pos_icg[2] ~ ":" ~ gating.pos_icg[3])|path}}{% endif -%}
{%- if gating.neg_icg %} -neg {{gating.neg_icg[0]|path}} {{(gating.neg_icg[1] ~ ":" ~ gating.neg_icg[2] ~ ":" ~ gating.neg_icg[3])|path}}{% endif -%}
{%- else -%}
{%- for lib in settings.liberty %} -liberty {{lib|path}}{% endfor %}{{dont_use_words}}
{%- endif -%}
{%- set gating_flags = render_flags(gating) -%}
{%- if gating_flags %} {{gating_flags}}{% endif %}
{%- endif %}
{%- if asic.dff_map %}
dfflegalize{% for cell in asic.dff_map_cells %} -cell {{cell|path}} x{% endfor %}{% if legalize_limits %} {{legalize_limits}}{% endif %}
techmap -map {{asic.dff_map|read_path}}
{%- else %}
{%- if legalize_limits %}
dfflegalize{% for cell in asic.legalize_any %} -cell {{cell|path}} x{% endfor %} {{legalize_limits}}
{%- endif %}
dfflibmap{% for lib in ([asic.dff_liberty] if asic.dff_liberty else settings.liberty) %} -liberty {{lib|read_path}}{% endfor %}{{dont_use_words}}
{%- endif %}
{%- for map in asic.techmaps("post_dff") %}
techmap -map {{map|read_path}}
{%- endfor %}
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
{%- if abc_options %} {{abc_options}}{% endif %}
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
{%- if settings.finish.names == "autoname" %}
autoname
{%- endif %}
{%- if settings.finish.normalize_single_bit %}
attrmap -remove single_bit_vector
{%- endif %}
{#- the wrapped modules leave `$buf` cells, which `check -mapped` refuses #}
check{% if settings.check_assert %} -assert{% endif %}{% if not wrap_operators %} -mapped{% if settings.finish.allow_tristate %} -allow-tbuf{% endif %}{% endif %}

{#- A Verilog library read with `-lib` keeps its `lib_whitebox` models with their `always` blocks,
    which `write_json` refuses to write. A liberty library holds blackboxes only. #}
{%- if settings.verilog_lib %}
blackbox =A:whitebox
{%- endif %}
{% include "write_netlist.tcl" %}

log -stdout "***** Synthesis Completed *****"
{%- endif %}
