{#- The mapping to a standard cell library: the commands of `yosys_liberty_synth.ys`, in the same
    order. Tcl has its own `proc` and `rename`, so `yosys` names those two passes. A `$` in a
    Tcl word starts a variable, so the words that hold one are in braces. The cells that the
    flow must not use (`dont_use`) are `tcl_word`s, so a `[`, `$` or `\` in a name reaches yosys
    as it is. -#}
{%- set liberty_read = true -%}
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
{% include 'read_files.tcl' %}
yosys proc
{%- if settings.flatten %}
flatten
{%- endif %}

{% include "post_rtl.tcl" %}
{#- as in the `.ys` script, `stop_after == "rtl"` omits the rest #}
{%- if settings.stop_after != "rtl" %}
synth{% if settings.flatten %} -flatten{% endif %} -run :fine{{extra_map}}{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
synth{% if design.rtl.top %} -top {{design.rtl.top}}{% endif %} -run fine: -noabc{{extra_map}}{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
chformal -remove
delete {t:$print}
yosys rename -wire
opt -purge
{%- if asic.adder_map %}
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
abc{% if asic.abc_script %} -script {{asic.abc_script|path}}{% endif %}{% for lib in settings.liberty %} -liberty {{lib|path}}{% endfor %}{{dont_use_words}}
{%- if abc_constr_file %} -constr {{abc_constr_file|path}}{% endif %}
{%- if asic.delay_target_ps is not none %} -D {{"%.3f"|format(asic.delay_target_ps)}}{% endif %}
{%- for flag in settings.abc_flags %} {{flag}}{% endfor %}
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
check {% if settings.check_assert -%} -assert {% endif -%} -mapped

{#- A Verilog library read with `-lib` keeps its `lib_whitebox` models with their `always` blocks,
    which `write_json` refuses to write. A liberty library holds blackboxes only. #}
{%- if settings.verilog_lib %}
blackbox =A:whitebox
{%- endif %}
{% include "write_netlist.tcl" %}

log -stdout "***** Synthesis Completed *****"
{%- endif %}
