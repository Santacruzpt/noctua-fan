import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import noctua_fan as nf  # noqa: E402


def make_cfg(tmp: Path, **kw) -> nf.Config:
    chip = tmp / "pwm" / "pwmchip0"
    (chip / "pwm0").mkdir(parents=True)
    (chip / "export").write_text("")
    for name, val in (("period", 0), ("duty_cycle", 0), ("enable", 0), ("polarity", "normal")):
        (chip / "pwm0" / name).write_text(f"{val}\n")
    (tmp / "temp").write_text("50000\n")
    cfg = nf.Config(pwm_root=str(tmp / "pwm"), run_dir=str(tmp / "run"),
                    thermal_zone=str(tmp / "temp"), tach_gpio=-1,
                    history_db=str(tmp / "hist" / "history.db"), **kw)
    cfg.validate()
    return cfg


class CurveTests(unittest.TestCase):
    curve = [[40, 0], [45, 20], [55, 40], [65, 70], [75, 100]]

    def test_interpolate(self):
        self.assertEqual(nf.interpolate(self.curve, 30), 0)
        self.assertEqual(nf.interpolate(self.curve, 45), 20)
        self.assertAlmostEqual(nf.interpolate(self.curve, 50), 30)
        self.assertAlmostEqual(nf.interpolate(self.curve, 70), 85)
        self.assertEqual(nf.interpolate(self.curve, 90), 100)

    def test_min_duty_and_stop(self):
        cfg = nf.Config(curve=[[40, 0], [50, 10], [60, 100]], min_duty=20)
        cfg.validate()
        c = nf.Controller(cfg)
        self.assertEqual(c.target(30), 0)
        self.assertEqual(nf.Controller(cfg).target(45), 20)  # 5% raised to min
        cfg.allow_stop = False
        self.assertEqual(nf.Controller(cfg).target(30), 20)

    def test_hysteresis(self):
        cfg = nf.Config(curve=self.curve, hysteresis=3)
        cfg.validate()
        c = nf.Controller(cfg)
        self.assertAlmostEqual(c.target(55), 40)
        self.assertAlmostEqual(c.target(53), 40)        # within hysteresis: hold
        self.assertAlmostEqual(c.target(50), 36)        # follows curve at 53
        self.assertAlmostEqual(c.target(60), 55)        # rises immediately

    def test_config_validation(self):
        cfg = nf.Config(curve=[[60, 150], [40, -5]])
        cfg.validate()
        self.assertEqual(cfg.curve, [[40.0, 0.0], [60.0, 100.0]])  # sorted + clamped

    def test_config_file_unknown_key(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("bogus = 1\n")
        try:
            with self.assertRaises(ValueError):
                nf.Config.load(f.name)
        finally:
            os.unlink(f.name)

    def test_example_config_loads(self):
        cfg = nf.Config.load(str(Path(__file__).resolve().parent.parent / "noctua-fan.toml"))
        self.assertEqual(cfg, nf.Config())  # example == defaults


class PWMTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.cfg = make_cfg(self.tmp)
        self.pwm_dir = self.tmp / "pwm" / "pwmchip0" / "pwm0"

    def tearDown(self):
        self.tmpdir.cleanup()

    def read(self, name):
        return (self.pwm_dir / name).read_text().strip()

    def test_open_and_duty(self):
        pwm = nf.HardwarePWM(self.cfg)
        pwm.open()
        self.assertEqual(self.read("period"), "40000")  # 25 kHz
        self.assertEqual(self.read("enable"), "1")
        pwm.set_duty(50)
        self.assertEqual(self.read("duty_cycle"), "20000")
        self.assertAlmostEqual(pwm.get_duty(), 50)
        pwm.set_duty(150)
        self.assertEqual(self.read("duty_cycle"), "40000")

    def test_missing_overlay(self):
        self.cfg.pwm_chip = 3
        with self.assertRaises(RuntimeError):
            nf.HardwarePWM(self.cfg).open()

    def test_cli_set_without_daemon(self):
        self.assertEqual(nf.cmd_set(self.cfg, 75), 0)
        self.assertEqual(self.read("duty_cycle"), "30000")

    def test_daemon_loop_override_and_exit(self):
        self.cfg.interval = 0.05
        self.cfg.kickstart_seconds = 0
        stop = threading.Event()
        t = threading.Thread(target=nf.cmd_daemon, args=(self.cfg, stop))
        try:
            t.start()
            rs = nf.RunState(self.cfg)
            for _ in range(100):
                if rs.state.exists():
                    break
                time.sleep(0.02)
            st = json.loads(rs.state.read_text())
            self.assertEqual(st["mode"], "auto")
            self.assertAlmostEqual(st["duty"], 30)       # 50 °C -> 30 %
            self.assertTrue(rs.daemon_running())

            nf.cmd_set(self.cfg, 90)                     # goes to override file
            time.sleep(0.3)
            self.assertEqual(self.read("duty_cycle"), "36000")
            self.assertEqual(json.loads(rs.state.read_text())["mode"], "manual")

            nf.cmd_auto(self.cfg)
            (self.tmp / "temp").write_text("70000\n")
            time.sleep(0.3)
            self.assertEqual(self.read("duty_cycle"), "34000")  # 85 %
        finally:
            stop.set()
            t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(self.read("duty_cycle"), "40000")  # exit_duty 100 %


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.cfg = make_cfg(self.tmp)

    def tearDown(self):
        self.tmpdir.cleanup()

    def fill(self, rows):
        """rows: (hours_ago, rpm_avg, rpm_max, rpm_min, duty, temp), 1 h each."""
        h = nf.History(self.cfg)
        now = time.time()
        for ago, avg, mx, mn, duty, temp in rows:
            h.db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?)",
                         (now - ago * 3600, 3600, avg, mx, mn, duty, temp))
        h.db.commit()
        h.close()

    def test_stats_24h_window(self):
        self.fill([(30, 4900, 5000, 4800, 100, 80),   # outside 24 h
                   (20, 2000, 2500, 1500, 40, 55),
                   (10, 3000, 3600, 2800, 60, 62),
                   (1, 1000, 1200, 0, 20, 46)])
        h = nf.History(self.cfg, readonly=True)
        st = h.stats(24)
        self.assertEqual(st["samples"], 3)
        self.assertAlmostEqual(st["rpm_avg"], 2000)
        self.assertEqual(st["rpm_max"], 3600)
        self.assertEqual(st["rpm_min"], 0)
        self.assertAlmostEqual(st["duty_avg"], 40)
        self.assertEqual(h.stats(48)["rpm_max"], 5000)
        self.assertEqual(h.stats(5)["samples"], 1)
        h.close()

    def test_time_weighted_average(self):
        h = nf.History(self.cfg)
        now = time.time()
        h.db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?)", (now - 10, 3000, 1000, 1000, 1000, 20, 40))
        h.db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?)", (now - 5, 1000, 5000, 5000, 5000, 100, 70))
        self.assertAlmostEqual(h.stats(24)["rpm_avg"], 2000)  # (1000*3 + 5000*1) / 4
        h.close()

    def test_aggregation_and_pruning(self):
        self.cfg.history_interval = 0
        self.fill([(24 * 8, 1, 1, 1, 1, 1)])               # older than history_days
        h = nf.History(self.cfg)
        h.add(1200, 20, 45)
        h.add(None, 30, 50)                                # no tach reading
        rows = h.db.execute("SELECT rpm_avg, duty_avg FROM samples ORDER BY ts").fetchall()
        self.assertEqual(rows, [(1200, 20), (None, 30)])
        h.close()

    def test_no_history_yet(self):
        with self.assertRaises(FileNotFoundError):
            nf.History(self.cfg, readonly=True)

    def test_stats_command_output(self):
        self.fill([(2, 2500, 3000, 2000, 50, 58)])
        out = self.run_cli("stats", "--hours", "24")
        self.assertIn("average : 2500 rpm", out)
        self.assertIn("maximum : 3000 rpm", out)
        self.assertIn("history shorter than the period", out)
        st = json.loads(self.run_cli("stats", "--json"))
        self.assertEqual(st["rpm_max"], 3000)
        self.assertEqual(st["period_hours"], 24)

    def run_cli(self, *args):
        import contextlib, io
        cfgfile = self.tmp / "c.toml"
        cfgfile.write_text("\n".join(f'{k} = {json.dumps(getattr(self.cfg, k))}' for k in
                                      ("pwm_root", "run_dir", "thermal_zone", "history_db"))
                           + "\ntach_gpio = -1\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            nf.main(["-c", str(cfgfile), *args])
        return buf.getvalue()


class HelpTests(unittest.TestCase):
    def capture(self, *args):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                rc = nf.main(list(args))
            except SystemExit as e:
                rc = e.code
        return rc, buf.getvalue()

    def test_no_args_shows_options(self):
        rc, out = self.capture()
        self.assertEqual(rc, 0)
        for cmd in ("speed", "stats", "status", "set", "auto", "help", "--config"):
            self.assertIn(cmd, out)

    def test_help_command(self):
        rc, out = self.capture("help", "stats")
        self.assertEqual(rc, 0)
        self.assertIn("--hours", out)


class TachTests(unittest.TestCase):
    def test_rpm_math(self):
        tach = nf.Tachometer(nf.Config())
        tach.last_time = time.monotonic() - 1.0
        tach.count = 100            # 100 pulses / 2 per rev in 1 s = 50 rps
        self.assertAlmostEqual(tach.read_rpm(), 3000, delta=10)


if __name__ == "__main__":
    unittest.main()
