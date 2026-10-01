set_param tclapp.enableGitAccess 0

{% include 'util.tcl' %}

set design_name    {{design.name|tcl_word}}
set snapshot_name  snapshot

load_feature simulator

set analyze_flags "-work {{settings.work_lib|tcl_quote}} {%- if settings.debug %} -verbose 2 {%- endif %} {{settings.analyze_flags|join(' ')}}"

puts "\n===========================( Analyzing HDL Sources )==========================="
{%- for src in design.sim_sources %}
{%- if src.type.name == "Verilog" %}
puts "Analyzing Verilog file {{src.file|tcl_quote}}"
if { [catch {exec xvlog {*}$analyze_flags {{src.file|tcl_word}}} error]} {
    errorExit $error
}
{%- elif src.type.name == "SystemVerilog" %}
puts "Analyzing SystemVerilog file {{src.file|tcl_quote}}"
if { [catch {exec xvlog {*}$analyze_flags -sv {{src.file|tcl_word}}} error]} {
    errorExit $error
}
{%- elif src.type.name == "Vhdl" %}
puts "Analyzing VHDL file {{src.file|tcl_quote}} {% if design.language.vhdl.standard -%} \[VHDL {{design.language.vhdl.standard|tcl_quote}}\] {%- endif %}"
if { [catch {exec xvhdl {*}$analyze_flags {% if design.language.vhdl.standard in ("08", "2008") %} -2008 {% elif design.language.vhdl.standard in ("93", "1993") %} -93_mode {% endif %} {{src.file|tcl_word}}} error]} {
    errorExit $error
}
{%- endif %}
{%- endfor %}

puts "\n===========================( Elaborating design )==========================="
if { [catch {exec xelab -s ${snapshot_name} -L {{settings.work_lib|tcl_word}} {%- for l,_ in settings.lib_paths %} -L {{l|tcl_word}} {%- endfor %} {{settings.elab_flags|join(' ')}} {{settings.optimization_flags|join(' ')}} {% if settings.xelab_log %} -log {{settings.xelab_log|tcl_word}} {%- endif %} {%- for k,v in design.tb.parameters.items() %} -generic_top {{("%s=%s"|format(k,v))|tcl_word}} {%- endfor %} {%- for top in design.tb.top %} {{top|tcl_word}} {%- endfor %}  } error]} {
    errorExit $error
}

puts "\n===========================( Loading Simulation )==========================="
if { [catch {xsim ${snapshot_name} {{settings.sim_flags|join(' ')}} -onfinish stop -onerror stop } error] } {
    errorExit $error
}

{%- if settings.saif %}
puts "\n===========================( Setting up SAIF )==========================="
{#- An earlier SAIF file in the run directory was removed before the script (`VivadoSim.run`,
    `RunDirectory.remove`): `open_saif` does not replace one. #}
open_saif {{settings.saif|tcl_word}}
{%- endif %}

{#- ## TODO: WDB support: open_wave_database ${wdb_file} #}

{%- if settings.vcd %}
puts "\n===========================( Setting up VCD )==========================="
open_vcd {{settings.vcd|tcl_word}}
## Vivado (tested on 2020.1) crashes if using * and shared/protected variables are present
## log_vcd [get_objects -r -filter { type == variable || type == signal || type == internal_signal || type == in_port || type == out_port || type == inout_port || type == port } /*]
log_vcd {%- if settings.is_quiet %} -quiet {%- elif settings.verbose %} -verbose {%- endif %} {%- if settings.vcd_level %} -level {{settings.vcd_level}} {%- endif %} {%- if settings.vcd_scope %} {{settings.vcd_scope|tcl_word}} {%- endif %}
{%- endif %}
{%- if settings.debug_traces %}
ltrace on
ptrace on
{%- endif %}

# The native Vivado log was opened before compilation. Only this bounded section is runtime.
puts XEDA_XSIM_RUNTIME_START
flush stdout
set xeda_runtime_log {{runtime_log|tcl_word}}
set xeda_stop_fs {{stop_fs if stop_fs is not none else -1}}

proc xedaTimeFs {} {
    if {![regexp {^([0-9.]+) *(fs|ps|ns|us|ms|sec)$} [current_time] -> value unit]} {
        errorExit "Unrecognized xsim current_time"
    }
    set scale [dict get {fs 1 ps 1000 ns 1000000 us 1000000000 ms 1000000000000 sec 1000000000000000} $unit]
    return [expr {wide(round($value * $scale))}]
}
proc xedaCheckpoint {stage} {
    puts "XEDA_XSIM_CHECKPOINT=$stage|[current_time]|[get_property PRECISION [current_sim]]"
    flush stdout
}
proc xedaRuntimeDone {} {
    # Do not continue past HDL finish/stop/fatal during prerun. A Tcl return proves none of these.
    if {[file type $::xeda_runtime_log] ne "file"} {errorExit "Unsafe xsim runtime log"}
    set channel [open $::xeda_runtime_log r]
    set text [read $channel]
    close $channel
    set marker "XEDA_XSIM_RUNTIME_START\n"
    set start [string first $marker $text]
    if {$start < 0} {errorExit "Missing xsim runtime boundary"}
    set text [string range $text [expr {$start + [string length $marker]}] end]
    return [regexp -line {\$(finish|stop) called at time : [0-9.]+ (fs|ps|ns|us|ms|sec)( : File ".+" Line [0-9]+)?$} $text]
}

set xeda_done 0
{%- if prerun_fs is not none %}
set xeda_prerun_fs {{prerun_fs}}
if {$xeda_stop_fs >= 0 && $xeda_prerun_fs > $xeda_stop_fs} {
    set xeda_prerun_fs $xeda_stop_fs
}
if {[catch {run $xeda_prerun_fs fs} error]} {
    xedaCheckpoint prerun
    errorExit $error
}
xedaCheckpoint prerun
set xeda_done [xedaRuntimeDone]
if {$xeda_stop_fs >= 0 && [xedaTimeFs] >= $xeda_stop_fs} {set xeda_done 1}
{%- endif %}

{%- if settings.saif %}
puts "Adding nets to be logged in SAIF"
set netlist_scope {{("./" ~ design.tb.uut)|tcl_word}}
describe $netlist_scope
log_saif [get_objects -r -filter { type == signal || type == internal_signal || type == in_port || type == out_port || type == inout_port || type == port } ${netlist_scope}/*]
{%- endif %}

if {!$xeda_done} {
    if {$xeda_stop_fs >= 0} {
        set xeda_remaining [expr {$xeda_stop_fs - [xedaTimeFs]}]
        if {[catch {run $xeda_remaining fs} error]} {
            xedaCheckpoint main
            errorExit $error
        }
    } else {
        if {[catch {run all} error]} {
            xedaCheckpoint main
            errorExit $error
        }
    }
}
xedaCheckpoint main
if {$xeda_stop_fs >= 0 && [xedaTimeFs] == $xeda_stop_fs && ![xedaRuntimeDone]} {
    puts "XEDA_XSIM_LIMIT=[current_time]"
}

{%- if settings.vcd %}
puts "\n===========================( Closing VCD file )==========================="
flush_vcd
close_vcd
{%- endif %}

{%- if settings.saif %}
puts "\n===========================( Closing SAIF file )==========================="
close_saif
{%- endif %}

puts XEDA_XSIM_RUNTIME_END
flush stdout
