yosys logger -notime
{% include 'read_files.tcl' -%}

{#- `synth_pass_only` runs the target's synthesis pass and nothing else, as
    `yosys -p 'synth_<target> ...' <sources>` does: the pass elaborates, flattens, optimizes and
    maps on its own. Every setting that would add a step before or after it is refused at launch
    (`YosysFpga.Settings.synth_pass_only_conflicts`), so the blocks it would render are only
    guarded here where the setting alone decides. #}
{% set pass_only = synth_pass_only|default(false) -%}

{% if not pass_only -%}
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
{% endif -%}

{#- ABC9's script and target delay: computed in one place, and empty under `synth_pass_only` #}
{% for command in settings.abc9_scratchpad() -%}
yosys {{command}}
{% endfor -%}

yosys log -stdout "** FPGA synthesis for device {{settings.fpga|tcl_quote}} **"
yosys log -stdout "*** Target: {{(settings.fpga.family or settings.fpga.vendor)|tcl_quote}} ***"
yosys {{synth_command|join(" ")}} {% if design.rtl.top %} -top {{design.rtl.top}}{% endif %}


{% if not pass_only -%}
{% if settings.post_synth_opt -%}
yosys log -stdout "** Post-synthesis optimization **"
yosys opt -full -fine -purge -sat -undriven
{% endif -%}

yosys opt_clean -purge

{% if settings.splitnets -%}
yosys splitnets
{% endif -%}
{% endif -%}

{% include "write_netlist.tcl" -%}
