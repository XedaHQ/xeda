#!/usr/bin/env python3

import configparser
import inspect
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from time import sleep
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Union,
    runtime_checkable,
)
from zipfile import ZipFile

import click

from xeda.dataclass import XedaBaseModel, asdict

log = logging.getLogger()

RESOURCE_DIR = Path(__file__).parent.absolute() / "resource"


def write_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        with open(path, "wb") as f:
            f.write(data)
    else:
        if data is None:
            data = []
        with open(path, "w") as f:
            if isinstance(data, list):
                f.writelines(data)
            else:
                f.write(data)


# The commands of the tool a script runs are recorded, not run: `unknown` catches every command
# tclsh does not know. The recording goes to `fake_<tool>.calls` in the working directory (the
# run directory), one `CALL <n>` line per command followed by its `ARG` lines -- and `ELEM` lines
# for the files of an argument that is a TCL list (`[list "a b.v"]`). The commands named in
# `XEDA_FAKE_TOOL_FAIL` (a TCL list) raise a TCL error after being recorded, and
# `XEDA_FAKE_TOOL_RETURNS` makes calls return what a test says (`__returns`). A tool's commands
# that `TCL_MODEL` models also write the files the real ones write (`__output`), unless
# `XEDA_FAKE_TOOL_NO_OUTPUT` is set: then every step succeeds and writes nothing. A model fails
# the steps it runs that `XEDA_FAKE_TOOL_FAIL` names, as the tool does (ISE's processes, the
# steps of Vivado's runs).
TCL_RECORDER = r"""
set __calls [open {%(calls)s} a]
proc __record {args} {
    puts $::__calls "CALL [llength $args]"
    foreach a $args {
        puts $::__calls "ARG $a"
        if {![catch {llength $a} n] && $n >= 1 && [lindex $a 0] ne $a} {
            foreach e $a { puts $::__calls "ELEM $e" }
        }
    }
    flush $::__calls
    return 1
}
# `XEDA_FAKE_TOOL_FAIL`: the tool commands that fail, as a compiler does on a bad source
set __fail [expr {[info exists ::env(XEDA_FAKE_TOOL_FAIL)] ? $::env(XEDA_FAKE_TOOL_FAIL) : {}}]
# What a tool command returns where the recorder's 1 would be misread (Vivado's
# `get_msg_config -count` is a number of messages, and 1 an error that never happened), or where
# a test makes the tool report what it would in a case the fake does not reach on its own:
# `XEDA_FAKE_TOOL_RETURNS`, a TCL dict of the same form, is merged over these. A key is the
# leading words of a call, and the longest that matches wins: `{get_property STATUS impl_1}`
# answers for that property of that run only.
set __returns [dict create get_msg_config 0]
if {[info exists ::env(XEDA_FAKE_TOOL_RETURNS)]} {
    set __returns [dict merge $__returns $::env(XEDA_FAKE_TOOL_RETURNS)]
}
proc __returned {words result} {
    for {set n [llength $words]} {$n > 0} {incr n -1} {
        set key [lrange $words 0 [expr {$n - 1}]]
        if {[dict exists $::__returns $key]} { return [dict get $::__returns $key] }
    }
    return $result
}
# A tool command a model answers: recorded, failed if `XEDA_FAKE_TOOL_FAIL` names it, and
# returning `result` unless `__returns` answers for it.
proc __model {result args} {
    __record {*}$args
    if {[lindex $args 0] in $::__fail} { error "[lindex $args 0] failed" }
    __returned $args $result
}
proc __call {args} { __model 1 {*}$args }
proc unknown {args} { __call {*}$args }
set __no_output [expr {[info exists ::env(XEDA_FAKE_TOOL_NO_OUTPUT)] ? $::env(XEDA_FAKE_TOOL_NO_OUTPUT) ne "" : 0}]
proc __output {path} {
    if {$::__no_output} { return }
    file mkdir [file dirname $path]
    close [open $path w]
}
# the value of option `name` in `words` (`-impl x`), or ""
proc __option {words name} {
    set i [lsearch -exact $words $name]
    expr {$i < 0 ? "" : [lindex $words $i+1]}
}
rename source __source
proc source {args} { __record source {*}$args }
# a program named in `XEDA_FAKE_TOOL_FAIL` fails, as `exec` of a failing program does
proc exec {args} {
    __record exec {*}$args
    if {[lindex $args 0] in $::__fail} { error "[lindex $args 0] failed" }
    return ""
}
rename package __package
proc package {sub args} {
    if {$sub eq "require"} { __record package require {*}$args; return 1 }
    __package $sub {*}$args
}
proc set_app_var {name value} { __record set_app_var $name $value; uplevel #0 [list set $name $value] }
set search_path {}
namespace eval rdi { variable mode batch }
rename exit __exit
%(exit_proc)s
%(tool_model)s
%(tool_procs)s
if {[catch {__source {%(script)s}} e]} {
    puts $::__calls "TCL-ERROR $e"
    puts stderr "TCL-ERROR in %(script)s: $e\n$::errorInfo"
    flush $::__calls
    __exit 1
}
flush $::__calls
"""


