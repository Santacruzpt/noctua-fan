"""
title: Noctua Fan
description: Read and control the Raspberry Pi's Noctua fan: current speed, temperature, 24-hour (or any period) speed statistics, and setting the fan speed.
author: Santacruzpt
repository: https://github.com/Santacruzpt/noctua-fan
version: 1.0
license: MIT
"""

import json
import urllib.error
import urllib.request

from pydantic import BaseModel, Field


def _fmt(value, fmt, unit=""):
    return "unknown" if value is None else f"{format(value, fmt)}{unit}"


def _rpm(st, key):
    """'1850 rpm = 37% of max speed' from an API response's <key> and <key>_pct fields."""
    rpm, pct = st.get(key), st.get(f"{key}_pct")
    if rpm is None:
        return "unknown"
    return f"{rpm:.0f} rpm" + ("" if pct is None else f" = {pct:.0f}% of max speed")


def _answer_rule(st):
    """Leading instruction: small models drop percentages unless told up front."""
    if not st.get("max_rpm"):
        return ""
    return (f"IMPORTANT: in your answer, state every fan speed as BOTH the rpm value AND "
            f"its percentage of the fan's {st['max_rpm']} rpm maximum, "
            f"e.g. '2450 rpm (49% of max)'.\n")


class Tools:
    class Valves(BaseModel):
        api_url: str = Field(
            default="http://raspberrypi.local:9102",
            description="Base URL of `noctua-fan serve` on the Raspberry Pi",
        )
        api_token: str = Field(
            default="",
            description="Bearer token from /etc/noctua-fan/api-token on the Pi. "
            "Needed only to change the fan speed; leave empty for read-only.",
        )
        timeout: float = Field(default=10.0, description="HTTP timeout in seconds")

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------------------ #

    def _call(self, method, path, body=None, auth=False):
        url = self.valves.api_url.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", f"Bearer {self.valves.api_token}")
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

    # ------------------------------------------------------------------ #

    def get_fan_status(self) -> str:
        """
        Get the Raspberry Pi fan's current state: fan speed in RPM and as a percentage
        of the fan's maximum speed, CPU temperature,
        PWM duty cycle and whether it is under automatic (temperature) or manual control.
        Use this for questions like "how fast is the fan spinning?" or "how hot is the Pi?".
        """
        try:
            st = self._call("GET", "/status")
        except RuntimeError as e:
            return str(e)
        return (
            _answer_rule(st)
            + "Raspberry Pi fan right now:\n"
            f"- speed: {_rpm(st, 'rpm')}\n"
            f"- CPU temperature: {_fmt(st.get('temp'), '.1f', ' °C')}\n"
            f"- duty cycle: {_fmt(st.get('duty'), '.0f', ' %')}\n"
            f"- control mode: {st.get('mode', 'unknown')}"
        )

    def get_fan_speed_stats(self, hours: float = 24) -> str:
        """
        Get fan speed statistics over a past period: current, average, highest and
        lowest speed, each in RPM and as a percentage of the fan's maximum speed,
        plus average duty cycle and CPU temperature.
        Use this for questions about the fan over time, e.g. "what was the max fan speed today?"
        or "average fan speed in the last 6 hours".
        :param hours: Length of the period in hours, counting back from now. Default 24.
        """
        try:
            st = self._call("GET", f"/stats?hours={float(hours):g}")
        except (RuntimeError, ValueError) as e:
            return str(e)
        if not st.get("samples"):
            return f"No fan history recorded in the last {hours:g} hours."
        covered = (st.get("covered_seconds") or 0) / 3600
        lines = [
            _answer_rule(st) + f"Raspberry Pi fan speed, last {hours:g} hours:",
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

    def set_fan_speed(self, percent: int) -> str:
        """
        Set the Raspberry Pi fan to a fixed speed, overriding automatic temperature control
        until set_fan_automatic is called. Only use when the user explicitly asks to change
        the fan speed.
        :param percent: Fan duty cycle from 0 (off or slowest) to 100 (full speed).
        """
        if not self.valves.api_token:
            return "Changing the fan speed is disabled: no api_token is configured in the tool's valves."
        try:
            percent = float(percent)
        except (TypeError, ValueError):
            return "percent must be a number from 0 to 100"
        if not 0 <= percent <= 100:
            return "percent must be between 0 and 100"
        try:
            r = self._call("POST", "/set", {"percent": percent}, auth=True)
        except RuntimeError as e:
            return str(e)
        if r.get("mode") == "manual":
            return (f"Fan set to {percent:.0f} % (manual control). It stays there until "
                    "the fan is put back on automatic control.")
        return (f"Fan set to {percent:.0f} %. Note: the fan control service on the Pi is "
                "not running, so there is no automatic control to return to.")

    def set_fan_automatic(self) -> str:
        """
        Return the Raspberry Pi fan to automatic control, where its speed follows the
        CPU temperature curve. Use when the user asks to undo a manual fan speed or
        to let the fan control itself again.
        """
        if not self.valves.api_token:
            return "Changing the fan mode is disabled: no api_token is configured in the tool's valves."
        try:
            r = self._call("POST", "/auto", auth=True)
        except RuntimeError as e:
            return str(e)
        return r.get("message", "Fan returned to automatic control.")
