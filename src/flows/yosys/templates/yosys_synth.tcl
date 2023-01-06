
{% include 'read_files.tcl' %}

{% if settings.prep is not none %}
yosys prep {% if settings.flatten %} -flatten {% endif %} {%if design.rtl.top %} -top {{design.rtl.top}} {% else %} -auto-top {% endif %} {{settings.prep|join(" ")}}
{% else %}
yosys proc
{% if settings.flatten %}
yosys flatten
{% endif %}
{% endif %}

{% if settings.rtl_json %}
puts "$log_prefix Writing JSON {{settings.rtl_json}}"
yosys write_json {{settings.rtl_json}}
{% endif %}
{% if settings.rtl_verilog %}
puts "$log_prefix Writing Verilog {{settings.rtl_verilog}}"
yosys write_verilog {{settings.rtl_verilog}}
{% endif %}
{% if settings.rtl_vhdl %}
puts "$log_prefix  Writing VHDL {{settings.rtl_vhdl}}"
yosys write_vhdl {{settings.rtl_vhdl}}
{% endif %}
{% if settings.show_rtl %}
yosys show -prefix rtl_show -format dot {{settings.show_rtl_flags|join(" ")}}
{% endif %}

if { {{settings.stop_after == "rtl"}} } {
    exit
}

puts "$log_prefix  Running synthesis"
yosys synth {{settings.synth_flags|join(" ")}} {% if design.rtl.top %} -top {{design.rtl.top}}{% endif %}

{#- TODO: ##### LSOracle ###### #}

yosys opt -purge

{% if settings.post_synth_opt %}
puts "$log_prefix Post-synth optimization"
yosys opt -full -purge -sat
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
puts "$log_prefix  Mapping FFs to technology library"
yosys dfflibmap -liberty {% if settings.dff_liberty %} {{settings.dff_liberty}} {% else %} {{settings.liberty[0]}} {% endif %}
yosys opt
{% endif %}

puts "$log_prefix  Running ABC"
yosys abc {{settings.abc_flags|join(" ")}}
    {%- if clock_period_ps %} -D {{clock_period_ps}} {% endif %}
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

tee -o "synth_check.log" check {% if settings.check_assert %} -assert {% endif %}

puts "$log_prefix Writing stat to {{artifacts["utilization_report"]}}"
tee -9 -q -o {{artifacts["utilization_report"]}} stat {% if artifacts["utilization_report"].endswith(".json") %} -json {% endif %} {% if settings.liberty %} {% for lib in settings.liberty %} -liberty {{lib}} {% endfor %} {% elif settings.gates %} -tech cmos {% endif %}

{% if artifacts.netlist_json %}
puts "$log_prefix Writing netlist {{artifacts.netlist_json}}"
yosys write_json {{artifacts.netlist_json}}
{% endif %}
{% if artifacts.netlist_verilog %}
puts "$log_prefix Writing netlist {{artifacts.netlist_verilog}}"
yosys write_verilog -noattr -noexpr -nohex -nodec {{artifacts.netlist_verilog}}
{% endif %}

{% if settings.sta %}
puts "$log_prefix Writing timing report to {{artifacts["timing_report"]}}"
tee -o {{artifacts["timing_report"]}} ltp
tee -a {{artifacts["timing_report"]}} sta
{% endif %}

{% if settings.show_netlist %}
puts "$log_prefix Writing netlist diagram to {{settings.show_netlist}}"
yosys show -prefix netlist_show -format dot {{settings.show_netlist_flags|join(" ")}}
{% endif %}
