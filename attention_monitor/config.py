"""Tunable settings for the attention monitor."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class MonitorConfig:
    # --- Camera -----------------------------------------------------------
    camera_index: int = 0
    frame_width: int = 640
    frame_height: int = 480
    mirror: bool = True              # flip horizontally so the preview acts like a mirror

    # --- Model ------------------------------------------------------------
    model_path: Path = PROJECT_ROOT / "models" / "face_landmarker.task"
    min_detection_confidence: float = 0.5
    min_tracking_confidence: float = 0.5

    # --- Calibration ------------------------------------------------------
    # Each step records one study posture. Anything between / around them
    # counts as focused, so looking down at your book while writing is fine.
    # Use ("screen",) if you only study from a screen.
    calibration_steps: tuple = ("screen", "book")
    calibration_seconds: float = 3.0     # recording time per step
    calibration_prep_s: float = 2.0      # time to move into the next posture
    calibration_min_samples: int = 15

    # --- "Focused" tolerances (margin around the calibrated study zone) --
    yaw_tolerance_deg: float = 20.0      # head turned left / right
    pitch_tolerance_deg: float = 15.0    # head tilted up / down
    use_gaze: bool = True
    gaze_tolerance: float = 0.16         # iris offset inside the eye (0..1 scale)
    # Eyes-closed detection. With `calibrate_eyes_closed`, calibration ends with a
    # short "close your eyes until the beep" step and the threshold is set between
    # YOUR open and closed eyes (0 = like open, 1 = like closed) for each posture.
    calibrate_eyes_closed: bool = True
    eye_closed_level: float = 0.55
    eye_closed_step_timeout_s: float = 8.0   # give up on that step if no face is seen
    # Fallback when there's no closed-eye calibration:
    eye_closed_threshold: float = 0.55   # blink score above which eyes count as closed
    eye_closed_margin: float = 0.25      # ...or this much above the calibrated posture's level

    smoothing: float = 0.35              # EMA factor for per-frame signals (0 = frozen, 1 = raw)

    # --- Timing -----------------------------------------------------------
    # How long a problem must last before you get alerted, per issue type.
    away_grace_s: float = 4.0
    eyes_closed_grace_s: float = 3.0
    no_face_grace_s: float = 5.0
    recover_s: float = 0.8               # must be focused this long to count as "back"
    repeat_alert_s: float = 8.0          # re-alert interval while still distracted

    # --- Session scoring --------------------------------------------------
    min_episode_s: float = 1.0           # shorter breaks are detection noise, dropped entirely
    min_counted_s: float = 10.0          # isolated breaks shorter than this don't count as wasted
    # ...unless they come in a burst: `restless_count` short breaks, each starting
    # within `restless_gap_s` of the previous one, all count (and trigger an alert).
    restless_count: int = 4
    restless_gap_s: float = 30.0

    # --- Output -----------------------------------------------------------
    show_preview: bool = True
    sound: bool = True
    volume: int = 70                     # beep volume, 0-100
    log_dir: Path = PROJECT_ROOT / "sessions"
    extra: dict = field(default_factory=dict)
