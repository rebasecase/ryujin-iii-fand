#!/usr/bin/python3
"""ryujin-fand — Fan curve daemon for ASUS ROG Ryujin III AIO coolers.

Uses hidapi for all USB communication. Requires kernel HID driver to be
bound (do NOT unbind via udev).

Usage:
    ryujin_fand.py [--interval SECS] [--config FILE] [--display [STYLE]]
    ryujin_fand.py --dump
"""

import argparse
import logging
import os
import signal
import sys
import time

import hid

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ryujin-fand")

VID = 0x0B05
PIDS = [0x1ADA, 0x1AA2, 0x1ADE, 0x1BCB, 0x1B4F]
PREFIX = 0xEC
REPORT_LEN = 65

TEMP_OFFSET = 5
PUMP_RPM_OFFSET = 7
FAN_RPM_OFFSET = 10

UNIT_DEGC = bytes([0xE2, 0x84, 0x83]).decode("utf-8")
UNIT_RPM = bytes([0xE2, 0x86, 0x8C]).decode("utf-8")

DEFAULT_FAN_CURVE = [(0, 0), (60, 30), (80, 40), (100, 70)]
DEFAULT_PUMP_CURVE = [(20, 20), (50, 40), (65, 55), (70, 65)]
DEFAULT_SPINDOWN = 2  # °C hysteresis before ramping down (from Armoury Crate)
STYLES = {"galactic": 0, "cyberpunk": 1, "custom": 2}


def interpolate(curve, temp):
    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t0, d0 = curve[i]
        t1, d1 = curve[i + 1]
        if t0 <= temp <= t1:
            return d0 + (temp - t0) * (d1 - d0) / (t1 - t0) if t1 != t0 else d0
    return curve[-1][1]


def interpolate_with_hysteresis(curve, temp, last_duty, spindown):
    """Interpolate duty with hysteresis to prevent fan hunting.

    Only ramps DOWN if temp drops spindown°C below the threshold that
    would produce the current duty. Ramps UP immediately.
    """
    target = int(round(interpolate(curve, temp)))

    if target >= last_duty:
        # Ramping up — apply immediately
        return target

    # Ramping down — check if we've dropped enough
    target_at_hysteresis = int(round(interpolate(curve, temp + spindown)))
    if target_at_hysteresis >= last_duty:
        # Haven't dropped enough — hold current duty
        return last_duty

    return target


