{% for arg in settings.post_synth_rename %}
yosys rename {{arg}}
{% endfor %}

tee -o "final_check.log" check {% if settings.check_assert %} -assert {% endif %}

yosys log -stdout "Writing stat to {{artifacts["utilization_report"]}}"
tee -q -o {{artifacts["utilization_report"]}} stat {% if (artifacts.utilization_report|string).endswith(".json") %} -json {% endif %} {% if settings.liberty is defined and settings.liberty %} {% for lib in settings.liberty %} -liberty {{lib}} {% endfor %} {% elif settings.gates is defined and settings.gates %} -tech cmos {% endif %}
{%- if settings.sta %}
yosys log -stdout "Writing timing report to {{artifacts["timing_report"]}}"
tee -o {{artifacts["timing_report"]}} ltp
tee -a {{artifacts["timing_report"]}} sta
{%- endif %}

{%- if settings.ltp %}
tee -o ltp.out ltp
{%- endif %}

{%- if settings.debug or settings.verbose > 1 %}
yosys echo on
{%- endif %}

{%- if artifacts.netlist_json %}
yosys log -stdout "Writing netlist {{artifacts.netlist_json}}"
yosys write_json {{artifacts.netlist_json}}
{%- endif %}

{%- if artifacts.netlist_verilog %}
{% for attr in settings.netlist_unset_attributes %}
yosys setattr -unset {{attr}}
{% endfor %}
yosys log -stdout "Writing netlist {{artifacts.netlist_verilog}}"
yosys write_verilog {{settings.netlist_verilog_flags|join(" ")}} {{artifacts.netlist_verilog}}
{%- endif %}

{%- if settings.write_blif %}
yosys log -stdout "Writing BLIF to {{settings.write_blif}}"
yosys write_blif -noalias {{settings.write_blif}}
{%- endif %}

{%- if settings.netlist_dot %}
yosys log -stdout "Writing netlist graph to {{settings.netlist_dot}}"
yosys show -prefix {{settings.netlist_dot.rsplit(".", 1)[0]}} -format dot {{settings.netlist_dot_flags|join(" ")}}
{%- endif %}

puts "***** Yosys DONE *****"
