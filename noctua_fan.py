#!/usr/bin/env python3
"""
noctua-fan: speed control for a Noctua NF-A4x10 5V PWM fan on a Raspberry Pi 4.

PWM  : hardware PWM via the kernel sysfs interface (dtoverlay=pwm), 25 kHz as
       per the Intel/Noctua 4-pin PWM spec. Default GPIO18 (pin 12).
Tach : open-collector output, 2 pulses per revolution, read with lgpio edge
       alerts using the Pi's internal pull-up. Default GPIO24 (pin 18). Optional.

Commands (run `noctua-fan` with no arguments to list them with their options):
  noctua-fan speed         current fan speed (RPM)
  noctua-fan stats         current, min, max and average speed over a period (default 24 h)
  noctua-fan status        temperature, duty, RPM and mode
  noctua-fan set <0-100>   set duty cycle (manual override if daemon is running)
  noctua-fan auto          clear manual override (daemon follows the curve again)
  noctua-fan rpm           measure RPM directly from the tach line
  noctua-fan daemon        run the temperature-controlled loop (used by systemd)
  noctua-fan serve         HTTP JSON API for remote clients (e.g. Open WebUI)

Stdlib only (Python >= 3.11); python3-lgpio is needed only for RPM readout.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
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
    # Speed history (for `stats`)
    history_db: str = "/var/lib/noctua-fan/history.db"
    history_interval: float = 60.0   # seconds aggregated into one stored sample
    history_days: float = 7.0        # samples older than this are deleted
    # HTTP API (`noctua-fan serve`)
    api_bind: str = "0.0.0.0"
    api_port: int = 9102
    api_token_file: str = "/etc/noctua-fan/api-token"  # empty/missing -> read-only API

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
        except PermissionError:
            return True  # exists, but owned by root (we're a gpio-group user)
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


class History:
    """Per-interval speed samples in SQLite: avg/max/min RPM, avg duty and temp."""

    SCHEMA = """CREATE TABLE IF NOT EXISTS samples (
        ts REAL PRIMARY KEY,      -- unix time at the end of the interval
        seconds REAL NOT NULL,    -- length of the interval
        rpm_avg REAL, rpm_max REAL, rpm_min REAL,   -- NULL if no tachometer
        duty_avg REAL NOT NULL,
        temp_avg REAL NOT NULL)"""

    def __init__(self, cfg: Config, readonly: bool = False):
        self.cfg = cfg
        path = Path(cfg.history_db)
        if readonly:
            if not path.exists():
                raise FileNotFoundError(f"no speed history yet ({path}); is the daemon running?")
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
            self.db.execute(self.SCHEMA)
            self.db.commit()
        self._reset()

    def _reset(self) -> None:
        self.start = time.time()
        self.rpms: list[float] = []
        self.duties: list[float] = []
        self.temps: list[float] = []

    def add(self, rpm: float | None, duty: float, temp: float) -> None:
        if rpm is not None:
            self.rpms.append(rpm)
        self.duties.append(duty)
        self.temps.append(temp)
        if time.time() - self.start >= self.cfg.history_interval:
            self.flush()

    def flush(self) -> None:
        if not self.duties:
            return
        now = time.time()
        r = self.rpms
        self.db.execute(
            "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?)",
            (now, now - self.start,
             sum(r) / len(r) if r else None, max(r) if r else None, min(r) if r else None,
             sum(self.duties) / len(self.duties), sum(self.temps) / len(self.temps)))
        self.db.execute("DELETE FROM samples WHERE ts < ?",
                        (now - self.cfg.history_days * 86400,))
        self.db.commit()
        self._reset()

    def stats(self, hours: float) -> dict:
        since = time.time() - hours * 3600
        row = self.db.execute(
            """SELECT COUNT(*), SUM(seconds), MIN(ts - seconds),
                      SUM(rpm_avg * seconds) / SUM(CASE WHEN rpm_avg IS NOT NULL
                                                        THEN seconds END),
                      MAX(rpm_max), MIN(rpm_min),
                      SUM(duty_avg * seconds) / SUM(seconds),
                      SUM(temp_avg * seconds) / SUM(seconds), MAX(temp_avg)
               FROM samples WHERE ts >= ?""", (since,)).fetchone()
        keys = ("samples", "covered_seconds", "first_sample", "rpm_avg", "rpm_max",
                "rpm_min", "duty_avg", "temp_avg", "temp_max")
        st = dict(zip(keys, row))
        st["max_time"] = None
        if st["rpm_max"] is not None:
            st["max_time"] = self.db.execute(
                "SELECT ts FROM samples WHERE ts >= ? AND rpm_max = ? ORDER BY ts DESC LIMIT 1",
                (since, st["rpm_max"])).fetchone()[0]
        return st

    def close(self) -> None:
        self.db.close()


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
    hist = History(cfg)

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

            hist.add(rpm, duty, temp)
            rs.write_state(pid=os.getpid(), time=time.time(), temp=temp,
                           duty=duty, rpm=rpm, mode="manual" if override is not None else "auto")
    finally:
        log.info("exiting, leaving fan at %d%%", cfg.exit_duty)
        pwm.set_duty(cfg.exit_duty)
        tach.close()
        hist.flush()
        hist.close()
        try:
            rs.state.unlink()
        except OSError:
            pass
    return 0


def set_duty(cfg: Config, percent: float) -> dict:
    percent = clamp(percent)
    rs = RunState(cfg)
    if rs.daemon_running():
        rs.override.write_text(f"{percent}\n")
        return {"duty": percent, "mode": "manual",
                "message": f"manual override: {percent:.0f}% (run 'noctua-fan auto' "
                           "to resume the curve)"}
    pwm = HardwarePWM(cfg)
    pwm.open()
    pwm.set_duty(percent)
    return {"duty": percent, "mode": "daemon not running",
            "message": f"duty set to {percent:.0f}%"}


def set_auto(cfg: Config) -> dict:
    rs = RunState(cfg)
    try:
        rs.override.unlink()
        msg = "manual override cleared; following the temperature curve"
    except FileNotFoundError:
        msg = "no manual override was set; already following the temperature curve"
    running = rs.daemon_running()
    if not running:
        msg += " (note: the daemon is not running - systemctl start noctua-fan)"
    return {"mode": "auto" if running else "daemon not running", "message": msg}


def cmd_set(cfg: Config, percent: float) -> int:
    print(set_duty(cfg, percent)["message"])
    return 0


def cmd_auto(cfg: Config) -> int:
    print(set_auto(cfg)["message"])
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


def get_status(cfg: Config) -> dict:
    rs = RunState(cfg)
    if rs.daemon_running():
        return json.loads(rs.state.read_text())
    st = {"mode": "daemon not running", "rpm": None}
    try:
        st["temp"] = read_temp(cfg)
    except OSError:
        st["temp"] = None
    try:
        st["duty"] = HardwarePWM(cfg).get_duty()
    except (OSError, ValueError):
        st["duty"] = None
    return st


def cmd_status(cfg: Config, as_json: bool) -> int:
    st = get_status(cfg)
    if as_json:
        print(json.dumps(st))
        return 0
    fmt = lambda v, f: "n/a" if v is None else f.format(v)
    print(f"mode : {st['mode']}")
    print(f"temp : {fmt(st.get('temp'), '{:.1f} °C')}")
    print(f"duty : {fmt(st.get('duty'), '{:.0f} %')}")
    print(f"rpm  : {fmt(st.get('rpm'), '{:.0f}')}")
    return 0


def current_rpm(cfg: Config) -> tuple[float | None, str]:
    """Latest RPM from the daemon, or a direct 2 s measurement if it isn't running."""
    rs = RunState(cfg)
    if rs.daemon_running():
        return json.loads(rs.state.read_text()).get("rpm"), "daemon"
    tach = Tachometer(cfg)
    if not tach.open():
        return None, "unavailable"
    try:
        time.sleep(2)
        return tach.read_rpm(), "measured"
    finally:
        tach.close()