# A tool's own `exit`, where it differs from TCL's. ModelSim's takes its status as `-code N` and
# ignores anything else: `exit 1` exits vsim with status 0, so a script must not rely on it.
TCL_EXIT = {
    "vsim": r"""proc exit {args} {
    set i [lsearch -exact $args -code]
    set code [expr {$i >= 0 ? [lindex $args [expr {$i + 1}]] : 0}]
    if {$::__no_output && [file exists modelsim_end.txt]} {file delete modelsim_end.txt}
    flush $::__calls; __exit $code
}""",
}
TCL_EXIT_DEFAULT = "proc exit {{code 0}} { flush $::__calls; __exit $code }"

# A tool's own commands, where recording them is not enough: what they return, and the files the
# real ones write -- the project a tool creates by name among them, as the real one does. (Their
# other effects on a project's files are `TCL_TOOL_PROCS`.)
TCL_MODEL = {
    # Synthetic ModelSim/Questa state, not measured vendor transcripts. The rendered script
    # executes under tclsh; runtime invocation, native reason, transcript and checkpoint are
    # independent. Ordinary fixtures finish; the behavioral oracle explicitly stays silent.
    "vsim": r"""
set __vsim_transcript {}
set __vsim_logfile [lindex $argv 0]
set __vsim_batch [lindex $argv 1]
set __vsim_enabled [expr {!$__vsim_batch || $__vsim_logfile ne ""}]
if {$__vsim_logfile ne "" && !$__no_output} {
    set __vsim_transcript [open $__vsim_logfile w]
}
set __vsim_state [expr {[info exists ::env(XEDA_FAKE_MODELSIM_STATE)] ? $::env(XEDA_FAKE_MODELSIM_STATE) : "finish0"}]
set now 0
set resolution 1ps
set __vsim_status 0
set __vsim_reason {ready end}
proc echo {args} {
    __call echo {*}$args
    set message [join $args " "]
    if {$::__vsim_transcript ne ""} {
        puts $::__vsim_transcript "# $message"
        flush $::__vsim_transcript
    }
    puts "# $message"
}
proc transcript {sub args} {
    __call transcript $sub {*}$args
    if {$sub eq "file"} {
        if {$::__vsim_transcript ne ""} {close $::__vsim_transcript; set ::__vsim_transcript {}}
        if {[lindex $args 0] ne "" && $::__vsim_enabled && !$::__no_output} {
            set ::__vsim_transcript [open [lindex $args 0] w]
        }
    }
}
proc vsim {args} {
    __call vsim {*}$args
    if {[info exists ::env(XEDA_FAKE_MODELSIM_LOAD_STATUS)]} {
        set ::__vsim_status $::env(XEDA_FAKE_MODELSIM_LOAD_STATUS)
        echo {** Error: analysis/load diagnostic}
    }
}
proc run {args} {
    __call run {*}$args
    set marker [open fake_vsim.runtime w]; puts $marker {runtime executed}; close $marker
    set ::now 5000
    set ::__vsim_reason {break simulation_stop {$finish}}
    switch -- $::__vsim_state {
        finish0 {set ::now 0}
        finish5 {}
        vhdl_finish {echo {Break in Process line__1 at tb.vhd line 3}}
        vhdl_stop {
            set ::__vsim_reason {break simulation_stop unknown}
            echo {Break in Process line__1 at uut.vhd line 3}
        }
        silent {set ::now 0; set ::__vsim_reason {ready end}}
        drain5 {set ::__vsim_reason {ready end}}
        error_finish {set ::__vsim_status 2; echo {** Error: Assertion error.}}
        warning_finish {set ::__vsim_status 1; echo {** Warning: Assertion warning.}}
        failure_finish {set ::__vsim_status 3; echo {** Failure: Assertion failure.}}
        fatal {
            set ::__vsim_status 3; set ::__vsim_reason {break simulation_stop unknown}
            echo {** Fatal: fatal check}
        }
        verilog_stop {
            set ::__vsim_reason {break simulation_stop unknown}
            echo {** Note: $stop    : tb.sv(3)}
            echo {Break in Module tb at tb.sv line 3}
        }
        status2_finish {set ::__vsim_status 2}
        limit10 {set ::now 10000; set ::__vsim_reason {ready end}}
        limit5 {set ::__vsim_reason {ready end}}
        break10 {set ::now 10000; set ::__vsim_reason {break user_break}}
        lookalike {
            set ::__vsim_reason {ready end}
            echo {** Note: Calling 'finish'}
            echo {** Note: body ** Error: fake}
            echo {initial $finish;}
            echo {puts "XEDA_MODELSIM_RUN_STATUS=break simulation_stop {$finish}"}
        }
        hang {echo {runtime waiting}; after 30000}
        default {error "unknown fake ModelSim state $::__vsim_state"}
    }
    if {$::__vsim_status > 0 && $::__vsim_state ne "status2_finish"} {
        echo {   Time: 5 ns  Iteration: 0  Instance: /tb}
    }
}
proc runStatus {args} {__model $::__vsim_reason runStatus {*}$args}
proc coverage {args} {
    set value [__option $args -value]
    if {$value ne ""} {set ::__vsim_status $value}
    __model $::__vsim_status coverage {*}$args
}
""",
    # ISE's `process run` reports a failed process only by its result and the process status
    # (`process get <name> status`): it raises no TCL error. A process named in
    # `XEDA_FAKE_TOOL_FAIL` fails, as `XEDA_FAKE_ISE_FAILURE` says: `result` (it returns false),
    # `status` (its status is `errors`), otherwise both. The project directory gets the reports
    # of "Implement Design" and the bitstream of "Generate Programming File", named after the top.
    "xtclsh": r"""
set __ise_top {}
array set __ise_status {}
# `project new` fails on an existing project (the script then opens it)
proc project {sub args} {
    if {$sub eq "set" && [lindex $args 0] eq "top"} { set ::__ise_top [lindex $args 1] }
    set result [__call project $sub {*}$args]
    if {$sub eq "new"} {
        set name [lindex $args 0]
        if {[file exists $name.xise]} { error "project $name already exists" }
        set f [open $name.xise w]; puts $f "<project name=\"$name\"/>"; close $f
    }
    return $result
}
proc process {command name args} {
    __record process $command $name {*}$args
    if {$command eq "get" && $args eq "status"} {
        if {[info exists ::__ise_status($name)]} { return $::__ise_status($name) }
        return never_run
    }
    if {$command ne "run"} { return 1 }
    if {$name in $::__fail} {
        set how [expr {[info exists ::env(XEDA_FAKE_ISE_FAILURE)] ? $::env(XEDA_FAKE_ISE_FAILURE) : ""}]
        set ::__ise_status($name) [expr {$how eq "result" ? "up_to_date" : "errors"}]
        return [expr {$how eq "status"}]
    }
    set ::__ise_status($name) up_to_date
    switch -- $name {
        "Implement Design" { __output $::__ise_top.syr; __output ${::__ise_top}_par.xrpt }
        "Generate Programming File" { __output $::__ise_top.bit }
    }
    return 1
}
""",
    # Diamond writes an implementation's files into its directory, named `<project>_<impl>`: the
    # map report, the place & route and timing reports, and the bitstream -- only when Export is
    # asked for its Bitgen task, as the default Export tasks are the device's.
    "diamondc": r"""
set __diamond_project {}
proc prj_project {command args} {
    if {$command eq "new"} { set ::__diamond_project $args }
    __call prj_project $command {*}$args
}
proc prj_run {step args} {
    set result [__call prj_run $step {*}$args]
    set name [__option $::__diamond_project -name]_[__option $args -impl]
    set impl [file join [__option $::__diamond_project -impl_dir] $name]
    switch -- $step {
        Map { __output $impl.mrp }
        PAR { __output $impl.par; __output $impl.twr }
        Export { if {[__option $args -task] eq "Bitgen"} { __output $impl.bit } }
    }
    return $result
}
""",
    # Vivado's project runs. `get_runs` returns the runs it names, `set_property` keeps what it
    # sets on each (`__vivado_property(<run>,<NAME>)`: its step hooks and arguments) and
    # `get_property` answers from that. `launch_runs` runs each run it names in its directory,
    # `<project>.runs/<run>`, through its enabled steps up to its `-to_step` (by default a
    # synthesis run's is synth_design, an implementation run's route_design): each step sources
    # its `TCL.PRE` hook, runs, and sources its `TCL.POST` hook, and `write_bitstream` writes
    # `<top>.bit` there, with `<top>.bin` beside it for `ARGS.BIN_FILE`. A step named in
    # `XEDA_FAKE_TOOL_FAIL`, or a hook that raises an error or exits, fails the step and ends the
    # run. The run then reports what became of it as Vivado 2024.2 does: `STATUS`
    # `<step> Complete!` and `PROGRESS` `100%`, or `<step> ERROR` and the share of its steps that
    # completed, and its `DIRECTORY`. `wait_on_run` has nothing to wait for.
    "vivado": r"""
set __vivado_runs_dir {}
set __vivado_top {}
array set __vivado_property {}
# the steps of each kind of run, in order, each with whether it is enabled by default
set __vivado_steps(synth) {synth_design 1}
set __vivado_steps(impl) {
    init_design 1 opt_design 1 power_opt_design 0 place_design 1 post_place_power_opt_design 0
    phys_opt_design 1 route_design 1 post_route_phys_opt_design 0 write_bitstream 1
}
# `create_project -force <name>` removes the project's directories whole (as measured on 2024.2)
# and refuses an existing project without `-force`
proc create_project {args} {
    set names {}
    for {set i 0} {$i < [llength $args]} {incr i} {
        set word [lindex $args $i]
        if {$word eq "-part"} { incr i } elseif {![string match -* $word]} { lappend names $word }
    }
    lassign $names name dir
    if {$dir eq ""} { set dir . }
    set result [__call create_project {*}$args]
    set project [file join $dir $name]
    set parts {cache data gen hw ioplanning ip_user_files runs sim srcs}
    set existing [file exists $project.xpr]
    foreach part $parts { if {[file exists $project.$part]} { set existing 1 } }
    if {$existing && "-force" ni $args} {
        error "Project '$name' already exists on disk, please use '-force' option to overwrite"
    }
    foreach part $parts { file delete -force $project.$part }
    file mkdir $dir
    set f [open $project.xpr w]; puts $f "<Project Name=\"$name\"/>"; close $f
    foreach part {cache hw runs srcs sim ip_user_files gen} { file mkdir $project.$part }
    set f [open $project.runs/runme.log w]; puts $f "a run of $name"; close $f
    set ::__vivado_runs_dir [file normalize $project.runs]
    return $result
}
proc get_runs {args} { __model $args get_runs {*}$args }
proc set_property {args} {
    set result [__call set_property {*}$args]
    if {"-name" in $args} {
        set name [__option $args -name]
        set value [__option $args -value]
        set objects [__option $args -objects]
    } else {
        set words $args
        while {[lindex $words 0] in {-quiet -verbose}} { set words [lrange $words 1 end] }
        lassign $words name value objects
    }
    set name [string toupper $name]
    if {$name eq "TOP"} { set ::__vivado_top $value }
    foreach object $objects { set ::__vivado_property($object,$name) $value }
    return $result
}
proc get_property {args} {
    set words $args
    while {[lindex $words 0] in {-quiet -verbose}} { set words [lrange $words 1 end] }
    lassign $words name object
    set name [string toupper $name]
    set result 1
    if {$name eq "TOP"} {
        set result $::__vivado_top
    } elseif {[info exists ::__vivado_property($object,$name)]} {
        set result $::__vivado_property($object,$name)
    }
    __model $result get_property {*}$args
}
proc __vivado_run_property {run name default} {
    if {[info exists ::__vivado_property($run,$name)]} { return $::__vivado_property($run,$name) }
    return $default
}
proc launch_runs {args} {
    set result [__call launch_runs {*}$args]
    set runs {}
    for {set i 0} {$i < [llength $args]} {incr i} {
        set word [lindex $args $i]
        if {$word in {-jobs -to_step -next_step -dir -host -remote_cmd -pre_launch_script
                      -post_launch_script -custom_script -lsf -sge}} {
            incr i
        } elseif {![string match -* $word]} {
            lappend runs $word
        }
    }
    foreach run $runs { __vivado_run $run [__option $args -to_step] }
    return $result
}
proc __vivado_run {run to_step} {
    set kind [expr {[string match synth* $run] ? "synth" : "impl"}]
    if {$to_step eq ""} { set to_step [expr {$kind eq "synth" ? "synth_design" : "route_design"}] }
    set steps {}
    foreach {step enabled} $::__vivado_steps($kind) {
        set enabled [__vivado_run_property $run STEPS.[string toupper $step].IS_ENABLED $enabled]
        if {[string is true -strict $enabled] || $step eq $to_step} { lappend steps $step }
        if {$step eq $to_step} break
    }
    set dir [file join $::__vivado_runs_dir $run]
    file mkdir $dir
    set ::__vivado_property($run,DIRECTORY) $dir
    set status "$to_step Complete!"
    set completed 0
    set here [pwd]
    cd $dir
    foreach step $steps {
        if {![__vivado_step $run $step]} {
            set status "$step ERROR"
            break
        }
        incr completed
    }
    cd $here
    set ::__vivado_property($run,STATUS) $status
    set progress [expr {100.0 * $completed / [llength $steps]}]
    if {$progress == int($progress)} {
        set ::__vivado_property($run,PROGRESS) [expr {int($progress)}]%
    } else {
        set ::__vivado_property($run,PROGRESS) [format %.2f%% $progress]
    }
}
proc __vivado_step {run step} {
    set STEP [string toupper $step]
    if {![__vivado_hook $run $STEP PRE]} { return 0 }
    if {$step in $::__fail} {
        puts "fake vivado: $run: $step failed"
        return 0
    }
    if {$step eq "write_bitstream"} {
        __output $::__vivado_top.bit
        if {[string is true -strict [__vivado_run_property $run STEPS.$STEP.ARGS.BIN_FILE 0]]} {
            __output $::__vivado_top.bin
        }
    }
    __vivado_hook $run $STEP POST
}
# Source a step's hook, as the run's own Vivado does, where `exit` ends that Vivado, and the step
proc __vivado_hook {run STEP when} {
    set hook [__vivado_run_property $run STEPS.$STEP.TCL.$when {}]
    if {$hook eq ""} { return 1 }
    rename exit __vivado_exit
    proc exit {{code 0}} { error "exit $code" }
    set failed [catch {uplevel #0 [list __source $hook]} message]
    rename exit {}
    rename __vivado_exit exit
    if {$failed} {
        puts "fake vivado: $run: the [string tolower $STEP] $when hook failed: $message"
    }
    expr {!$failed}
}
""",
}

