{% for arg in settings.post_synth_rename -%}
yosys rename {{arg}}
{% endfor -%}

tee -o "final_check.log" check {% if settings.check_assert %} -assert {% endif %}

yosys log -stdout "Writing stat to {{artifacts["utilization_report"]|tcl_quote}}"
tee -q -o {{artifacts["utilization_report"]|path}} stat {% if (artifacts.utilization_report|string).endswith(".json") %} -json {% endif %} {% if settings.liberty is defined and settings.liberty %} {% for lib in settings.liberty %} -liberty {{lib|path}} {% endfor %} {% elif settings.gates is defined and settings.gates %} -tech cmos {% endif %}
{# An analysis runs on a copy of the design, so that it never changes what the script writes.
    The `sta` pass derives every parametrized cell type that it meets. The derived library cell
    has processes, which `write_json` refuses, and the netlist gets a definition that no cell
    uses. -#}
{% if settings.sta or settings.ltp -%}
yosys design -push-copy
{% if settings.sta -%}
yosys log -stdout "Writing timing report to {{artifacts["timing_report"]|tcl_quote}}"
tee -o {{artifacts["timing_report"]|path}} ltp
tee -a {{artifacts["timing_report"]|path}} sta
{% endif -%}
{% if settings.ltp -%}
tee -o ltp.out ltp
{% endif -%}
yosys design -pop
{% endif -%}

{% if settings.debug or settings.verbose > 1 -%}
yosys echo on
{% endif -%}

{#- Remove the attributes once, before the script writes any netlist: from the objects of every
    module and from the modules themselves. `=*` selects the library boxes too. -#}
{% if artifacts.get("netlist_json") or artifacts.get("netlist_verilog") or artifacts.get("netlist_edif") or settings.write_blif -%}
{% for attr in settings.attributes_to_unset() -%}
yosys setattr -unset {{attr}} =*
yosys setattr -mod -unset {{attr}} =*
{% endfor -%}
{% endif -%}

{% if artifacts.get("netlist_json") -%}
yosys log -stdout "Writing netlist {{artifacts.netlist_json|tcl_quote}}"
yosys write_json {{artifacts.netlist_json|path}}
{% endif -%}

{#- `-pvector bra` writes every bus with its range. Without it the netlist has a bus as plain
    bits, and Vivado reads member i as bit i. A yosys bus has the most significant bit as member
    0, so it comes out reversed, with no message. -#}
{% if artifacts.get("netlist_edif") -%}
yosys log -stdout "Writing netlist {{artifacts.netlist_edif|tcl_quote}}"
yosys write_edif -pvector bra {{artifacts.netlist_edif|path}}
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
