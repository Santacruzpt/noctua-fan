#!/usr/bin/env python3
"""
noctua-fan: speed control for a Noctua NF-A4x10 5V PWM fan on a Raspberry Pi 4.

PWM  : hardware PWM via the kernel sysfs interface (dtoverlay=pwm), 25 kHz as
       per the Intel/Noctua 4-pin PWM spec. Default GPIO18 (pin 12).
Tach : open-collector output, 2 pulses per revolution, read with lgpio edge
       alerts using the Pi's internal pull-up. Default GPIO24 (pin 18). Optional.

Commands:
  noctua-fan set <0-100>   set duty cycle (manual override if daemon is running)
  noctua-fan auto          clear manual override (daemon follows the curve again)
  noctua-fan status        print temperature, duty and RPM
  noctua-fan rpm           measure RPM for a few seconds
  noctua-fan daemon        run the temperature-controlled loop (used by systemd)

Stdlib only (Python >= 3.11); python3-lgpio is needed only for RPM readout.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("noctua-fan")

DEFAULT_CONFIG_PATH = "/etc/noctua-fan.toml"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    # PWM output
    pwm_chip: int = 0                # /sys/class/pwm/pwmchip<N>
    pwm_channel: int = 0             # GPIO18 -> PWM0 channel 0
    pwm_frequency: int = 25_000      # Hz, Noctua spec: 25 kHz (21-28 kHz ok)
    # Tachometer input (set tach_gpio = -1 to disable)
    tach_gpio: int = 24
    gpio_chip: int = 0               # /dev/gpiochip<N>
    pulses_per_rev: int = 2
    # Control loop
    interval: float = 2.0            # seconds between temperature reads
    hysteresis: float = 3.0          # degC; speed only drops once temp falls this far
    min_duty: int = 20               # lowest non-zero duty applied (Noctua min ~20%)
    allow_stop: bool = True          # if the curve says 0, drive 0% (newer Noctua fans stop)
    kickstart_duty: int = 100        # duty briefly applied when starting from a stop
    kickstart_seconds: float = 1.0
    exit_duty: int = 100             # duty left on the pin when the daemon exits (fail-safe)
    stall_seconds: float = 10.0      # warn if RPM reads 0 this long while duty > 0
    thermal_zone: str = "/sys/class/thermal/thermal_zone0/temp"
    # Fan curve: list of [temperature_C, duty_percent], linearly interpolated
    curve: list[list[float]] = field(default_factory=lambda: [
        [40, 0], [45, 20], [55, 40], [65, 70], [75, 100],
    ])
    # Paths (overridable for testing)
    pwm_root: str = "/sys/class/pwm"
    run_dir: str = "/run/noctua-fan"

    @classmethod
    def load(cls, path: str | None) -> "Config":
        cfg = cls()
        p = Path(path or os.environ.get("NOCTUA_FAN_CONFIG", DEFAULT_CONFIG_PATH))
        if p.exists():
            with p.open("rb") as f:
                data = tomllib.load(f)
            for key, value in data.items():
                if not hasattr(cfg, key):
                    raise ValueError(f"{p}: unknown option '{key}'")
                setattr(cfg, key, value)
        elif path:
            raise FileNotFoundError(path)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not 1_000 <= self.pwm_frequency <= 100_000:
            raise ValueError("pwm_frequency out of range")
        if not self.curve or any(len(pt) != 2 for pt in self.curve):
            raise ValueError("curve must be a list of [temp, duty] pairs")
        self.curve = sorted([float(t), clamp(float(d))] for t, d in self.curve)
        for name in ("min_duty", "kickstart_duty", "exit_duty"):
            setattr(self, name, int(clamp(getattr(self, name))))


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


# --------------------------------------------------------------------------- #
# Hardware PWM (sysfs)
# --------------------------------------------------------------------------- #

class HardwarePWM:
    """Kernel sysfs PWM channel. Requires `dtoverlay=pwm,pin=18,func=2`."""

    def __init__(self, cfg: Config):
        self.chip = Path(cfg.pwm_root) / f"pwmchip{cfg.pwm_chip}"
        self.dir = self.chip / f"pwm{cfg.pwm_channel}"
        self.channel = cfg.pwm_channel
        self.period_ns = round(1e9 / cfg.pwm_frequency)

    def _write(self, name: str, value) -> None:
        (self.dir / name).write_text(f"{value}\n")

    def open(self) -> None:
        if not self.chip.exists():
            raise RuntimeError(
                f"{self.chip} not found. Add 'dtoverlay=pwm,pin=18,func=2' to "
                "/boot/firmware/config.txt and reboot (see README).")
        if not self.dir.exists():
            (self.chip / "export").write_text(f"{self.channel}\n")
            # udev needs a moment to create the node and fix permissions
            for _ in range(50):
                if (self.dir / "period").exists() and os.access(self.dir / "period", os.W_OK):
                    break
                time.sleep(0.02)
        # Duty must never exceed period, so shrink duty before changing period.
        try:
            if int((self.dir / "duty_cycle").read_text()) > self.period_ns:
                self._write("duty_cycle", 0)
        except (OSError, ValueError):
            pass
        self._write("period", self.period_ns)
        try:
            self._write("polarity", "normal")
        except OSError:
            pass  # cannot change polarity while enabled; normal is the default
        self._write("enable", 1)

    def set_duty(self, percent: float) -> None:
        self._write("duty_cycle", round(self.period_ns * clamp(percent) / 100))

    def get_duty(self) -> float:
        return 100.0 * int((self.dir / "duty_cycle").read_text()) / self.period_ns


# --------------------------------------------------------------------------- #
# Tachometer (lgpio)
# --------------------------------------------------------------------------- #

class Tachometer:
    """Counts falling edges on the tach line and converts them to RPM."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.count = 0
        self.lock = threading.Lock()
        self.last_time = time.monotonic()
        self.handle = None
        self.cb = None

    def open(self) -> bool:
        if self.cfg.tach_gpio < 0:
            return False
        try:
            import lgpio  # type: ignore
        except ImportError:
            log.warning("python3-lgpio not installed; RPM readout disabled")
            return False
        try:
            self.handle = lgpio.gpiochip_open(self.cfg.gpio_chip)
            lgpio.gpio_claim_alert(self.handle, self.cfg.tach_gpio,
                                   lgpio.FALLING_EDGE, lgpio.SET_PULL_UP)
            # Filter PWM crosstalk; 5000 rpm = 167 Hz, so 200 us is safe.
            lgpio.gpio_set_debounce_micros(self.handle, self.cfg.tach_gpio, 200)
            self.cb = lgpio.callback(self.handle, self.cfg.tach_gpio,
                                     lgpio.FALLING_EDGE, self._edge)
        except Exception as e:  # lgpio raises its own error type
            log.warning("cannot open tach on GPIO%d: %s", self.cfg.tach_gpio, e)
            self.close()
            return False
        self.last_time = time.monotonic()
        return True

    def _edge(self, chip, gpio, level, tick) -> None:
        with self.lock:
            self.count += 1

    def read_rpm(self) -> float:
        """RPM averaged over the time since the previous call."""
        now = time.monotonic()
        with self.lock:
            pulses, self.count = self.count, 0
        dt, self.last_time = now - self.last_time, now
        if dt <= 0:
            return 0.0
        return pulses / self.cfg.pulses_per_rev / dt * 60.0

    def close(self) -> None:
        if self.cb is not None:
            self.cb.cancel()
            self.cb = None
        if self.handle is not None:
            import lgpio  # type: ignore
            try:
                lgpio.gpio_free(self.handle, self.cfg.tach_gpio)
            except Exception:
                pass
            lgpio.gpiochip_close(self.handle)
            self.handle = None


