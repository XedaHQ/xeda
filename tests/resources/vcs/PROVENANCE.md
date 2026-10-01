# VCS fixture provenance and verification boundary

No VCS binary or license was available for Task 7. All stand-in output in
`tests/fake_tools/vcs_runtime.py` is synthetic, including the VHDL cases. It is
not a recording of a licensed run. The fake executes the generated Tcl under
`tclsh`, models native run/quit/end-of-simulation behavior, and leaves separate
analysis, elaboration and runtime invocation markers.

The contract was checked against these primary vendor manuals, available from
third-party mirrors (read 2026-10-01):

- [Synopsys UCLI T-2022.06, June 2022](https://studylib.net/doc/28088917/ucli-ug):
  sections 1-15 and 3-3/3-5, `config endofsim noexit`; 3-23 through 3-27, `run`;
  3-70 through 3-72, `senv time` and `senv timePrecision` (unit-bearing values);
  3-74/3-82, absolute time breakpoints and `stop -command` callbacks; 3-6,
  simulation input with `simv -ucli -i run.tcl`.
- [Synopsys VCS 2019 user guide](https://studylib.net/doc/28607901/vcs-user-guide-2019):
  14-43, SV severity-task diagnostic headers; 14-118 through 14-120, native
  finish source diagnostics; 5-62/5-63, optional `-exitstatus` and its lack of
  VHDL assertion support. Xeda does not enable this extra status policy or
  reinterpret the actual process status.
- [Synopsys VCS MX G-2012.09](https://studylib.net/doc/25887484/vcsmx-ug):
  17-31, SVA failure messages carrying source, start time and failed-at time.
- [Executable VCS help, reproduced](https://sopho-help-info.readthedocs.io/en/latest/vcs-help/index.html):
  `-R`, `-ucli`, `-gui`, and runtime limit flags. Limit flags without an observed
  callback and actual final time are not proof of reaching a bound.

The fake's SV diagnostic and SVA examples follow those documented grammars;
message content, source names and times are synthetic. Tagged VHDL runtime
errors exercise the distinct VCS `SIMERR` diagnostic family; the fatal tag is
explicitly synthetic. These cases do not certify VHDL assertion output for any
release. Quiet `$finish(0)`, VHDL `std.env.finish` and `std.env.stop` deliberately
supply no finish diagnostic: time/checkpoint/exit alone cannot make them pass.

The owned script captures a time checkpoint before quit and brackets runtime
stdout/stderr. A time-limit callback is separate from a returned run command;
a drain or unrelated break at the same timestamp is not that callback. User
scripts and GUI arguments retain the same evidence boundary, but supplying
`stop_time` with either is refused because arbitrary control could resume past
or remove the bound. An absolute breakpoint at zero is also refused because
the documented interface requires a time greater than the current time.

Real-release verification must check script/GUI startup, endofsim behavior,
stdout markers, finish and severity grammars, VHDL assertions, breakpoint
behavior on queue drain, actual-time precision, and nonzero exit propagation.
The bsc VCS/vcsi launch wiring belongs to Task 4c, after the simulator families.
