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
        self.assertEqual(self.call("GET", "/speed"), (200, {"rpm": 1850.0, "source": "daemon"}))

    def test_status_without_daemon(self):
        code, st = self.call("GET", "/status")
        self.assertEqual((code, st["mode"], st["temp"]), (200, "daemon not running", 50.0))

    def test_stats(self):
        self.fill_history()
        code, st = self.call("GET", "/stats?hours=24")
        self.assertEqual(code, 200)
        self.assertAlmostEqual(st["rpm_avg"], 2000)
        self.assertEqual((st["rpm_max"], st["rpm_min"], st["samples"]), (3600, 900, 3))
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
    def setUp(self):
        super().setUp()
        self.tool = load_tool().Tools()
        self.tool.valves.api_url = self.url
        self.tool.valves.api_token = self.token

    def test_status(self):
        self.fake_daemon()
        out = self.tool.get_fan_status()
        self.assertIn("speed: 1850 rpm", out)
        self.assertIn("CPU temperature: 52.0 °C", out)
        self.assertIn("control mode: auto", out)

    def test_stats(self):
        self.fill_history()
        out = self.tool.get_fan_speed_stats()
        self.assertIn("average: 2000 rpm", out)
        self.assertIn("maximum: 3600 rpm", out)
        self.assertIn("minimum: 900 rpm", out)
        self.assertIn("last 24 hours", out)
        self.assertIn("1200 rpm", self.tool.get_fan_speed_stats(hours=5))

    def test_set_and_auto(self):
        rs = self.fake_daemon()
        self.assertIn("Fan set to 45 %", self.tool.set_fan_speed(45))
        self.assertEqual(rs.read_override(), 45)
        self.assertIn("cleared", self.tool.set_fan_automatic())
        self.assertIn("between 0 and 100", self.tool.set_fan_speed(120))

    def test_no_token_and_wrong_token(self):
        self.tool.valves.api_token = ""
        self.assertIn("disabled", self.tool.set_fan_speed(50))
        self.tool.valves.api_token = "wrong"
        self.assertIn("401", self.tool.set_fan_speed(50))

    def test_unreachable(self):
        self.tool.valves.api_url = "http://127.0.0.1:1"
        self.assertIn("cannot reach the fan API", self.tool.get_fan_status())


if __name__ == "__main__":
    unittest.main()
