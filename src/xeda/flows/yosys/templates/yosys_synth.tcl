
{% include 'read_files.tcl' %}

{% if settings.prep is not none %}
yosys prep {% if settings.flatten %} -flatten {% endif %} {%if design.rtl.top %} -top {{design.rtl.top}} {% else %} -auto-top {% endif %} {{settings.prep|join(" ")}}
{% else %}
yosys proc
{% if settings.flatten %}
yosys flatten
{% endif %}
{% endif %}

{% include "post_rtl.tcl" %}

yosys log -stdout "Running synthesis"
yosys synth {{settings.synth_flags|join(" ")}} {% if design.rtl.top %} -top {{design.rtl.top}}{% endif %}

{#- TODO: ##### LSOracle ###### #}

{% if settings.post_synth_opt %}
yosys log -stdout "Post-synth optimization"
yosys opt -full -purge -sat
{% else %}
yosys opt -purge
{% endif %}

{% if settings.adder_map %}
yosys extract_fa
yosys techmap -map {{settings.adder_map}}
yosys techmap
yosys opt -purge
{% endif %}

{% for map in settings.other_maps %}
yosys techmap -map {{map}}
{% endfor %}

{% if settings.liberty %}
yosys log -stdout " Mapping FFs to technology library"
yosys dfflibmap -liberty {% if settings.dff_liberty %} {{settings.dff_liberty}} {% else %} {{settings.liberty[0]}} {% endif %}
yosys opt
{% endif %}

yosys log -stdout " Running ABC"
yosys abc {{settings.abc_flags|join(" ")}}
{%- if settings.main_clock and settings.main_clock.period_ps %} -D {{"%.3f"|format(settings.main_clock.period_ps)}} {% endif %}
{%- if abc_script_file %} -script {{abc_script_file}} {% endif %}
{%- if settings.liberty %} -liberty {{settings.liberty[0]}} {% endif %}
{%- if abc_constr_file %} -constr {{abc_constr_file}} {% endif %}

# replace undefined values with 0
yosys setundef -zero

{% if settings.splitnets %}
yosys splitnets {% if settings.splitnets_driver %} -driver {% endif %}
{% endif %}

yosys opt_clean -purge

{% if settings.hilomap %}
yosys hilomap {% if settings.hilomap.singleton %} -singleton {% endif %} -hicell {{settings.hilomap.hi|join(" ")}} -locell {{settings.hilomap.lo|join(" ")}}
{% endif %}
{% if settings.insbuf %}
yosys insbuf -buf {{settings.insbuf|join(" ")}}
{% endif %}

{% include "write_netlist.tcl" %}
