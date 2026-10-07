{#- See post_rtl.ys: `write_json -selected` writes the design's own modules only, every block
    ends its last command with a newline, and `stop_after == "rtl"` is handled by the including
    template, which simply does not emit the post-RTL commands. -#}
{% if settings.rtl_json -%}
yosys log -stdout "Writing JSON {{settings.rtl_json|tcl_quote}}"
yosys write_json -selected {{settings.rtl_json|path}}
{% endif -%}
{% if settings.rtl_verilog -%}
yosys log -stdout "Writing Verilog {{settings.rtl_verilog|tcl_quote}}"
yosys write_verilog {{settings.rtl_verilog|path}}
{% endif -%}
{% if settings.rtl_graph -%}
yosys log -stdout "Writing RTL graph to {{settings.rtl_graph.with_suffix('.dot')|tcl_quote}}"
yosys show -prefix {{settings.rtl_graph.with_suffix("")|verbatim_path}} -format dot {{settings.rtl_graph_flags|join(" ")}}
{% endif -%}
