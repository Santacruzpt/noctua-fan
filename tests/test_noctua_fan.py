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
                    thermal_zone=str(tmp / "temp"), tach_gpio=-1, **kw)
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


class TachTests(unittest.TestCase):
    def test_rpm_math(self):
        tach = nf.Tachometer(nf.Config())
        tach.last_time = time.monotonic() - 1.0
        tach.count = 100            # 100 pulses / 2 per rev in 1 s = 50 rps
        self.assertAlmostEqual(tach.read_rpm(), 3000, delta=10)


if __name__ == "__main__":
    unittest.main()
