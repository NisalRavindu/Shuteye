import unittest

from attention_monitor.config import MonitorConfig
from attention_monitor.detector import FaceObservation
from attention_monitor.session import StudySession, score_episodes
from attention_monitor.tracker import AttentionState as S, AttentionTracker, Episode

FPS = 10
DT = 1.0 / FPS

# Two study postures: screen (head level) and book (head tilted down, eyes down,
# eyelids naturally lower).
SCREEN = dict(yaw=5, pitch=-10, closure=0.10, look_down=0.05, ear=0.30)
BOOK = dict(yaw=5, pitch=-40, closure=0.45, look_down=0.60, ear=0.20)
CLOSED = dict(yaw=5, pitch=-10, closure=0.90, look_down=0.50, ear=0.08)


def face(yaw=0.0, pitch=0.0, gaze=0.5, closure=0.1, look_down=0.0, ear=0.3):
    return FaceObservation(True, yaw=yaw, pitch=pitch, gaze_h=gaze, eye_closure=closure,
                           look_down=look_down, ear=ear)


NO_FACE = FaceObservation(False)


class Harness:
    def __init__(self, steps=("screen", "book"), eyes=False, **cfg):
        self.cfg = MonitorConfig(smoothing=1.0, calibration_seconds=1.0, calibration_prep_s=1.0,
                                 calibration_min_samples=5, calibration_steps=steps,
                                 calibrate_eyes_closed=eyes, **cfg)
        self.tr = AttentionTracker(self.cfg)
        self.t = 0.0
        self.tr.start_calibration(0.0)

    def feed(self, obs, seconds):
        out = []
        for _ in range(int(round(seconds * FPS))):
            out.append(self.tr.update(obs, self.t))
            self.t += DT
        return out

    def calibrate(self, closed=CLOSED):
        ups = self.feed(face(**SCREEN), 2.5)
        if self.cfg.calibrate_eyes_closed:
            ups += self.feed(face(**closed), 2.5)
        if len(self.cfg.calibration_steps) > 1:
            ups += self.feed(face(**BOOK), 2.5)
        ups += self.feed(face(**SCREEN), 0.5)
        assert self.tr.zone is not None
        return ups


class CalibrationTests(unittest.TestCase):
    def test_waits_for_face(self):
        h = Harness(steps=("screen",))
        h.feed(NO_FACE, 3)
        self.assertIsNone(h.tr.zone)
        h.feed(face(), 2)
        self.assertIsNotNone(h.tr.zone)

    def test_two_steps_prompt_and_record_both_postures(self):
        h = Harness()
        ups = h.calibrate()
        reasons = {u.reason for u in ups if u.state is S.CALIBRATING}
        self.assertTrue(any("Step 1/2" in r for r in reasons))
        self.assertTrue(any("Step 2/2" in r and "book" in r for r in reasons))
        pitches = sorted(p.pitch for p in h.tr.zone.poses)
        self.assertEqual(pitches, [-40, -10])


class FocusTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(away_grace_s=4.0, eyes_closed_grace_s=3.0, no_face_grace_s=5.0,
                         repeat_alert_s=8.0, recover_s=0.8)
        self.h.calibrate()

    def test_screen_book_and_in_between_are_focused(self):
        for pose in (SCREEN, BOOK, dict(yaw=5, pitch=-25, closure=0.3, look_down=0.3)):
            ups = self.h.feed(face(**pose), 5)
            self.assertTrue(all(u.state is S.FOCUSED for u in ups), pose)

    def test_lowered_eyelids_while_writing_not_eyes_closed(self):
        ups = self.h.feed(face(yaw=5, pitch=-40, closure=0.62, look_down=0.65), 6)
        self.assertTrue(all(u.state is S.FOCUSED for u in ups))

    def test_really_closed_eyes_while_looking_down(self):
        ups = self.h.feed(face(yaw=5, pitch=-40, closure=0.95, look_down=0.6), 4)
        self.assertEqual(ups[-1].state, S.EYES_CLOSED)
        self.assertEqual(sum(1 for u in ups if u.alert), 1)

    def test_head_dropping_below_book_is_away(self):
        ups = self.h.feed(face(yaw=5, pitch=-70, closure=0.4, look_down=0.6), 1)
        self.assertEqual(ups[-1].state, S.LOOKING_AWAY)

    def test_looking_away_alerts_after_grace(self):
        ups = self.h.feed(face(yaw=50, pitch=-10), 5)
        alerts = [i for i, u in enumerate(ups) if u.alert]
        self.assertEqual(len(alerts), 1)
        self.assertAlmostEqual(alerts[0] * DT, 4.0, delta=0.15)
        self.assertIn("right", ups[-1].reason)

    def test_no_face_repeat_and_episode(self):
        ups = self.h.feed(NO_FACE, 22)
        self.assertEqual(sum(1 for u in ups if u.alert), 3)  # at 5s, 13s, 21s
        ups = self.h.feed(face(**SCREEN), 2)
        eps = [u.episode for u in ups if u.episode]
        self.assertEqual(len(eps), 1)
        self.assertAlmostEqual(eps[0].duration, 22, delta=0.2)
        self.assertEqual(eps[0].issue, S.NO_FACE)
        self.assertTrue(eps[0].alerted)

    def test_flicker_back_does_not_reset_timer(self):
        ups = []
        for _ in range(4):  # away 1.5s, back only 0.3s -> stays one episode
            ups += self.h.feed(face(yaw=50, pitch=-10), 1.5) + self.h.feed(face(**SCREEN), 0.3)
        self.assertTrue(any(u.alert and u.alert.issue is S.LOOKING_AWAY for u in ups))

    def test_blip_is_noise(self):
        ups = self.h.feed(face(yaw=50, pitch=-10), 0.5) + self.h.feed(face(**SCREEN), 2)
        self.assertFalse(any(u.episode for u in ups))

    def test_pause(self):
        self.h.tr.set_paused(True)
        ups = self.h.feed(NO_FACE, 30)
        self.assertTrue(all(u.state is S.PAUSED and not u.alert for u in ups))


class ClosedEyeCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(eyes=True, eyes_closed_grace_s=3.0)
        self.ups = self.h.calibrate()

    def test_still_two_positions_with_beeps(self):
        reasons = [u.reason for u in self.ups if u.state is S.CALIBRATING]
        self.assertTrue(any("Step 1/2" in r and "Close your eyes" in r for r in reasons))
        self.assertFalse(any("Step 3" in r for r in reasons))
        self.assertTrue(any("Step 2/2" in r and "book" in r for r in reasons))
        self.assertEqual(sum(1 for u in self.ups if u.calibration_cue), 2)  # close, open
        self.assertIsNotNone(self.h.tr.zone.closed)
        self.assertEqual(sorted(p.pitch for p in self.h.tr.zone.poses), [-40, -10])

    def test_closing_eyes_at_camera_is_caught(self):
        # Partly-closed blink score (0.68) plus eyes "looking down": the old logic
        # matched this to the book posture and missed it.
        ups = self.h.feed(face(yaw=5, pitch=-10, closure=0.68, look_down=0.7, ear=0.12), 4)
        self.assertEqual(ups[-1].state, S.EYES_CLOSED)
        self.assertTrue(any(u.alert and u.alert.issue is S.EYES_CLOSED for u in ups))

    def test_weak_blink_score_face(self):
        # For some faces the blink blendshape barely moves even with eyes shut.
        # Calibration learns that, and lid geometry carries the decision.
        h = Harness(eyes=True)
        h.calibrate(closed=dict(yaw=5, pitch=-10, closure=0.35, look_down=0.4, ear=0.08))
        self.assertIsNotNone(h.tr.zone.closed)
        ups = h.feed(face(yaw=5, pitch=-10, closure=0.33, look_down=0.4, ear=0.08), 2)
        self.assertEqual(ups[-1].state, S.EYES_CLOSED)
        ups = h.feed(face(yaw=5, pitch=-40, closure=0.50, look_down=0.7, ear=0.18), 5)
        self.assertTrue(all(u.state is S.FOCUSED for u in ups))

    def test_writing_with_lowered_lids_still_focused(self):
        for pose in (BOOK, dict(yaw=5, pitch=-40, closure=0.55, look_down=0.7, ear=0.18),
                     dict(yaw=5, pitch=-12, closure=0.40, look_down=0.6, ear=0.21)):
            ups = self.h.feed(face(**pose), 5)
            self.assertTrue(all(u.state is S.FOCUSED for u in ups), pose)

    def test_eyes_not_closed_during_step_falls_back(self):
        h = Harness(eyes=True)
        ups = h.calibrate(closed=SCREEN)
        self.assertIsNone(h.tr.zone.closed)
        self.assertTrue(any("default eye sensitivity" in u.reason for u in ups))
        ups = h.feed(face(yaw=5, pitch=-10, closure=0.95, ear=0.08), 2)
        self.assertEqual(ups[-1].state, S.EYES_CLOSED)

    def test_step_times_out_without_face(self):
        h = Harness(eyes=True)
        h.feed(face(**SCREEN), 2.5)
        h.feed(NO_FACE, 12)        # closed-eye step gives up
        h.feed(face(**BOOK), 3.5)
        self.assertIsNotNone(h.tr.zone)
        self.assertIsNone(h.tr.zone.closed)


