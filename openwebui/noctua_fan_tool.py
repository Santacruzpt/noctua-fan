"""
title: Noctua Fan
description: Read and control the Noctua fans of one or more Raspberry Pis: current speed, temperature, speed statistics over any period, and setting the fan speed.
author: Santacruzpt
repository: https://github.com/Santacruzpt/noctua-fan
version: 2.0
license: MIT
"""

import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

DEFAULT_PORT = 9102


def _fmt(value, fmt, unit=""):
    return "unknown" if value is None else f"{format(value, fmt)}{unit}"


def _rpm(st, key):
    """'1850 rpm = 37% of max speed' from an API response's <key> and <key>_pct fields."""
    rpm, pct = st.get(key), st.get(f"{key}_pct")
    if rpm is None:
        return "unknown"
    return f"{rpm:.0f} rpm" + ("" if pct is None else f" = {pct:.0f}% of max speed")


ANSWER_RULE = (
    "IMPORTANT: in your answer, state every fan speed as BOTH the rpm value AND its "
    "percentage of the fan's maximum speed, e.g. '2450 rpm (49% of max)'. "
    "When several Raspberry Pis are listed, name the Pi for each value.\n"
)


def _split(text):
    return [e.strip() for e in re.split(r"[,;\n]", text or "") if e.strip()]


def _to_url(target):
    target = target.strip().rstrip("/")
    if "://" in target:
        return target
    if target.count(":") == 1:  # host:port
        return f"http://{target}"
    if target.startswith("["):  # [ipv6]:port
        return f"http://{target}"
    if ":" in target:  # bare IPv6
        return f"http://[{target}]:{DEFAULT_PORT}"
    return f"http://{target}:{DEFAULT_PORT}"