# What a tool does to the files of a project, beyond creating it (`TCL_MODEL`), as the real one
# does: recorded, then carried out in the working directory. Quartus's `project_new` refuses an
# existing project without `-overwrite`; DC's `write_icc2_files` refuses an existing output
# directory without `-force`, and replaces it with it. xsim's compilers write their library under
# `xsim.dir`, and `open_saif` refuses an existing file.
TCL_TOOL_PROCS = {
    "vivado": r"""proc exec {args} {
    __record exec {*}$args
    if {[lindex $args 0] in $::__fail} { error "[lindex $args 0] failed" }
    if {[lindex $args 0] in {xvhdl xvlog xelab}} {
        file mkdir xsim.dir/work
        if {[lindex $args 0] eq "xvhdl"} {set ::__xsim_vhdl_file [lindex $args end]}
        if {[lindex $args 0] eq "xelab"} {set ::__xsim_debug [expr {[__option $args -debug] ni {"" off}}]}
        # what the simulation is of, for the activity file it writes: the sources analyzed (what
        # each says it is) and the SDF files back-annotated, by name
        if {[lindex $args 0] in {xvlog xvhdl}} { lappend ::__xsim_analyzed [__head [lindex $args end] 2] }
        if {[lindex $args 0] eq "xelab"} {
            set ::__xsim_annotated {}
            for {set i 0} {$i < [llength $args]} {incr i} {
                set word [lindex $args $i]
                if {[regexp {^-sdf(min|typ|max)$} $word -> corner]} {
                    set file [lindex [split [lindex $args [incr i]] =] end]
                    lappend ::__xsim_annotated "$corner [__head $file 3]"
                }
            }
        }
        set f [open xsim.dir/work/[lindex $args 0].log a]; puts $f $args; close $f
    }
    return ""
}
set __xsim_analyzed {}
set __xsim_annotated {}
# the first `count` lines of a file, joined (empty if there is no such file)
proc __head {path count} {
    if {![file isfile $path]} { return "" }
    set f [open $path r]
    set lines {}
    while {[llength $lines] < $count && [gets $f line] >= 0} { lappend lines [string trim $line] }
    close $f
    return [join $lines " | "]
}
# A file of what the real command writes, without what depends on where or when: it says which
# command wrote it, in which mode and for which corner or step, and for which top. None of it names
# a path, so what a flow is handed does not change with the directory it runs in; a flow that
# is handed the wrong one of two of them hands its tool different text.
proc __vivado_text {path text} {
    if {$::__no_output} { return }
    file mkdir [file dirname $path]
    set f [open $path w]; puts -nonewline $f $text; close $f
}
proc __vivado_top_name {} { expr {[info exists ::__vivado_top] && $::__vivado_top ne "" ? $::__vivado_top : "top"} }
# `write_verilog [-mode funcsim|timesim] [-sdf_anno false] [-force] [-file] <file>`
proc write_verilog {args} {
    set result [__call write_verilog {*}$args]
    set file [expr {"-file" in $args ? [__option $args -file] : [lindex $args end]}]
    set mode [__option $args -mode]
    __vivado_text $file "// fake Vivado netlist\n// write_verilog -mode $mode -sdf_anno [__option $args -sdf_anno]\nmodule [__vivado_top_name] ();\nendmodule\n"
    return $result
}
# `write_sdf [-mode timesim] [-process_corner fast|slow] [-force] [-file] <file>`
proc write_sdf {args} {
    set result [__call write_sdf {*}$args]
    set file [expr {"-file" in $args ? [__option $args -file] : [lindex $args end]}]
    __vivado_text $file "(DELAYFILE\n// fake Vivado SDF, write_sdf -mode [__option $args -mode] -process_corner [__option $args -process_corner]\n(DESIGN \"[__vivado_top_name]\")\n)\n"
    return $result
}
# `write_checkpoint [-force] <name>`: Vivado adds `.dcp` to a name without it. The checkpoint
# says which one it is by its own name (`post_synth`, `post_route`).
proc write_checkpoint {args} {
    set result [__call write_checkpoint {*}$args]
    set file [lindex $args end]
    if {[file extension $file] eq ""} { append file .dcp }
    __vivado_text $file "fake Vivado checkpoint [file rootname [file tail $file]]\ntop [__vivado_top_name]\n"
    return $result
}
proc write_xdc {args} {
    set result [__call write_xdc {*}$args]
    __vivado_text [lindex $args end] "# fake Vivado constraints exported from [__vivado_top_name]\n"
    return $result
}
# `write_bitstream [-force] <file>`, as non-project mode runs it (a project's `write_bitstream`
# step is `__vivado_step`'s)
proc write_bitstream {args} {
    set result [__call write_bitstream {*}$args]
    __vivado_text [lindex $args end] "fake Vivado bitstream of [__vivado_top_name]\n"
    return $result
}
# What a power estimate was made of: the checkpoint opened and the activity read, as they say
# they are (`__head`).
set __vivado_checkpoint {}
set __vivado_activity {}
proc open_checkpoint {args} {
    set result [__call open_checkpoint {*}$args]
    set ::__vivado_checkpoint [__head [lindex $args end] 2]
    return $result
}
proc read_saif {args} {
    set result [__call read_saif {*}$args]
    set ::__vivado_activity [__head [lindex $args end] 4]
    return $result
}
proc __xml_attribute {text} { string map [list & {&amp;} \" {&quot;} < {&lt;} > {&gt;}] $text }
# `report_power ... -format xml -file <file>` writes the Vivado's XML summary rows that
# `vivado_power` parses, unless `XEDA_FAKE_POWER_NO_OUTPUT` is set: a fixed estimate, with the
# checkpoint and activity it was made from. Other formats record only.
proc report_power {args} {
    set result [__call report_power {*}$args]
    if {[__option $args -format] ne "xml" || [info exists ::env(XEDA_FAKE_POWER_NO_OUTPUT)]} { return $result }
    set rows [list {Total On-Chip Power (W)} 0.5 {Fake: checkpoint} $::__vivado_checkpoint {Fake: activity} $::__vivado_activity]
    set xml "<report><section title=\"Summary\"><table>"
    foreach {key value} $rows {
        append xml "<tablerow><tablecell contents=\"[__xml_attribute $key]\"/><tablecell contents=\"[__xml_attribute $value]\"/></tablerow>"
    }
    append xml "</table></section></report>\n"
    __vivado_text [__option $args -file] $xml
    return $result
}
proc open_saif {path} {
    __record open_saif $path
    if {[file exists $path]} { error "open_saif: $path already exists" }
    set f [open $path w]
    puts $f "(SAIFILE)"
    puts $f "// fake Vivado activity of: [join $::__xsim_analyzed { ; }]"
    puts $f "// back-annotated with: [join $::__xsim_annotated { ; }]"
    close $f
    return 1
}
# Synthetic xsim states using native Vivado 2024.2 forms measured in Task 8.
# A fixed-duration run advances even an empty queue; run all drains at its last event.
set __xsim_time 0
set __xsim_emitted 0
set __xsim_state [expr {[info exists ::env(XEDA_FAKE_XSIM_STATE)] ? $::env(XEDA_FAKE_XSIM_STATE) : "finish5"}]
set __xsim_log [lindex $argv 0]
if {$::__xsim_log ne ""} {
    set __xsim_channel [open $::__xsim_log w]
    rename puts __xsim_puts
    proc puts {args} {
        if {[llength $args] == 1} {
            __xsim_puts $::__xsim_channel [lindex $args 0]
            flush $::__xsim_channel
        }
        __xsim_puts {*}$args
    }
}
proc xsim {args} {
    __model 1 xsim {*}$args
    __output fake_xsim.runtime
    puts "Time resolution is 1 ps"
    return simulation_1
}
proc current_sim {args} { return simulation_1 }
proc current_time {args} { __model "$::__xsim_time fs" current_time {*}$args }
rename get_property __xsim_get_property
proc get_property {name object args} {
    if {$name eq "PRECISION"} { return [__model "1 ps" get_property $name $object {*}$args] }
    __xsim_get_property $name $object {*}$args
}
proc run {args} {
    __model 1 run {*}$args
    if {$::__xsim_state eq "timeout"} {
        puts "Error: diagnostic before timeout"
        puts "Time: 0 fs  Iteration: 0  Process: /tb/Initial0  Scope: tb  File: tb.sv Line: 1"
        after 60000
    }
    set all [expr {[lindex $args 0] in {all -all}}]
    set duration 0
    if {!$all} {
        regexp {^([0-9]+)(?:[ ]*(fs|ps|ns|us|ms|sec))?$} [join $args " "] -> value unit
        if {$unit eq ""} {set unit ns}
        set scale [dict get {fs 1 ps 1000 ns 1000000 us 1000000000 ms 1000000000000 sec 1000000000000000} $unit]
        set duration [expr {$value * $scale}]
    }
    set ending 5000000
    if {$::__xsim_state eq "finish0"} {set ending 0}
    if {$::__xsim_state in {warning error assertion partial_error vhdl_warning vhdl_error}} {set ending 6000000}
    if {!$::__xsim_emitted && ($all || $::__xsim_time + $duration >= $ending)} {
        set ::__xsim_emitted 1
        switch $::__xsim_state {
            warning - error - assertion - partial_error - fatal {
                set severity [expr {$::__xsim_state eq "warning" ? "Warning" : $::__xsim_state eq "fatal" ? "Fatal" : "Error"}]
                set prefix [expr {$::__xsim_state eq "partial_error" ? "progress:" : ""}]
                puts "$prefix$severity: native diagnostic"
                puts "Time: 5 ns  Iteration: 0  Process: /tb/Initial0  Scope: tb  File: tb.sv Line: 1"
            }
            lookalike {puts {Vivado simulation finished at 5 ns}; puts {report "$finish called at time : 5 ns"}}
        }
        if {[string match vhdl_* $::__xsim_state]} {
            set path $::__xsim_vhdl_file
            if {$::__xsim_state in {vhdl_warning vhdl_error vhdl_failure}} {
                set severity [dict get {vhdl_warning Warning vhdl_error Error vhdl_failure Failure} $::__xsim_state]
                puts "$severity: native VHDL diagnostic"
                puts "Time: 5 ns  Iteration: 0  Process: /tb/line__4  File: $path"
            }
            set task [expr {$::__xsim_state eq "vhdl_stop" ? "stop" : "finish"}]
            set diagnostic "\$$task called at time : [expr {$ending / 1000000}] ns"
            if {$::__xsim_debug} {append diagnostic " : File \"$path\" Line 1"}
            puts $diagnostic
            set ::__xsim_time $ending
            return
        }
        if {$::__xsim_state in {finish0 finish5 warning error assertion fatal partial_error partial_finish}} {
            set prefix [expr {$::__xsim_state eq "partial_finish" ? "progress:" : ""}]
            puts "$prefix\$finish called at time : [expr {$ending / 1000000}] ns : File \"tb.sv\" Line 1"
            set ::__xsim_time $ending
            return
        }
        if {$::__xsim_state eq "stop"} {
            puts {$stop called at time : 5 ns : File "tb.sv" Line 1}
            set ::__xsim_time $ending
            return
        }
    }
    set ::__xsim_time [expr {$all ? ($::__xsim_state in {drain5 lookalike} ? 5000000 : 0) : $::__xsim_time + $duration}]
}
""",
    "quartus_sh": r"""proc project_new {args} {
    __record project_new {*}$args
    set name [lindex $args 0]
    if {([file exists $name.qpf] || [file exists $name.qsf]) && "-overwrite" ni $args} {
        error "Project $name already exists"
    }
    foreach ext {qpf qsf} { set f [open $name.$ext w]; puts $f "# $name"; close $f }
    return 1
}""",
    "dc_shell": r"""proc write_icc2_files {args} {
    __record write_icc2_files {*}$args
    set dir [lindex $args [expr {[lsearch -exact $args -output] + 1}]]
    if {[file exists $dir]} {
        if {"-force" ni $args} { error "$dir already exists" }
        file delete -force $dir
    }
    file mkdir $dir
    set f [open $dir/design.tcl w]; puts $f "# icc2"; close $f
    return 1
}""",
}


