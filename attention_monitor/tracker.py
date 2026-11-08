"""Turns per-frame observations into attention states and alert decisions.

Pure logic, no camera or OpenCV GUI, so it can be unit-tested with fake data.

Key ideas
---------
* **Study zone.** Calibration records one or more postures (e.g. "screen" and
  "book"). Any head pose inside the box spanned by those postures, plus a
  tolerance margin, counts as focused, so looking down to write is fine.
* **Episodes.** A distraction episode starts when you stop being focused and ends
  once you've been focused again for ``recover_s``. Alerts fire when an episode
  outlasts its grace period.
* **Restless pattern.** Several short episodes in quick succession (each gap
  <= ``restless_gap_s``) trigger a RESTLESS alert, so repeated quick glances
  can't slip under the grace periods.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from statistics import median
from typing import Optional

from .config import MonitorConfig
from .observation import FaceObservation


class AttentionState(str, Enum):
    CALIBRATING = "calibrating"
    PAUSED = "paused"
    FOCUSED = "focused"
    LOOKING_AWAY = "looking_away"
    EYES_CLOSED = "eyes_closed"
    NO_FACE = "no_face"
    RESTLESS = "restless"   # alert-only: pattern of repeated short distractions


ISSUE_STATES = (AttentionState.LOOKING_AWAY, AttentionState.EYES_CLOSED, AttentionState.NO_FACE)

STATE_LABELS = {
    AttentionState.FOCUSED: "FOCUSED",
    AttentionState.LOOKING_AWAY: "LOOKING AWAY",
    AttentionState.EYES_CLOSED: "EYES CLOSED",
    AttentionState.NO_FACE: "NOT AT CAMERA",
    AttentionState.RESTLESS: "RESTLESS",
    AttentionState.CALIBRATING: "CALIBRATING",
    AttentionState.PAUSED: "PAUSED",
}

CALIBRATION_PROMPTS = {
    "screen": "Look at your screen the way you normally study",
    "book": "Now look down at your book / paper as if you're writing",
    "eyes_closed": "Beep! Close your eyes now, open them at the next beep",
}
EYES_CLOSED_STEP = "eyes_closed"


@dataclass
class Pose:
    name: str
    yaw: float
    pitch: float
    gaze_h: float
    eye_closure: float
    look_down: float
    ear: float = 0.3


@dataclass
class StudyZone:
    poses: list[Pose]
    closed: Optional[Pose] = None   # your eyes-closed reference, if calibrated

    def _range(self, attr: str) -> tuple[float, float]:
        vals = [getattr(p, attr) for p in self.poses]
        return min(vals), max(vals)

    def outside(self, attr: str, value: float, angle: bool) -> float:
        """Signed distance outside the calibrated range (0 when inside)."""
        lo, hi = self._range(attr)
        if angle:
            centre = lo + _angle_diff(hi, lo) / 2
            half = abs(_angle_diff(hi, lo)) / 2
            d = _angle_diff(value, centre)
        else:
            centre, half = (lo + hi) / 2, (hi - lo) / 2
            d = value - centre
        if abs(d) <= half:
            return 0.0
        return d - half if d > 0 else d + half

    def nearest_by_pitch(self, pitch: float) -> Pose:
        # Head pitch only: eye-direction signals are unreliable once the eyes close.
        return min(self.poses, key=lambda p: abs(_angle_diff(pitch, p.pitch)))


@dataclass
class AlertEvent:
    issue: AttentionState
    duration_s: float      # how long the distraction has lasted so far
    reason: str
    repeat: bool           # False for the first alert of an episode


@dataclass
class Episode:
    start: float
    end: float
    issue: AttentionState  # the issue that took up most of the episode
    alerted: bool

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class TrackerUpdate:
    state: AttentionState             # current (per-frame) classification
    reason: str
    distracted_for: float = 0.0       # seconds since you stopped being focused (0 if focused)
    grace_s: float = 0.0              # grace period for the current issue
    alerting: bool = False            # True once the grace period has been exceeded
    alert: Optional[AlertEvent] = None
    episode: Optional[Episode] = None # a distraction episode that just ended
    calibration_progress: float = 1.0
    calibration_cue: bool = False     # play a short beep (e.g. "you can open your eyes")
    closedness: float = 0.0           # eyes-closed score; >= 1.0 means closed
    d_yaw: float = 0.0                # degrees outside the study zone
    d_pitch: float = 0.0
    d_gaze: float = 0.0


def _angle_diff(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


class AttentionTracker:
    def __init__(self, config: MonitorConfig):
        self.cfg = config
        self.zone: Optional[StudyZone] = None
        self.paused = False
        self._calib_start: Optional[float] = None
        self._calib_step = 0
        self._step_start = 0.0
        self._calib_samples: list[FaceObservation] = []
        self._calib_poses: list[Pose] = []
        self._closed_pose: Optional[Pose] = None
        self._ema: Optional[dict] = None
        self._last_now: Optional[float] = None
        # episode state
        self._off_since: Optional[float] = None
        self._focused_since: Optional[float] = None
        self._issue_time: dict = defaultdict(float)
        self._last_alert: Optional[float] = None
        self._alerted = False
        # restless-pattern state
        self._last_episode_end: Optional[float] = None
        self._cluster_short = 0

    # ------------------------------------------------------------------ control
    def start_calibration(self, now: float) -> None:
        self._calib_start = now
        self._calib_step = 0
        self._step_start = now
        self._calib_samples = []
        self._calib_poses = []
        self._closed_pose = None
        self._reset_episode()

    @property
    def calibrating(self) -> bool:
        return self._calib_start is not None

    @property
    def baseline(self) -> Optional[StudyZone]:  # kept for backwards compatibility
        return self.zone

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._reset_episode()

    def _reset_episode(self) -> None:
        self._off_since = None
        self._focused_since = None
        self._issue_time = defaultdict(float)
        self._last_alert = None
        self._alerted = False

    # ------------------------------------------------------------------ update
    def update(self, obs: FaceObservation, now: float) -> TrackerUpdate:
        dt = 0.0 if self._last_now is None else max(0.0, min(now - self._last_now, 1.0))
        self._last_now = now

        # A calibration you asked for always runs, even while tracking is paused
        # (study days pause during breaks / free time). It used to wait for the
        # pause to end, which never happened.
        if self.calibrating:
            return self._calibrate(obs, now)

        if self.paused:
            return TrackerUpdate(AttentionState.PAUSED, "Paused")

        if self.zone is None:
            self.start_calibration(now)
            return self._calibrate(obs, now)

        sm = self._smooth(obs)
        state, reason, d_yaw, d_pitch, d_gaze, closedness = self._classify(obs, sm)
        upd = TrackerUpdate(state, reason, d_yaw=d_yaw, d_pitch=d_pitch, d_gaze=d_gaze,
                            closedness=closedness)

        if state is AttentionState.FOCUSED:
            if self._off_since is not None:
                if self._focused_since is None:
                    self._focused_since = now
                if now - self._focused_since >= self.cfg.recover_s:
                    self._end_episode(upd)
                else:
                    # Brief glance back: still inside the distraction episode.
                    upd.distracted_for = now - self._off_since
                    upd.grace_s = self._grace(self._dominant_issue())
                    upd.alerting = self._alerted
            return upd

        # Not focused.
        self._focused_since = None
        if self._off_since is None:
            self._off_since = now
        self._issue_time[state] += dt
        elapsed = now - self._off_since
        grace = self._grace(state)
        upd.distracted_for, upd.grace_s = elapsed, grace

        if elapsed >= grace:
            due = self._last_alert is None or now - self._last_alert >= self.cfg.repeat_alert_s
            if due:
                upd.alert = AlertEvent(state, elapsed, reason, repeat=self._alerted)
                self._last_alert = now
                self._alerted = True
        upd.alerting = self._alerted
        return upd

    def _end_episode(self, upd: TrackerUpdate) -> None:
        start, end = self._off_since, self._focused_since
        ep = Episode(start, end, self._dominant_issue(), self._alerted)
        self._reset_episode()
        if ep.duration < self.cfg.min_episode_s:
            return  # detection noise / a blink
        upd.episode = ep

        # Restless pattern: count short (un-alerted) episodes separated by small gaps.
        if self._last_episode_end is None or start - self._last_episode_end > self.cfg.restless_gap_s:
            self._cluster_short = 0
        self._last_episode_end = end
        if not ep.alerted:
            self._cluster_short += 1
            n = self.cfg.restless_count
            if n > 0 and self._cluster_short % n == 0:
                upd.alert = AlertEvent(
                    AttentionState.RESTLESS, ep.duration,
                    f"{self._cluster_short} quick distractions in a row", repeat=self._cluster_short > n)

    def _dominant_issue(self) -> AttentionState:
        if not self._issue_time:
            return AttentionState.LOOKING_AWAY
        return max(self._issue_time.items(), key=lambda kv: kv[1])[0]

    # ----------------------------------------------------------------- helpers
    def _grace(self, issue: Optional[AttentionState]) -> float:
        return {
            AttentionState.LOOKING_AWAY: self.cfg.away_grace_s,
            AttentionState.EYES_CLOSED: self.cfg.eyes_closed_grace_s,
            AttentionState.NO_FACE: self.cfg.no_face_grace_s,
        }.get(issue, self.cfg.away_grace_s)

    def _steps(self) -> list[tuple[str, int]]:
        """(step name, posture number). The eyes-closed step happens right after the
        first posture, without moving, so it doesn't add a position."""
        postures = list(self.cfg.calibration_steps or ("screen",))
        steps = [(postures[0], 0)]
        if self.cfg.calibrate_eyes_closed:
            steps.append((EYES_CLOSED_STEP, 0))
        steps += [(p, i) for i, p in enumerate(postures[1:], start=1)]
        return steps

    def _step_info(self, steps) -> tuple[str, str, str]:
        name, pos = steps[self._calib_step]
        n_pos = len(self.cfg.calibration_steps or ("screen",))
        label = f"Step {pos + 1}/{n_pos}: " if n_pos > 1 else ""
        return name, label, CALIBRATION_PROMPTS.get(name, f"Look at your {name}")

    def _calibrate(self, obs: FaceObservation, now: float) -> TrackerUpdate:
        steps = self._steps()
        name, step_label, prompt = self._step_info(steps)
        if self._calib_step == 0:
            prep = 0.5
        elif name == EYES_CLOSED_STEP:
            prep = 1.0   # time to close the eyes after the beep
        else:
            prep = self.cfg.calibration_prep_s
        t = now - self._step_start

        if t >= prep and obs.face_present:
            self._calib_samples.append(obs)
        step_frac = 0.0 if t < prep else min(
            (t - prep) / self.cfg.calibration_seconds,
            len(self._calib_samples) / self.cfg.calibration_min_samples, 1.0)
        timed_out = (name == EYES_CLOSED_STEP
                     and t >= prep + self.cfg.eye_closed_step_timeout_s)

        cue = False
        if step_frac >= 1.0 or timed_out:
            pose = self._pose_from(name, self._calib_samples) if step_frac >= 1.0 else None
            if name == EYES_CLOSED_STEP:
                self._closed_pose = self._validated_closed(pose)
                cue = True  # "open your eyes"
            else:
                self._calib_poses.append(pose)
                nxt = self._calib_step + 1
                if nxt < len(steps) and steps[nxt][0] == EYES_CLOSED_STEP:
                    cue = True  # "close your eyes"
            self._calib_step += 1
            self._step_start = now
            self._calib_samples = []
            if self._calib_step >= len(steps):
                self.zone = StudyZone(self._calib_poses, closed=self._closed_pose)
                self._calib_start = None
                self._ema = None
                reason = "Calibrated"
                if self.cfg.calibrate_eyes_closed and self._closed_pose is None:
                    reason += " (closed eyes not detected - using default eye sensitivity)"
                return TrackerUpdate(AttentionState.CALIBRATING, reason,
                                     calibration_progress=1.0, calibration_cue=cue)
            step_frac = 0.0
            name, step_label, prompt = self._step_info(steps)

        if not obs.face_present and t >= prep:
            reason = step_label + "Face the camera to calibrate"
        else:
            reason = step_label + prompt
        progress = (self._calib_step + step_frac) / len(steps)
        return TrackerUpdate(AttentionState.CALIBRATING, reason, calibration_progress=progress,
                             calibration_cue=cue)

    @staticmethod
    def _pose_from(name: str, s: list[FaceObservation]) -> Pose:
        return Pose(
            name=name,
            yaw=median(o.yaw for o in s), pitch=median(o.pitch for o in s),
            gaze_h=median(o.gaze_h for o in s), eye_closure=median(o.eye_closure for o in s),
            look_down=median(o.look_down for o in s), ear=median(o.ear for o in s),
        )

    def _validated_closed(self, closed: Optional[Pose]) -> Optional[Pose]:
        """Keep the closed-eye reference only if it clearly differs from open eyes."""
        if closed is None or not self._calib_poses:
            return None
        ref = self._calib_poses[0]
        if (closed.eye_closure - ref.eye_closure >= 0.2) or (closed.ear <= 0.7 * ref.ear):
            return closed
        return None

    def _smooth(self, obs: FaceObservation) -> Optional[dict]:
        if not obs.face_present:
            self._ema = None
            return None
        cur = {"yaw": obs.yaw, "pitch": obs.pitch, "gaze_h": obs.gaze_h,
               "eye_closure": obs.eye_closure, "look_down": obs.look_down, "ear": obs.ear}
        if self._ema is None:
            self._ema = cur
        else:
            a = self.cfg.smoothing
            for k, v in cur.items():
                prev = self._ema[k]
                if k in ("yaw", "pitch"):
                    self._ema[k] = prev + a * _angle_diff(v, prev)
                else:
                    self._ema[k] = prev + a * (v - prev)
        return self._ema

    def _classify(self, obs: FaceObservation, sm: Optional[dict]):
        if sm is None:
            return AttentionState.NO_FACE, "No face in view", 0.0, 0.0, 0.0, 0.0

        z, c = self.zone, self.cfg
        d_yaw = z.outside("yaw", sm["yaw"], angle=True)
        d_pitch = z.outside("pitch", sm["pitch"], angle=True)
        d_gaze = z.outside("gaze_h", sm["gaze_h"], angle=False)
        closedness = self._closedness(sm)
        rest = (d_yaw, d_pitch, d_gaze, closedness)

        if closedness >= 1.0:
            return (AttentionState.EYES_CLOSED, "Eyes closed") + rest
        if abs(d_yaw) > c.yaw_tolerance_deg:
            side = "right" if d_yaw > 0 else "left"
            return (AttentionState.LOOKING_AWAY, f"Head turned {side}") + rest
        if abs(d_pitch) > c.pitch_tolerance_deg:
            return (AttentionState.LOOKING_AWAY, "Head tilted away") + rest
        if c.use_gaze and abs(d_gaze) > c.gaze_tolerance:
            side = "right" if d_gaze > 0 else "left"
            return (AttentionState.LOOKING_AWAY, f"Eyes looking {side}") + rest
        return (AttentionState.FOCUSED, "Focused") + rest

    def _closedness(self, sm: dict) -> float:
        """Eyes-closed score, scaled so that 1.0 is the threshold."""
        z, c = self.zone, self.cfg
        closed = z.closed
        if closed is not None:
            # Per posture: 0 = looks like your open eyes there, 1 = like your closed
            # eyes. Combine the blink blendshape with the geometric lid opening, and
            # use the most lenient posture so lowered lids while writing are fine.
            scores = []
            for p in z.poses:
                parts = []
                db = closed.eye_closure - p.eye_closure
                if db > 0.1:
                    parts.append((sm["eye_closure"] - p.eye_closure) / db)
                de = p.ear - closed.ear
                if de > 0.02:
                    parts.append((p.ear - sm["ear"]) / de)
                if parts:
                    # Clip so one signal reading "more open than usual" can't cancel the other.
                    scores.append(sum(min(max(x, 0.0), 1.5) for x in parts) / len(parts))
            if scores:
                return min(scores) / c.eye_closed_level
        # Fallback: blink score vs. the posture closest in head pitch.
        ref = z.nearest_by_pitch(sm["pitch"])
        thr = max(c.eye_closed_threshold, ref.eye_closure + c.eye_closed_margin)
        return max(0.0, sm["eye_closure"] - ref.eye_closure) / max(thr - ref.eye_closure, 1e-6)
