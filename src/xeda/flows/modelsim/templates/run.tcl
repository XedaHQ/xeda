{#- ModelSim's `exit` takes its status as `-code N`: a plain `exit 1` exits vsim with status 0 #}
puts "\n===========================( Compiling HDL Sources )==========================="
{%- for src in design.sim_sources if src.type %}
{%- if src.type.name in ("Verilog", "SystemVerilog") %}
if { [catch {vlog {{src.file|tcl_word}}{% if src.type.name == "SystemVerilog" or src.variant == "systemverilog" %} -sv{% endif %} {{vlog_opts|map("tcl_word")|join(' ')}} } error]} {
    puts $error
    exit -code 1
}
{%- elif src.type.name == "Vhdl" %}
if { [catch {vcom {{src.file|tcl_word}} {{vcom_opts|map("tcl_word")|join(' ')}} {%- if design.language.vhdl.standard in ("93", "1993") %} -93 {% elif design.language.vhdl.standard in ("08", "2008") %} -2008 {% elif design.language.vhdl.standard %} -{{design.language.vhdl.standard}} {% endif -%} } error]} {
    puts $error
    exit -code 1
}
{%- endif %}
{%- endfor %}

puts "\n===========================( Running simulation )==========================="

{% if settings.vcd %}
vcd file {{settings.vcd|tcl_word}}
{% endif %}

puts "\n===========================( *ENABLE ECHO* )==========================="
{#- `-onfinish stop`: `$finish` (VHDL `std.env.finish`) returns to this script rather than exiting
    vsim with status 0, which would skip the VCD flush and the test-status check below #}
if { [catch {vsim -t ps -onfinish stop {{design.sim_tops|join(' ')}} {{vsim_opts|map("tcl_word")|join(' ')}} } error]} {
    puts $error
    exit -code 1
}
vcd add -r {% if not settings.debug and design.tb.uut %} {{design.tb.uut}}/* {% else %} * {% endif %}
#run_wave
run {% if settings.stop_time is not none %} {{settings.stop_time}} {%- else %} -all {%- endif %}
puts "\n===========================( *DISABLE ECHO* )==========================="

{% if settings.vcd %}
vcd flush
{% endif %}

{#- TESTSTATUS is the most severe message the simulation reported: 0 note, 1 warning, 2 error
    (`$error`, a VHDL `severity error` assertion), 3 failure, 4 fatal. A testbench signals a failed
    test with one of these, and vsim exits with status 0 regardless. #}
set test_status [lindex [coverage attribute -name TESTSTATUS -concise] 0]
if { $test_status >= {{fail_status}} } {
    puts "ERROR: the simulation reported a message of severity {{settings.fail_severity}} or higher (TESTSTATUS=$test_status)"
    exit -code 1
}

exit