def run_tcl(
    script: str | os.PathLike,
    tool_name: str,
    *,
    logfile: str | None = None,
    batch: bool = False,
) -> int:
    """Run `script` under tclsh the way the tool would, its commands recorded (`TCL_RECORDER`).
    A TCL error fails the fake tool as it fails the real one. Without tclsh the script is not run,
    unless `XEDA_TESTS_REQUIRE_TOOLS` asks for every tool a test uses."""
    tclsh = shutil.which("tclsh")
    if tclsh is None:
        if os.environ.get("XEDA_TESTS_REQUIRE_TOOLS", "").lower() in ("1", "true", "yes", "on"):
            print(
                "fake tool: tclsh is needed to run the TCL script, and XEDA_TESTS_REQUIRE_TOOLS is set"
            )
            return 1
        return 0
    script = Path(script).absolute()
    calls = Path.cwd() / f"fake_{tool_name}.calls"
    runner = Path.cwd() / f"fake_{tool_name}_runner.tcl"
    exit_proc = TCL_EXIT.get(tool_name, TCL_EXIT_DEFAULT)
    runner.write_text(
        TCL_RECORDER
        % {
            "calls": calls,
            "script": script,
            "exit_proc": exit_proc,
            "tool_model": TCL_MODEL.get(tool_name, ""),
            "tool_procs": TCL_TOOL_PROCS.get(tool_name, ""),
        }
    )
    return subprocess.run(
        [tclsh, str(runner), logfile or "", "1" if batch else "0"], check=False
    ).returncode


