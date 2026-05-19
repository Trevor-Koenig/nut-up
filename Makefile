VENV     = /opt/nut-up
BIN      = $(VENV)/bin/nut-up
SVCFILE  = /etc/systemd/system/nut-up.service
CONFDIR  = /etc/nut-up
CONFFILE = $(CONFDIR)/config.yaml

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
	mkdir -p $(CONFDIR)
	[ -f $(CONFFILE) ] || cp deploy/config.example.yaml $(CONFFILE)
	chown nut-up:nut-up $(CONFFILE)
	chmod 640 $(CONFFILE)
	chown -R nut-up:nut-up $(CONFDIR)
	chmod 750 $(CONFDIR)
	cp deploy/nut-up.service $(SVCFILE)
	systemctl daemon-reload
	systemctl enable nut-up
	sed 's|^REPO=.*|REPO="$(CURDIR)"|' nutup > /usr/local/bin/nutup
	chmod +x /usr/local/bin/nutup
	@echo ""
	@echo "Installed. Edit $(CONFFILE) then run: sudo systemctl enable --now nut-up"
	@echo "You can now run 'nutup <command>' from anywhere."

update: ## Pull latest changes, reinstall package, and restart the service
	git pull
	$(VENV)/bin/pip install --quiet .
	systemctl restart nut-up

test: ## Check NUT server connectivity, credentials, and IPMI BMC access
	$(BIN) check --config $(CONFFILE)

uninstall: ## Stop and disable service, remove venv and unit file (config kept)
	systemctl disable --now nut-up || true
	rm -f $(SVCFILE)
	rm -rf $(VENV)
	rm -f /usr/local/bin/nutup
	systemctl daemon-reload
	@echo "Config left at $(CONFDIR) — remove manually if desired"

purge: uninstall ## Remove everything: config, system user, and this repo
	rm -rf $(CONFDIR)
	userdel nut-up 2>/dev/null || true
	@echo "Purge complete — all nut-up files and the system user have been removed"
	@echo "Removing repo at $(CURDIR) — if your shell is inside this directory, run: cd ~"
	@REPO="$(CURDIR)"; (sleep 1 && rm -rf "$$REPO") &
