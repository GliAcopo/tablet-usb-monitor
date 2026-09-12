# Builds the native capture helper against headers unpacked under
# .local/sysroot (see scripts/setup-native.sh); links the distro's runtime
# libraries directly, so no -dev packages are needed system-wide.
SYSROOT ?= ../.local/sysroot/usr/include
CFLAGS ?= -O2 -g -Wall -Wextra -std=gnu11
CPPFLAGS += -I$(SYSROOT) -I$(SYSROOT)/pipewire-0.3 -I$(SYSROOT)/spa-0.2 -I$(SYSROOT)/libdrm -D_REENTRANT
LDLIBS += -l:libpipewire-0.3.so.0 -l:libva.so.2 -l:libva-drm.so.2 -lpthread

tabs9-capture: tabs9-capture.c
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f tabs9-capture
