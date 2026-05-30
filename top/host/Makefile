# Build the host-side firmware as a shared object so the cocotb test harness
# can load it via ctypes.
#
#   make EXTRA_INCLUDES="-I/path/to/gen1 -I/path/to/gen2"
#
# The pytest harness invokes this Makefile automatically before the test
# runs, passing EXTRA_INCLUDES with the FuseSoC build directories where
# peakrdl-cpp dropped the generated unit headers (axil_shell_regs.hh,
# syscon_regs.hh). Manual building is only needed if you're iterating on
# the C++ outside the test loop — in that case you'll need to supply
# EXTRA_INCLUDES yourself.

CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wno-reorder -fPIC
LDFLAGS  ?= -shared

EXTRA_INCLUDES ?=

LIB := libplover_hello.so
SRCS := plover_hello.cc
OBJS := $(SRCS:.cc=.o)
HDRS := plover_hello.h

.PHONY: all clean

all: $(LIB)

$(LIB): $(OBJS)
	$(CXX) $(LDFLAGS) -o $@ $^

%.o: %.cc $(HDRS)
	$(CXX) $(CXXFLAGS) $(EXTRA_INCLUDES) -c $< -o $@

clean:
	rm -f $(LIB) $(OBJS)
