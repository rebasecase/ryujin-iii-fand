#!/usr/bin/python3
"""ryujin-fand — Fan curve daemon for ASUS ROG Ryujin III AIO coolers.

Uses hidapi for all USB communication. Requires kernel HID driver to be
bound (do NOT unbind via udev).

Usage:
    ryujin_fand.py [--interval SECS] [--config FILE] [--display [STYLE]]
    ryujin_fand.py --dump
"""

import argparse
import glob
import logging
import os
import signal
import subprocess
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
DEFAULT_SPINDOWN = 2
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
        return target

    target_at_hysteresis = int(round(interpolate(curve, temp + spindown)))
    if target_at_hysteresis >= last_duty:
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
        expected = cmd[0] & 0x7F
        self.write([PREFIX] + cmd)
        time.sleep(0.02)
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            msg = self.read(100)
            if msg is None:
                continue
            if len(msg) >= 2 and (msg[0] == PREFIX or msg[0] == expected):
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
            self.send_cmd([0x5C, 0x10])

    def init_hw_monitor(self, style=2):
        """Set up HW monitor display mode. Fire-and-forget commands."""
        self.send_cmd([0x52, style, 0x02, 0x02, 0x00,
                       0, 0, 0, 0xFF,
                       255, 255, 255, 0xFF, 255, 255, 255, 0xFF,
                       255, 255, 255, 0xFF, 255, 255, 255, 0xFF])
        time.sleep(0.05)
        self.send_cmd([0x51, 0x21])
        time.sleep(0.2)
        while self.dev.read(64, 50):
            pass

    def update_hw_strings(self, temp, pump_rpm, fan_rpm, temp_label="Liquid"):
        """Update HW monitor display strings. Fire-and-forget."""
        lines = [
            (temp_label, f"{temp:.1f}{UNIT_DEGC}"),
            ("Pump", f"{pump_rpm}{UNIT_RPM}"),
            ("Fan", f"{fan_rpm}{UNIT_RPM}"),
        ]
        for i, (label, value) in enumerate(lines):
            lb = list(label.encode("utf-8")[:18]) + [0] * 18
            vb = list(value.encode("utf-8")[:12]) + [0] * 12
            self.send_cmd([0x53, i] + lb[:18] + vb[:12])


def parse_curve_config(path):
    """Parse config file."""
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
    return fan or DEFAULT_FAN_CURVE, pump or DEFAULT_PUMP_CURVE, settings


def read_hardware_temps():
    """Reads current CPU and GPU temperatures in °C across AMD/Intel/NVIDIA."""
    temps = {}

    try:
        for path in glob.glob("/sys/class/thermal/thermal_zone*/"):
            type_file = os.path.join(path, "type")
            temp_file = os.path.join(path, "temp")
            if os.path.exists(type_file) and os.path.exists(temp_file):
                with open(type_file, "r") as f:
                    ztype = f.read().strip().lower()
                if any(k in ztype for k in ["x86_pkg_temp", "coretemp", "k10temp", "cpu"]):
                    with open(temp_file, "r") as f:
                        temps["cpu"] = float(f.read().strip()) / 1000.0
                        break

        if "cpu" not in temps:
            for path in glob.glob("/sys/class/hwmon/hwmon*/"):
                name_file = os.path.join(path, "name")
                if os.path.exists(name_file):
                    with open(name_file, "r") as f:
                        name = f.read().strip().lower()
                    if name in ["k10temp", "coretemp", "cpu_thermal"]:
                        temp_file = os.path.join(path, "temp1_input")
                        if os.path.exists(temp_file):
                            with open(temp_file, "r") as f:
                                temps["cpu"] = float(f.read().strip()) / 1000.0
                                break
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1
        )
        if res.returncode == 0 and res.stdout.strip():
            temps["gpu"] = float(res.stdout.strip().split("\n")[0])
    except Exception:
        pass

    if "gpu" not in temps:
        try:
            for path in glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*/temp1_input"):
                with open(path, "r") as f:
                    temps["gpu"] = float(f.read().strip()) / 1000.0
                    break
        except Exception:
            pass

    return temps


def main():
    parser = argparse.ArgumentParser(description="Ryujin III fan curve daemon")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--config", type=str, default="/etc/ryujin-iii-fand.conf")
    parser.add_argument("--display", nargs="?", const="cyberpunk", default=None, metavar="STYLE")
    parser.add_argument("--dump", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    settings = {}
    if args.config and os.path.exists(args.config):
        fan_curve, pump_curve, settings = parse_curve_config(args.config)
        spindown = float(settings.get("spindown", DEFAULT_SPINDOWN))
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

    display_style = args.display if args.display is not None else settings.get("display")
    if display_style is not None:
        style = STYLES.get(display_style, 2)
        dev.init_hw_monitor(style)
        log.info("LCD: %s", display_style)

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
            if display_style:
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

    temp_source = settings.get("temp_source", "cpu").lower()
    try:
        while running:
            if suspended:
                time.sleep(1)
                continue

            try:
                liquid_temp, pump_rpm, fan_rpm = dev.get_sensors()

                if liquid_temp is None:
                    errors += 1
                    if errors > 10:
                        log.warning("reconnecting...")
                        dev.close()
                        time.sleep(2)
                        if not dev.open():
                            log.error("device lost")
                            break
                        if display_style:
                            dev.init_hw_monitor(style)
                        errors = 0
                    time.sleep(args.interval)
                    continue

                errors = 0

                hw_temps = read_hardware_temps()
                cpu_t = hw_temps.get("cpu")
                gpu_t = hw_temps.get("gpu")

                if temp_source == "gpu" and gpu_t is not None:
                    active_temp = gpu_t
                    label_text = "GPU"
                elif temp_source == "cpu" and cpu_t is not None:
                    active_temp = cpu_t
                    label_text = "CPU"
                elif temp_source == "max":
                    valid_temps = [t for t in [liquid_temp, cpu_t, gpu_t] if t is not None]
                    active_temp = max(valid_temps) if valid_temps else liquid_temp

                    if active_temp == gpu_t:
                        label_text = "GPU"
                    elif active_temp == cpu_t:
                        label_text = "CPU"
                    else:
                        label_text = "Liquid"
                else:
                    active_temp = liquid_temp if liquid_temp is not None else 0.0
                    label_text = "Liquid"

                tf = interpolate_with_hysteresis(
                    fan_curve, active_temp, last_fan if last_fan >= 0 else 0, spindown)
                tp = interpolate_with_hysteresis(
                    pump_curve, active_temp, last_pump if last_pump >= 0 else 0, spindown)

                if tf != last_fan or tp != last_pump:
                    dev.set_duties(tf, tp)
                    log.info("%.1f%s (%s) → fan=%d%% pump=%d%% [%dr %dr]",
                             active_temp, UNIT_DEGC, label_text, tf, tp, pump_rpm, fan_rpm)
                    last_fan, last_pump = tf, tp
                else:
                    log.debug("%.1f%s [%dr %dr]", active_temp, UNIT_DEGC, pump_rpm, fan_rpm)

                if display_style is not None:
                    dev.update_hw_strings(active_temp, pump_rpm, fan_rpm, temp_label=label_text)

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
