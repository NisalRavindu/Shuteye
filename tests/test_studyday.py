import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from attention_monitor import studyday as sd

D = dt.date(2026, 11, 9)          # a Monday
H = 3600


def at(hh, mm=0, day=D):
    return dt.datetime.combine(day, dt.time(hh, mm)).timestamp()


def tt(*blocks, subjects=("m", "p", "c")):
    """blocks: (weekday, 'HH:MM', 'HH:MM', subject)"""
    def mins(s):
        h, m = map(int, s.split(":"))
        return h * 60 + m
    return {"subjects": [{"id": s, "name": s.upper(), "color": "#4e79a7"} for s in subjects],
            "entries": [{"day": d, "start": mins(a), "end": mins(b), "subject": s} for d, a, b, s in blocks]}


class PlanTests(unittest.TestCase):
    def test_break_carved_from_end_of_first_subject_only_between_hugging_blocks(self):
        plan, _ = sd.build_plan(tt((0, "09:00", "10:00", "m"), (0, "10:00", "11:00", "p"),
                                   (0, "12:00", "13:00", "c")), D, 4, 5)
        got = [(p["kind"], p["subject"], dt.datetime.fromtimestamp(p["s"]).strftime("%H:%M"),
                dt.datetime.fromtimestamp(p["e"]).strftime("%H:%M")) for p in plan]
        self.assertEqual(got, [("study", "m", "09:00", "09:55"), ("break", "p", "09:55", "10:00"),
                               ("study", "p", "10:00", "11:00"),            # gap after: no break
                               ("study", "c", "12:00", "13:00")])          # last block: no break

    def test_zero_break(self):
        plan, _ = sd.build_plan(tt((0, "09:00", "10:00", "m"), (0, "10:00", "11:00", "p")), D, 4, 0)
        self.assertEqual([p["kind"] for p in plan], ["study", "study"])

    def test_after_midnight_belongs_to_previous_study_day(self):
        t = tt((0, "23:00", "24:00", "m"), (1, "01:00", "02:00", "p"), (0, "02:00", "03:00", "c"))
        plan, _ = sd.build_plan(t, D, 4, 0)
        self.assertEqual([p["subject"] for p in plan], ["m", "p"])   # Tue 01:00 is Monday's study day
        self.assertEqual(plan[1]["s"], at(1, 0, D + dt.timedelta(days=1)))
        self.assertEqual(sd.study_date_of(at(2, 30, D + dt.timedelta(days=1)), 4), D)
        self.assertEqual(sd.study_date_of(at(4, 30, D + dt.timedelta(days=1)), 4), D + dt.timedelta(days=1))

    def test_free_windows(self):
        plan, _ = sd.build_plan(tt((0, "09:00", "10:00", "m"), (0, "12:00", "13:00", "p")), D, 4, 0)
        w = sd.free_windows(plan, at(9, 30), at(14))
        self.assertEqual(w, [(at(10), at(12)), (at(13), at(14))])


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.subjects = sd.build_plan(
            tt((0, "09:00", "10:00", "m"), (0, "10:00", "11:00", "p")), D, 4, 5)
        lo, hi = sd.day_window(D, 4)
        self.rec = {"date": D.isoformat(), "started_at": at(9), "ended_at": at(11), "window_end": hi,
                    "settings": {"min_counted_s": 10, "restless_count": 4, "restless_gap_s": 30},
                    "intervals": [{"s": at(9), "e": at(9, 40), "kind": "study", "subject": "m"},
                                  {"s": at(9, 40), "e": at(9, 55), "kind": "manual_break", "subject": "m"},
                                  {"s": at(10, 30), "e": at(11), "kind": "study", "subject": "p"}],
                    "episodes": [{"s": at(9, 10), "e": at(9, 15), "issue": "phone", "alerted": True},   # 5 min: wasted (legacy issue type still loads)
                                 {"s": at(9, 20), "e": at(9, 20) + 4, "issue": "looking_away", "alerted": False},  # forgiven
                                 {"s": at(10, 40), "e": at(10, 42), "issue": "no_face", "alerted": True}]}

    def test_attribution(self):
        a = sd.analyze_day(self.rec, self.plan, self.subjects, now=at(20))
        m = next(r for r in a["subjects"] if r["id"] == "m")
        p = next(r for r in a["subjects"] if r["id"] == "p")
        self.assertEqual(m["planned"], 55 * 60)
        self.assertEqual(m["wasted"], 5 * 60)                 # short 4 s glance forgiven
        self.assertEqual(m["studied"], 35 * 60)
        self.assertEqual(m["breaks"], 15 * 60)
        self.assertEqual(m["missed"], 0)
        self.assertEqual(p["missed"], 30 * 60)                # started Physics late
        self.assertEqual(p["wasted"], 2 * 60)
        self.assertEqual(p["short"], 60 * 60 - 28 * 60)

    def test_suggestions_fill_free_time(self):
        rec = dict(self.rec, ended_at=None)
        a = sd.analyze_day(rec, self.plan, self.subjects, now=at(11, 0))
        sug = sd.suggestions(a, self.plan, at(11), rec["window_end"])
        self.assertEqual(sug[0]["subject"], "p")              # largest shortfall first
        self.assertEqual(sug[0]["s"], at(11))
        self.assertEqual({s["subject"] for s in sug}, {"m", "p"})


class FakeTracker:
    paused = False
    calibrating = False


class FakeMonitor:
    def __init__(self):
        self.tracker = FakeTracker()
        self.commands = []

    def command(self, name):
        self.commands.append(name)
        if name in ("pause", "camera_off"):
            self.tracker.paused = True
        if name in ("resume", "camera_on"):
            self.tracker.paused = False