class RyujinHID:
    def __init__(self):
        self.dev = None
        self.pid = None

    def open(self):
        for pid in PIDS:
            try:
                d = hid.device()
                d.open(VID, pid)
                self.dev = d
                self.pid = pid
                log.info("opened PID=0x%04x (%s)", pid, d.get_product_string())
                return True
            except Exception:
                continue
        return False

    def close(self):
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None

    def write(self, data):
        """Write HID report. Pads to 65 bytes."""
        padded = data + [0] * (REPORT_LEN - len(data))
        self.dev.write(padded[:REPORT_LEN])

    def read(self, timeout_ms=500):
        data = self.dev.read(REPORT_LEN, timeout_ms)
        return list(data) if data else None

    def send_cmd(self, cmd):
        """Send a command, no response expected."""
        self.write([PREFIX] + cmd)

    def send_recv(self, cmd, timeout_ms=500):
        """Send command and read matching response. Skips stale ACKs."""
        expected = cmd[0] & 0x7F  # firmware echoes cmd with bit 7 cleared
        self.write([PREFIX] + cmd)
        time.sleep(0.02)
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            msg = self.read(100)
            if msg is None:
                continue
            if len(msg) >= 2 and (msg[0] == PREFIX or msg[0] == expected):
                # hidapi may or may not include the EC prefix
                resp_cmd = msg[1] if msg[0] == PREFIX else msg[0]
                if (resp_cmd & 0x7F) == expected:
                    return msg
            log.debug("skipped stale: [%s] (wanted 0x%02x)",
                       " ".join(f"{b:02x}" for b in msg[:4]), expected)
        return None

    def get_sensors(self):
        msg = self.send_recv([0x99])
        if msg is None or len(msg) < 12:
            return None, None, None
        # Response: [EC, 19, 00, 00, 00, temp_int, temp_frac, pump_lo, pump_hi, ?, fan_lo, fan_hi]
        temp = msg[TEMP_OFFSET] + msg[TEMP_OFFSET + 1] / 10.0
        pump = msg[PUMP_RPM_OFFSET] | (msg[PUMP_RPM_OFFSET + 1] << 8)
        fan = msg[FAN_RPM_OFFSET] | (msg[FAN_RPM_OFFSET + 1] << 8)
        return temp, pump, fan

    def get_duties(self):
        msg = self.send_recv([0x9A])
        if msg is None or len(msg) < 6:
            return None, None
        return msg[4], msg[5]

    def set_duties(self, fan_duty, pump_duty):
        fan_duty = max(0, min(100, int(fan_duty)))
        pump_duty = max(0, min(100, int(pump_duty)))
        self.send_cmd([0x1A, 0x01, fan_duty, pump_duty])

    def release_control(self):
        self.send_cmd([0x1A, 0x00, 0x00, 0x00])

    def set_standby(self, standby=True):
        """Enter/exit standby (screen off for sleep). EC 5C 20/01."""
        if standby:
            self.send_cmd([0x5C, 0x20])
        else:
            self.send_cmd([0x5C, 0x10])  # reset display

    def init_hw_monitor(self, style=2):
        """Set up HW monitor display mode. Fire-and-forget commands."""
        self.send_cmd([0x52, style, 0x02, 0x02, 0x00,
                       0, 0, 0, 0xFF,
                       255, 255, 255, 0xFF, 255, 255, 255, 0xFF,
                       255, 255, 255, 0xFF, 255, 255, 255, 0xFF])
        time.sleep(0.05)
        self.send_cmd([0x51, 0x21])
        time.sleep(0.2)
        # Drain any ACKs from the setup commands
        while self.dev.read(64, 50):
            pass

    def update_hw_strings(self, temp, pump_rpm, fan_rpm):
        """Update HW monitor display strings. Fire-and-forget."""
        lines = [
            ("Liquid", f"{temp:.1f}{UNIT_DEGC}"),
            ("Pump", f"{pump_rpm}{UNIT_RPM}"),
            ("Fan", f"{fan_rpm}{UNIT_RPM}"),
        ]
        for i, (label, value) in enumerate(lines):
            lb = list(label.encode("utf-8")[:18]) + [0] * 18
            vb = list(value.encode("utf-8")[:12]) + [0] * 12
            self.send_cmd([0x53, i] + lb[:18] + vb[:12])


def parse_curve_config(path):
    """Parse config file.

    Format:
        [settings]
        spindown = 2    # °C hysteresis before ramping down

        [fan]
        # temp_c = duty_%
        0 = 0
        60 = 30
        80 = 40
        100 = 70

        [pump]
        20 = 20
        50 = 40
        65 = 55
        70 = 65
    """
    fan, pump, settings = [], [], {}
    current = None
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            if line == "[fan]":
                current = fan
            elif line == "[pump]":
                current = pump
            elif line == "[settings]":
                current = settings
            elif "=" in line and current is not None:
                k, v = line.split("=", 1)
                if current is settings:
                    settings[k.strip()] = v.strip()
                else:
                    current.append((float(k.strip()), float(v.strip())))
    fan.sort(key=lambda x: x[0])
    pump.sort(key=lambda x: x[0])
    spindown = float(settings.get("spindown", DEFAULT_SPINDOWN))
    return fan or DEFAULT_FAN_CURVE, pump or DEFAULT_PUMP_CURVE, spindown


