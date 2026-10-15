- The new flow `openroad` routes the globally routed database of `openroad_grt` in detail with
  OpenROAD, writes the final database, SDC file, DEF file, netlist and parasitics, and fails the
  run on a timing miss, a design rule violation or an antenna violation. It can also write the
  GDSII layout with KLayout (`write_gds`) and save images (`save_images`).