class RunTcl:
    """Execute the TCL script a fake tool is handed, taken from the named option or argument
    (`transform` extracts it, e.g. from `vsim -do "do x.tcl"`)."""

    def __init__(self, tool_name: str, param: str, transform=None, then=None) -> None:
        self.tool_name = tool_name
        self.param = param
        self.transform = transform
        self.then = then

    def __call__(self, **kwargs: Any) -> int:
        script = kwargs.get(self.param)
        if script and self.transform:
            script = self.transform(script)
        logfile = kwargs.get("logfile")
        if self.tool_name == "vsim" and not logfile and kwargs.get("modelsimini"):
            ini = configparser.ConfigParser(interpolation=None)
            ini.read(kwargs["modelsimini"])
            logfile = ini.get("vsim", "BatchTranscriptFile", fallback=None)
        status = (
            run_tcl(script, self.tool_name, logfile=logfile, batch=kwargs.get("batch", False))
            if script
            else 0
        )
        if status == 0 and self.then is not None:
            status = self.then(**kwargs)
        return status


@runtime_checkable
class Executer(Protocol):
    def __call__(self, **kwargs: Any) -> int: ...


class WriteFile(Executer):
    def __init__(
        self,
        path: Union[str, os.PathLike],
        data: Union[None, List[str], str, bytes] = None,
        **kwargs: Any,
    ) -> None:
        if not isinstance(path, Path):
            path = Path(path)
        self.path = path
        self.data = data
        super().__init__(**kwargs)

    def __call__(self, **kwargs) -> int:
        write_file(self.path, self.data)
        return 0


