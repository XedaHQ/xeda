# The commands of OpenROAD that the fake OpenROAD (`fake_tool.py`, as `openroad`) knows.
#
# Usage: tclsh fake_openroad_prelude.tcl <calls file> <script> <roots> <commands>
#   calls file  where each command is recorded (appended); `fake_calls` in `tests/tool_utils.py`
#               reads it: `CALL <n>` and one `ARG` line for each word of the command (and `ELEM`
#               lines for the items of a word that is a list), or `TCL-ERROR <message>`
#   script      the Tcl script of the OpenROAD run; it ends the run when it ends or fails
#   roots       a list of directories that the files written by a command leave out of their text
#               (the working directory, in each spelling), longest first
#   commands    a list of the commands to define: `OPENROAD_COMMANDS`, the one list of commands
#
# The prelude defines one command for each name in `commands` and no `unknown` command, so any
# other command is a Tcl error ("invalid command name"), as it is in OpenROAD. A command records
# itself, raises an error if `XEDA_FAKE_TOOL_FAIL` (a Tcl list of command names) names it, and
# returns what `XEDA_FAKE_TOOL_RETURNS` (a Tcl dictionary) says, or else its value below.
# `gui::show` also runs its first argument as a script, as the interface does.
# `extract_parasitics` also writes `<top>.totCap` in the working directory when the environment
# variable `XEDA_FAKE_OPENROAD_TOP` names the top module: the files of OpenRCX that some versions
# leave there. The fake has no design, so it cannot know the name by itself.

lassign $argv __calls_file __script __roots __commands

# The standard `unknown` of tclsh loads packages and may start a program of the same name.
catch {rename unknown {}}

set __calls [open $__calls_file a]

proc __record {args} {
    puts $::__calls "CALL [llength $args]"
    foreach a $args {
        puts $::__calls "ARG $a"
        if {![catch {llength $a} n] && $n >= 1 && [lindex $a 0] ne $a} {
            foreach e $a { puts $::__calls "ELEM $e" }
        }
    }
    flush $::__calls
}

set __fail [expr {[info exists ::env(XEDA_FAKE_TOOL_FAIL)] ? $::env(XEDA_FAKE_TOOL_FAIL) : {}}]
set __returns {}
if {[info exists ::env(XEDA_FAKE_TOOL_RETURNS)]} { set __returns $::env(XEDA_FAKE_TOOL_RETURNS) }

# The value of a call: the longest leading words of the call that `XEDA_FAKE_TOOL_RETURNS` has as
# a key, else `default`.
proc __returned {words default} {
    for {set n [llength $words]} {$n > 0} {incr n -1} {
        set key [lrange $words 0 [expr {$n - 1}]]
        if {[dict exists $::__returns $key]} { return [dict get $::__returns $key] }
    }
    return $default
}

# What a command returns when nothing else says. A command that is not here returns "". A number
# is there where a template computes with the value; `get_*` names are there so that a later
# `$library/$cell/$port` is a word.
set __defaults [dict create \
    all_pins_placed 0 \
    design_is_routed 1 \
    check_antennas 0 \
    repair_antennas 0 \
    grt::have_routes 1 \
    ord::openroad_gui_compiled 1 \
    gpl::get_global_placement_uniform_density 0.5 \
    ord::dbu_to_microns 100.0 \
    ord::get_db ::fake_db \
    ord::get_db_block ::fake_block \
    get_lib_cell fake_lib_cell \
    get_property fake_property \
    get_name fake_name]

# The commands that write a file, and the options of each that take a value. The file is the
# first word of the command that is not an option and not the value of one: `write_sdc
# -no_timestamp <file>` and `write_verilog <file> -remove_cells <cells>` both work. As in
# OpenROAD, the directory of the file must exist. The file holds the command and its arguments
# (as a Tcl list), with the working directory left out: the same request in another directory
# writes the same bytes.
set __writers [dict create \
    write_db {-compression} \
    write_sdc {-mode -digits} \
    write_def {-version} \
    write_verilog {-remove_cells} \
    write_spef {-net_id -nets} \
    write_guides {} \
    write_cdl {-masters} \
    write_sdf {-scene -divider -digits} \
    write_abstract_lef {-bloat_factor} \
    write_timing_model {-scene -library_name -cell_name} \
    save_image {-resolution -area -width -display_option -scene}]

