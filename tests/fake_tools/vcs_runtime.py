#!/usr/bin/env python3
"""Synthetic VCS/UCLI stand-in: executes scripts; does not certify a vendor release.

SV task headers follow the 2019 user guide, section 14-43. UCLI follows T-2022.06
(run, senv, stop -command, config endofsim). Quiet VHDL completion is deliberately
silent; no invented FINISH/STOP diagnostic. VHDL tagged runtime errors are modeled
separately from SV tasks. Independent invocation markers are never verdict evidence.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

name = Path(sys.argv[0]).stem
words = sys.argv[1:]
with Path("fake_vcs.invocations").open("a") as stream:
    stream.write(json.dumps([name, words]) + "\n")
state = os.environ.get("XEDA_FAKE_VCS_STATE", "silent")
if name in ("vlogan", "vhdlan", "vcs"):
    Path("fake_vcs." + name).write_text("build executed")
    print('$finish called from file "compiler.sv", line 1.', flush=True)
    print('Warning: "compiler.sv", 1: tb: at time 0', flush=True)
    if name != "vcs":
        sys.exit(0)
    if not Path("simv").exists():
        Path("simv").symlink_to(Path(__file__).resolve())
    if "-R" not in words:
        sys.exit(0)
Path("fake_vcs.runtime").write_text("runtime executed")
if "-i" not in words:
    # Legacy flow: runtime silence or native messages; never manufacture owned markers.
    if state == "finish5":
        print('$finish called from file "tb.sv", line 4.')
    sys.exit(0)
script = Path(words[words.index("-i") + 1])
model = r"""
set state $::env(XEDA_FAKE_VCS_STATE)
set now "0 NS"
set precision "1 PS"
set callback {}
set noexit 0
proc config {key value} {if {$key eq "endofsim"} {set ::noexit [expr {$value eq "noexit"}]}}
proc senv {key} {
    if {$key eq "time"} {return $::now}
    if {$key eq "timePrecision"} {return $::precision}
    error "unsupported senv element"
}
proc stop {args} {
    if {[lindex $args 0] eq "-absolute"} {return 1}
    if {[lindex $args 0] eq "-command"} {set ::callback [lindex $args 1]; return 1}
    error "unsupported stop"
}
proc dump {args} {puts "waveform configured"}
proc run {args} {
    set ::now "5 NS"
    switch -- $::state {
        silent - finish0 - vhdl_finish - vhdl_stop {set ::now "0 NS"}
        finish5 - warning_finish - error_finish - assertion - error_stderr - fatal - finish_nonzero - rt_warning {
            if {$::state in {warning_finish error_finish error_stderr fatal}} {
                set severity [expr {$::state eq "warning_finish" ? "Warning" : $::state eq "fatal" ? "Fatal" : "Error"}]
                set header "$severity: \"tb.sv\", 3: tb: at time 5"
                if {$::state eq "error_stderr"} {puts stderr $header} else {puts $header}
            }
            if {$::state eq "rt_warning"} {
                puts {RT Warning: No condition matches in 'unique if' statement.}
                puts {"tb.sv", line 3, for tb, at time 5000.}
            }
            if {$::state eq "assertion"} {
                puts {"tb.sv", 3: tb.a1: started at 4ns failed at 5ns}
                puts {Offending '(1 == 0)'}
            }
            puts {$finish called from file "tb.sv", line 4.}
            puts {$finish at simulation time 5000}
        }
        verilog_stop {puts {$stop called from file "tb.sv", line 4.}}
        vhdl_error {puts {Error-[SIMERR_NEGTIME] Wrong Time Format}}
        vhdl_failure {puts {Fatal-[SYNTHETIC] VHDL runtime failure}}
        limit10 {
            set ::now "10 NS"
            if {$::callback ne ""} {uplevel #0 $::callback}
        }
        break10 - drain10 {set ::now "10 NS"}
        timeout {puts {Error: "tb.sv", 3: tb: at time 5}; flush stdout; after 60000}
        lookalike {puts {initial $finish;}; puts {Warning: user text}; puts {V C S Simulation Report}; puts {Time: 5 ns}}
    }
    flush stdout
    if {!$::noexit && $::state in {finish0 finish5 vhdl_finish vhdl_stop drain5 drain10}} {exit 0}
    return $::now
}
proc quit {args} {exit 0}
"""
env = {**os.environ, "XEDA_FAKE_VCS_STATE": state}
result = subprocess.run(
    ["tclsh"], input=model + "\nsource {" + str(script) + "}\n", text=True, env=env
)
sys.exit(
    3
    if state in ("fatal", "vhdl_failure")
    else 2 if state == "finish_nonzero" else result.returncode
)
