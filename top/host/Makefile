# Build the host-side firmware as a shared object so the cocotb test harness
# can load it via ctypes.
#
#   make            # build libplover_hello.so
#   make clean      # remove build artifacts
#
# The pytest harness invokes this Makefile automatically before the test
# runs, so manual building is only needed if you're iterating on the C++
# outside the test loop.

CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -fPIC
LDFLAGS  ?= -shared

LIB := libplover_hello.so
SRCS := plover_hello.cc
OBJS := $(SRCS:.cc=.o)
HDRS := plover_hello.h

.PHONY: all clean

all: $(LIB)

$(LIB): $(OBJS)
	$(CXX) $(LDFLAGS) -o $@ $^

%.o: %.cc $(HDRS)
	$(CXX) $(CXXFLAGS) -c $< -o $@

clean:
	rm -f $(LIB) $(OBJS)