class FakeCtrl:
    def __init__(self):
        self.running, self.frame, self.monitor = False, None, None
        self.on_episode = None
        self.last_update = None
        self.day_mode = False

    @property
    def receiving(self):
        return self.running

    def start(self, settings):
        if not self.running:
            self.running, self.frame, self.monitor = True, b"jpg", FakeMonitor()

    def stop(self):
        self.running = False


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = (sd.DAYS_DIR, sd.TIMETABLE_FILE, sd.SETTINGS_FILE)
        sd.DAYS_DIR, sd.TIMETABLE_FILE, sd.SETTINGS_FILE = root / "days", root / "tt.json", root / "s.json"
        sd.TIMETABLE_FILE.write_text(json.dumps(tt((0, "09:00", "10:00", "m"), (0, "10:00", "11:00", "p"))))
        self.now = at(8, 50)
        self.ctrl = FakeCtrl()
        self.m = sd.DayManager(self.ctrl, clock=lambda: self.now, start_thread=False)

    def tearDown(self):
        sd.DAYS_DIR, sd.TIMETABLE_FILE, sd.SETTINGS_FILE = self._saved
        self.tmp.cleanup()

    def run_until(self, t, step=1):
        while self.now < t:
            self.now += step
            self.m.tick()

    def test_full_day(self):
        st = self.m.status()
        self.assertFalse(st["active"])
        self.assertEqual(st["today"]["planned"], 115 * 60)

        self.m.start_day({})
        self.run_until(at(8, 59))
        self.assertTrue(self.ctrl.monitor.tracker.paused)          # free time before 09:00: not watched
        self.run_until(at(9, 1))
        self.assertFalse(self.ctrl.monitor.tracker.paused)         # Maths started: watching

        st = self.m.status()
        self.assertEqual(st["current"]["name"], "M")
        self.assertEqual(st["next"]["name"], "P")
        self.assertAlmostEqual(st["current"]["remaining"], 54 * 60, delta=2)

        # a 2-minute distraction reported by the monitor
        self.ctrl.on_episode({"s": at(9, 10), "e": at(9, 12), "issue": "looking_away", "alerted": True})
        # own break 09:30-09:45: camera off
        self.run_until(at(9, 30)); self.m.set_break(True)
        self.assertIn("camera_off", self.ctrl.monitor.commands)
        self.run_until(at(9, 45)); self.m.set_break(False)
        # automatic break 09:55-10:00 pauses the tracker
        self.run_until(at(9, 57))
        self.assertTrue(self.ctrl.monitor.tracker.paused)
        self.assertEqual(self.m.status()["current"]["kind"], "break")
        self.run_until(at(10, 30))

        st = self.m.status()
        m = next(r for r in st["subjects"] if r["id"] == "m")
        self.assertAlmostEqual(m["wasted"], 120, delta=2)
        self.assertAlmostEqual(m["breaks"], 15 * 60, delta=2)
        self.assertAlmostEqual(m["studied"], 55 * 60 - 15 * 60 - 120, delta=3)

        date = self.m.end_day()
        self.assertEqual(date, D.isoformat())
        a = self.m.day_analysis(date)
        self.assertEqual(a["status"], "concluded")
        p = next(r for r in a["subjects"] if r["id"] == "p")
        self.assertAlmostEqual(p["missed"], 30 * 60, delta=3)      # ended early: rest of Physics missed
        self.assertFalse(self.ctrl.running)

    def test_catchup_in_free_time(self):
        self.now = at(10, 59)
        self.m.start_day({})
        self.run_until(at(11, 5))                                  # free time, everything so far was missed
        st = self.m.status()
        self.assertEqual(st["current"]["kind"], "free")
        self.assertTrue(st["suggestions"])
        self.m.set_catchup("m")
        self.run_until(at(11, 20))
        self.assertFalse(self.ctrl.monitor.tracker.paused)         # catching up is monitored
        m = next(r for r in self.m.status()["subjects"] if r["id"] == "m")
        self.assertAlmostEqual(m["catchup"], 15 * 60, delta=3)

    def test_forgot_to_end_day_auto_concludes_at_limit(self):
        self.m.start_day({})
        self.run_until(at(4, 0, D + dt.timedelta(days=1)) + 4, step=4)
        self.assertIsNone(self.m.active)
        self.assertEqual(self.m.last_concluded["reason"], "limit")
        self.assertFalse(self.ctrl.running)

    def test_restart_recovers_or_concludes(self):
        self.m.start_day({})
        self.run_until(at(9, 10))
        # server restarts during the day -> day still active, monitoring off
        m2 = sd.DayManager(FakeCtrl(), clock=lambda: self.now, start_thread=False)
        self.assertIsNotNone(m2.active)
        # server restarts the next afternoon -> yesterday concluded automatically
        self.now = at(15, 0, D + dt.timedelta(days=1))
        m3 = sd.DayManager(FakeCtrl(), clock=lambda: self.now, start_thread=False)
        self.assertIsNone(m3.active)
        a = m3.day_analysis(D.isoformat())
        self.assertEqual(a["status"], "concluded")
        self.assertEqual(a["end_reason"], "limit")

    def test_day_never_started_counts_as_missed(self):
        self.now = at(12, 0, D + dt.timedelta(days=7))
        a = self.m.day_analysis(D.isoformat())
        self.assertEqual(a["status"], "missed")
        self.assertEqual(a["totals"]["missed"], 115 * 60)
        w = self.m.week_analysis(D.isoformat())
        self.assertEqual(len(w["days"]), 7)
        self.assertEqual(w["days"][0]["status"], "missed")


if __name__ == "__main__":
    unittest.main()
