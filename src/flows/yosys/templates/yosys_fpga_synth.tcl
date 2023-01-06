
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

puts "$log_prefix Running FPGA synthesis for device {{settings.fpga}}"
{% if settings.fpga.vendor == "xilinx" %}
    puts "$log_prefix Target: Xilinx {%if settings.fpga.part%} {{settings.fpga.part}} {%else%} {{settings.fpga.device}} {%endif%}"
    yosys synth_xilinx {% if settings.fpga.family %} -family {{settings.fpga.family}} {% endif %} {{settings.synth_flags|join(" ")}}
{% elif settings.fpga.family %}
    puts "$log_prefix  Target: {{settings.fpga.family}}"
    yosys synth_{{settings.fpga.family}} {{settings.synth_flags|join(" ")}} {% if design.rtl.top %} -top {{design.rtl.top}}{% endif %}
{% else %}
    puts "$log_prefix Unknown FPGA vendor, family, or device"
{% endif %}


{% if settings.post_synth_opt %}
    puts "$log_prefix Post-synth optimization"
    yosys opt -full -purge -sat
{% endif %}

{% if settings.splitnets %}
    yosys splitnets
{% endif %}

puts "$log_prefix Writing stat to {{artifacts["utilization_report"]}}"
yosys tee -9 -q -o {{artifacts["utilization_report"]}} stat {% if artifacts["utilization_report"].endswith(".json") %} -json {% endif %} {% if settings.fpga.vendor == "xilinx" %} -tech xilinx {% endif %}

yosys check {% if settings.check_assert %} -assert {% endif %}

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
yosys tee -o {{artifacts["timing_report"]}} ltp
yosys tee -a {{artifacts["timing_report"]}} sta
{% endif %}

{% if settings.show_netlist %}
puts "$log_prefix Writing netlist diagram to {{settings.show_netlist}}"
yosys show -prefix netlist_show -format dot {{settings.show_netlist_flags|join(" ")}}
{% endif %}
