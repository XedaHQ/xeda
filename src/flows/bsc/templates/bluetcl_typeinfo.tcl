if { $argc != 2 } {
    puts "Usage: $argv0 <bdir> <top-package>"
    exit 1
}

set bdir [lindex $argv 0]
set top_package [lindex $argv 1]

package require Bluetcl

package require utils
namespace import ::utils::*

Bluetcl::flags set -bdir ${bdir}


proc fullTypeWrap { t } {
    return [Bluetcl::type full $t]
}

proc fullTypeNoAlias { t } {
    set ft [fullTypeWrap $t]
    # unwind aliases
    while { [lindex $ft 0] == "Alias"} {
        set tx [lindex $ft 2]
        set ft [fullTypeWrap $tx]
    }
    return $ft
}

Bluetcl::bpackage load ${top_package}
set loaded_packages [Bluetcl::bpackage list]


proc qq x {return "\"$x\""}

set indent_level 0
set num_indents 4
proc putsi line {
    global indent_level
    global num_indents
    set i [string repeat " " [expr $indent_level * $num_indents] ]
    puts "${i}${line}"
}
proc putkv {k v} {
    putsi "[qq $k]: ${v},"
}

proc indent {} {
    global indent_level
    set indent_level [expr $indent_level+1]
}
proc dedent {} {
    global indent_level
    set indent_level [expr $indent_level-1]
}

putsi "\{"
indent

foreach package ${loaded_packages} {
    if { $package in {Prelude PreludeBSV List ListN Array Vector BUtils FIFOF InOut Clocks RevertingVirtualReg} } {
        continue
    }

    set types [Bluetcl::bpackage types ${package}]

    # puts stderr "package: ${package} types: ${types}"

    foreach type ${types} {
        # puts stderr "type: ${type}"
        if { [catch {fullTypeNoAlias ${type}} ft] } {
            # puts stderr "type full failed for ${type}"
            continue
        }
        # puts stderr "fulltype: ${ft}"
        set key [lindex $ft 0]
        set name [lindex $ft 1]
        set details [lrange $ft 2 end]

        switch -exact $key {
            "Alias"   {
                continue
            }
            "Primary" {
                continue
            }
            "Enum" {
            }
            "Struct"  {
                continue
            }
            "TaggedUnion" {
                continue
            }
            "Vector"  {
                continue
            }
            "Interface"  {
                continue
            }
            default {
                # puts stderr "Error: unknown key $key"
                continue
            }
        }



        putsi "[qq ${name}] : \{"
        indent

        putkv type [qq ${key}]
        # puts stderr ">>details=$details"
        foreach d $details {
            set k [lindex $d 0]
            set v [lindex $d 1]
            if { $k == "members" } {
                set m [join [map qq $v] ", "]
                putkv $k  "\[$m\]"
            } elseif { $k == "position" } {
                set position [join [map qq $v] ", "]
                putkv $k "\[${position}\]"
            } elseif { $k == "width" } {
                putkv $k $v
            } else {
                putkv $k [qq $v]
            }
        }

        dedent
        putsi "\},"
    }
}

dedent
putsi "\}"