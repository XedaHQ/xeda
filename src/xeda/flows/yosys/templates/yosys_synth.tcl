yosys logger -notime -stderr
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

{%- if settings.adder_map %}
extract_fa
techmap -map {{settings.adder_map}}
techmap
opt -purge
{%- endif %}

{%- for map in settings.other_maps %}
techmap -map {{map}}
{%- endfor %}

{%- if settings.liberty %}
log -stdout "Prepare mapping FFs to technology"
dfflibmap -prepare -liberty {% if settings.dff_liberty -%} {{settings.dff_liberty}} {%- else -%} {{settings.liberty[0]}} {%- endif %}
opt -purge
{%- endif %}

{%- if not settings.noabc %}
log -stdout "Running ABC"
abc {{settings.abc_flags|join(" ")}}
{%- if settings.main_clock and settings.main_clock.period_ps %} -D {{"%.3f"|format(settings.main_clock.period_ps)}} {% endif %}
{%- if settings.abc_script %} -script "{{settings.abc_script}}" {%- endif %}
{%- if settings.liberty %} -liberty "{{settings.liberty[0]}}" {%- endif %}
{%- if abc_constr_file %} -constr "{{abc_constr_file}}" {%- endif %}
opt -purge
{%- endif %}

{%- if settings.liberty %}
log -stdout "Mapping FFs to technology"
dfflibmap -liberty {% if settings.dff_liberty -%} {{settings.dff_liberty}} {%- else -%} {{settings.liberty[0]}} {%- endif %}
opt -purge
{%- endif %}

# replace undefined values with 0
setundef -zero
opt -full -purge

{%- if settings.splitnets %}
splitnets {%- if settings.splitnets_driver %} -driver {%- endif %} {%- if settings.splitnets_ports %} -ports {%-endif %}
{%- endif %}

opt_clean -purge

{%- if settings.hilomap %}
hilomap {% if settings.hilomap.singleton %} -singleton {% endif %} -hicell {{settings.hilomap.hi|join(" ")}} -locell {{settings.hilomap.lo|join(" ")}}
{%- endif %}
{% if settings.insbuf %}
insbuf -buf {{settings.insbuf|join(" ")}}
{%- endif %}

opt -full -purge
clean -purge

{%- if settings.rmports %}
rmports
{%- endif %}

{%- if settings.liberty %}
check {% if settings.check_assert -%} -assert {% endif -%} -mapped
{%- endif %}
{% include "write_netlist.tcl" %}

log -stdout "***** Synthesis Completed *****"
