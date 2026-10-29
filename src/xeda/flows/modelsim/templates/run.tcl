onerror {exit -code 1}
{#- ModelSim's `exit` takes its status as `-code N`: a plain `exit 1` exits vsim with status 0 #}
puts "\n===========================( Compiling HDL Sources )==========================="
{%- for src in sources_read(rtl=true, tb=true) %}
{%- if src.type.name in ("Verilog", "SystemVerilog") %}
if { [catch {vlog {{src.file|tcl_word}}{% if src.type.name == "SystemVerilog" %} -sv{% endif %} {{vlog_opts|map("tcl_word")|join(' ')}} } error]} {
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
{#- `-onfinish stop` makes `$finish` (VHDL `std.env.finish`) return to this script. Otherwise
    `$finish` exits vsim with status 0 and skips the VCD flush and the test-status check below. #}
if { [catch {vsim -t ps -onfinish stop {{design.sim_tops|map("tcl_word")|join(' ')}} {{vsim_opts|map("tcl_word")|join(' ')}} } error]} {
    puts $error
    exit -code 1
}
{#- Loading a design restores BreakOnAssertion from modelsim.ini (default 3, VHDL failure).
    Leave lower-severity assertions running so the testbench reaches its finish and status check.
    Fatal is the highest supported break level; a fatal break still returns to this script. #}
set BreakOnAssertion 4
# A built-in break returns to this script and must not skip the status and time checkpoint.
onbreak {resume}
{% if settings.vcd %}
vcd add -r {% if settings.verbosity != "debug" and design.tb.uut %} {{(design.tb.uut ~ "/*")|tcl_word}} {% else %} * {% endif %}
{% endif %}
# The launch option -logfile enables transcript capture in batch mode. Disable macro echo.
# The adapter bounds the runtime evidence with the markers below.
transcript off
if {![catch {file lstat modelsim_runtime.log existing}] && $existing(type) ne "file"} {
    error "Refusing a nonregular ModelSim runtime transcript path"
}
coverage attribute -name TESTSTATUS -value 0
# Reset the native attribute to exclude diagnostics from analysis and elaboration. Runtime
# messages raise it again. ModelSim echo reaches the transcript. Tcl puts reaches only the
# process stdout.
echo "XEDA_MODELSIM_RUNTIME_START"
if {[catch {run {% if stop_time is not none %} {{stop_time}} {% else %} -all {% endif %}} error]} {
    puts $error
    transcript file ""
    exit -code 1
}
# These are native observations. The script derives no completion token from the result of the
# run command.
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
