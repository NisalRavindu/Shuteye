"""Attention monitor for study sessions.

The web app (``python -m attention_monitor.web``) needs only the standard
library: the browser runs the face model and sends numbers. The local OpenCV
version (``python -m attention_monitor``) additionally needs opencv-python and
mediapipe, so it's imported lazily.
"""
from .config import MonitorConfig
from .observation import FaceObservation
from .tracker import AlertEvent, AttentionState, AttentionTracker

__all__ = ["MonitorConfig", "FaceObservation", "AttentionMonitor", "AttentionTracker",
           "AttentionState", "AlertEvent"]


def __getattr__(name):
    if name == "AttentionMonitor":          # pulls in OpenCV + MediaPipe
        from .monitor import AttentionMonitor
        return AttentionMonitor
    raise AttributeError(name)
