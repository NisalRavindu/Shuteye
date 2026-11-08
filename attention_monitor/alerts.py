"""Audible alerts with adjustable volume. Non-blocking so the video loop never stalls.

Beeps are synthesised as short in-memory WAV clips (so their loudness can be
scaled) and played with winsound on Windows. Elsewhere it falls back to the
terminal bell, which has no volume control.
"""
from __future__ import annotations

import io
import math
import struct
import sys
import threading
import wave
from functools import lru_cache

from .tracker import AttentionState

try:
    import winsound
except ImportError:  # not Windows
    winsound = None

RATE = 22050
# (frequency Hz, duration ms) per issue, so you can tell them apart by ear.
_PATTERNS = {
    AttentionState.LOOKING_AWAY: ((880, 180), (660, 220)),
    AttentionState.EYES_CLOSED: ((1200, 120), (1200, 120), (1200, 120)),
    AttentionState.NO_FACE: ((520, 300), (390, 400)),
    AttentionState.RESTLESS: ((600, 120), (800, 120), (1000, 200)),
}
_CUE = ((660, 120), (990, 250))
_GAP_MS = 40


@lru_cache(maxsize=64)
def render_wav(pattern: tuple, volume: int) -> bytes:
    """A WAV clip of the tone pattern at `volume` (0-100)."""
    amp = 32767 * (max(0, min(100, volume)) / 100) ** 1.6   # perceptual-ish curve
    frames = bytearray()
    fade = int(RATE * 0.006)                                  # 6 ms fade: no clicks
    for freq, ms in pattern:
        n = int(RATE * ms / 1000)
        for i in range(n):
            env = min(1.0, i / fade, (n - 1 - i) / fade)
            frames += struct.pack("<h", int(amp * env * math.sin(2 * math.pi * freq * i / RATE)))
        frames += b"\x00\x00" * int(RATE * _GAP_MS / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class AlertPlayer:
    def __init__(self, enabled: bool = True, volume: int = 70):
        self.enabled = enabled
        self.volume = volume
        self._busy = threading.Lock()

    def play(self, event) -> None:
        if self.enabled:
            self._start(_PATTERNS.get(event.issue, ((800, 250),)))

    def cue(self) -> None:
        """Calibration prompt; plays even when alerts are muted."""
        self._start(_CUE)

    def test(self, issue: AttentionState = AttentionState.LOOKING_AWAY) -> None:
        """Preview the current volume (ignores mute)."""
        self._start(_PATTERNS[issue])

    def _start(self, pattern) -> None:
        if self.volume <= 0 or not self._busy.acquire(blocking=False):
            return  # silent, or the previous sound is still playing
        threading.Thread(target=self._run, args=(pattern,), daemon=True).start()

    def _run(self, pattern) -> None:
        try:
            if winsound is not None:
                winsound.PlaySound(render_wav(tuple(pattern), int(self.volume)), winsound.SND_MEMORY)
            else:
                sys.stdout.write("\a")
                sys.stdout.flush()
        except Exception:
            pass
        finally:
            self._busy.release()
