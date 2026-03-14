# ryujin-iii-fand

Fan curve daemon for ASUS ROG Ryujin III AIO liquid coolers on Linux.

Single-file Python daemon. Reads liquid temperature via USB HID, interpolates
piecewise linear fan/pump curves with hysteresis, and optionally drives the LCD
HW monitor display with live sensor data.

## Supported devices

| PID | Device |
|-----|--------|
| `0x1ADA` | ROG Ryujin III White Edition |
| `0x1AA2` | ROG Ryujin III 360 |
| `0x1ADE` | ROG Ryujin III EVA Edition |
| `0x1BCB` | ROG Ryujin III Extreme |
| `0x1B4F` | ROG Ryujin III (variant) |

## Dependencies

- Python 3.10+
- `python-hidapi` (Arch: `sudo pacman -S python-hidapi`)

## Install

```bash
sudo make install
sudo make enable start
```

## Uninstall

```bash
sudo make uninstall
```

## Usage

```bash
# Run with cyberpunk HW monitor display (default)
ryujin-iii-fand --display cyberpunk

# Run with galactic background
ryujin-iii-fand --display galactic

# Fan control only (no display)
ryujin-iii-fand

# Custom curves from config file
ryujin-iii-fand --config /etc/ryujin-iii-fand.conf

# Dump sensor readings and exit
ryujin-iii-fand --dump
```

## Configuration

Create a config file with `[fan]` and `[pump]` curve sections.
Each line is `temperature = duty_percent`. Linear interpolation between points.

```ini
[settings]
spindown = 2    # degrees C hysteresis before ramping down

[fan]
# temp (C) = duty (%)
0 = 0
60 = 30
80 = 40
100 = 70

[pump]
20 = 20
50 = 40
65 = 55
70 = 65
```

Default curves match the Armoury Crate stock profiles.

## Display styles

| Style | Description |
|-------|-------------|
| `cyberpunk` | Animated cyberpunk neon background |
| `galactic` | Animated space/nebula background |
| `custom` | Solid black background |

## How it works

- Polls liquid temperature every 3 seconds (configurable with `--interval`)
- Interpolates fan and pump duty from the curve with hysteresis
- Sends `EC 1A` HID command to set duties (`ctrl_src=1` for host control)
- Optionally sends `EC 52`/`EC 53` to update LCD HW monitor display
- On shutdown (SIGTERM), sends `ctrl_src=0` to release pump back to internal Asetek controller
- On sleep (SIGUSR1), sends `EC 5C 20` standby command
- On wake (SIGUSR2), sends `EC 5C 10` to resume display

The embedded fan has no internal controller and must always be host-driven.
The pump has an internal Asetek controller that takes over when `ctrl_src=0`.

## Files

| File | Installed to | Purpose |
|------|-------------|---------|
| `ryujin_iii_fand.py` | `/usr/local/bin/ryujin-iii-fand` | Daemon |
| `ryujin-iii-fand.service` | `/etc/systemd/system/` | systemd unit |
| `ryujin-iii-sleep.sh` | `/usr/lib/systemd/system-sleep/` | Sleep/wake hook |
| `99-ryujin.rules` | `/etc/udev/rules.d/` | USB permissions |
| `ryujin-iii-reset.sh` | — | Virtual USB replug (manual) |

## License

GPL-3.0