class Tools:
    class Valves(BaseModel):
        pis: str = Field(
            default="raspberrypi=raspberrypi.local",
            description="Raspberry Pis running `noctua-fan serve`, as comma-separated "
            "name=address entries. Address is an IP or hostname, optionally with :port "
            "(default 9102), or a full URL. "
            "Example: office=192.168.1.20, garage=192.168.1.31:9102",
        )
        api_tokens: str = Field(
            default="",
            description="Tokens from /etc/noctua-fan/api-token, needed only to change fan "
            "speeds. Either one token used for every Pi, or per-Pi name=token entries "
            "(comma-separated). Empty = read-only.",
        )
        timeout: float = Field(default=10.0, description="HTTP timeout in seconds")

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #

    def _pis(self):
        """{name: url} in configured order."""
        pis = {}
        for entry in _split(self.valves.pis):
            name, sep, target = entry.partition("=")
            if not sep:  # bare address: use it as its own name
                name, target = entry, entry
            pis[name.strip()] = _to_url(target)
        return pis

    def _token(self, name):
        default = ""
        names = {n.lower() for n in self._pis()}
        for entry in _split(self.valves.api_tokens):
            key, sep, value = entry.partition("=")
            if sep and key.strip().lower() in names:
                if key.strip().lower() == name.lower():
                    return value.strip()
            else:
                default = entry
        return default

    def _select(self, pi, allow_all=True):
        """Resolve the `pi` argument to [(name, url)], or raise ValueError."""
        pis = self._pis()
        if not pis:
            raise ValueError("No Raspberry Pis are configured in the tool's 'pis' valve.")
        names = ", ".join(pis)
        wanted = (pi or "").strip().lower()
        if wanted in ("", "all", "every", "all pis", "*"):
            if len(pis) == 1 or wanted or allow_all:
                return list(pis.items())
            raise ValueError(f"Several Raspberry Pis are configured ({names}); "
                             "say which one, or 'all'.")
        for name, url in pis.items():
            address = url.lower().split("//", 1)[-1]          # host:port
            if wanted in (name.lower(), url.lower(), address, address.rsplit(":", 1)[0]):
                return [(name, url)]
        # tolerant match: "the garage pi" -> garage, "office" -> office-pi
        fuzzy = [(n, u) for n, u in pis.items() if n.lower() in wanted or wanted in n.lower()]
        if len(fuzzy) == 1:
            return fuzzy
        raise ValueError(f"Unknown Raspberry Pi '{pi}'. Configured Pis: {names}.")

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #

    def _call(self, url, method, path, body=None, token=None):
        full = url + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(full, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=self.valves.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("error", e.reason)
            except ValueError:
                msg = e.reason
            raise RuntimeError(f"fan API error {e.code}: {msg}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise RuntimeError(f"cannot reach the fan API at {url}: {reason}") from None

    def _each(self, targets, fn):
        """Run fn(name, url) for every target in parallel; one text block per Pi."""
        def run(t):
            try:
                return fn(*t)
            except RuntimeError as e:
                return str(e)
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
            results = list(ex.map(run, targets))
        if len(targets) == 1:
            return results[0]
        return "\n\n".join(f"### Raspberry Pi '{n}'\n{r}" for (n, _), r in zip(targets, results))

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #

    def get_fan_status(self, pi: str = "") -> str:
        """
        Get the current state of a Raspberry Pi's fan: speed in RPM and as a percentage
        of the fan's maximum speed, CPU temperature, PWM duty cycle and whether it is
        under automatic (temperature) or manual control.
        Use this for questions like "how fast is the fan spinning?" or "how hot is the Pi?".
        :param pi: Name of the Raspberry Pi. Leave empty to get every configured Pi.
        """
        try:
            targets = self._select(pi)
        except ValueError as e:
            return str(e)

        def one(name, url):
            st = self._call(url, "GET", "/status")
            return (f"- speed: {_rpm(st, 'rpm')}\n"
                    f"- CPU temperature: {_fmt(st.get('temp'), '.1f', ' °C')}\n"
                    f"- duty cycle: {_fmt(st.get('duty'), '.0f', ' %')}\n"
                    f"- control mode: {st.get('mode', 'unknown')}")
        return ANSWER_RULE + "Fan status right now:\n" + self._each(targets, one)

    def get_fan_speed_stats(self, hours: float = 24, pi: str = "") -> str:
        """
        Get fan speed statistics over a past period: current, average, highest and
        lowest speed, each in RPM and as a percentage of the fan's maximum speed,
        plus average duty cycle and CPU temperature.
        Use this for questions about the fan over time, e.g. "what was the max fan speed today?"
        or "average fan speed in the last 6 hours".
        :param hours: Length of the period in hours, counting back from now. Default 24.
        :param pi: Name of the Raspberry Pi. Leave empty to get every configured Pi.
        """
        try:
            hours = float(hours)
            targets = self._select(pi)
        except (TypeError, ValueError) as e:
            return str(e)

        def one(name, url):
            st = self._call(url, "GET", f"/stats?hours={hours:g}")
            if not st.get("samples"):
                return f"No fan history recorded in the last {hours:g} hours."
            covered = (st.get("covered_seconds") or 0) / 3600
            lines = [
                f"- current: {_rpm(st, 'rpm_current')}",
                f"- average: {_rpm(st, 'rpm_avg')}",
                f"- highest: {_rpm(st, 'rpm_max')}",
                f"- lowest: {_rpm(st, 'rpm_min')}",
                f"- average duty cycle: {_fmt(st.get('duty_avg'), '.0f', ' %')}",
                f"- average CPU temperature: {_fmt(st.get('temp_avg'), '.1f', ' °C')} "
                f"(highest {_fmt(st.get('temp_max'), '.1f', ' °C')})",
            ]
            if covered < hours * 0.95:
                lines.append(f"Note: history only covers {covered:.1f} of the {hours:g} hours.")
            return "\n".join(lines)
        return ANSWER_RULE + f"Fan speed, last {hours:g} hours:\n" + self._each(targets, one)

    def set_fan_speed(self, percent: int, pi: str = "") -> str:
        """
        Set a Raspberry Pi's fan to a fixed speed, overriding automatic temperature control
        until set_fan_automatic is called. Only use when the user explicitly asks to change
        the fan speed.
        :param percent: Fan duty cycle from 0 (off or slowest) to 100 (full speed).
        :param pi: Name of the Raspberry Pi, or "all". Required when several Pis are configured.
        """
        try:
            percent = float(percent)
        except (TypeError, ValueError):
            return "percent must be a number from 0 to 100"
        if not 0 <= percent <= 100:
            return "percent must be between 0 and 100"
        try:
            targets = self._select(pi, allow_all=bool(pi))
        except ValueError as e:
            return str(e)

        def one(name, url):
            token = self._token(name)
            if not token:
                return (f"Changing the fan speed on '{name}' is disabled: no API token is "
                        "configured for it in the tool's valves.")
            r = self._call(url, "POST", "/set", {"percent": percent}, token=token)
            if r.get("mode") == "manual":
                return (f"Fan on '{name}' set to {percent:.0f} % (manual control). It stays "
                        "there until the fan is put back on automatic control.")
            return (f"Fan on '{name}' set to {percent:.0f} %. Note: the fan control service "
                    "on that Pi is not running, so there is no automatic control to return to.")
        return self._each(targets, one)

    def set_fan_automatic(self, pi: str = "") -> str:
        """
        Return a Raspberry Pi's fan to automatic control, where its speed follows the
        CPU temperature curve. Use when the user asks to undo a manual fan speed or
        to let the fan control itself again.
        :param pi: Name of the Raspberry Pi, or "all". Required when several Pis are configured.
        """
        try:
            targets = self._select(pi, allow_all=bool(pi))
        except ValueError as e:
            return str(e)

        def one(name, url):
            token = self._token(name)
            if not token:
                return (f"Changing the fan mode on '{name}' is disabled: no API token is "
                        "configured for it in the tool's valves.")
            r = self._call(url, "POST", "/auto", token=token)
            if r.get("mode") != "auto":
                return (f"The fan control service on '{name}' is not running, so automatic "
                        "control is unavailable until it is started.")
            if r.get("message", "").startswith("no manual override"):
                return f"No change: the fan on '{name}' was already on automatic control."
            return (f"Done: the fan on '{name}' was switched from manual back to automatic "
                    "control; its speed now follows the CPU temperature.")
        return self._each(targets, one)
