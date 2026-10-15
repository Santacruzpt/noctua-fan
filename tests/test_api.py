import importlib.util
import json
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import noctua_fan as nf  # noqa: E402
from test_noctua_fan import make_cfg  # noqa: E402


def load_tool():
    """Import the Open WebUI tool; stub pydantic when it isn't installed."""
    if "pydantic" not in sys.modules and importlib.util.find_spec("pydantic") is None:
        stub = types.ModuleType("pydantic")

        class BaseModel:
            def __init__(self, **kw):
                for k, v in type(self).__dict__.items():
                    if not k.startswith("_") and not callable(v):
                        setattr(self, k, kw.get(k, v))

        stub.BaseModel = BaseModel
        stub.Field = lambda default=None, **_: default
        sys.modules["pydantic"] = stub
    spec = importlib.util.spec_from_file_location("noctua_fan_tool",
                                                  ROOT / "openwebui" / "noctua_fan_tool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class APITestBase(unittest.TestCase):
    token = "s3cret-token"

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.cfg = make_cfg(self.tmp, api_bind="127.0.0.1", api_port=0,
                            api_token_file=str(self.tmp / "token"))
        if self.token:
            (self.tmp / "token").write_text(self.token + "\n")
        self.server = nf.make_server(self.cfg)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmpdir.cleanup()

    def call(self, method, path, body=None, token=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def pwm_duty(self):
        return int((self.tmp / "pwm" / "pwmchip0" / "pwm0" / "duty_cycle").read_text())

    def fake_daemon(self):
        """Pretend the daemon is running (state file with our own pid)."""
        rs = nf.RunState(self.cfg)
        rs.dir.mkdir(parents=True, exist_ok=True)
        rs.write_state(pid=nf.os.getpid(), time=time.time(), temp=52.0,
                       duty=34.0, rpm=1850.0, mode="auto")
        return rs

    def fill_history(self):
        h = nf.History(self.cfg)
        now = time.time()
        for ago, avg, mx, mn in ((20, 2000, 2500, 1500), (10, 3000, 3600, 2800), (1, 1000, 1200, 900)):
            h.db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?)",
                         (now - ago * 3600, 3600, avg, mx, mn, 40, 55))
        h.db.commit()
        h.close()


class ReadEndpointTests(APITestBase):
    def test_help(self):
        code, body = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertTrue(body["control_enabled"])
        self.assertIn("GET /stats?hours=24", body["endpoints"])

    def test_status_and_speed(self):
        self.fake_daemon()
        code, st = self.call("GET", "/status")
        self.assertEqual((code, st["rpm"], st["mode"]), (200, 1850.0, "auto"))
        self.assertEqual(self.call("GET", "/speed"), (200, {"rpm": 1850.0, "source": "daemon",
                                                         "rpm_pct": 37.0, "max_rpm": 5000}))
        self.assertEqual((st["rpm_pct"], st["max_rpm"]), (37.0, 5000))

    def test_status_without_daemon(self):
        code, st = self.call("GET", "/status")
        self.assertEqual((code, st["mode"], st["temp"]), (200, "daemon not running", 50.0))

    def test_stats(self):
        self.fill_history()
        code, st = self.call("GET", "/stats?hours=24")
        self.assertEqual(code, 200)
        self.assertAlmostEqual(st["rpm_avg"], 2000)
        self.assertEqual((st["rpm_max"], st["rpm_min"], st["samples"]), (3600, 900, 3))
        self.assertEqual((st["rpm_avg_pct"], st["rpm_max_pct"], st["rpm_min_pct"], st["max_rpm"]),
                         (40.0, 72.0, 18.0, 5000))
        self.assertIsNone(st["rpm_current_pct"])                  # no daemon, no tach
        code, st = self.call("GET", "/stats?hours=5")
        self.assertEqual((st["rpm_max"], st["samples"]), (1200, 1))

    def test_stats_errors(self):
        self.assertEqual(self.call("GET", "/stats")[0], 503)      # no history db yet
        self.fill_history()
        self.assertEqual(self.call("GET", "/stats?hours=-1")[0], 400)
        self.assertEqual(self.call("GET", "/stats?hours=abc")[0], 400)

    def test_unknown_path(self):
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("POST", "/nope", token=self.token)[0], 404)


class ControlEndpointTests(APITestBase):
    def test_auth_required(self):
        self.assertEqual(self.call("POST", "/set", {"percent": 50})[0], 401)
        self.assertEqual(self.call("POST", "/set", {"percent": 50}, token="wrong")[0], 401)
        self.assertEqual(self.call("POST", "/auto")[0], 401)
        self.assertEqual(self.pwm_duty(), 0)

    def test_set_without_daemon_writes_pwm(self):
        code, r = self.call("POST", "/set", {"percent": 60}, token=self.token)
        self.assertEqual((code, r["duty"]), (200, 60))
        self.assertEqual(self.pwm_duty(), 24000)

    def test_set_and_auto_with_daemon(self):
        rs = self.fake_daemon()
        code, r = self.call("POST", "/set", {"percent": 80}, token=self.token)
        self.assertEqual((code, r["mode"]), (200, "manual"))
        self.assertEqual(rs.read_override(), 80)
        self.assertEqual(self.pwm_duty(), 0)                      # daemon applies it, not us
        code, r = self.call("POST", "/auto", token=self.token)
        self.assertEqual((code, r["mode"]), (200, "auto"))
        self.assertIsNone(rs.read_override())

    def test_bad_input(self):
        for body in ({"percent": 150}, {"percent": "x"}, {}, {"pct": 5}):
            self.assertEqual(self.call("POST", "/set", body, token=self.token)[0], 400, body)


