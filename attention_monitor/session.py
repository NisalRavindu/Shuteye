"""Session statistics, wasted-time scoring and a CSV event log."""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import MonitorConfig
from .tracker import AttentionState, Episode, TrackerUpdate

_UNTRACKED = (AttentionState.CALIBRATING, AttentionState.PAUSED)


@dataclass
class Score:
    wasted_s: float = 0.0
    counted: int = 0              # episodes that count as wasted
    ignored: int = 0              # brief, isolated breaks that were forgiven
    ignored_s: float = 0.0
    restless_spells: list = field(default_factory=list)   # (start, end, n_breaks)
    longest_s: float = 0.0


def score_episodes(episodes: list[Episode], cfg: MonitorConfig) -> Score:
    """Decide which distraction episodes count as wasted time.

    * An episode of at least ``min_counted_s`` always counts.
    * A shorter one is forgiven (grabbing something, a quick stretch)...
    * ...unless it belongs to a restless spell: a run of episodes, each starting
      within ``restless_gap_s`` of the previous one ending, containing at least
      ``restless_count`` short breaks. Then the whole run counts.
    """
    s = Score()
    for cl, restless, flags in _clusters(episodes, cfg):
        if restless:
            s.restless_spells.append((cl[0].start, cl[-1].end, len(cl)))
        for e, counted in zip(cl, flags):
            s.longest_s = max(s.longest_s, e.duration)
            if counted:
                s.wasted_s += e.duration
                s.counted += 1
            else:
                s.ignored += 1
                s.ignored_s += e.duration
    return s


def _clusters(episodes: list[Episode], cfg: MonitorConfig):
    """Yield (cluster, is_restless, counted_flags) using the rules in score_episodes."""
    clusters: list[list[Episode]] = []
    for ep in sorted(episodes, key=lambda e: e.start):
        if clusters and ep.start - clusters[-1][-1].end <= cfg.restless_gap_s:
            clusters[-1].append(ep)
        else:
            clusters.append([ep])
    for cl in clusters:
        n_short = sum(1 for e in cl if not e.alerted and e.duration < cfg.min_counted_s)
        restless = cfg.restless_count > 0 and n_short >= cfg.restless_count
        yield cl, restless, [restless or e.duration >= cfg.min_counted_s for e in cl]


def counted_episodes(episodes: list[Episode], cfg: MonitorConfig) -> list[Episode]:
    """The episodes that count as wasted time (brief isolated ones are forgiven)."""
    return [e for cl, _, flags in _clusters(episodes, cfg) for e, c in zip(cl, flags) if c]


def fmt_duration(s: float) -> str:
    m, s = divmod(int(round(s)), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


class StudySession:
    def __init__(self, log_dir: Optional[Path], config: Optional[MonitorConfig] = None):
        self.cfg = config or MonitorConfig()
        self.started = datetime.now()
        self.time_in: dict[AttentionState, float] = defaultdict(float)
        self.alerts = 0
        self.episodes: list[Episode] = []
        self.score = Score()
        self._ongoing = 0.0
        self._last_now: Optional[float] = None
        self._last_state: Optional[AttentionState] = None
        self._file = self._writer = None
        self.path: Optional[Path] = None
        if log_dir is not None:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            self.path = log_dir / f"session_{self.started:%Y%m%d_%H%M%S}.csv"
            self._file = open(self.path, "w", newline="", encoding="utf-8")
            self._writer = csv.writer(self._file)
            self._writer.writerow(["time", "event", "state", "seconds", "detail"])

    # ------------------------------------------------------------------
    def record(self, upd: TrackerUpdate, now: float) -> None:
        if self._last_now is not None and self._last_state is not None:
            dt = min(now - self._last_now, 1.0)  # ignore long stalls
            self.time_in[self._last_state] += dt
        self._last_now, self._last_state = now, upd.state
        self._ongoing = upd.distracted_for if upd.state not in _UNTRACKED else 0.0

        if upd.alert is not None:
            self.alerts += 1
            self._log("alert", upd.alert.issue, upd.alert.duration_s, upd.alert.reason)
        if upd.episode is not None:
            ep = upd.episode
            self.episodes.append(ep)
            self.score = score_episodes(self.episodes, self.cfg)
            self._log("distraction", ep.issue, ep.duration, "alerted" if ep.alerted else "")

    def note(self, event: str, detail: str = "") -> None:
        self._log(event, None, 0.0, detail)

    def _log(self, event: str, state: Optional[AttentionState], secs: float, detail: str) -> None:
        if self._writer:
            self._writer.writerow([datetime.now().isoformat(timespec="seconds"), event,
                                   state.value if state else "", f"{secs:.1f}", detail])
            self._file.flush()

    # ------------------------------------------------------------------
    @property
    def tracked_seconds(self) -> float:
        return sum(v for k, v in self.time_in.items() if k not in _UNTRACKED)

    @property
    def wasted_seconds(self) -> float:
        """Counted wasted time, including a still-running long distraction."""
        ongoing = self._ongoing if self._ongoing >= self.cfg.min_counted_s else 0.0
        return self.score.wasted_s + ongoing

    @property
    def focus_ratio(self) -> float:
        total = self.tracked_seconds
        if not total:
            return 1.0
        return max(0.0, 1.0 - self.wasted_seconds / total)

    def summary(self) -> str:
        t, sc = self.time_in, self.score
        lines = [
            "=== Study session summary ===",
            f"Tracked time : {fmt_duration(self.tracked_seconds)}",
            f"Focus        : {self.focus_ratio * 100:.0f}%",
            f"Wasted time  : {fmt_duration(self.wasted_seconds)} "
            f"({sc.counted} distraction{'s' if sc.counted != 1 else ''} counted)",
        ]
        if sc.restless_spells:
            n = sum(c for *_, c in sc.restless_spells)
            lines.append(f"Restless     : {len(sc.restless_spells)} spell(s) of repeated short breaks "
                         f"({n} breaks, all counted)")
        lines += [
            f"Ignored      : {sc.ignored} brief break(s) under {self.cfg.min_counted_s:.0f}s "
            f"({fmt_duration(sc.ignored_s)})",
            f"Raw time off : looking away {fmt_duration(t[AttentionState.LOOKING_AWAY])}, "
            f"eyes closed {fmt_duration(t[AttentionState.EYES_CLOSED])}, "
            f"not at camera {fmt_duration(t[AttentionState.NO_FACE])}",
            f"Alerts       : {self.alerts}",
        ]
        if sc.longest_s:
            lines.append(f"Longest break: {fmt_duration(sc.longest_s)}")
        if self.path is not None:
            lines.append(f"Log          : {self.path}")
        return "\n".join(lines)

    def close(self) -> None:
        if self._file:
            self._log("end", None, self.tracked_seconds,
                      f"focus={self.focus_ratio:.2f} wasted={self.wasted_seconds:.0f}s "
                      f"ignored={self.score.ignored}")
            self._file.close()
            self._file = self._writer = None
