PREFIX ?= /usr/local
SYSTEMD_UNIT_DIR ?= /etc/systemd/system
SYSTEMD_SLEEP_DIR ?= /usr/lib/systemd/system-sleep
UDEV_RULES_DIR ?= /etc/udev/rules.d

.PHONY: check install uninstall enable disable start stop status restart

check:
	@python3 -c "import hid" 2>/dev/null || { echo "ERROR: python-hidapi not installed"; echo "  Arch: sudo pacman -S python-hidapi"; echo "  Debian: sudo apt install python3-hid"; exit 1; }
	@echo "Dependencies OK"

install: check
	install -Dm755 ryujin_iii_fand.py $(DESTDIR)$(PREFIX)/bin/ryujin-iii-fand
	install -Dm644 ryujin-iii-fand.service $(DESTDIR)$(SYSTEMD_UNIT_DIR)/ryujin-iii-fand.service
	install -Dm755 ryujin-iii-sleep.sh $(DESTDIR)$(SYSTEMD_SLEEP_DIR)/ryujin-iii-sleep.sh
	install -Dm644 99-ryujin.rules $(DESTDIR)$(UDEV_RULES_DIR)/99-ryujin.rules
	sed -i 's|ExecStart=.*|ExecStart=$(PREFIX)/bin/ryujin-iii-fand --display cyberpunk|' \
		$(DESTDIR)$(SYSTEMD_UNIT_DIR)/ryujin-iii-fand.service
	udevadm control --reload-rules
	systemctl daemon-reload
	@echo "Installed. Run: sudo make enable start"

uninstall:
	systemctl stop ryujin-iii-fand 2>/dev/null || true
	systemctl disable ryujin-iii-fand 2>/dev/null || true
	rm -f $(DESTDIR)$(PREFIX)/bin/ryujin-iii-fand
	rm -f $(DESTDIR)$(SYSTEMD_UNIT_DIR)/ryujin-iii-fand.service
	rm -f $(DESTDIR)$(SYSTEMD_SLEEP_DIR)/ryujin-iii-sleep.sh
	rm -f $(DESTDIR)$(UDEV_RULES_DIR)/99-ryujin.rules
	udevadm control --reload-rules
	systemctl daemon-reload

enable:
	systemctl enable ryujin-iii-fand

disable:
	systemctl disable ryujin-iii-fand

start:
	systemctl start ryujin-iii-fand

stop:
	systemctl stop ryujin-iii-fand

restart:
	systemctl restart ryujin-iii-fand

status:
	systemctl status ryujin-iii-fand
