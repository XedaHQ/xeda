# Xeda: hands the declaration of `vl_stop_maybe` (xeda_hooks.h) to Verilator's runtime.
#
# make reads this file first, named by the environment variable MAKEFILES. With
# `VL_USER_STOP_MAYBE`, verilated.cpp calls `vl_stop_maybe` with no declaration of it in scope, so
# verilated.o, and only it, is compiled with the header forced in. A model compiled in parallel
# includes its precompiled header with `-include`, and GCC uses that header only when no other
# file is forced in before it: a forced include on every compile would break that model's build.
# The header is next to this file.
XEDA_HOOKS_DIR := $(dir $(lastword $(MAKEFILE_LIST)))
verilated.o: CPPFLAGS += -include $(XEDA_HOOKS_DIR)xeda_hooks.h