def cmd_speed(cfg: Config, as_json: bool) -> int:
    rpm, source = current_rpm(cfg)
    if as_json:
        print(json.dumps({"rpm": rpm, "source": source}))
    elif rpm is None:
        print("current speed: n/a (no tachometer reading)")
    else:
        print(f"current speed: {rpm:.0f} rpm")
    return 0 if rpm is not None else 1


def get_stats(cfg: Config, hours: float) -> dict:
    if not 0 < hours <= 24 * 366:
        raise ValueError("hours must be between 0 and 8784")
    hist = History(cfg, readonly=True)
    try:
        st = hist.stats(hours)
    finally:
        hist.close()
    st["period_hours"] = hours
    st["rpm_current"], _ = current_rpm(cfg)
    return st


def cmd_stats(cfg: Config, hours: float, as_json: bool) -> int:
    st = get_stats(cfg, hours)
    if as_json:
        print(json.dumps(st))
        return 0

    fmt = lambda v, f: "n/a" if v is None else f.format(v)
    when = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    period = f"{hours:g} h"
    print(f"Fan speed, last {period}")
    print(f"  current : {fmt(st['rpm_current'], '{:.0f} rpm')}")
    print(f"  average : {fmt(st['rpm_avg'], '{:.0f} rpm')}")
    print(f"  maximum : {fmt(st['rpm_max'], '{:.0f} rpm')}"
          + (f"  (at {when(st['max_time'])})" if st["max_time"] else ""))
    print(f"  minimum : {fmt(st['rpm_min'], '{:.0f} rpm')}")
    print(f"  avg duty: {fmt(st['duty_avg'], '{:.0f} %')}")
    print(f"  avg temp: {fmt(st['temp_avg'], '{:.1f} °C')}"
          f"   (max {fmt(st['temp_max'], '{:.1f} °C')})")
    if st["samples"]:
        covered = st["covered_seconds"] / 3600
        note = "" if covered >= hours * 0.95 else "  - history shorter than the period"
        print(f"  data    : {st['samples']} samples since {when(st['first_sample'])}, "
              f"{covered:.1f} h covered{note}")
    else:
        print(f"  data    : no samples in the last {period}")
    return 0


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #

