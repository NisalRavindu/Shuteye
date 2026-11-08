"""What the face analysis reports for one video frame.

Kept dependency-free: the web server receives these numbers from the browser
(which runs the face model itself) and never needs OpenCV or MediaPipe.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class FaceObservation:
    face_present: bool
    yaw: float = 0.0           # degrees, + = turned to image-right
    pitch: float = 0.0         # degrees
    roll: float = 0.0          # degrees
    gaze_h: float = 0.5        # 0..1 iris position between eye corners (0.5 ~ centred)
    eye_closure: float = 0.0   # 0 = open, 1 = closed (blink blendshape)
    look_down: float = 0.0     # 0..1 how far the eyes look down (blendshape)
    ear: float = 0.3           # eye aspect ratio (lid opening / eye width); ~0.3 open, <0.15 closed
    outline: Optional[Any] = None   # Nx2 pixel coords of the face outline (local OpenCV mode only)
