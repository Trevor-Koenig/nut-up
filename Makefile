VENV     = /opt/nut-up
BIN      = $(VENV)/bin/nut-up
SVCFILE  = /etc/systemd/system/nut-up.service
CONFDIR  = /etc/nut-up
CONFFILE = $(CONFDIR)/config.yaml
TLSDIR   = $(CONFDIR)/tls

.PHONY: install update uninstall purge test help

help: ## Show available targets
	@echo "Usage: nutup <command>  |  sudo make <target>"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Install nut-up (venv, systemd service, config template)
	@which python3 >/dev/null || (echo "python3 required"; exit 1)
	id -u nut-up >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin nut-up
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade pip
	$(VENV)/bin/pip install --quiet .
	mkdir -p $(CONFDIR) $(TLSDIR)
	[ -f $(CONFFILE) ] || cp deploy/config.example.yaml $(CONFFILE)
	chmod 640 $(CONFFILE)
	@if [ ! -f $(TLSDIR)/cert.pem ]; then \
	  echo "Generating self-signed TLS certificate..."; \
	  openssl req -x509 -newkey rsa:4096 -days 3650 -nodes -quiet \
	    -keyout $(TLSDIR)/key.pem -out $(TLSDIR)/cert.pem \
	    -subj "/CN=$$(hostname)"; \
	fi
	chmod 600 $(TLSDIR)/key.pem
	chmod 640 $(TLSDIR)/cert.pem
	chown -R nut-up:nut-up $(CONFDIR)
	chmod 750 $(CONFDIR) $(TLSDIR)
	cp deploy/nut-up.service $(SVCFILE)
	systemctl daemon-reload
	systemctl enable nut-up
	sed 's|^REPO=.*|REPO="$(CURDIR)"|' nutup > /usr/local/bin/nutup
	chmod +x /usr/local/bin/nutup
	@if [ -n "$$SUDO_USER" ] && [ "$$SUDO_USER" != "root" ]; then \
	  echo "$$SUDO_USER ALL=(nut-up) NOPASSWD: $(BIN)" > /etc/sudoers.d/nut-up-runtime; \
	  chmod 440 /etc/sudoers.d/nut-up-runtime; \
	fi
	@echo ""
	@echo "Installed. Edit $(CONFFILE) then run: sudo systemctl enable --now nut-up"
	@echo "You can now run 'nutup <command>' from anywhere."

update: ## Pull latest changes, reinstall package, and restart the service
	@test -x $(VENV)/bin/pip || (echo "nut-up is not installed — run: sudo make install"; exit 1)
	git pull
	$(VENV)/bin/pip install --quiet .
	@if [ ! -f $(TLSDIR)/cert.pem ]; then \
	  echo "Generating self-signed TLS certificate..."; \
	  mkdir -p $(TLSDIR); \
	  openssl req -x509 -newkey rsa:4096 -days 3650 -nodes -quiet \
	    -keyout $(TLSDIR)/key.pem -out $(TLSDIR)/cert.pem \
	    -subj "/CN=$$(hostname)"; \
	  chown nut-up:nut-up $(TLSDIR)/key.pem $(TLSDIR)/cert.pem; \
	  chmod 600 $(TLSDIR)/key.pem; \
	  chmod 640 $(TLSDIR)/cert.pem; \
	  chmod 750 $(TLSDIR); \
	  echo "  To enable HTTPS, add to $(CONFFILE):"; \
	  echo "    tls_cert: $(TLSDIR)/cert.pem"; \
	  echo "    tls_key:  $(TLSDIR)/key.pem"; \
	fi
	sed 's|^REPO=.*|REPO="$(CURDIR)"|' nutup > /usr/local/bin/nutup
	chmod +x /usr/local/bin/nutup
	cp deploy/nut-up.service $(SVCFILE)
	systemctl daemon-reload
	systemctl restart nut-up
	@systemctl is-active --quiet nut-up && echo "nut-up restarted successfully" || (echo "ERROR: nut-up failed to start — check: journalctl -u nut-up -n 30"; exit 1)
	@if [ -n "$$SUDO_USER" ] && [ "$$SUDO_USER" != "root" ]; then \
	  echo "$$SUDO_USER ALL=(nut-up) NOPASSWD: $(BIN)" > /etc/sudoers.d/nut-up-runtime; \
	  chmod 440 /etc/sudoers.d/nut-up-runtime; \
	fi

test: ## Check NUT server connectivity, credentials, and IPMI BMC access
	$(BIN) check --config $(CONFFILE)

uninstall: ## Stop and disable service, remove venv and unit file (config kept)
	systemctl disable --now nut-up || true
	rm -f $(SVCFILE)
	rm -rf $(VENV)
	rm -f /usr/local/bin/nutup
	rm -f /etc/sudoers.d/nut-up-runtime
	systemctl daemon-reload
	@echo "Config left at $(CONFDIR) — remove manually if desired"

purge: uninstall ## Remove everything: config, system user, and this repo
	rm -rf $(CONFDIR)
	userdel nut-up 2>/dev/null || true
	@echo "Purge complete — all nut-up files and the system user have been removed"
	@echo "Removing repo at $(CURDIR) — if your shell is inside this directory, run: cd ~"
	@REPO="$(CURDIR)"; (sleep 1 && rm -rf "$$REPO") &
