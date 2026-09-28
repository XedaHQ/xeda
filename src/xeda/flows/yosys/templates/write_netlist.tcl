{% for arg in settings.post_synth_rename -%}
yosys rename {{arg}}
{% endfor -%}

tee -o "final_check.log" check {% if settings.check_assert %} -assert {% endif %}

yosys log -stdout "Writing stat to {{artifacts["utilization_report"]|tcl_quote}}"
tee -q -o {{artifacts["utilization_report"]|path}} stat {% if (artifacts.utilization_report|string).endswith(".json") %} -json {% endif %} {% if settings.liberty is defined and settings.liberty %} {% for lib in settings.liberty %} -liberty {{lib|path}} {% endfor %} {% elif settings.gates is defined and settings.gates %} -tech cmos {% endif %}
{% if settings.sta -%}
yosys log -stdout "Writing timing report to {{artifacts["timing_report"]|tcl_quote}}"
tee -o {{artifacts["timing_report"]|path}} ltp
tee -a {{artifacts["timing_report"]|path}} sta
{% endif -%}

{% if settings.ltp -%}
tee -o ltp.out ltp
{% endif -%}

{% if settings.debug or settings.verbose > 1 -%}
yosys echo on
{% endif -%}

{#- Remove the attributes once, before any netlist is written, from every module's objects
    and from the modules themselves; `=*` selects the library boxes too. -#}
{% if artifacts.get("netlist_json") or artifacts.get("netlist_verilog") or settings.write_blif -%}
{% for attr in settings.attributes_to_unset() -%}
yosys setattr -unset {{attr}} =*
yosys setattr -mod -unset {{attr}} =*
{% endfor -%}
{% endif -%}

{% if artifacts.get("netlist_json") -%}
yosys log -stdout "Writing netlist {{artifacts.netlist_json|tcl_quote}}"
yosys write_json {{artifacts.netlist_json|path}}
{% endif -%}

{% if artifacts.get("netlist_verilog") -%}
yosys log -stdout "Writing netlist {{artifacts.netlist_verilog|tcl_quote}}"
yosys write_verilog {{settings.write_verilog_flags()|join(" ")}} {{artifacts.netlist_verilog|path}}
{% endif -%}

{% if settings.write_blif -%}
yosys log -stdout "Writing BLIF to {{settings.write_blif|tcl_quote}}"
yosys write_blif -noalias {{settings.write_blif|path}}
{% endif -%}

{% if settings.netlist_graph -%}
yosys log -stdout "Writing netlist graph to {{settings.netlist_graph.with_suffix('.dot')|tcl_quote}}"
yosys show -prefix {{settings.netlist_graph.with_suffix("")|verbatim_path}} -format dot {{settings.netlist_graph_flags|join(" ")}}
{% endif -%}