class RestlessTests(unittest.TestCase):
    def test_repeated_short_glances_trigger_restless(self):
        h = Harness(away_grace_s=4.0, restless_count=4, restless_gap_s=30)
        h.calibrate()
        alerts = []
        for _ in range(4):  # 3s glances (under the 4s grace), 10s apart
            ups = h.feed(face(yaw=50, pitch=-10), 3) + h.feed(face(**SCREEN), 10)
            alerts += [u.alert for u in ups if u.alert]
        self.assertEqual([a.issue for a in alerts], [S.RESTLESS])

    def test_spread_out_glances_are_fine(self):
        h = Harness(away_grace_s=4.0, restless_count=4, restless_gap_s=30)
        h.calibrate()
        for _ in range(6):  # same glances, but 40s apart
            ups = h.feed(face(yaw=50, pitch=-10), 3) + h.feed(face(**SCREEN), 40)
            self.assertFalse(any(u.alert for u in ups))


class VolumeTests(unittest.TestCase):
    def test_volume_scales_amplitude(self):
        import array, io, wave
        from attention_monitor.alerts import render_wav
        def peak(vol):
            with wave.open(io.BytesIO(render_wav(((880, 100),), vol))) as w:
                return max(abs(x) for x in array.array("h", w.readframes(w.getnframes())))
        self.assertEqual(peak(0), 0)
        self.assertLess(peak(20), peak(60))
        self.assertLess(peak(60), peak(100))
        self.assertGreater(peak(100), 30000)


def ep(start, dur, alerted=False):
    return Episode(start, start + dur, S.LOOKING_AWAY, alerted)


class ScoringTests(unittest.TestCase):
    cfg = MonitorConfig(min_counted_s=10, restless_count=4, restless_gap_s=30)

    def test_isolated_short_breaks_ignored(self):
        s = score_episodes([ep(0, 3), ep(100, 5), ep(300, 8)], self.cfg)
        self.assertEqual((s.wasted_s, s.ignored, s.counted), (0, 3, 0))
        self.assertEqual(s.ignored_s, 16)

    def test_long_breaks_counted(self):
        s = score_episodes([ep(0, 3), ep(100, 45, alerted=True)], self.cfg)
        self.assertEqual((s.wasted_s, s.ignored, s.counted), (45, 1, 1))

    def test_burst_of_short_breaks_counted_in_full(self):
        burst = [ep(t, 3) for t in (0, 15, 30, 45, 60)]
        s = score_episodes(burst + [ep(500, 4)], self.cfg)
        self.assertEqual(s.wasted_s, 15)
        self.assertEqual(len(s.restless_spells), 1)
        self.assertEqual(s.ignored, 1)

    def test_session_focus_uses_counted_time(self):
        sess = StudySession(None, self.cfg)
        from attention_monitor.tracker import TrackerUpdate
        t = 0.0
        while t < 100:  # 100s tracked
            sess.record(TrackerUpdate(S.FOCUSED, ""), t)
            t += 1
        sess.record(TrackerUpdate(S.FOCUSED, "", episode=ep(10, 4)), t)
        self.assertEqual(sess.focus_ratio, 1.0)   # one 4s break: forgiven
        sess.record(TrackerUpdate(S.FOCUSED, "", episode=ep(40, 20)), t + 1)
        self.assertAlmostEqual(sess.focus_ratio, 0.8, delta=0.01)
        self.assertIn("Ignored      : 1 brief", sess.summary())


if __name__ == "__main__":
    unittest.main()
