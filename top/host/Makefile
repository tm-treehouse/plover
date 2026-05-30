# Build the host-side firmware as a shared object so the cocotb test harness
# can load it via ctypes. Plain C — gcc, -std=c11.
#
#   make EXTRA_INCLUDES="-I/path/to/gen1 -I/path/to/gen2"
#
# The pytest harness invokes this Makefile automatically before the test
# runs, passing EXTRA_INCLUDES with the FuseSoC build directories where
# peakrdl-cheader dropped the generated unit headers (axil_shell_regs.h,
# syscon_regs.h). Manual building is only needed if you're iterating on
# the C outside the test loop — in that case you'll need to supply
# EXTRA_INCLUDES yourself.

CC       ?= gcc
CFLAGS   ?= -std=c11 -O2 -Wall -Wextra -fPIC
LDFLAGS  ?= -shared

EXTRA_INCLUDES ?=

LIB := libplover_hello.so
SRCS := plover_hello.c
OBJS := $(SRCS:.c=.o)
HDRS := plover_hello.h

.PHONY: all clean

all: $(LIB)

$(LIB): $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $^

%.o: %.c $(HDRS)
	$(CC) $(CFLAGS) $(EXTRA_INCLUDES) -c $< -o $@

clean:
	rm -f $(LIB) $(OBJS)