def load_token(cfg: Config) -> str:
    if not cfg.api_token_file:
        return ""
    try:
        return Path(cfg.api_token_file).read_text().strip()
    except FileNotFoundError:
        return ""


API_HELP = {
    "GET /status": "temperature, duty, rpm and control mode",
    "GET /speed": "current fan speed in rpm",
    "GET /stats?hours=24": "current/avg/max/min rpm, avg duty and temp over a period",
    "POST /set {\"percent\": 0-100}": "manual duty override (needs Bearer token)",
    "POST /auto": "return to temperature control (needs Bearer token)",
}


def make_handler(cfg: Config, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "noctua-fan"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_errors(self, fn) -> None:
            try:
                fn()
            except (ValueError, KeyError, TypeError) as e:
                self.reply(400, {"error": str(e)})
            except FileNotFoundError as e:
                self.reply(503, {"error": str(e)})
            except Exception as e:  # never kill the server thread
                log.exception("API error")
                self.reply(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self) -> None:
            url = urlparse(self.path)
            q = parse_qs(url.query)

            def run():
                if url.path in ("/", "/help"):
                    self.reply(200, {"endpoints": API_HELP, "control_enabled": bool(token)})
                elif url.path == "/status":
                    self.reply(200, get_status(cfg))
                elif url.path == "/speed":
                    rpm, source = current_rpm(cfg)
                    self.reply(200, {"rpm": rpm, "source": source})
                elif url.path == "/stats":
                    self.reply(200, get_stats(cfg, float(q.get("hours", ["24"])[0])))
                else:
                    self.reply(404, {"error": "not found", "endpoints": API_HELP})
            self.handle_errors(run)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path not in ("/set", "/auto"):
                self.reply(404, {"error": "not found", "endpoints": API_HELP})
                return
            if not token:
                self.reply(403, {"error": "fan control over the API is disabled "
                                          f"(no token in {cfg.api_token_file})"})
                return
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
                self.reply(401, {"error": "missing or wrong Bearer token"})
                return

            def run():
                length = int(self.headers.get("Content-Length") or 0)
                if length > 1024:
                    raise ValueError("request body too large")
                body = json.loads(self.rfile.read(length)) if length else {}
                if path == "/set":
                    percent = float(body["percent"])
                    if not 0 <= percent <= 100:
                        raise ValueError("percent must be between 0 and 100")
                    result = set_duty(cfg, percent)
                else:
                    result = set_auto(cfg)
                log.info("API %s from %s: %s", path, self.address_string(), result["message"])
                self.reply(200, result)
            self.handle_errors(run)

    return Handler


def make_server(cfg: Config) -> ThreadingHTTPServer:
    token = load_token(cfg)
    server = ThreadingHTTPServer((cfg.api_bind, cfg.api_port), make_handler(cfg, token))
    server.daemon_threads = True
    log.info("API listening on %s:%d (fan control %s)", cfg.api_bind,
             server.server_address[1], "enabled" if token else "disabled: read-only")
    return server


def cmd_serve(cfg: Config) -> int:
    server = make_server(cfg)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="noctua-fan",
        description="Speed control and monitoring for a Noctua NF-A4x10 5V PWM fan "
                    "on a Raspberry Pi 4.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  noctua-fan speed               current fan speed
  noctua-fan stats               current / avg / max / min speed, last 24 hours
  noctua-fan stats --hours 6     same, for the last 6 hours
  noctua-fan stats --json        machine-readable output
  noctua-fan set 60              run the fan at 60 %
  noctua-fan auto                return to temperature control
  noctua-fan serve               start the HTTP API (port 9102)
  noctua-fan help stats          options of one command""")
    ap.add_argument("-c", "--config", help=f"config file (default {DEFAULT_CONFIG_PATH})")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = ap.add_subparsers(dest="cmd", title="commands", metavar="<command>")
    p = sub.add_parser("speed", help="current fan speed (rpm)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p = sub.add_parser("stats", help="current, average, max and min speed over a period")
    p.add_argument("-H", "--hours", type=float, default=24.0,
                   help="period to summarise, in hours (default 24)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p = sub.add_parser("status", help="temperature, duty, rpm and control mode")
    p.add_argument("--json", action="store_true", help="JSON output")
    p = sub.add_parser("set", help="set duty cycle 0-100 (manual override)")
    p.add_argument("percent", type=float, help="duty cycle in percent")
    sub.add_parser("auto", help="clear manual override, follow the temperature curve")
    p = sub.add_parser("rpm", help="measure rpm directly (daemon must be stopped)")
    p.add_argument("-s", "--seconds", type=float, default=3.0,
                   help="measurement window (default 3)")
    sub.add_parser("daemon", help="run the temperature control loop (systemd)")
    sub.add_parser("serve", help="HTTP JSON API for remote clients such as Open WebUI")
    p = sub.add_parser("help", help="show these options, or a command's options")
    p.add_argument("topic", nargs="?", help="command name")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_help()
        return 0
    if args.cmd == "help":
        if args.topic:
            ap.parse_args([args.topic, "--help"])  # prints the command's options, exits
        ap.print_help()
        return 0

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    try:
        cfg = Config.load(args.config)
        if args.cmd == "speed":
            return cmd_speed(cfg, args.json)
        if args.cmd == "stats":
            return cmd_stats(cfg, args.hours, args.json)
        if args.cmd == "set":
            return cmd_set(cfg, args.percent)
        if args.cmd == "auto":
            return cmd_auto(cfg)
        if args.cmd == "status":
            return cmd_status(cfg, args.json)
        if args.cmd == "rpm":
            return cmd_rpm(cfg, args.seconds)
        if args.cmd == "serve":
            return cmd_serve(cfg)
        return cmd_daemon(cfg)
    except PermissionError as e:
        print(f"permission denied: {e.filename} (run with sudo or add your user "
              "to the 'gpio' group)", file=sys.stderr)
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
