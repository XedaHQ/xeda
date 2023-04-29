
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

yosys log -stdout "Running FPGA synthesis for device {{settings.fpga}}"
{% if settings.fpga.vendor == "xilinx" %}
yosys log -stdout "Target: Xilinx {%if settings.fpga.part%} {{settings.fpga.part}} {%else%} {{settings.fpga.device}} {%endif%}"
yosys synth_xilinx {% if settings.fpga.family %} -family {{settings.fpga.family}} {% endif %} {{settings.synth_flags|join(" ")}}
{% elif settings.fpga.family %}
yosys log -stdout " Target: {{settings.fpga.family}}"
yosys synth_{{settings.fpga.family}} {{settings.synth_flags|join(" ")}} {% if design.rtl.top %} -top {{design.rtl.top}}{% endif %}
{% else %}
yosys log -stdout "Unknown FPGA vendor, family, or device"
{% endif %}


{% if settings.post_synth_opt %}
yosys log -stdout "Post-synth optimization"
yosys opt -full -purge -sat
{% endif %}

{% if settings.splitnets %}
yosys splitnets
{% endif %}

{% include "write_netlist.tcl" %}
