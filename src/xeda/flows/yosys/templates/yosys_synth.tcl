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
log -stdout " Mapping FFs to technology library"
dfflibmap -liberty {% if settings.dff_liberty %} {{settings.dff_liberty}} {% else %} {{settings.liberty[0]}} {% endif %}
opt
{%- endif %}

{%- if not settings.noabc %}
log -stdout " Running ABC"
abc {{settings.abc_flags|join(" ")}}
{%- if settings.main_clock and settings.main_clock.period_ps %} -D {{"%.3f"|format(settings.main_clock.period_ps)}} {% endif %}
{%- if abc_script_file %} -script {{abc_script_file}} {%- endif %}
{%- if settings.liberty %} -liberty {{settings.liberty[0]}} {%- endif %}
{%- if abc_constr_file %} -constr {{abc_constr_file}} {%- endif %}
{%- endif %}
# replace undefined values with 0
setundef -zero

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

opt -full
clean -purge

{%- if settings.rmports %}
rmports
{%- endif %}

{% include "write_netlist.tcl" %}