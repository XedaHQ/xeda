
yosys logger -notime -stderr
{% include 'read_files.tcl' %}

{% if settings.prep is not none %}
yosys prep {%- if settings.flatten %} -flatten {%- endif %} {%- if design.rtl.top %} -top {{design.rtl.top}} {%- else %} -auto-top {%- endif %} {{settings.prep|join(" ")}}
{% else %}
yosys proc
{% if settings.flatten %}
yosys flatten
{% endif %}
{% endif %}

{% include "post_rtl.tcl" %}

{% if settings.pre_synth_opt %}
yosys log -stdout "** Pre-synthesis optimization **"
yosys opt -full -purge -sat
{% endif %}

{% if settings.abc9 -%}
{% if settings.flow3 -%} yosys scratchpad -copy abc9.script.flow3 abc9.script {%- endif %}
{# decrease the target delay to account for interconnect delay #}
{% if settings.main_clock and settings.main_clock.period_ps -%} yosys scratchpad -set abc9.D {{settings.main_clock.period_ps / 1.5}} {%- endif %}
{%- endif %}

yosys log -stdout "** FPGA synthesis for device {{settings.fpga}} **"
{% if settings.fpga.vendor == "xilinx" %}
yosys log -stdout "*** Target: Xilinx {%if settings.fpga.part%} {{settings.fpga.part}} {%else%} {{settings.fpga.device}} {%endif%} ***"
yosys synth_xilinx {% if settings.fpga.family %} -family {{settings.fpga.family}} {% endif %} {{settings.synth_flags|join(" ")}}
{% elif settings.fpga.family %}
yosys log -stdout "*** Target: {{settings.fpga.family}} ***"
yosys synth_{{settings.fpga.family}} {{settings.synth_flags|join(" ")}} {% if design.rtl.top %} -top {{design.rtl.top}}{% endif %}
{% else %}
yosys log -stdout "[ERROR] Unknown FPGA vendor, family, or device"
{% endif %}


{% if settings.post_synth_opt %}
yosys log -stdout "** Post-synthesis optimization **"
yosys opt -full -purge -sat
{% endif %}

{% if settings.splitnets %}
yosys splitnets
{% endif %}

{% include "write_netlist.tcl" %}
