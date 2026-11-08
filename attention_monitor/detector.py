"""Per-frame face analysis: presence, head pose, eye closure and gaze."""
from __future__ import annotations

import urllib.request
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

from .observation import FaceObservation

MODEL_URLS = {
    "face_landmarker.task": "https://storage.googleapis.com/mediapipe-models/"
                            "face_landmarker/face_landmarker/float16/1/face_landmarker.task",
}

# Landmark indices (MediaPipe 478-point face mesh).
_EYE_A = (33, 362)     # image-left corner of each eye
_EYE_B = (133, 263)    # image-right corner of each eye
_IRIS = (468, 473)     # iris centres
# Eye-aspect-ratio points per eye: (corner, top1, top2, corner, bottom2, bottom1)
_EAR_PTS = ((33, 160, 158, 133, 153, 144), (362, 385, 387, 263, 373, 380))
_OUTLINE = (10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365,
            379, 378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93,
            234, 127, 162, 21, 54, 103, 67, 109)


__all__ = ["FaceAnalyzer", "FaceObservation", "ensure_model", "MODEL_URLS"]


def ensure_model(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        url = MODEL_URLS.get(path.name)
        if url is None:
            raise FileNotFoundError(f"Model not found: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {path.name} to {path} ...")
        urllib.request.urlretrieve(url, path)
    return path


def _wrap_deg(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


class FaceAnalyzer:
    def __init__(self, model_path: Path, min_detection_confidence: float = 0.5,
                 min_tracking_confidence: float = 0.5):
        model_bytes = ensure_model(model_path).read_bytes()
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_buffer=model_bytes),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=min_detection_confidence,
            min_face_presence_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(options)
        self._last_ts = -1

    def analyze(self, frame_bgr: np.ndarray, timestamp_ms: int) -> FaceObservation:
        # VIDEO mode requires strictly increasing timestamps.
        ts = max(int(timestamp_ms), self._last_ts + 1)
        self._last_ts = ts

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(image, ts)
        if not result.face_landmarks:
            return FaceObservation(face_present=False)

        h, w = frame_bgr.shape[:2]
        pts = np.array([(p.x * w, p.y * h) for p in result.face_landmarks[0]], dtype=np.float32)

        yaw = pitch = roll = 0.0
        if result.facial_transformation_matrixes:
            rot = np.asarray(result.facial_transformation_matrixes[0])[:3, :3]
            angles, *_ = cv2.RQDecomp3x3(rot)
            pitch, yaw, roll = (_wrap_deg(a) for a in angles)

        closure = look_down = 0.0
        if result.face_blendshapes:
            scores = {c.category_name: c.score for c in result.face_blendshapes[0]}
            closure = (scores.get("eyeBlinkLeft", 0.0) + scores.get("eyeBlinkRight", 0.0)) / 2
            look_down = (scores.get("eyeLookDownLeft", 0.0) + scores.get("eyeLookDownRight", 0.0)) / 2

        gaze = 0.5
        if len(pts) > max(_IRIS):
            ratios = []
            for a, b, iris in zip(_EYE_A, _EYE_B, _IRIS):
                axis = pts[b] - pts[a]
                denom = float(axis @ axis)
                if denom > 1e-6:
                    ratios.append(float((pts[iris] - pts[a]) @ axis) / denom)
            if ratios:
                gaze = float(np.mean(ratios))

        ears = []
        for p1, p2, p3, p4, p5, p6 in _EAR_PTS:
            width = float(np.linalg.norm(pts[p1] - pts[p4]))
            if width > 1e-3:
                ears.append((np.linalg.norm(pts[p2] - pts[p6]) + np.linalg.norm(pts[p3] - pts[p5]))
                            / (2 * width))
        ear = float(np.mean(ears)) if ears else 0.3

        return FaceObservation(
            face_present=True, yaw=yaw, pitch=pitch, roll=roll, ear=ear,
            gaze_h=gaze, eye_closure=closure, look_down=look_down, outline=pts[list(_OUTLINE)],
        )

    def close(self) -> None:
        self._landmarker.close()
