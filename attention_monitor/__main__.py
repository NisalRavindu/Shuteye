"""CLI: python -m attention_monitor [options]"""
from __future__ import annotations

import argparse
from pathlib import Path

from .config import MonitorConfig
from .monitor import AttentionMonitor


def main(argv=None) -> None:
    d = MonitorConfig()
    p = argparse.ArgumentParser(prog="attention_monitor",
                                description="Alert me when I get distracted while studying.")
    p.add_argument("--camera", type=int, default=d.camera_index, help="camera index (default 0)")
    p.add_argument("--no-preview", action="store_true", help="run without the video window")
    p.add_argument("--mute", action="store_true", help="no sound alerts")
    p.add_argument("--no-mirror", action="store_true", help="don't flip the image horizontally")
    p.add_argument("--away-grace", type=float, default=d.away_grace_s,
                   help="seconds looking away before alert (default %(default)s)")
    p.add_argument("--closed-grace", type=float, default=d.eyes_closed_grace_s,
                   help="seconds with eyes closed before alert (default %(default)s)")
    p.add_argument("--absent-grace", type=float, default=d.no_face_grace_s,
                   help="seconds with no face before alert (default %(default)s)")
    p.add_argument("--repeat", type=float, default=d.repeat_alert_s,
                   help="re-alert every N seconds while distracted (default %(default)s)")
    p.add_argument("--yaw-tol", type=float, default=d.yaw_tolerance_deg,
                   help="allowed left/right head turn in degrees (default %(default)s)")
    p.add_argument("--pitch-tol", type=float, default=d.pitch_tolerance_deg,
                   help="allowed up/down head tilt in degrees (default %(default)s)")
    p.add_argument("--gaze-tol", type=float, default=d.gaze_tolerance,
                   help="allowed sideways eye movement, 0..1 (default %(default)s)")
    p.add_argument("--no-gaze", action="store_true", help="ignore eye direction, use head pose only")
    p.add_argument("--calibrate", type=float, default=d.calibration_seconds,
                   help="seconds per calibration step (default %(default)s)")
    p.add_argument("--no-book", action="store_true",
                   help="screen-only calibration (skip the 'look down at your book' step)")
    p.add_argument("--no-eyes-calib", action="store_true",
                   help="skip the 'close your eyes until the beep' calibration step")
    p.add_argument("--volume", type=int, default=d.volume,
                   help="beep volume 0-100 (default %(default)s)")
    p.add_argument("--min-counted", type=float, default=d.min_counted_s,
                   help="isolated breaks shorter than this aren't counted as wasted (default %(default)s)")
    p.add_argument("--restless-count", type=int, default=d.restless_count,
                   help="quick breaks in a row that count as restless; 0 disables (default %(default)s)")
    p.add_argument("--restless-gap", type=float, default=d.restless_gap_s,
                   help="max seconds between quick breaks to be 'in a row' (default %(default)s)")
    p.add_argument("--log-dir", type=Path, default=d.log_dir, help="where session CSVs go")
    p.add_argument("--no-log", action="store_true", help="don't write a session CSV")
    a = p.parse_args(argv)

    cfg = MonitorConfig(
        camera_index=a.camera, show_preview=not a.no_preview, sound=not a.mute,
        mirror=not a.no_mirror, away_grace_s=a.away_grace, eyes_closed_grace_s=a.closed_grace,
        no_face_grace_s=a.absent_grace, repeat_alert_s=a.repeat, yaw_tolerance_deg=a.yaw_tol,
        pitch_tolerance_deg=a.pitch_tol, gaze_tolerance=a.gaze_tol, use_gaze=not a.no_gaze,
        calibration_seconds=a.calibrate, log_dir=None if a.no_log else a.log_dir,
        calibration_steps=("screen",) if a.no_book else d.calibration_steps,
        volume=max(0, min(100, a.volume)),
        calibrate_eyes_closed=not a.no_eyes_calib,
        min_counted_s=a.min_counted, restless_count=a.restless_count, restless_gap_s=a.restless_gap,
    )
    session = AttentionMonitor(cfg).run()
    print()
    print(session.summary())


if __name__ == "__main__":
    main()
