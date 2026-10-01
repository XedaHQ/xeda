onerror {exit -code 1}
{#- ModelSim's `exit` takes its status as `-code N`: a plain `exit 1` exits vsim with status 0 #}
puts "\n===========================( Compiling HDL Sources )==========================="
{%- for src in design.sim_sources if src.type %}
{%- if src.type.name in ("Verilog", "SystemVerilog") %}
if { [catch {vlog {{src.file|tcl_word}}{% if src.type.name == "SystemVerilog" or src.variant == "systemverilog" %} -sv{% endif %} {{vlog_opts|map("tcl_word")|join(' ')}} } error]} {
    puts $error
    exit -code 1
}
{%- elif src.type.name == "Vhdl" %}
if { [catch {vcom {{src.file|tcl_word}} {{vcom_opts|map("tcl_word")|join(' ')}} {%- if design.language.vhdl.standard in ("93", "1993") %} -93 {% elif design.language.vhdl.standard in ("08", "2008") %} -2008 {% elif design.language.vhdl.standard %} {{("-" ~ design.language.vhdl.standard)|tcl_word}} {% endif -%} } error]} {
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
if { [catch {vsim -t ps -onfinish stop {{design.sim_tops|map("tcl_word")|join(' ')}} {{vsim_opts|map("tcl_word")|join(' ')}} } error]} {
    puts $error
    exit -code 1
}
{#- Loading a design restores BreakOnAssertion from modelsim.ini (default 3, VHDL failure).
    Leave lower-severity assertions running so the testbench reaches its finish and status check.
    Fatal is the highest supported break level; a fatal break still returns to this script. #}
set BreakOnAssertion 4
# A builtin break returns to our script; it must not skip the status/time checkpoint.
onbreak {resume}
vcd add -r {% if not settings.debug and design.tb.uut %} {{(design.tb.uut ~ "/*")|tcl_word}} {% else %} * {% endif %}
# Launch -logfile enables transcript capture in batch mode. Disable macro echo;
# the adapter bounds runtime evidence with the markers below.
transcript off
if {![catch {file lstat modelsim_runtime.log existing}] && $existing(type) ne "file"} {
    error "Refusing a nonregular ModelSim runtime transcript path"
}
coverage attribute -name TESTSTATUS -value 0
# Exclude analysis/elaboration diagnostics; runtime messages raise this native attribute again.
# ModelSim echo reaches the transcript; Tcl puts only reaches process stdout.
echo "XEDA_MODELSIM_RUNTIME_START"
if {[catch {run {% if stop_time is not none %} {{stop_time}} {% else %} -all {% endif %}} error]} {
    puts $error
    transcript file ""
    exit -code 1
}
# These are native observations, not completion tokens invented from a returned run command.
set run_state [runStatus -full]
set test_status [lindex [coverage attribute -name TESTSTATUS -concise] 0]
if {![string is integer -strict $test_status] || $test_status < 0 || $test_status > 3} {
    error "Invalid ModelSim TESTSTATUS"
}
echo "XEDA_MODELSIM_RUN_STATUS=$run_state"
set fd [open modelsim_end.txt {WRONLY CREAT EXCL}]
puts $fd "XEDA_MODELSIM_V1"
puts $fd $now
puts $fd $resolution
puts $fd $test_status
puts $fd $run_state
close $fd
{% if settings.vcd %}
vcd flush
{% endif %}
# Closing the file flushes the runtime transcript, including returned assertion/finish breaks.
transcript file ""

{#- TESTSTATUS is 0 OK, 1 warning, 2 error, 3 fatal. ModelSim groups VHDL severity failure
    and SystemVerilog $fatal at 3, unlike BreakOnAssertion's separate 3 and 4 levels.
    A testbench signals a failed test with one of these, and vsim exits with status 0 regardless. #}
if { $test_status >= {{fail_status}} } {
    puts "ERROR: simulation TESTSTATUS=$test_status reached threshold {{fail_status}} (fail_severity={{settings.fail_severity}})"
    exit -code 1
}

exit