class ReadOnlyServerTests(APITestBase):
    token = ""  # no token file -> control disabled

    def test_control_disabled(self):
        self.assertFalse(self.call("GET", "/")[1]["control_enabled"])
        code, r = self.call("POST", "/set", {"percent": 50}, token="anything")
        self.assertEqual(code, 403)
        self.assertIn("disabled", r["error"])


class RpmPercentTests(unittest.TestCase):
    def test_pct_and_format(self):
        cfg = nf.Config()
        self.assertEqual(nf.rpm_pct(cfg, 2500), 50.0)
        self.assertEqual(nf.rpm_pct(cfg, 5300), 106.0)            # within ±10 % tolerance
        self.assertIsNone(nf.rpm_pct(cfg, None))
        self.assertEqual(nf.fmt_rpm(cfg, 1850), "1850 rpm (37 % of 5000 rpm max)")
        self.assertEqual(nf.fmt_rpm(cfg, None), "n/a")
        cfg.max_rpm = 4500
        self.assertEqual(nf.rpm_pct(cfg, 4500), 100.0)
        cfg.max_rpm = 0                                           # disables percentages
        self.assertEqual(nf.fmt_rpm(cfg, 1850), "1850 rpm")


class DaemonRunningTests(unittest.TestCase):
    def test_root_owned_daemon_counts_as_running(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = nf.Config(run_dir=d)
            rs = nf.RunState(cfg)
            rs.write_state(pid=1)
            with mock.patch.object(nf.os, "kill", side_effect=PermissionError):
                self.assertTrue(rs.daemon_running())
            with mock.patch.object(nf.os, "kill", side_effect=ProcessLookupError):
                self.assertFalse(rs.daemon_running())


class OpenWebUIToolTests(APITestBase):
    """One Pi configured."""

    def setUp(self):
        super().setUp()
        self.tool = load_tool().Tools()
        self.tool.valves.pis = f"pi1={self.url}"
        self.tool.valves.api_tokens = self.token

    def test_status(self):
        self.fake_daemon()
        out = self.tool.get_fan_status()
        self.assertTrue(out.startswith("IMPORTANT:"))
        self.assertIn("speed: 1850 rpm = 37% of max speed", out)
        self.assertIn("CPU temperature: 52.0 °C", out)
        self.assertIn("control mode: auto", out)
        self.assertNotIn("###", out)                        # single Pi: no per-Pi headings

    def test_stats(self):
        self.fill_history()
        out = self.tool.get_fan_speed_stats()
        self.assertIn("average: 2000 rpm = 40% of max speed", out)
        self.assertIn("highest: 3600 rpm = 72% of max speed", out)
        self.assertIn("lowest: 900 rpm = 18% of max speed", out)
        self.assertTrue(out.startswith("IMPORTANT:"))
        self.assertIn("last 24 hours", out)
        self.assertIn("1200 rpm", self.tool.get_fan_speed_stats(hours=5))

    def test_set_and_auto(self):
        rs = self.fake_daemon()
        self.assertIn("Fan on 'pi1' set to 45 %", self.tool.set_fan_speed(45))  # pi optional
        self.assertEqual(rs.read_override(), 45)
        self.assertIn("Done: the fan on 'pi1' was switched", self.tool.set_fan_automatic())
        self.assertIn("No change", self.tool.set_fan_automatic())
        self.assertIn("between 0 and 100", self.tool.set_fan_speed(120))

    def test_no_token_and_wrong_token(self):
        self.tool.valves.api_tokens = ""
        self.assertIn("disabled", self.tool.set_fan_speed(50))
        self.tool.valves.api_tokens = "wrong"
        self.assertIn("401", self.tool.set_fan_speed(50))

    def test_unreachable(self):
        self.tool.valves.pis = "pi1=127.0.0.1:1"
        self.assertIn("cannot reach the fan API", self.tool.get_fan_status())


class MultiPiToolTests(APITestBase):
    """Two Pis: 'office' (this test's server) and 'garage' (a second server)."""

    def setUp(self):
        super().setUp()
        self.tmpdir2 = tempfile.TemporaryDirectory()
        tmp2 = Path(self.tmpdir2.name)
        self.cfg2 = make_cfg(tmp2, api_bind="127.0.0.1", api_port=0,
                             api_token_file=str(tmp2 / "token"), max_rpm=4000)
        (tmp2 / "token").write_text("garage-token\n")
        self.server2 = nf.make_server(self.cfg2)
        threading.Thread(target=self.server2.serve_forever, daemon=True).start()
        port2 = self.server2.server_address[1]

        rs2 = nf.RunState(self.cfg2)
        rs2.dir.mkdir(parents=True)
        rs2.write_state(pid=nf.os.getpid(), time=time.time(), temp=61.0,
                        duty=60.0, rpm=3000.0, mode="auto")
        self.rs1 = self.fake_daemon()                        # office: 1850 rpm
        self.rs2 = rs2

        self.tool = load_tool().Tools()
        port1 = self.server.server_address[1]
        self.tool.valves.pis = f"office=127.0.0.1:{port1}, garage=http://127.0.0.1:{port2}"
        self.tool.valves.api_tokens = f"office={self.token}, garage=garage-token"

    def tearDown(self):
        self.server2.shutdown()
        self.server2.server_close()
        self.tmpdir2.cleanup()
        super().tearDown()

    def test_status_all_pis(self):
        out = self.tool.get_fan_status()
        self.assertIn("### Raspberry Pi 'office'", out)
        self.assertIn("### Raspberry Pi 'garage'", out)
        self.assertIn("1850 rpm = 37% of max speed", out)
        self.assertIn("3000 rpm = 75% of max speed", out)     # garage max_rpm = 4000
        self.assertLess(out.index("'office'"), out.index("'garage'"))

    def test_status_one_pi(self):
        for name in ("garage", "GARAGE", "the garage pi", "Garage Pi"):
            out = self.tool.get_fan_status(pi=name)
            self.assertIn("3000 rpm", out, name)
            self.assertNotIn("1850 rpm", out, name)

    def test_select_by_address(self):
        port2 = self.server2.server_address[1]
        self.assertIn("3000 rpm", self.tool.get_fan_status(pi=f"127.0.0.1:{port2}"))

    def test_unknown_pi(self):
        out = self.tool.get_fan_status(pi="kitchen")
        self.assertIn("Unknown Raspberry Pi 'kitchen'", out)
        self.assertIn("office, garage", out)

    def test_stats_all_pis_with_one_missing_history(self):
        self.fill_history()                                  # office only
        out = self.tool.get_fan_speed_stats(hours=24)
        self.assertIn("average: 2000 rpm", out)
        self.assertIn("no speed history yet", out)           # garage: 503 from its API

    def test_set_requires_pi_when_several(self):
        out = self.tool.set_fan_speed(50)
        self.assertIn("say which one", out)
        self.assertIsNone(self.rs1.read_override())
        self.assertIsNone(self.rs2.read_override())

    def test_set_one_and_all_with_per_pi_tokens(self):
        self.assertIn("'garage' set to 50 %", self.tool.set_fan_speed(50, pi="garage"))
        self.assertEqual((self.rs1.read_override(), self.rs2.read_override()), (None, 50))
        out = self.tool.set_fan_speed(80, pi="all")
        self.assertIn("'office' set to 80 %", out)
        self.assertIn("'garage' set to 80 %", out)
        self.assertEqual((self.rs1.read_override(), self.rs2.read_override()), (80, 80))
        self.tool.set_fan_automatic(pi="all")
        self.assertEqual((self.rs1.read_override(), self.rs2.read_override()), (None, None))

    def test_token_for_one_pi_only(self):
        self.tool.valves.api_tokens = "garage=garage-token"
        out = self.tool.set_fan_speed(30, pi="all")
        self.assertIn("'office' is disabled", out)
        self.assertIn("'garage' set to 30 %", out)


class ToolConfigParsingTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_tool()
        self.tool = self.mod.Tools()

    def test_to_url(self):
        u = self.mod._to_url
        self.assertEqual(u("192.168.1.20"), "http://192.168.1.20:9102")
        self.assertEqual(u("pi.local:8000"), "http://pi.local:8000")
        self.assertEqual(u("https://pi.example/"), "https://pi.example")
        self.assertEqual(u("fe80::1"), "http://[fe80::1]:9102")
        self.assertEqual(u("[fe80::1]:9000"), "http://[fe80::1]:9000")

    def test_pis_parsing(self):
        self.tool.valves.pis = "office = 192.168.1.20 ;\n garage=192.168.1.31:9200, 10.0.0.5"
        self.assertEqual(self.tool._pis(), {
            "office": "http://192.168.1.20:9102",
            "garage": "http://192.168.1.31:9200",
            "10.0.0.5": "http://10.0.0.5:9102",
        })

    def test_tokens(self):
        self.tool.valves.pis = "office=1.2.3.4, garage=1.2.3.5"
        self.tool.valves.api_tokens = "shared-tok"
        self.assertEqual(self.tool._token("garage"), "shared-tok")
        self.tool.valves.api_tokens = "office=tok1, garage=tok2"
        self.assertEqual((self.tool._token("office"), self.tool._token("Garage")), ("tok1", "tok2"))
        self.tool.valves.api_tokens = "default-tok, garage=tok2"
        self.assertEqual((self.tool._token("office"), self.tool._token("garage")),
                         ("default-tok", "tok2"))

    def test_no_pis(self):
        self.tool.valves.pis = ""
        self.assertIn("No Raspberry Pis are configured", self.tool.get_fan_status())


if __name__ == "__main__":
    unittest.main()