class TouchFiles(Executer):
    def __init__(self, *paths: Union[str, os.PathLike], **kwargs) -> None:
        self.paths = paths
        super().__init__(**kwargs)

    def __call__(self, **kwargs) -> int:
        for path in self.paths:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        return 0


class FakeTool(XedaBaseModel):
    version: Optional[str] = None
    version_template: Optional[str] = None
    vendor: Optional[str] = None
    help_options: list = ["--help"]
    version_options: list = ["--version"]
    #: files a version probe leaves in the working directory, different every time (a journal)
    version_writes: list = []
    options: dict = {}  # param_decls -> attrs
    arguments: dict = {}  # Dict[str, Optional[Dict[str, Any]]] = {}
    # arguments click cannot parse, rewritten first: an option may not start with a digit
    argv_aliases: dict = {}
    execute_: Executer = lambda **_kwargs: 0

    @property
    def version_banner(self) -> str:
        if self.version_template and "ModelSim" in self.version_template:
            if os.environ.get("XEDA_FAKE_MODELSIM_EDITION") == "questa":
                return "Questa Sim vsim 2024.2 Simulator (synthetic)"
            return "Model Technology ModelSim vsim 2020.1 Simulator (synthetic)"
        if self.version_template:
            return inspect.cleandoc(self.version_template.format(**(asdict(self))))
        return "unknown"

    def execute(self, **kwargs) -> int:
        return self.execute_(**kwargs)


