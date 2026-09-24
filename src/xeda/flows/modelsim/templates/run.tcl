puts "\n===========================( Compiling HDL Sources )==========================="
{%- for src in design.sim_sources if src.type %}
{%- if src.type.name in ("Verilog", "SystemVerilog") %}
if { [catch {vlog {{src.file|tcl_word}}{% if src.type.name == "SystemVerilog" or src.variant == "systemverilog" %} -sv{% endif %} {{vlog_opts}} } error]} {
    puts $error
    exit 1
}
{%- elif src.type.name == "Vhdl" %}
if { [catch {vcom {{src.file|tcl_word}} {{vcom_opts}} {%- if design.language.vhdl.standard in ("93", "1993") %} -93 {% elif design.language.vhdl.standard in ("08", "2008") %} -2008 {% elif design.language.vhdl.standard %} -{{design.language.vhdl.standard}} {% endif -%} } error]} {
    puts $error
    exit 1
}
{%- endif %}
{%- endfor %}

puts "\n===========================( Running simulation )==========================="

{% if settings.vcd %}
vcd file {{settings.vcd|tcl_word}}
{% endif %}

puts "\n===========================( *ENABLE ECHO* )==========================="
if { [catch {vsim -t ps {{design.sim_tops|join(' ')}} {{vsim_opts|map("tcl_word")|join(' ')}} } error]} {
    puts $error
    exit 1
}
vcd add -r {% if not settings.debug and design.tb.uut %} {{design.tb.uut}}/* {% else %} * {% endif %}
#run_wave
run {% if settings.stop_time is not none %} {{settings.stop_time}} {%- else %} -all {%- endif %}
puts "\n===========================( *DISABLE ECHO* )==========================="

{% if settings.vcd %}
vcd flush
{% endif %}

exit
