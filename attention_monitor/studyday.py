"""Study days: the timetable's plan for a day, live progress, wasted-time attribution
and end-of-day / weekly analysis.

Concepts
--------
* **Study day.** Runs from ``day_end_hour`` (default 04:00) to the same hour the
  next morning, so studying past midnight still belongs to the day it started on.
  A timetable block on weekday W at/after that hour belongs to W's study day; a
  block before it (e.g. Tue 01:00) belongs to the previous study day.
* **Automatic breaks.** When two subjects follow each other with no gap, the last
  ``break_min`` minutes of the first one become a break (not monitored, not
  wasted). The next subject still starts on time. Breaks aren't shown in the
  timetable.
* **Intervals.** While a day is running, every second is recorded as one of:
  ``study`` (scheduled subject, camera monitoring), ``catchup`` (making up a
  subject in free time), ``manual_break`` (you paused - camera off). Time in a
  scheduled subject that isn't any of these (day not started, camera stopped,
  server off) is *missed*.
* **Wasted time** comes from the face tracker's distraction episodes, using the
  same forgiveness rules as a normal session, attributed to the subject whose
  study/catch-up interval it overlaps.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from .config import PROJECT_ROOT, MonitorConfig
from .session import counted_episodes
from .tracker import AttentionState, Episode

DATA_DIR = PROJECT_ROOT / "data"
DAYS_DIR = DATA_DIR / "days"
TIMETABLE_FILE = DATA_DIR / "timetable.json"
SETTINGS_FILE = DATA_DIR / "study_settings.json"
DEFAULT_SETTINGS = {"break_min": 5, "day_end_hour": 4}
MIN_SUGGEST_S = 5 * 60          # don't suggest catching up less than this
MIN_WINDOW_S = 10 * 60          # ignore free gaps shorter than this


# ---------------------------------------------------------------------------- io
def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_settings(path: Optional[Path] = None) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in _load_json(path or SETTINGS_FILE, {}).items() if k in DEFAULT_SETTINGS})
    s["break_min"] = max(0, min(5, int(s["break_min"])))
    s["day_end_hour"] = max(0, min(8, int(s["day_end_hour"])))
    return s


def save_settings(patch: dict, path: Optional[Path] = None) -> dict:
    path = path or SETTINGS_FILE
    s = load_settings(path)
    s.update({k: patch[k] for k in DEFAULT_SETTINGS if k in patch})
    _save_json(path, s)
    return load_settings(path)


# ---------------------------------------------------------------------------- calendar
def study_date_of(ts: float, day_end_hour: int) -> dt.date:
    return (dt.datetime.fromtimestamp(ts) - dt.timedelta(hours=day_end_hour)).date()


def day_window(d: dt.date, day_end_hour: int) -> tuple[float, float]:
    start = dt.datetime.combine(d, dt.time(day_end_hour))
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


def _midnight(d: dt.date) -> float:
    return dt.datetime.combine(d, dt.time()).timestamp()


def build_plan(timetable: dict, d: dt.date, day_end_hour: int, break_min: int):
    """Segments for study day ``d``: [{s, e, kind: study|break, subject}] + subjects."""
    subjects = {s["id"]: {"name": s.get("name", "?"), "color": s.get("color", "#888888")}
                for s in timetable.get("subjects", []) if isinstance(s, dict) and "id" in s}
    cut = day_end_hour * 60
    w0, w1 = d.weekday(), (d.weekday() + 1) % 7
    base0, base1 = _midnight(d), _midnight(d + dt.timedelta(days=1))
    lo, hi = day_window(d, day_end_hour)

    blocks = []
    for e in timetable.get("entries", []):
        try:
            day, start, end, sub = int(e["day"]), float(e["start"]), float(e["end"]), e["subject"]
        except (KeyError, TypeError, ValueError):
            continue
        if sub not in subjects or end <= start:
            continue
        if day == w0 and start >= cut:
            s, en = base0 + start * 60, base0 + end * 60
        elif day == w1 and start < cut:
            s, en = base1 + start * 60, base1 + end * 60
        else:
            continue
        s, en = max(s, lo), min(en, hi)
        if en > s:
            blocks.append([s, en, sub])
    blocks.sort()

    merged: list[list] = []
    for b in blocks:
        if merged and merged[-1][2] == b[2] and merged[-1][1] >= b[0]:
            merged[-1][1] = max(merged[-1][1], b[1])
        else:
            merged.append(b)

    segs = []
    for i, (s, e, sub) in enumerate(merged):
        nxt = merged[i + 1] if i + 1 < len(merged) else None
        if break_min > 0 and nxt is not None and abs(nxt[0] - e) < 1:
            br = min(break_min * 60, (e - s) / 2)          # never more than half the block
            segs.append({"s": s, "e": e - br, "kind": "study", "subject": sub})
            segs.append({"s": e - br, "e": e, "kind": "break", "subject": nxt[2]})
        else:
            segs.append({"s": s, "e": e, "kind": "study", "subject": sub})
    return segs, subjects


def current_segment(plan: list[dict], t: float) -> Optional[dict]:
    for seg in plan:
        if seg["s"] <= t < seg["e"]:
            return seg
    return None


def free_windows(plan: list[dict], frm: float, to: float, min_len: float = MIN_WINDOW_S):
    out, cur = [], frm
    for seg in sorted(plan, key=lambda x: x["s"]):
        if seg["e"] <= cur:
            continue
        if seg["s"] > cur and seg["s"] - cur >= min_len:
            out.append((cur, min(seg["s"], to)))
        cur = max(cur, seg["e"])
        if cur >= to:
            break
    if to - cur >= min_len:
        out.append((cur, to))
    return [(a, b) for a, b in out if b - a >= min_len]


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


# ---------------------------------------------------------------------------- analysis
def _cfg_from(settings: dict) -> MonitorConfig:
    keys = ("min_counted_s", "restless_count", "restless_gap_s", "min_episode_s")
    return MonitorConfig(**{k: settings[k] for k in keys if k in settings})


def analyze_day(record: dict, plan: list[dict], subjects: dict, now: float) -> dict:
    """Per-subject planned / studied / wasted / breaks / missed for one study day."""
    window_end = record["window_end"]
    active = record.get("started_at") is not None and record.get("ended_at") is None
    until = min(now, window_end) if active else window_end   # an ended day owes its full plan

    def issue(v):   # older records may hold issue types that no longer exist (e.g. "phone")
        try:
            return AttentionState(v)
        except ValueError:
            return AttentionState.LOOKING_AWAY
    eps = [Episode(e["s"], e["e"], issue(e.get("issue", "looking_away")), bool(e.get("alerted")))
           for e in record.get("episodes", [])]
    counted = counted_episodes(eps, _cfg_from(record.get("settings", {})))

    rows: dict[str, dict] = {}
    def row(sid):
        if sid not in rows:
            info = subjects.get(sid, {"name": "Deleted subject", "color": "#888888"})
            rows[sid] = {"id": sid, "name": info["name"], "color": info["color"], "planned": 0.0,
                         "planned_so_far": 0.0, "monitored": 0.0, "catchup": 0.0, "breaks": 0.0, "wasted": 0.0}
        return rows[sid]

    for seg in plan:
        if seg["kind"] != "study":
            continue
        r = row(seg["subject"])
        r["planned"] += seg["e"] - seg["s"]
        r["planned_so_far"] += _overlap(seg["s"], seg["e"], 0, until)

    for iv in record.get("intervals", []):
        r = row(iv["subject"])
        d = iv["e"] - iv["s"]
        if iv["kind"] == "study":
            r["monitored"] += d
        elif iv["kind"] == "catchup":
            r["catchup"] += d
        elif iv["kind"] == "manual_break":
            r["breaks"] += d
        if iv["kind"] in ("study", "catchup"):
            r["wasted"] += sum(_overlap(iv["s"], iv["e"], ep.start, ep.end) for ep in counted)

    out_rows = []
    for r in rows.values():
        r["wasted"] = min(r["wasted"], r["monitored"] + r["catchup"])
        r["studied"] = r["monitored"] + r["catchup"] - r["wasted"]
        r["missed"] = max(0.0, r["planned_so_far"] - r["monitored"] - r["breaks"])
        r["short"] = max(0.0, r["planned_so_far"] - r["studied"])
        out_rows.append({k: (round(v) if isinstance(v, float) else v) for k, v in r.items()})
    out_rows.sort(key=lambda r: (-r["planned"], r["name"]))

    keys = ("planned", "planned_so_far", "studied", "wasted", "breaks", "missed", "short", "catchup")
    totals = {k: sum(r[k] for r in out_rows) for k in keys}
    return {"date": record["date"], "started_at": record.get("started_at"), "ended_at": record.get("ended_at"),
            "end_reason": record.get("end_reason"), "window_end": window_end, "subjects": out_rows,
            "totals": totals}


def suggestions(analysis: dict, plan: list[dict], now: float, window_end: float, limit: int = 5) -> list[dict]:
    """Fit each subject's shortfall so far into the remaining free time of the day."""
    need = [[r["id"], r["name"], r["color"], r["short"]] for r in analysis["subjects"] if r["short"] >= MIN_SUGGEST_S]
    need.sort(key=lambda x: -x[3])
    out = []
    for a, b in free_windows(plan, now, window_end):
        t = a
        for item in need:
            if item[3] < MIN_SUGGEST_S or b - t < MIN_SUGGEST_S:
                continue
            take = min(item[3], b - t)
            out.append({"subject": item[0], "name": item[1], "color": item[2], "s": t, "e": t + take,
                        "minutes": round(take / 60)})
            item[3] -= take
            t += take
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------- manager
class DayManager:
    """Runs the active study day on top of the web MonitorController."""

    def __init__(self, ctrl, clock: Callable[[], float] = time.time, start_thread: bool = True,
                 paths: Optional[dict] = None):
        """`paths` = {"days", "timetable", "settings"} for one user's data (defaults: module paths)."""
        paths = paths or {}
        self.days_dir = Path(paths.get("days", DAYS_DIR))
        self.timetable_file = Path(paths.get("timetable", TIMETABLE_FILE))
        self.settings_file = Path(paths.get("settings", SETTINGS_FILE))
        self.ctrl = ctrl
        self.clock = clock
        self.lock = threading.RLock()
        self.active: Optional[dict] = None
        self.manual_break = False
        self.catchup: Optional[dict] = None          # {"subject", "target_s", "done_s"}
        self.last_concluded: Optional[dict] = None
        self.monitor_settings: dict = {}
        self._inbox: deque = deque()                 # episodes from the monitor thread (no lock)
        self._last_tick: Optional[float] = None
        self._last_save = 0.0
        self._plan_cache: dict = {}
        ctrl.on_episode = self._inbox.append
        self._recover()
        if start_thread:
            threading.Thread(target=self._loop, daemon=True).start()

    # ---------------------------------------------------------------- plan
    def plan_for(self, d: dt.date) -> tuple[list[dict], dict]:
        s = load_settings(self.settings_file)
        try:
            mtime = self.timetable_file.stat().st_mtime
        except FileNotFoundError:
            mtime = 0
        key = (d, mtime, s["break_min"], s["day_end_hour"])
        if key not in self._plan_cache:
            self._plan_cache = {key: build_plan(_load_json(self.timetable_file, {}), d, s["day_end_hour"], s["break_min"])}
        return self._plan_cache[key]

    def _record_plan(self, rec: dict) -> tuple[list[dict], dict]:
        if rec.get("ended_at") is not None and "plan" in rec:
            return rec["plan"], rec["subjects"]          # frozen at the end of the day
        return self.plan_for(dt.date.fromisoformat(rec["date"]))

    # ---------------------------------------------------------------- lifecycle
    def _path(self, date_str: str) -> Path:
        return self.days_dir / f"{date_str}.json"

    def _recover(self) -> None:
        """After a restart: close days whose limit passed, resume today's open day."""
        now = self.clock()
        for f in sorted(self.days_dir.glob("*.json")):
            rec = _load_json(f, None)
            if not rec or rec.get("ended_at") is not None:
                continue
            if now >= rec["window_end"]:
                self.active = rec
                self._conclude(rec["window_end"], "limit")
            else:
                self.active = rec                       # monitoring is off until resumed

    def start_day(self, monitor_settings: dict) -> None:
        with self.lock:
            now = self.clock()
            s = load_settings(self.settings_file)
            d = study_date_of(now, s["day_end_hour"])
            if self.active is None or self.active["date"] != d.isoformat():
                rec = _load_json(self._path(d.isoformat()), None)
                if rec is None:
                    lo, hi = day_window(d, s["day_end_hour"])
                    rec = {"date": d.isoformat(), "started_at": now, "ended_at": None, "end_reason": None,
                           "window_start": lo, "window_end": hi, "intervals": [], "episodes": [], "notes": []}
                else:                                    # reopen a day that was ended earlier
                    rec.update(ended_at=None, end_reason=None)
                    rec.pop("plan", None); rec.pop("subjects", None)
                    rec.setdefault("notes", []).append({"t": now, "note": "reopened"})
                cfg = MonitorConfig(**{k: v for k, v in monitor_settings.items()
                                       if k in ("min_counted_s", "restless_count", "restless_gap_s")})
                rec["settings"] = {"min_counted_s": cfg.min_counted_s, "restless_count": cfg.restless_count,
                                   "restless_gap_s": cfg.restless_gap_s, "min_episode_s": cfg.min_episode_s,
                                   **s}
                self.active = rec
            self.manual_break = False
            self.catchup = None
            self.monitor_settings = monitor_settings
            self._save(force=True)
        if self.ctrl.running and not getattr(self.ctrl, "day_mode", False):
            self.ctrl.stop()                             # a quick session was running
        self.ctrl.day_mode = True
        self.ctrl.start(monitor_settings)

    def end_day(self, reason: str = "manual") -> Optional[str]:
        with self.lock:
            if self.active is None:
                return None
            self._flush_ongoing()
            date_str = self._conclude(self.clock(), reason)
        self.ctrl.stop()
        self.ctrl.day_mode = False
        return date_str

    def _conclude(self, at: float, reason: str) -> str:
        rec = self.active
        self._drain()
        at = min(at, rec["window_end"])
        for iv in rec["intervals"]:
            iv["e"] = min(iv["e"], at)
        rec["intervals"] = [iv for iv in rec["intervals"] if iv["e"] > iv["s"]]
        rec["ended_at"] = at
        rec["end_reason"] = reason
        plan, subjects = self.plan_for(dt.date.fromisoformat(rec["date"]))
        rec["plan"], rec["subjects"] = plan, subjects
        _save_json(self._path(rec["date"]), rec)
        self.last_concluded = {"date": rec["date"], "at": self.clock(), "reason": reason}
        self.active = None
        self.manual_break = False
        self.catchup = None
        return rec["date"]

    def set_break(self, on: bool) -> None:
        with self.lock:
            if self.active is None or on == self.manual_break:
                return
            if on:
                self._flush_ongoing()
            self.manual_break = on
        mon = self.ctrl.monitor
        if mon is not None and self.ctrl.running:
            mon.command("camera_off" if on else "camera_on")

    def set_catchup(self, subject: Optional[str]) -> None:
        with self.lock:
            if self.active is None:
                return
            if subject is None:
                self.catchup = None
                return
            a = self._analysis_now()
            short = next((r["short"] for r in a["subjects"] if r["id"] == subject), 0)
            self.catchup = {"subject": subject, "target_s": max(short, 15 * 60), "done_s": 0.0}

    # ---------------------------------------------------------------- ticking
    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:  # never let the ticker die
                print("day tick error:", exc)
            time.sleep(1.0)

    def tick(self) -> None:
        with self.lock:
            now = self.clock()
            prev, self._last_tick = self._last_tick, now
            rec = self.active
            if rec is None:
                return
            self._drain()
            if now >= rec["window_end"]:
                self._flush_ongoing()
                self._conclude(rec["window_end"], "limit")
                stop = True
            else:
                stop = False
                self._tick_active(rec, now, prev)
        if stop:
            self.ctrl.stop()
            self.ctrl.day_mode = False

    def _tick_active(self, rec: dict, now: float, prev: Optional[float]) -> None:
        plan, _ = self.plan_for(dt.date.fromisoformat(rec["date"]))
        seg = current_segment(plan, now)
        # Monitored = the browser is actually sending numbers (camera on, tab alive).
        running = self.ctrl.receiving or (self.manual_break and self.ctrl.running)

        # catch-up only lives in free time
        if self.catchup and seg is not None:
            self.catchup = None

        kind = subject = None
        if self.manual_break:
            if seg and seg["kind"] == "study":
                kind, subject = "manual_break", seg["subject"]
            elif self.catchup:
                kind, subject = "manual_break", self.catchup["subject"]
        elif running:
            if seg and seg["kind"] == "study":
                kind, subject = "study", seg["subject"]
            elif seg is None and self.catchup:
                kind, subject = "catchup", self.catchup["subject"]

        if kind and prev is not None and now - prev < 5:
            ivs = rec["intervals"]
            last = ivs[-1] if ivs else None
            if last and last["kind"] == kind and last["subject"] == subject and prev - last["e"] < 2.5:
                last["e"] = now
            else:
                ivs.append({"s": prev, "e": now, "kind": kind, "subject": subject})
            if kind == "catchup":
                self.catchup["done_s"] += now - prev
                if self.catchup["done_s"] >= self.catchup["target_s"]:
                    self.catchup = None                    # made up the missing time

        # Tell the tracker whether to watch you right now.
        mon = self.ctrl.monitor
        if mon is not None and self.ctrl.receiving and not self.manual_break:
            want_paused = kind not in ("study", "catchup")
            if not mon.tracker.calibrating and mon.tracker.paused != want_paused:
                if want_paused:
                    self._flush_ongoing()
                mon.command("pause" if want_paused else "resume")
        self._save()

    def _drain(self) -> None:
        rec = self.active
        while self._inbox:
            ep = self._inbox.popleft()
            if rec is not None:
                rec["episodes"].append(ep)

    def _flush_ongoing(self) -> None:
        """Record a distraction still in progress (before pausing/ending would drop it)."""
        upd = getattr(self.ctrl, "last_update", None)
        if self.active is None or upd is None or not self.ctrl.running:
            return
        if upd.distracted_for >= 1.0 and upd.state not in (AttentionState.PAUSED, AttentionState.CALIBRATING):
            now = self.clock()
            issue = upd.state if upd.state != AttentionState.FOCUSED else AttentionState.LOOKING_AWAY
            self.active["episodes"].append({"s": now - upd.distracted_for, "e": now, "issue": issue.value,
                                            "alerted": bool(upd.alerting)})
            self.ctrl.last_update = None

    def _save(self, force: bool = False) -> None:
        now = self.clock()
        if self.active is not None and (force or now - self._last_save > 10):
            _save_json(self._path(self.active["date"]), self.active)
            self._last_save = now

    # ---------------------------------------------------------------- reads
    def _analysis_now(self) -> dict:
        rec = self.active
        plan, subjects = self._record_plan(rec)
        return analyze_day(rec, plan, subjects, self.clock())

    def status(self) -> dict:
        with self.lock:
            self._drain()
            now = self.clock()
            s = load_settings(self.settings_file)
            d = study_date_of(now, s["day_end_hour"])
            plan, subjects = self.plan_for(d)
            name = lambda sid: subjects.get(sid, {}).get("name", "?")
            color = lambda sid: subjects.get(sid, {}).get("color", "#888888")
            out = {"settings": s, "now": now, "study_date": d.isoformat(), "last_concluded": self.last_concluded}
            studies = [x for x in plan if x["kind"] == "study"]
            out["today"] = {"planned": round(sum(x["e"] - x["s"] for x in studies)),
                            "first": ({"name": name(studies[0]["subject"]), "s": studies[0]["s"]} if studies else None),
                            "remaining_blocks": sum(1 for x in studies if x["e"] > now)}
            existing = _load_json(self._path(d.isoformat()), None)
            out["today"]["concluded"] = bool(existing and existing.get("ended_at"))

            rec = self.active
            out["active"] = rec is not None
            if rec is None:
                return out
            plan, subjects = self._record_plan(rec)
            seg = current_segment(plan, now)
            nxt = next((x for x in plan if x["kind"] == "study" and x["s"] >= (seg["e"] if seg else now) - 0.5
                        and x is not seg), None)
            if seg is None:
                free_end = min([x["s"] for x in plan if x["s"] > now] + [rec["window_end"]])
                cur = {"kind": "free", "ends_at": free_end, "remaining": round(free_end - now)}
            else:
                cur = {"kind": seg["kind"], "subject": seg["subject"], "name": name(seg["subject"]),
                       "color": color(seg["subject"]), "starts_at": seg["s"], "ends_at": seg["e"],
                       "remaining": round(seg["e"] - now), "length": round(seg["e"] - seg["s"])}
            a = analyze_day(rec, plan, subjects, now)
            out.update({
                "date": rec["date"], "started_at": rec["started_at"], "auto_end_at": rec["window_end"],
                "current": cur,
                "next": ({"subject": nxt["subject"], "name": name(nxt["subject"]), "color": color(nxt["subject"]),
                          "starts_at": nxt["s"], "length": round(nxt["e"] - nxt["s"])} if nxt else None),
                "manual_break": self.manual_break,
                "monitoring": self.ctrl.running,
                "receiving": self.ctrl.receiving,
                "catchup": ({**self.catchup, "name": name(self.catchup["subject"]),
                             "color": color(self.catchup["subject"])} if self.catchup else None),
                "totals": a["totals"], "subjects": a["subjects"],
                "suggestions": suggestions(a, plan, now, rec["window_end"]),
            })
            return out

    def day_analysis(self, date_str: str) -> dict:
        with self.lock:
            self._drain()
            now = self.clock()
            s = load_settings(self.settings_file)
            d = dt.date.fromisoformat(date_str)
            today = study_date_of(now, s["day_end_hour"])
            if self.active is not None and self.active["date"] == date_str:
                rec = self.active
            else:
                rec = _load_json(self._path(date_str), None)
            if rec is None:                              # never started
                lo, hi = day_window(d, s["day_end_hour"])
                rec = {"date": date_str, "started_at": None, "ended_at": None, "window_start": lo,
                       "window_end": hi, "intervals": [], "episodes": [], "settings": {}}
                status = "upcoming" if d > today else ("not_started" if d == today else "missed")
            else:
                status = "active" if rec.get("ended_at") is None else "concluded"
            plan, subjects = self._record_plan(rec)
            if status in ("upcoming", "not_started"):
                a = analyze_day({**rec, "started_at": 0, "ended_at": None}, plan, subjects,
                                now if status == "not_started" else 0)
            else:
                a = analyze_day(rec, plan, subjects, now)
            a["status"] = status
            return a

    def week_analysis(self, date_str: str) -> dict:
        d = dt.date.fromisoformat(date_str)
        monday = d - dt.timedelta(days=d.weekday())
        days = [self.day_analysis((monday + dt.timedelta(days=i)).isoformat()) for i in range(7)]
        subj: dict[str, dict] = {}
        for day in days:
            for r in day["subjects"]:
                t = subj.setdefault(r["id"], {"id": r["id"], "name": r["name"], "color": r["color"],
                                              **{k: 0 for k in ("planned", "planned_so_far", "studied", "wasted",
                                                                "breaks", "missed", "short", "catchup")}})
                for k in ("planned", "planned_so_far", "studied", "wasted", "breaks", "missed", "short", "catchup"):
                    t[k] += r[k]
        rows = sorted(subj.values(), key=lambda r: (-r["planned"], r["name"]))
        keys = ("planned", "planned_so_far", "studied", "wasted", "breaks", "missed", "short", "catchup")
        return {"week_start": monday.isoformat(), "days": days, "subjects": rows,
                "totals": {k: sum(r[k] for r in rows) for k in keys}}

    def recorded_dates(self) -> list[str]:
        return sorted(f.stem for f in self.days_dir.glob("*.json"))
