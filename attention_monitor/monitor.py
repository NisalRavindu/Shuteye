"""Main loop: camera -> analyzer -> tracker -> alerts / overlay / session log."""
from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Callable, Optional

import cv2
import numpy as np

from .alerts import AlertPlayer
from .config import MonitorConfig
from .detector import FaceAnalyzer, FaceObservation
from .session import StudySession
from .tracker import AlertEvent, AttentionState, AttentionTracker, TrackerUpdate

WINDOW = "Attention Monitor"
_KEYS = {ord("c"): "calibrate", ord("p"): "toggle_pause", ord("m"): "toggle_mute"}

# BGR colours
_COLORS = {
    AttentionState.FOCUSED: (80, 190, 80),
    AttentionState.LOOKING_AWAY: (0, 165, 255),
    AttentionState.EYES_CLOSED: (200, 80, 200),
    AttentionState.NO_FACE: (60, 60, 230),
    AttentionState.RESTLESS: (0, 200, 230),
    AttentionState.CALIBRATING: (220, 170, 40),
    AttentionState.PAUSED: (150, 150, 150),
}
_LABELS = {
    AttentionState.FOCUSED: "FOCUSED",
    AttentionState.LOOKING_AWAY: "LOOKING AWAY",
    AttentionState.EYES_CLOSED: "EYES CLOSED",
    AttentionState.NO_FACE: "NOT AT CAMERA",
    AttentionState.RESTLESS: "RESTLESS",
    AttentionState.CALIBRATING: "CALIBRATING",
    AttentionState.PAUSED: "PAUSED",
}


