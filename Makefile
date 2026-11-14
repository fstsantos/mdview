# Installs mdview.py as the `mdview` command.
#
#   sudo make install                 # system-wide, into /usr/local/bin
#   make install PREFIX=~/.local      # current user only, no sudo
#   make install-link PREFIX=~/.local # symlink to this checkout (for development)
#   sudo make uninstall               # use the same PREFIX as on install

PREFIX  ?= /usr/local
BINDIR  ?= $(PREFIX)/bin
DESTDIR ?=

SRC    := mdview.py
TARGET := $(DESTDIR)$(BINDIR)/mdview

.PHONY: help install install-link uninstall

help:
	@echo "make install        copy $(SRC) to $(BINDIR)/mdview"
	@echo "make install-link   symlink $(BINDIR)/mdview to this checkout"
	@echo "make uninstall      remove $(BINDIR)/mdview"
	@echo
	@echo "PREFIX defaults to /usr/local (needs sudo). For a per-user install:"
	@echo "  make install PREFIX=~/.local"

install:
	install -d "$(DESTDIR)$(BINDIR)"
	install -m 755 $(SRC) "$(TARGET)"
	@echo "Installed $(TARGET)"
	@$(MAKE) --no-print-directory check-path

install-link:
	install -d "$(DESTDIR)$(BINDIR)"
	chmod +x $(SRC)
	ln -sfn "$(CURDIR)/$(SRC)" "$(TARGET)"
	@echo "Linked $(TARGET) -> $(CURDIR)/$(SRC)"
	@$(MAKE) --no-print-directory check-path

uninstall:
	rm -f "$(TARGET)"
	@echo "Removed $(TARGET)"

.PHONY: check-path
check-path:
	@case ":$$PATH:" in \
		*":$(BINDIR):"*) ;; \
		*) echo "Note: $(BINDIR) is not on your PATH. Add this to your shell profile:"; \
		   echo "  export PATH=\"$(BINDIR):\$$PATH\"" ;; \
	esac