# --------------------------------------------------------------------------- #
# Control logic
# --------------------------------------------------------------------------- #

def read_temp(cfg: Config) -> float:
    return int(Path(cfg.thermal_zone).read_text().strip()) / 1000.0


def interpolate(curve: list[list[float]], temp: float) -> float:
    if temp <= curve[0][0]:
        return curve[0][1]
    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if temp <= t1:
            return d0 + (d1 - d0) * (temp - t0) / (t1 - t0) if t1 > t0 else d1
    return curve[-1][1]


class Controller:
    """Maps temperature to duty with hysteresis, min duty and stop handling."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.duty: float | None = None

    def shape(self, raw: float) -> float:
        if raw <= 0:
            return 0.0 if self.cfg.allow_stop else float(self.cfg.min_duty)
        return max(raw, float(self.cfg.min_duty))

    def target(self, temp: float) -> float:
        up = self.shape(interpolate(self.cfg.curve, temp))
        if self.duty is None or up >= self.duty:
            self.duty = up
        else:
            # Falling: only follow the curve as if temp were `hysteresis` higher,
            # so the fan doesn't hunt around a curve point.
            down = self.shape(interpolate(self.cfg.curve, temp + self.cfg.hysteresis))
            self.duty = min(self.duty, down)
        return self.duty


# --------------------------------------------------------------------------- #
# Runtime state (shared between daemon and CLI)
# --------------------------------------------------------------------------- #

class RunState:
    def __init__(self, cfg: Config):
        self.dir = Path(cfg.run_dir)
        self.state = self.dir / "state.json"
        self.override = self.dir / "override"

    def daemon_running(self) -> bool:
        try:
            pid = json.loads(self.state.read_text())["pid"]
            os.kill(pid, 0)
            return True
        except (OSError, ValueError, KeyError):
            return False

    def write_state(self, **data) -> None:
        tmp = self.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.state)

    def read_override(self) -> float | None:
        try:
            return clamp(float(self.override.read_text()))
        except (OSError, ValueError):
            return None


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_daemon(cfg: Config, stop: threading.Event | None = None) -> int:
    pwm = HardwarePWM(cfg)
    pwm.open()
    tach = Tachometer(cfg)
    has_tach = tach.open()
    ctl = Controller(cfg)
    rs = RunState(cfg)
    rs.dir.mkdir(parents=True, exist_ok=True)

    if stop is None:
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: stop.set())

    log.info("started: %d Hz on pwmchip%d/pwm%d, tach %s, curve %s",
             cfg.pwm_frequency, cfg.pwm_chip, cfg.pwm_channel,
             f"GPIO{cfg.tach_gpio}" if has_tach else "off", cfg.curve)

    applied: float | None = None
    zero_rpm_since: float | None = None
    try:
        while not stop.is_set():
            temp = read_temp(cfg)
            override = rs.read_override()
            duty = override if override is not None else ctl.target(temp)

            if duty != applied:
                if (applied == 0 or applied is None) and 0 < duty < cfg.kickstart_duty \
                        and cfg.kickstart_seconds > 0:
                    pwm.set_duty(cfg.kickstart_duty)
                    stop.wait(cfg.kickstart_seconds)
                pwm.set_duty(duty)
                log.info("temp %.1f°C -> duty %.0f%%%s", temp, duty,
                         " (manual)" if override is not None else "")
                applied = duty
                if has_tach:
                    tach.read_rpm()  # restart the measuring window

            stop.wait(cfg.interval)
            rpm = tach.read_rpm() if has_tach else None

            if has_tach and duty > 0 and rpm == 0:
                zero_rpm_since = zero_rpm_since or time.monotonic()
                if time.monotonic() - zero_rpm_since >= cfg.stall_seconds:
                    log.warning("fan stall? 0 rpm at %.0f%% duty for %.0fs",
                                duty, time.monotonic() - zero_rpm_since)
                    zero_rpm_since = time.monotonic()
            else:
                zero_rpm_since = None

            rs.write_state(pid=os.getpid(), time=time.time(), temp=temp,
                           duty=duty, rpm=rpm, mode="manual" if override is not None else "auto")
    finally:
        log.info("exiting, leaving fan at %d%%", cfg.exit_duty)
        pwm.set_duty(cfg.exit_duty)
        tach.close()
        try:
            rs.state.unlink()
        except OSError:
            pass
    return 0


def cmd_set(cfg: Config, percent: float) -> int:
    percent = clamp(percent)
    rs = RunState(cfg)
    if rs.daemon_running():
        rs.override.write_text(f"{percent}\n")
        print(f"manual override: {percent:.0f}% (run 'noctua-fan auto' to resume the curve)")
    else:
        pwm = HardwarePWM(cfg)
        pwm.open()
        pwm.set_duty(percent)
        print(f"duty set to {percent:.0f}%")
    return 0


def cmd_auto(cfg: Config) -> int:
    rs = RunState(cfg)
    try:
        rs.override.unlink()
        print("manual override cleared; following the temperature curve")
    except FileNotFoundError:
        print("no manual override was set")
    if not rs.daemon_running():
        print("note: the daemon is not running (systemctl start noctua-fan)")
    return 0


def cmd_rpm(cfg: Config, seconds: float) -> int:
    tach = Tachometer(cfg)
    if not tach.open():
        print("tachometer unavailable", file=sys.stderr)
        return 1
    try:
        time.sleep(seconds)
        print(f"{tach.read_rpm():.0f} rpm")
    finally:
        tach.close()
    return 0


def cmd_status(cfg: Config, as_json: bool) -> int:
    rs = RunState(cfg)
    if rs.daemon_running():
        st = json.loads(rs.state.read_text())
    else:
        st = {"mode": "daemon not running", "rpm": None}
        try:
            st["temp"] = read_temp(cfg)
        except OSError:
            st["temp"] = None
        try:
            st["duty"] = HardwarePWM(cfg).get_duty()
        except (OSError, ValueError):
            st["duty"] = None
    if as_json:
        print(json.dumps(st))
        return 0
    fmt = lambda v, f: "n/a" if v is None else f.format(v)
    print(f"mode : {st['mode']}")
    print(f"temp : {fmt(st.get('temp'), '{:.1f} °C')}")
    print(f"duty : {fmt(st.get('duty'), '{:.0f} %')}")
    print(f"rpm  : {fmt(st.get('rpm'), '{:.0f}')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="noctua-fan", description=__doc__.split("\n\n")[0])
    ap.add_argument("-c", "--config", help=f"config file (default {DEFAULT_CONFIG_PATH})")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("set", help="set duty cycle 0-100")
    p.add_argument("percent", type=float)
    sub.add_parser("auto", help="clear manual override")
    p = sub.add_parser("status", help="show temperature, duty and RPM")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("rpm", help="measure RPM")
    p.add_argument("-s", "--seconds", type=float, default=3.0)
    sub.add_parser("daemon", help="run the temperature control loop")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    try:
        cfg = Config.load(args.config)
        if args.cmd == "set":
            return cmd_set(cfg, args.percent)
        if args.cmd == "auto":
            return cmd_auto(cfg)
        if args.cmd == "status":
            return cmd_status(cfg, args.json)
        if args.cmd == "rpm":
            return cmd_rpm(cfg, args.seconds)
        return cmd_daemon(cfg)
    except PermissionError as e:
        print(f"permission denied: {e.filename} (run with sudo or add your user "
              "to the 'gpio' group)", file=sys.stderr)
    except (RuntimeError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