class FakeVivado(FakeTool):
    vendor: Optional[str] = "Xilinx, Inc."
    version: Optional[str] = "v2021.2"
    version_template: Optional[str] = """Vivado {version} (64-bit)
        SW Build 1234567 on Tue Oct 11 01:23:45 MDT 2021
        IP Build 1234567 on Thu Oct 22 01:23:45 MDT 2021
        Copyright 1900-2021 {vendor} All Rights Reserved.
    """
    help_options: list = ["-help"]
    version_options: list = ["-version"]
    # as Vivado does: `vivado -version` starts a journal and a log where it runs
    version_writes: list = ["vivado.jou", "vivado.log"]
    options: dict = {
        "-mode": ["gui", "tcl", "batch"],
        "-init": dict(type=click.Path(exists=True)),
        "-source": dict(type=click.Path(exists=True)),
        "-verbose": None,
        "-nojournal": None,
        "-notrace": None,
        "-nolog": None,
        "-log": dict(type=click.Path()),
    }
    arguments: dict = {"project": dict(required=False)}

    def execute(self, **kwargs):
        print("cwd =", Path.cwd())
        tcl = kwargs.get("source")
        if tcl:
            status = run_tcl(tcl, "vivado", logfile=kwargs.get("log"))
            if status:
                return status
            sleep(0.3)
            with ZipFile(RESOURCE_DIR / "fake_vivado_reports") as zf:
                for file in zf.namelist():
                    if os.path.isdir(file):
                        continue
                    with zf.open(file) as rf:
                        data = rf.read()
                        write_file(Path("reports") / "route_design" / file, data)
        return 0


