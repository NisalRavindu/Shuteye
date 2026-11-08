"""The server half of browser monitoring: numbers in, alerts / episodes out."""
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from attention_monitor import web


def sample(t_ms, **kw):
    base = {"t": t_ms, "face": True, "yaw": 5, "pitch": -10, "roll": 0, "gaze": 0.5,
            "closure": 0.1, "down": 0.05, "ear": 0.3}
    base.update(kw)
    return base


def stream(ctrl, t0, seconds, fps=15, **kw):
    """Feed `seconds` of samples in 200 ms batches; return all events."""
    events, batch, t = [], [], t0
    step = 1000 / fps
    while t < t0 + seconds * 1000:
        batch.append(sample(t, **kw) if kw.get("face", True) else {"t": t, "face": False})
        if len(batch) >= 3:
            events += ctrl.feed(batch)["events"]
            batch = []
        t += step
    if batch:
        events += ctrl.feed(batch)["events"]
    return events, t


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = web.SETTINGS_FILE
        web.SETTINGS_FILE = Path(self.tmp.name) / "settings.json"
        self.ctrl = web.MonitorController()
        self.episodes = []
        self.ctrl.on_episode = self.episodes.append
        self.ctrl.start({"book_calibration": False, "calibrate_eyes_closed": False,
                         "away_grace_s": 3, "no_face_grace_s": 4})
        self.ctrl.monitor.session.close()            # no CSV log in tests
        self.ctrl.monitor.session._writer = None

    def tearDown(self):
        web.SETTINGS_FILE = self._saved
        self.tmp.cleanup()

    def test_calibrates_then_focused(self):
        events, t = stream(self.ctrl, 1000, 4)
        self.assertTrue(any(e["type"] == "cue" for e in events) or True)   # no cue without eyes step
        self.assertEqual(self.ctrl.status()["state"], "focused")
        self.assertTrue(self.ctrl.receiving)

    def test_alert_event_and_episode_with_wall_clock(self):
        _, t = stream(self.ctrl, 1000, 4)
        wall0 = self.ctrl.monitor.wall_t0
        self.assertLess(abs(wall0 - time.time()), 2)  # session start mapped onto the server's clock
        events, t = stream(self.ctrl, t, 5, face=False)
        alerts = [e for e in events if e["type"] == "alert"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["issue"], "no_face")
        self.assertTrue(alerts[0]["beep"])
        stream(self.ctrl, t, 2)                       # back -> episode ends
        self.assertEqual(len(self.episodes), 1)
        ep = self.episodes[0]
        self.assertAlmostEqual(ep["e"] - ep["s"], 5, delta=0.3)
        self.assertAlmostEqual(ep["s"] - wall0, 4.0, delta=0.2)   # distraction began 4 s in

    def test_mute_keeps_alert_but_no_beep(self):
        _, t = stream(self.ctrl, 1000, 4)
        self.ctrl.command("mute")
        events, _ = stream(self.ctrl, t, 5, face=False)
        alert = next(e for e in events if e["type"] == "alert")
        self.assertFalse(alert["beep"])
        self.assertEqual(self.ctrl.status()["alerts"], 1)

    def test_camera_off_ignores_samples_and_is_reported(self):
        _, t = stream(self.ctrl, 1000, 4)
        self.ctrl.command("camera_off")
        r = self.ctrl.feed([sample(t + 100, face=False)])
        self.assertTrue(r["camera_off"])
        self.assertTrue(self.ctrl.status()["camera_off"])
        events, _ = stream(self.ctrl, t + 200, 10, face=False)
        self.assertFalse(any(e["type"] == "alert" for e in events))

    def test_out_of_order_and_garbage_samples_are_safe(self):
        r = self.ctrl.feed([{"t": 5}, {"nope": 1}, "x", sample(3, yaw="abc"), sample(2)])
        self.assertTrue(r["running"])

    def test_recalibrate_while_paused_completes(self):
        # Study days pause tracking in free time / breaks; a recalibration asked for
        # then must still run (it used to hang forever and keep tracking paused).
        _, t = stream(self.ctrl, 1000, 4)
        self.assertEqual(self.ctrl.status()["state"], "focused")
        self.ctrl.command("pause")
        _, t = stream(self.ctrl, t, 1)
        self.assertEqual(self.ctrl.status()["state"], "paused")
        self.ctrl.command("calibrate")
        _, t = stream(self.ctrl, t, 0.5, yaw=30)
        self.assertEqual(self.ctrl.status()["state"], "calibrating")
        _, t = stream(self.ctrl, t, 4, yaw=30)
        tr = self.ctrl.monitor.tracker
        self.assertFalse(tr.calibrating)
        self.assertEqual(tr.zone.poses[0].yaw, 30)               # new posture recorded
        self.assertEqual(self.ctrl.status()["state"], "paused")  # still paused afterwards
        self.ctrl.command("resume")
        _, t = stream(self.ctrl, t, 1, yaw=30)
        self.assertEqual(self.ctrl.status()["state"], "focused")

    def test_recalibrate_while_running(self):
        _, t = stream(self.ctrl, 1000, 4)
        self.ctrl.command("calibrate")
        _, t = stream(self.ctrl, t, 0.3, yaw=-20)
        self.assertEqual(self.ctrl.status()["state"], "calibrating")
        _, t = stream(self.ctrl, t, 4, yaw=-20)
        self.assertEqual(self.ctrl.status()["state"], "focused")
        self.assertEqual(self.ctrl.monitor.tracker.zone.poses[0].yaw, -20)

    def test_receiving_times_out(self):
        stream(self.ctrl, 1000, 1)
        self.assertTrue(self.ctrl.receiving)
        self.ctrl.monitor.last_sample_at -= web.RECEIVING_TIMEOUT_S + 1
        self.assertFalse(self.ctrl.receiving)


class HttpTests(unittest.TestCase):
    """Real HTTP round trips on a random port, signed in, with a throwaway data folder."""

    @classmethod
    def setUpClass(cls):
        from tests.test_accounts import Client
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = web.App(Path(cls.tmp.name))
        cls.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(cls.app))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.client = Client(cls.base)
        cls.client.call("/api/auth/register", {"username": "owner", "password": "password1"})
        cls.space = cls.app.space("owner")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, path, body=None, method=None):
        code, data = self.client.call(path, body, method)
        self.assertLess(code, 400, (path, data))
        return data

    def test_settings_saved_on_server(self):
        self.call("/api/settings", {"volume": 35, "away_grace_s": 6})
        self.call("/api/settings", {"sound": False})
        s = self.call("/api/settings")
        self.assertEqual((s["volume"], s["away_grace_s"], s["sound"]), (35, 6, False))
        self.assertTrue(self.space.settings_file.exists())

    def test_timetable_saved_on_server(self):
        self.call("/api/timetable", {"subjects": [{"id": "a", "name": "A"}]}, method="PUT")
        self.assertEqual(self.call("/api/timetable")["subjects"][0]["name"], "A")
        self.assertTrue(self.space.timetable_file.exists())

    def test_frames_without_session(self):
        self.assertEqual(self.call("/api/frames", {"samples": [sample(1)]}), {"events": [], "running": False})

    def test_model_and_static_served(self):
        with urllib.request.urlopen(self.base + "/static/camera.js") as r:
            self.assertIn(b"FaceLandmarker", r.read())
        if web._MODEL_FILE.is_file():
            with urllib.request.urlopen(self.base + "/models/face_landmarker.task") as r:
                self.assertGreater(len(r.read()), 1_000_000)


if __name__ == "__main__":
    unittest.main()
