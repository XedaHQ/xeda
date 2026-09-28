// Build options for the BH testbench.
//
// bsc runs only BSV (.bsv) sources through its preprocessor: `-D` macros, such as the ones
// xeda passes from a design's `defines`/`parameters`, never reach BH (.bs) sources. A BH
// package that must follow a macro imports it as a constant from a BSV package like this one.
package TbCollatzOptions;

`ifdef XEDA_INJECT_BUG
Bool injectBug = True;  // deliberate test bug for negative tests
`else
Bool injectBug = False;
`endif

endpackage