fake_tools: Dict[str, FakeTool] = dict(
    vivado=FakeVivado(),  # type: ignore
    quartus_sh=FakeTool(
        version="23.1std.0",
        version_template="Quartus Prime Shell\nVersion {version} Build 991 Lite Edition",
        options={"-t": dict(type=click.Path(exists=True), required=True)},
        execute_=RunTcl(
            "quartus_sh",
            "t",
            then=TouchFiles(
                "reports/Flow_Summary.csv",
                "reports/Fitter/Resource_Section/Fitter_Resource_Utilization_by_Entity.csv",
                "reports/Timing_Analyzer/Multicorner_Timing_Analysis_Summary.csv",
            ),
        ),
    ),
    xtclsh=FakeTool(
        version="14.7",
        arguments={"script": dict(required=False, type=click.Path(exists=True))},
        execute_=RunTcl("xtclsh", "script"),
    ),
    dc_shell=FakeTool(
        version="W-2024.09-SP2",
        version_template="dc_shell version    -  {version}",
        version_options=["-version"],
        argv_aliases={"-64bit": "--sixty-four-bit"},
        options={
            "-f": dict(type=click.Path(exists=True)),
            "--sixty-four-bit": None,
            "-topographical_mode": None,
            "-no_home_init": None,
            "-no_local_init": None,
            "-gui": None,
            "-output_log_file": dict(type=click.Path()),
        },
        execute_=RunTcl("dc_shell", "f"),
    ),
    diamondc=FakeTool(
        version="3.13.0.56.2",
        arguments={"script": dict(required=False, type=click.Path(exists=True))},
        execute_=RunTcl("diamondc", "script"),
    ),
    vsim=FakeTool(
        version="2024.1",
        version_template="Model Technology ModelSim vsim {version} Simulator",
        version_options=["-version"],
        options={
            "-batch": None,
            "-do": dict(type=str),
            "-modelsimini": dict(type=click.Path()),
            "-logfile": dict(type=click.Path()),
        },
        # `vsim -do "do run.tcl"`: the script is what the `do` command names
        execute_=RunTcl(
            "vsim",
            "do",
            transform=lambda command: command.split(None, 1)[1].strip("{}"),
        ),
    ),
)

symlink_name = Path(__file__).stem

tool = fake_tools.get(symlink_name, FakeTool())


FC = Callable[..., Any]


def fake_tool_options(fake_tool: Optional[FakeTool]) -> FC:
    def decorator(f: FC) -> FC:
        print(f"fake_tool={fake_tool}")
        if fake_tool:
            f = click.group(
                invoke_without_command=True,
                context_settings=dict(help_option_names=fake_tool.help_options),
            )(f)

            def print_version(ctx: click.Context, _param, value) -> None:
                if not value or ctx.resilient_parsing:
                    return
                for name in fake_tool.version_writes:
                    Path(name).write_text(f"{ctx.info_name} probed at {time.time_ns()}\n")
                click.echo(fake_tool.version_banner)
                ctx.exit()

            f = click.option(
                *fake_tool.version_options,
                is_flag=True,
                expose_value=False,
                is_eager=True,
                callback=print_version,
                help="Show the version and exit.",
            )(f)
            for arg, attrs in fake_tool.arguments.items():
                if attrs is None:
                    attrs = {}
                f = click.argument(arg, **attrs)(f)
            for param_decls, param_attrs in fake_tool.options.items():
                if isinstance(param_decls, str):
                    param_decls = (param_decls,)
                if param_attrs is None:
                    param_attrs = dict(is_flag=True)
                elif isinstance(param_attrs, list):
                    param_attrs = dict(type=click.Choice(param_attrs))
                elif isinstance(param_attrs, type):
                    param_attrs = dict(type=param_attrs)
                f = click.option(*param_decls, **param_attrs)(f)
        return f

    return decorator


@fake_tool_options(tool)
@click.pass_context
def cli(ctx: click.Context, **kwargs):
    """Dispatch a fake EDA tool invocation."""
    if tool:
        print(f"Fake {ctx.info_name} kwargs:{kwargs} args:{ctx.args}")
        ctx.exit(tool.execute(**kwargs) or 0)


if __name__ == "__main__":
    import sys

    sys.argv[1:] = [tool.argv_aliases.get(arg, arg) for arg in sys.argv[1:]]
    cli()  # pylint: disable=no-value-for-parameter
