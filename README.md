# noctua-fan

Temperature-controlled speed control for a **Noctua NF-A4x10 5V PWM** fan on a
**Raspberry Pi 4**, using the Pi's hardware PWM at 25 kHz (Noctua's spec) and
the fan's tachometer for RPM readout.

## Wiring

Noctua's 4-pin connector → Pi 40-pin header (no extra components needed):

| Fan wire | Function      | Pi header pin       |
|----------|---------------|---------------------|
| Black    | GND           | Pin 6 (GND)         |
| Yellow   | +5 V          | Pin 4 (5 V)         |
| Green    | Tach (RPM)    | Pin 18 (GPIO24)     |
| Blue     | PWM control   | Pin 12 (GPIO18)     |

```
 3V3  (1) (2)  5V
      (3) (4)  5V      <- yellow
      (5) (6)  GND     <- black
      ...
      (11)(12) GPIO18  <- blue  (PWM0)
      ...
      (17)(18) GPIO24  <- green (tach)
```

* The PWM input accepts the Pi's 3.3 V logic directly. Noctua documents this
  in its own Raspberry Pi guidance.
* The tach line is open-collector. The app enables the Pi's internal pull-up
  to 3.3 V, so **never pull it up to 5 V**.
* The fan draws about 50 mA, well within what the 5 V rail can supply.
* Fail-safe: if the PWM line floats (Pi off, service not running), the fan's
  internal pull-up makes it run at **full speed**.
* Hardware PWM0 on GPIO18 is shared with the 3.5 mm analog audio jack. Don't
  use analog audio at the same time. HDMI audio is unaffected.

## Install (on the Pi)

```sh
scp -r noctua-fan pi@raspberrypi:   # from your PC
ssh pi@raspberrypi
cd noctua-fan && sudo ./install.sh
sudo reboot                         # only needed the first time (enables the PWM overlay)
```

`install.sh` does the following:
- installs `python3-lgpio` (for the tachometer)
- copies the app to `/usr/local/bin/noctua-fan` and the config to `/etc/noctua-fan.toml`
- adds `dtoverlay=pwm,pin=18,func=2` to `/boot/firmware/config.txt`
- enables the `noctua-fan` systemd service

## Usage

Run `noctua-fan` with no arguments (or `noctua-fan help`) to list every
command and option. Use `noctua-fan help <command>` for one command's options.

```sh
noctua-fan speed               # current fan speed (rpm)
noctua-fan stats               # current / average / max / min speed, last 24 h
noctua-fan stats --hours 6     # same, for any period (up to history_days)
noctua-fan stats --json        # machine-readable output
noctua-fan status              # temp, duty, rpm, mode
noctua-fan set 60              # manual 60 % (overrides the curve while the service runs)
noctua-fan auto                # back to the temperature curve
noctua-fan rpm                 # raw tach measurement (only when the service isn't running)
journalctl -u noctua-fan -f
```

Example `stats` output:

```
Fan speed, last 24 h
  current : 1840 rpm
  average : 1712 rpm
  maximum : 3655 rpm  (at 2026-10-15 14:02)
  minimum : 1190 rpm
  avg duty: 31 %
  avg temp: 51.4 °C   (max 63.0 °C)
  data    : 1440 samples since 2026-10-14 15:30, 24.0 h covered
```

### How the speed history works

The service measures RPM every `interval` seconds (default 2). Each minute
(`history_interval`) it stores one row in `/var/lib/noctua-fan/history.db`
(SQLite) with that minute's average, maximum and minimum RPM, plus the average
duty and temperature. That's about 1,440 small writes a day, which is gentle on
the SD card. Rows older than `history_days` (default 7) are deleted.

`stats` reports the following:
- **average** is time-weighted over the period.
- **maximum** is the highest 2-second reading.
- **minimum** is the lowest 2-second reading. It is 0 if the fan was stopped.

If the service has run for less than the requested period, `stats` says so.

Users in the `gpio` group (the default `pi` user is) can run these commands
without sudo.

## Configuration

Edit `/etc/noctua-fan.toml`, then run `sudo systemctl restart noctua-fan`.
Main settings:

- `curve`: `[temp °C, duty %]` points, linearly interpolated. The default is
  off below 40 °C, 20 % at 45 °C, 40 % at 55 °C, 70 % at 65 °C, and 100 % at 75 °C.
- `hysteresis`: how many °C the temperature must fall before the fan slows
  down. This stops the speed from hunting.
- `min_duty`: the lowest non-zero duty. The NF-A4x10 is rated for about
  1200 rpm at 20 %.
- `allow_stop`: whether a duty of 0 turns the fan off. Noctua fans built in
  2019 or later stop at 0 %; older units keep spinning at minimum speed.
- `kickstart_*`: a short full-speed burst when the fan starts from a stop.
- `exit_duty`: the duty left on the pin when the service stops. The default is
  100 % so the Pi stays safe.
- `tach_gpio = -1`: turns RPM readout off if the green wire isn't connected.
  Speed statistics then show n/a, but duty and temperature are still recorded.
- `history_interval` and `history_days`: the speed-history granularity and how
  long it is kept.

## Development

```sh
python3 -m unittest -v tests/test_noctua_fan.py
```

The tests run on any Linux machine against a fake sysfs PWM/thermal tree.
