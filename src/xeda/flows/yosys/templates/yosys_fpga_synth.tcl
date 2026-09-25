yosys logger -notime
{% include 'read_files.tcl' -%}

{% if settings.prep is not none -%}
yosys prep {%- if settings.flatten %} -flatten {%- endif %} {%- if design.rtl.top %} -top {{design.rtl.top}} {%- else %} -auto-top {%- endif %} {{settings.prep|join(" ")}}
{% else %}
yosys proc
{% if settings.flatten -%}
yosys flatten
{% endif -%}
{% endif -%}

{% include "post_rtl.tcl" -%}

{% if settings.pre_synth_opt -%}
yosys log -stdout "** Pre-synthesis optimization **"
yosys opt -undriven -purge -keepdc -noff
{% endif -%}

yosys opt_clean -purge

{% if settings.abc9 and not settings.noabc -%}
{% if settings.flow3 -%} yosys scratchpad -copy abc9.script.flow3 abc9.script {%- endif %}
{# decrease the target delay to account for interconnect delay #}
{% if settings.main_clock and settings.main_clock.period_ps -%} yosys scratchpad -set abc9.D {{settings.main_clock.period_ps / 1.5}} {%- endif %}
{% endif -%}

yosys log -stdout "** FPGA synthesis for device {{settings.fpga|tcl_quote}} **"
yosys log -stdout "*** Target: {{(settings.fpga.family or settings.fpga.vendor)|tcl_quote}} ***"
yosys {{synth_command|join(" ")}} {% if design.rtl.top %} -top {{design.rtl.top}}{% endif %}


{% if settings.post_synth_opt -%}
yosys log -stdout "** Post-synthesis optimization **"
yosys opt -full -fine -purge -sat -undriven
{% endif -%}

yosys opt_clean -purge

{% if settings.splitnets -%}
yosys splitnets
{% endif -%}

{% include "write_netlist.tcl" -%}