class AttentionMonitor:
    """Watches a webcam stream and alerts when you get distracted.

    Use it standalone (``python -m attention_monitor``) or embed it::

        mon = AttentionMonitor(MonitorConfig(), on_alert=lambda e: print(e))
        mon.run()          # blocks until 'q' / Esc / Ctrl+C / mon.stop()
    """

    def __init__(self, config: Optional[MonitorConfig] = None,
                 on_alert: Optional[Callable[[AlertEvent], None]] = None,
                 on_state_change: Optional[Callable[[AttentionState, AttentionState], None]] = None,
                 on_frame: Optional[Callable[[np.ndarray, TrackerUpdate, StudySession], None]] = None,
                 analyzer: Optional[FaceAnalyzer] = None):
        self.cfg = config or MonitorConfig()
        self._shared_analyzer = analyzer  # if given, reused and NOT closed by run()
        self._analyzer: Optional[FaceAnalyzer] = analyzer
        self.last_observation: Optional[FaceObservation] = None
        self.camera_off = threading.Event()   # set during manual breaks
        self.wall_t0 = time.time()
        self.on_alert = on_alert
        self.on_state_change = on_state_change
        self.on_frame = on_frame          # receives the annotated frame each loop
        self.tracker = AttentionTracker(self.cfg)
        self.alerts = AlertPlayer(self.cfg.sound, self.cfg.volume)
        self.session: Optional[StudySession] = None
        self._stop = threading.Event()
        self._commands: "queue.Queue[str]" = queue.Queue()

    def stop(self) -> None:
        self._stop.set()

    def command(self, name: str) -> None:
        """Thread-safe control: 'calibrate', 'pause', 'resume', 'toggle_pause', 'mute', 'unmute',
        'camera_off' / 'camera_on' (release the camera for a break, keeping calibration)."""
        self._commands.put(name)

    # ------------------------------------------------------------------ run
    def run(self) -> StudySession:
        cfg = self.cfg
        cap = self._open_camera()

        analyzer = self._shared_analyzer
        if analyzer is None:
            analyzer = FaceAnalyzer(cfg.model_path, cfg.min_detection_confidence,
                                    cfg.min_tracking_confidence)
        self._analyzer = analyzer
        self.session = session = StudySession(cfg.log_dir, cfg)
        t0 = time.monotonic()
        self.wall_t0 = time.time()        # tracker times + wall_t0 = wall-clock time
        self.tracker.start_calibration(0.0)
        session.note("start")
        prev_state: Optional[AttentionState] = None
        fail_count = 0

        if cfg.show_preview:
            cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
        elif self.on_frame is None:
            print("Running without preview. Press Ctrl+C to stop.")

        try:
            while not self._stop.is_set():
                while not self._commands.empty():
                    self._apply(self._commands.get_nowait(), time.monotonic() - t0, session)
                if self.camera_off.is_set():
                    # Break: release the camera entirely (light goes off); keep calibration.
                    if cap is not None:
                        cap.release()
                        cap = None
                    time.sleep(0.1)
                    continue
                if cap is None:
                    cap = self._open_camera()
                ok, frame = cap.read()
                if not ok:
                    fail_count += 1
                    if fail_count > 50:
                        raise RuntimeError("Camera stopped delivering frames")
                    time.sleep(0.02)
                    continue
                fail_count = 0
                if cfg.mirror:
                    frame = cv2.flip(frame, 1)

                now = time.monotonic() - t0
                obs = analyzer.analyze(frame, int(now * 1000))
                self.last_observation = obs
                upd = self.tracker.update(obs, now)
                session.record(upd, now)

                if upd.state != prev_state:
                    if prev_state is not None and self.on_state_change:
                        self.on_state_change(prev_state, upd.state)
                    if (not cfg.show_preview and self.on_frame is None
                            and upd.state != AttentionState.CALIBRATING):
                        print(f"[{_clock(now)}] {_LABELS[upd.state]} - {upd.reason}")
                    prev_state = upd.state

                if upd.calibration_cue:
                    self.alerts.cue()
                if upd.alert is not None:
                    self.alerts.play(upd.alert)
                    if self.on_alert:
                        self.on_alert(upd.alert)
                    if not cfg.show_preview and self.on_frame is None:
                        print(f"[{_clock(now)}] ALERT: {upd.alert.reason} "
                              f"({upd.alert.duration_s:.0f}s)")

                if cfg.show_preview or self.on_frame is not None:
                    self._draw(frame, obs, upd, session, now)
                if self.on_frame is not None:
                    self.on_frame(frame, upd, session)
                if cfg.show_preview:
                    cv2.imshow(WINDOW, frame)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (ord("q"), 27) or cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                        break
                    self._apply(_KEYS.get(key, ""), now, session)
        except KeyboardInterrupt:
            pass
        finally:
            if cap is not None:
                cap.release()
            if cfg.show_preview:
                cv2.destroyAllWindows()
            session.close()
            if self._shared_analyzer is None:
                if threading.current_thread() is threading.main_thread():
                    analyzer.close()
                else:
                    # MediaPipe's close() can block forever off the main thread.
                    threading.Thread(target=analyzer.close, daemon=True).start()
        return session

    def _open_camera(self):
        cfg = self.cfg
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(cfg.camera_index, backend)
        if not cap.isOpened():
            cap = cv2.VideoCapture(cfg.camera_index)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open camera {cfg.camera_index}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.frame_height)
        return cap

    def _apply(self, cmd: str, now: float, session: StudySession) -> None:
        if cmd in ("camera_off", "camera_on"):
            off = cmd == "camera_off"
            self.tracker.set_paused(off)
            (self.camera_off.set if off else self.camera_off.clear)()
            session.note(cmd)
        elif cmd == "calibrate":
            self.tracker.start_calibration(now)
            session.note("recalibrate")
        elif cmd in ("pause", "resume", "toggle_pause"):
            paused = not self.tracker.paused if cmd == "toggle_pause" else cmd == "pause"
            if paused != self.tracker.paused:
                self.tracker.set_paused(paused)
                session.note("pause" if paused else "resume")
        elif cmd in ("mute", "unmute", "toggle_mute"):
            self.alerts.enabled = (not self.alerts.enabled if cmd == "toggle_mute"
                                   else cmd == "unmute")

    # ------------------------------------------------------------------ overlay
    def _draw(self, frame: np.ndarray, obs: FaceObservation, upd: TrackerUpdate,
              session: StudySession, now: float) -> None:
        h, w = frame.shape[:2]
        color = _COLORS[upd.state]
        if upd.state is AttentionState.FOCUSED and upd.distracted_for > 0:
            color = _COLORS[AttentionState.LOOKING_AWAY]

        if obs.outline is not None:
            cv2.polylines(frame, [obs.outline.astype(np.int32)], True, color, 1, cv2.LINE_AA)

        # Top banner
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (w, 64), (25, 25, 25), -1)
        cv2.addWeighted(overlay, 0.65, frame, 0.35, 0, frame)
        cv2.putText(frame, _LABELS[upd.state], (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    color, 2, cv2.LINE_AA)
        cv2.putText(frame, upd.reason, (12, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (230, 230, 230), 1, cv2.LINE_AA)
        stats = f"Focus {session.focus_ratio * 100:.0f}%  |  {_clock(now)}  |  alerts {session.alerts}"
        (tw, _), _ = cv2.getTextSize(stats, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.putText(frame, stats, (w - tw - 12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (230, 230, 230), 1, cv2.LINE_AA)
        if not self.alerts.enabled:
            cv2.putText(frame, "MUTED", (w - 70, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (120, 120, 255), 1, cv2.LINE_AA)

        # Progress bar: calibration or countdown to alert
        if upd.state is AttentionState.CALIBRATING:
            frac = upd.calibration_progress
        elif upd.grace_s > 0 and upd.distracted_for > 0:
            frac = min(upd.distracted_for / upd.grace_s, 1.0)
        else:
            frac = 0.0
        if frac > 0:
            cv2.rectangle(frame, (0, 62), (int(w * frac), 66), color, -1)

        # Flashing border once alerting
        if upd.alerting and int(now * 3) % 2 == 0:
            cv2.rectangle(frame, (0, 0), (w - 1, h - 1), (0, 0, 255), 10)

        # Debug line + key help
        if obs.face_present and self.tracker.baseline is not None:
            dbg = (f"outside zone: yaw {upd.d_yaw:+.0f}  pitch {upd.d_pitch:+.0f}  "
                   f"gaze {upd.d_gaze:+.2f}  |  eyes closed {upd.closedness:.2f}/1.00")
            cv2.putText(frame, dbg, (12, h - 34), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (200, 200, 200), 1, cv2.LINE_AA)
        cv2.putText(frame, "[C] recalibrate  [P] pause  [M] mute  [Q] quit", (12, h - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1, cv2.LINE_AA)


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"