proc __file_argument {name words} {
    set valued [dict get $::__writers $name]
    set skip 0
    foreach word $words {
        if {$skip} { set skip 0; continue }
        if {[string index $word 0] eq "-"} {
            if {$word in $valued} { set skip 1 }
            continue
        }
        return $word
    }
    error "$name: no file name given"
}

# The path with every symbolic link resolved, the last one too (`file normalize` leaves it).
proc __resolve {path} {
    set resolved [file normalize $path]
    for {set depth 0} {$depth < 40} {incr depth} {
        if {[catch {file readlink $resolved} target]} { return $resolved }
        set resolved [file normalize [file join [file dirname $resolved] $target]]
    }
    error "too many levels of symbolic links: $path"
}

# A command writes only inside the working directory, whatever the path it is given says:
# absolute, with `..`, or through a symbolic link that leads out.
proc __inside {path} {
    set root [__resolve [pwd]]
    set resolved [__resolve $path]
    expr {$resolved eq $root || [string first "$root/" $resolved] == 0}
}

proc __write {name args} {
    set path [__file_argument $name $args]
    if {![__inside $path]} {
        error "$name: $path is outside the working directory; the fake OpenROAD does not write it"
    }
    set text [list $name {*}$args]
    foreach root $::__roots { set text [string map [list $root .] $text] }
    set f [open $path w]
    puts $f $text
    close $f
}

# The commands that read a file, and what OpenROAD does when it is missing: an error that names
# the file. The file is the last word of the command; its options are not checked.
set __readers {read_db read_sdc read_lef read_liberty read_verilog read_def read_spef}

proc __read {name args} {
    set path [lindex $args end]
    if {![file isfile $path]} { error "$name: cannot read $path: no such file" }
}

# The file that OpenRCX leaves in the working directory while it extracts. It holds a line of
# text, and it lies inside the working directory like every file that the fake writes.
proc __write_totcap {} {
    if {![info exists ::env(XEDA_FAKE_OPENROAD_TOP)]} { return }
    set path "$::env(XEDA_FAKE_OPENROAD_TOP).totCap"
    if {![__inside $path]} {
        error "extract_parasitics: $path is outside the working directory; the fake OpenROAD does not write it"
    }
    set f [open $path w]
    puts $f "totCap"
    close $f
}

proc __command {name args} {
    __record $name {*}$args
    if {$name in $::__fail} { error "$name failed" }
    if {$name in $::__readers} { __read $name {*}$args }
    if {[dict exists $::__writers $name]} { __write $name {*}$args }
    if {$name eq "extract_parasitics"} { __write_totcap }
    # The graphical interface runs the script it is given, in the same interpreter.
    if {$name eq "gui::show"} { uplevel #0 [lindex $args 0] }
    __returned [list $name {*}$args] [expr {[dict exists $::__defaults $name] ? [dict get $::__defaults $name] : ""}]
}

foreach __name $__commands {
    namespace eval [namespace qualifiers ::$__name] {}
    proc $__name args "__command [list $__name] {*}\$args"
}

# The objects of the database that the templates walk: `[ord::get_db] getLibs`, `$lib getMasters`,
# `$master getType`, `[ord::get_db_block] getObstructions`. The database is empty unless
# `XEDA_FAKE_TOOL_RETURNS` says otherwise, for example `{getLibs ::fake_lib}`. A method calls
# nothing and records nothing.
set __methods [dict create \
    getLibs {} \
    getMasters {} \
    getObstructions {} \
    getName FAKE_MASTER \
    getType CORE \
    getBBox ::fake_bbox \
    getDX 1000 \
    getDY 1000 \
    findMarkerCategory NULL \
    getMarkerCount 0]

proc __method {method args} {
    if {![dict exists $::__methods $method]} { error "fake object has no method \"$method\"" }
    __returned [list $method {*}$args] [dict get $::__methods $method]
}

foreach __object {fake_db fake_lib fake_master fake_block fake_obstruction fake_bbox} {
    proc ::$__object {method args} {
        __method $method {*}$args
    }
}

if {$__script ne "" && [catch {source $__script} __error]} {
    puts $__calls "TCL-ERROR $__error"
    flush $__calls
    puts stderr "Error: $__script: $__error\n$::errorInfo"
    exit 1
}
flush $__calls
exit 0
