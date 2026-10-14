{#- The mapping to a standard cell library: the commands of `yosys_liberty_synth.ys`, in the same
    order. Tcl has its own `proc` and `rename`, so `yosys` names those two passes. A `$` in a
    Tcl word starts a variable, so the words that hold one are in braces. The cells that the
    flow must not use (`dont_use`) are `tcl_word`s, so a `[`, `$` or `\` in a name reaches yosys
    as it is. -#}
{%- set liberty_read = true -%}
{%- set dont_use_words -%}
{%- for cell in dont_use %} -dont_use {{cell|tcl_word}}{% endfor -%}
{%- endset -%}
{% for lib in settings.liberty -%}
yosys read_liberty -overwrite -setattr liberty_cell -lib {{lib|read_path}}
{% endfor -%}
{% if settings.dff_liberty and settings.dff_liberty not in settings.liberty -%}
yosys read_liberty -overwrite -setattr liberty_cell -lib {{settings.dff_liberty|read_path}}
{% endif -%}
{% include 'read_files.tcl' %}
yosys proc
{%- if settings.flatten %}
flatten
{%- endif %}

{% include "post_rtl.tcl" %}
{#- as in the `.ys` script, `stop_after == "rtl"` omits the rest #}
{%- if settings.stop_after != "rtl" %}
synth{% if settings.flatten %} -flatten{% endif %} -run :fine{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
synth{% if design.rtl.top %} -top {{design.rtl.top}}{% endif %} -run fine: -noabc{% for flag in settings.synth_flags %} {{flag}}{% endfor %}
chformal -remove
delete {t:$print}
yosys rename -wire
opt -purge
{%- if settings.adder_map %}
extract_fa
techmap -map {{settings.adder_map|read_path}}
{%- endif %}
techmap
opt -fast -purge
{%- for map in settings.other_maps or [] %}
dfflegalize -cell {$_DLATCH_P_} x -cell {$_DLATCH_N_} x {t:$_DLATCH_*}
techmap -map {{map|read_path}}
{%- endfor %}
dfflibmap{% for lib in ([settings.dff_liberty] if settings.dff_liberty else settings.liberty) %} -liberty {{lib|read_path}}{% endfor %}{{dont_use_words}}
opt
setundef -zero
abc{% if settings.abc_script %} -script {{settings.abc_script|path}}{% endif %}{% for lib in settings.liberty %} -liberty {{lib|path}}{% endfor %}{{dont_use_words}}
{%- if abc_constr_file %} -constr {{abc_constr_file|path}}{% endif %}
{%- for flag in settings.abc_flags %} {{flag}}{% endfor %}
{%- if settings.splitnets %}
splitnets {%- if settings.splitnets_driver %} -driver {%- endif %} {%- if settings.splitnets_ports %} -ports {%- endif %}
{%- endif %}
opt_clean -purge
{%- if settings.hilomap %}
hilomap{% if settings.hilomap.singleton %} -singleton{% endif %} -hicell {{settings.hilomap.hi|join(" ")}} -locell {{settings.hilomap.lo|join(" ")}}
{%- endif %}
{%- if settings.insbuf %}
insbuf -buf {{settings.insbuf|join(" ")}}
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