def main():
    parser = argparse.ArgumentParser(description="Ryujin III fan curve daemon")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--display", nargs="?", const="cyberpunk", default=None,
                        metavar="STYLE")
    parser.add_argument("--dump", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    if args.config and os.path.exists(args.config):
        fan_curve, pump_curve, spindown = parse_curve_config(args.config)
        log.info("loaded curves from %s", args.config)
    else:
        fan_curve, pump_curve, spindown = DEFAULT_FAN_CURVE, DEFAULT_PUMP_CURVE, DEFAULT_SPINDOWN

    log.info("fan: %s", fan_curve)
    log.info("pump: %s", pump_curve)
    log.info("hysteresis: %.1f°C", spindown)

    dev = RyujinHID()
    if not dev.open():
        log.error("no Ryujin III found (is kernel HID driver bound?)")
        sys.exit(1)

    if args.dump:
        temp, pump_rpm, fan_rpm = dev.get_sensors()
        fan_duty, pump_duty = dev.get_duties()
        if temp is not None:
            print(f"Liquid temp:  {temp:.1f} °C")
            print(f"Pump:         {pump_rpm} RPM ({pump_duty}%)")
            print(f"Fan:          {fan_rpm} RPM ({fan_duty}%)")
        else:
            print("Failed to read sensors")
        dev.close()
        return

    display = args.display is not None
    if display:
        style = STYLES.get(args.display, 2)
        dev.init_hw_monitor(style)
        log.info("LCD: %s", args.display)

    running = True
    suspended = False

    def shutdown(sig, frame):
        nonlocal running
        log.info("shutting down (signal %d)", sig)
        running = False

    def suspend(sig, frame):
        nonlocal suspended
        if not suspended:
            log.info("suspending display (SIGUSR1)")
            dev.set_standby(True)
            suspended = True

    def resume(sig, frame):
        nonlocal suspended
        if suspended:
            log.info("resuming display (SIGUSR2)")
            dev.set_standby(False)
            if display:
                time.sleep(0.5)
                dev.init_hw_monitor(style)
            suspended = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGUSR1, suspend)
    signal.signal(signal.SIGUSR2, resume)

    last_fan = last_pump = -1
    errors = 0
    log.info("running (interval=%.1fs)", args.interval)

    try:
        while running:
            if suspended:
                time.sleep(1)
                continue
            try:
                temp, pump_rpm, fan_rpm = dev.get_sensors()
                if temp is None:
                    errors += 1
                    if errors > 10:
                        log.warning("reconnecting...")
                        dev.close()
                        time.sleep(2)
                        if not dev.open():
                            log.error("device lost")
                            break
                        if display:
                            dev.init_hw_monitor(style)
                        errors = 0
                    time.sleep(args.interval)
                    continue

                errors = 0
                tf = interpolate_with_hysteresis(
                    fan_curve, temp, last_fan if last_fan >= 0 else 0, spindown)
                tp = interpolate_with_hysteresis(
                    pump_curve, temp, last_pump if last_pump >= 0 else 0, spindown)

                if tf != last_fan or tp != last_pump:
                    dev.set_duties(tf, tp)
                    log.info("%.1f%s → fan=%d%% pump=%d%% [%dr %dr]",
                             temp, UNIT_DEGC, tf, tp, pump_rpm, fan_rpm)
                    last_fan, last_pump = tf, tp
                else:
                    log.debug("%.1f%s [%dr %dr]", temp, UNIT_DEGC, pump_rpm, fan_rpm)

                if display:
                    dev.update_hw_strings(temp, pump_rpm, fan_rpm)

            except Exception as e:
                log.warning("error: %s", e)
                errors += 1
                if errors > 10:
                    log.error("too many errors")
                    break

            time.sleep(args.interval)
    finally:
        log.info("releasing control")
        try:
            dev.release_control()
        except Exception:
            pass
        dev.close()
        log.info("stopped")


if __name__ == "__main__":
    main()
