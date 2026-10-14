yosys logger -notime
{% if settings.maps_to_liberty() -%}
{% include 'yosys_liberty_synth.tcl' %}
{%- else -%}
{% include 'read_files.tcl' %}

{% if settings.prep is not none %}
prep {% if settings.flatten %} -flatten {% endif %} {%if design.rtl.top %} -top {{design.rtl.top}} {% else %} -auto-top {% endif %} {{settings.prep|join(" ")}}
{%- else %}
procs
{%- if settings.flatten %}
flatten
{%- endif %}
{%- endif %}

{% include "post_rtl.tcl" %}
{#- as in the `.ys` script, `stop_after == "rtl"` omits the rest #}
{%- if settings.stop_after != "rtl" %}

{%- if not settings.nosynth %}
log -stdout "Running synthesis"
synth {{settings.synth_flags|join(" ")}} {% if design.rtl.top %} -top {{design.rtl.top}}{% endif %} {%- if settings.noabc %} -noabc {%- endif %}
{%- endif %}

{#- TODO: ##### LSOracle ###### #}

{%- if settings.post_synth_opt %}
log -stdout "Post-synth optimization"
opt -full -purge -sat
{%- else %}
opt -purge
{%- endif %}

{%- if settings.liberty %}
log -stdout "Prepare mapping FFs to technology"
dfflibmap -prepare -liberty {{settings.liberty[0]|read_path}}
opt -purge
{%- endif %}

{%- if not settings.noabc %}
log -stdout "Running ABC"
abc {{settings.abc_flags|join(" ")}}
{%- if settings.main_clock and settings.main_clock.period_ps %} -D {{"%.3f"|format(settings.main_clock.period_ps)}} {% endif %}
{%- if settings.abc_script %} -script {{settings.abc_script|path}} {%- endif %}
{%- if settings.liberty %} -liberty {{settings.liberty[0]|path}} {%- endif %}
{%- if abc_constr_file %} -constr {{abc_constr_file|path}} {%- endif %}
opt -purge
{%- endif %}

{%- if settings.liberty %}
log -stdout "Mapping FFs to technology"
dfflibmap -liberty {{settings.liberty[0]|read_path}}
opt -purge
{%- endif %}

# replace undefined values with 0
setundef -zero
opt -full -purge

{%- if settings.splitnets %}
splitnets {%- if settings.splitnets_driver %} -driver {%- endif %} {%- if settings.splitnets_ports %} -ports {%-endif %}
{%- endif %}

opt_clean -purge


opt -full -purge
clean -purge

{%- if settings.rmports %}
rmports
{%- endif %}

{%- if settings.liberty %}
check {% if settings.check_assert -%} -assert {% endif -%} -mapped
{%- endif %}

{#- A Verilog library read with `-lib` keeps its `lib_whitebox` models with their `always` blocks,
    which `write_json` refuses to write. The FPGA passes end with this command, so none leaves a
    whitebox behind. `synth` does not. A liberty library holds blackboxes only. #}
{%- if settings.verilog_lib %}
blackbox =A:whitebox
{%- endif %}
{% include "write_netlist.tcl" %}

log -stdout "***** Synthesis Completed *****"
{%- endif %}
{%- endif %}
