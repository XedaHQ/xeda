# Stand-ins for the Vivado TCL commands that the timing path reports in
# util.tcl call, so they can be exercised under a plain `tclsh`.
# Driven by tests/test_vivado.py.

array set PROPS {}
set PATHS {}

# Register a fake timing path object. Paths must be registered in the order
# Vivado returns them, i.e. ascending slack (worst first).
proc addpath {name slack delay {levels 4} {logic_delay 0.729}} {
  global PROPS PATHS
  set net_delay ""
  if {[string is double -strict $delay] && [string is double -strict $logic_delay]} {
    set net_delay [format %.3f [expr {$delay - $logic_delay}]]
  }
  array set PROPS [list \
    $name,SLACK $slack \
    $name,DATAPATH_DELAY $delay \
    $name,DATAPATH_LOGIC_DELAY $logic_delay \
    $name,DATAPATH_NET_DELAY $net_delay \
    $name,LOGIC_LEVELS $levels \
    $name,MAX_FANOUT 2 \
    $name,STARTPOINT_PIN start_$name \
    $name,ENDPOINT_PIN end_$name \
    $name,STARTPOINT_CLOCK clock \
    $name,ENDPOINT_CLOCK clock \
    $name,GROUP clock \
    $name,REQUIREMENT 5.556 \
    $name,SKEW -0.145 \
    $name,UNCERTAINTY 0.035 \
  ]
  lappend PATHS $name
}

# Strict on purpose: a property that Vivado does not put on a timing path object
# must fail here rather than silently produce an empty CSV column.
proc get_property {prop obj} {
  global PROPS
  if {![info exists PROPS($obj,$prop)]} {
    error "get_property: no property $prop on $obj"
  }
  return $PROPS($obj,$prop)
}

# Mimics `get_timing_paths` of Vivado 2024.2: unknown options are a hard error,
# `-sort_by` defaults to slack, and with a slack sort `-max_paths` is a
# design-wide cap on the number of worst paths returned.
proc get_timing_paths {args} {
  global PATHS
  set with_value {-delay_type -max_paths -nworst -sort_by -group -cell -filter
                  -slack_lesser_than -slack_greater_than -from -to -through}
  set boolean {-setup -hold -unique_pins -no_report_unconstrained -user_ignored
               -routable_nets -quiet -verbose}
  set opts [dict create -max_paths 1 -sort_by slack -delay_type max]
  for {set i 0} {$i < [llength $args]} {incr i} {
    set opt [lindex $args $i]
    if {[lsearch -exact $boolean $opt] >= 0} {
      dict set opts $opt 1
    } elseif {[lsearch -exact $with_value $opt] >= 0} {
      dict set opts $opt [lindex $args [incr i]]
    } else {
      error "get_timing_paths: unrecognized option \"$opt\""
    }
  }
  set sort_by [dict get $opts -sort_by]
  if {$sort_by ne "slack" && $sort_by ne "group"} {
    error "get_timing_paths: bad -sort_by value \"$sort_by\""
  }
  set delay_type [dict get $opts -delay_type]
  if {$delay_type ne "max"} {
    error "get_timing_paths: expected setup (max) analysis, got \"$delay_type\""
  }
  return [lrange $PATHS 0 [expr {[dict get $opts -max_paths] - 1}]]
}
