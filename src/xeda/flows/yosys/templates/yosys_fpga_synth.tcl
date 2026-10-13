yosys logger -notime
{% include 'read_files.tcl' -%}

{#- `synth_pass_only` runs the synthesis pass of the family and nothing else, as
    `yosys -p 'synth_<family> ...' <sources>` does: the pass elaborates, flattens, optimizes and
    maps on its own. The launcher refuses every setting that would add a step before or after the
    pass (`YosysFpga.Settings.synth_pass_only_conflicts`), so this template guards a block only
    where the setting alone decides. #}
{% set pass_only = synth_pass_only|default(false) -%}

{% if not pass_only -%}
{% if settings.prep is not none -%}
yosys prep {%- if settings.effective_flatten() %} -flatten {%- endif %} {%- if design.rtl.top %} -top {{design.rtl.top}} {%- else %} -auto-top {%- endif %} {{settings.prep|join(" ")}}
{% else %}
yosys proc
{% if settings.effective_flatten() -%}
yosys flatten
{% endif -%}
{% endif -%}

{% include "post_rtl.tcl" -%}
{% endif -%}
{#- as in the `.ys` script, `stop_after == "rtl"` omits the rest -#}
{% if settings.stop_after != "rtl" -%}

{% if not pass_only -%}
{% if settings.pre_synth_opt -%}
yosys log -stdout "** Pre-synthesis optimization **"
yosys opt -undriven -purge -keepdc -noff
{% endif -%}

yosys opt_clean -purge
{% endif -%}

{#- One method computes the ABC9 choices. Pass-only may render an explicit script, but it omits
    the inferred flow3 and the delay derived from the clock. #}
{% for command in settings.abc9_scratchpad() -%}
yosys {{command}}
{% endfor -%}

yosys log -stdout "** FPGA synthesis for device {{settings.fpga|tcl_quote}} **"
yosys log -stdout "*** Family: {{(settings.fpga.family or settings.fpga.vendor)|tcl_quote}} ***"
yosys {{synth_command|join(" ")}} {% if design.rtl.top %} -top {{design.rtl.top}}{% endif %}


{% if not pass_only -%}
{% if settings.post_synth_opt -%}
yosys log -stdout "** Post-synthesis optimization **"
yosys opt -full -fine -purge -sat -undriven
{% endif -%}

yosys opt_clean -purge

{% if settings.splitnets -%}
yosys splitnets {%- if settings.splitnets_driver %} -driver {%- endif %} {%- if settings.splitnets_ports %} -ports {%- endif %}
{% endif -%}
{% endif -%}

{% include "write_netlist.tcl" -%}
{% endif -%}
