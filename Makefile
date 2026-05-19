VENV     = /opt/nut-up
BIN      = $(VENV)/bin/nut-up
SVCFILE  = /etc/systemd/system/nut-up.service
CONFDIR  = /etc/nut-up
CONFFILE = $(CONFDIR)/config.yaml

.PHONY: install update uninstall purge

install:
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
	@echo ""
	@echo "Installed. Edit $(CONFFILE) then run: sudo systemctl enable --now nut-up"

update:
	$(VENV)/bin/pip install --quiet .
	systemctl restart nut-up

uninstall:
	systemctl disable --now nut-up || true
	rm -f $(SVCFILE)
	rm -rf $(VENV)
	systemctl daemon-reload
	@echo "Config left at $(CONFDIR) — remove manually if desired"

purge: uninstall
	rm -rf $(CONFDIR)
	userdel nut-up 2>/dev/null || true
	@echo "Purge complete — all nut-up files and the system user have been removed"
