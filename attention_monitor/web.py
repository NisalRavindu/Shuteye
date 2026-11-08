"""Web app server: timetable, study days, analysis, and the attention tracker.

    python -m attention_monitor.web            # opens http://127.0.0.1:8765

The browser opens the camera and runs the face model itself (MediaPipe for the
web), then sends only the resulting numbers - head angles, gaze, eyelids - to
this server about 15 times a second. Video never leaves the browser. The server
runs the tracker and scoring and saves everything to the ``data`` folder, so
nothing depends on browser storage.

Uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .accounts import Accounts, AuthError
from .config import PROJECT_ROOT, MonitorConfig
from .observation import FaceObservation
from .session import StudySession
from .studyday import DayManager, load_settings, save_settings
from .tracker import STATE_LABELS, AlertEvent, AttentionState, AttentionTracker, TrackerUpdate

_UI_FILE = Path(__file__).with_name("web_ui.html")
_LOGIN_FILE = Path(__file__).with_name("login.html")
DATA_DIR = PROJECT_ROOT / "data"
COOKIE = "sid"
_STATIC_DIR = Path(__file__).with_name("static")
_STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}
_MODEL_FILE = PROJECT_ROOT / "models" / "face_landmarker.task"
TIMETABLE_FILE = PROJECT_ROOT / "data" / "timetable.json"
SETTINGS_FILE = PROJECT_ROOT / "data" / "settings.json"
_MAX_BODY = 1_000_000
RECEIVING_TIMEOUT_S = 4.0       # no numbers from the browser for this long = not monitoring

# Settings the page may change, with their types.
_SETTINGS = {
    "sound": bool, "use_gaze": bool,
    "away_grace_s": float, "eyes_closed_grace_s": float, "no_face_grace_s": float,
    "repeat_alert_s": float, "yaw_tolerance_deg": float, "pitch_tolerance_deg": float,
    "gaze_tolerance": float, "calibration_seconds": float,
    "calibrate_eyes_closed": bool, "eye_closed_level": float, "volume": int,
    "min_counted_s": float, "restless_count": int, "restless_gap_s": float,
}
_COMMANDS = {"calibrate", "pause", "resume", "toggle_pause", "mute", "unmute", "toggle_mute",
             "camera_off", "camera_on"}


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    tmp.replace(path)                       # atomic: never leaves a half-written file


def config_from(settings: dict) -> MonitorConfig:
    kwargs = {}
    for key, typ in _SETTINGS.items():
        if key in settings and settings[key] is not None:
            kwargs[key] = typ(settings[key])
    if "book_calibration" in settings:
        kwargs["calibration_steps"] = ("screen", "book") if settings["book_calibration"] else ("screen",)
    return MonitorConfig(show_preview=False, **kwargs)


class BrowserMonitor:
    """The server half of a monitoring session, fed with numbers from the browser."""

    def __init__(self, cfg: MonitorConfig, on_alert=None, on_episode=None):
        self.cfg = cfg
        self.tracker = AttentionTracker(cfg)
        self.session = StudySession(cfg.log_dir, cfg)
        self.camera_off = threading.Event()     # set during your own breaks: browser turns the camera off
        self.muted = not cfg.sound
        self.on_alert, self.on_episode = on_alert, on_episode
        self.lock = threading.Lock()
        self.events: deque = deque(maxlen=50)   # for the browser: beeps and calibration cues
        self.last_update: Optional[TrackerUpdate] = None
        self.last_sample_at = 0.0               # time.monotonic() of the last batch
        self.samples = 0
        self._t0: Optional[float] = None        # browser ms of the first sample
        self.wall_t0 = time.time()              # tracker time 0 as wall-clock time
        self._now = -1.0
        self.session.note("start", "browser")

    # ---------------------------------------------------------------- control
    def command(self, name: str) -> None:
        with self.lock:
            now = max(self._now, 0.0)
            if name == "calibrate":
                self.tracker.start_calibration(now)
                self.session.note("recalibrate")
            elif name in ("pause", "resume", "toggle_pause"):
                paused = not self.tracker.paused if name == "toggle_pause" else name == "pause"
                if paused != self.tracker.paused:
                    self.tracker.set_paused(paused)
                    self.session.note("pause" if paused else "resume")
            elif name in ("mute", "unmute", "toggle_mute"):
                self.muted = not self.muted if name == "toggle_mute" else name == "mute"
            elif name in ("camera_off", "camera_on"):
                off = name == "camera_off"
                self.tracker.set_paused(off)
                (self.camera_off.set if off else self.camera_off.clear)()
                self.session.note(name)

    # ---------------------------------------------------------------- data in
    def feed(self, samples: list[dict]) -> Optional[TrackerUpdate]:
        """Process a batch of per-frame numbers: [{t: ms, face: bool, yaw, pitch, ...}]."""
        with self.lock:
            if self.camera_off.is_set() or not samples:
                return self.last_update
            arrived = time.time()
            samples = sorted((s for s in samples if isinstance(s, dict) and "t" in s), key=lambda s: s["t"])
            if not samples:
                return self.last_update
            latest = float(samples[-1]["t"])
            for s in samples:
                t = float(s["t"])
                if self._t0 is None:
                    self._t0 = t
                    self.wall_t0 = arrived - (latest - t) / 1000
                    self.tracker.start_calibration(0.0)
                now = (t - self._t0) / 1000
                if now <= self._now:
                    continue                    # duplicate / out of order
                self._now = now
                obs = _observation(s)
                upd = self.tracker.update(obs, now)
                self.session.record(upd, now)
                self.samples += 1
                if upd.calibration_cue:
                    self.events.append({"type": "cue"})
                if upd.alert is not None:
                    self.events.append({"type": "alert", "issue": upd.alert.issue.value,
                                        "label": STATE_LABELS[upd.alert.issue], "reason": upd.alert.reason,
                                        "beep": not self.muted})
                    if self.on_alert:
                        self.on_alert(upd.alert)
                if upd.episode is not None and self.on_episode:
                    ep = upd.episode
                    self.on_episode({"s": self.wall_t0 + ep.start, "e": self.wall_t0 + ep.end,
                                     "issue": ep.issue.value, "alerted": ep.alerted})
                self.last_update = upd
            self.last_sample_at = time.monotonic()
            return self.last_update

    def take_events(self) -> list[dict]:
        with self.lock:
            out = list(self.events)
            self.events.clear()
            return out


def _num(s: dict, key: str, default: float) -> float:
    try:
        v = float(s.get(key, default))
        return v if v == v else default          # NaN guard
    except (TypeError, ValueError):
        return default


def _observation(s: dict) -> FaceObservation:
    if not s.get("face"):
        return FaceObservation(face_present=False)
    return FaceObservation(face_present=True, yaw=_num(s, "yaw", 0), pitch=_num(s, "pitch", 0),
                           roll=_num(s, "roll", 0), gaze_h=_num(s, "gaze", 0.5),
                           eye_closure=_num(s, "closure", 0), look_down=_num(s, "down", 0),
                           ear=_num(s, "ear", 0.3))


class MonitorController:
    """Owns one user's current monitoring session (quick session or study day)."""

    def __init__(self, settings_file: Optional[Path] = None, log_dir: Optional[Path] = None) -> None:
        self.settings_file = Path(settings_file) if settings_file else SETTINGS_FILE
        self.log_dir = log_dir
        self._lock = threading.Lock()
        self.monitor: Optional[BrowserMonitor] = None
        self.on_episode = None                  # set by DayManager: receives wall-clock episodes
        self.day_mode = False                   # monitor is being driven by a study day
        self.recent_alerts: deque = deque(maxlen=15)
        self.live: dict = {}
        self.summary: Optional[str] = None
        self.error: Optional[str] = None
        self.started_at: Optional[float] = None
        self._last_update: Optional[TrackerUpdate] = None

    # DayManager reads/clears this to record a distraction still in progress
    @property
    def last_update(self) -> Optional[TrackerUpdate]:
        return self._last_update

    @last_update.setter
    def last_update(self, v) -> None:
        self._last_update = v

    @property
    def running(self) -> bool:
        return self.monitor is not None

    @property
    def receiving(self) -> bool:
        """True while the browser is actually sending numbers (camera on, tab alive)."""
        m = self.monitor
        return m is not None and time.monotonic() - m.last_sample_at < RECEIVING_TIMEOUT_S

    def start(self, settings: dict) -> None:
        with self._lock:
            if self.monitor is not None:
                return
            stored = _read_json(self.settings_file, {})
            cfg = config_from({**stored, **(settings or {})})
            if self.log_dir is not None:
                cfg.log_dir = self.log_dir
            self.monitor = BrowserMonitor(cfg, on_alert=self._on_alert, on_episode=self._forward_episode)
            self.live, self.summary, self.error = {}, None, None
            self.recent_alerts.clear()
            self.started_at = time.monotonic()
            self._last_update = None

    def stop(self) -> None:
        with self._lock:
            m, self.monitor = self.monitor, None
            if m is not None:
                with m.lock:
                    self.summary = m.session.summary()
                    m.session.close()

    def command(self, name: str) -> None:
        if name in _COMMANDS and self.monitor is not None:
            self.monitor.command(name)

    def feed(self, samples: list) -> dict:
        m = self.monitor
        if m is None:
            return {"events": [], "running": False}
        upd = m.feed(samples)
        if upd is not None:
            self._last_update = upd
            self._update_live(m, upd)
        return {"events": m.take_events(), "running": True, "camera_off": m.camera_off.is_set(),
                "state": self.live.get("state")}

    # ----------------------------------------------------------- callbacks
    def _forward_episode(self, ep: dict) -> None:
        if self.on_episode is not None:
            self.on_episode(ep)

    def _on_alert(self, e: AlertEvent) -> None:
        self.recent_alerts.appendleft({
            "time": datetime.now().strftime("%H:%M:%S"),
            "issue": e.issue.value, "label": STATE_LABELS[e.issue],
            "reason": e.reason, "seconds": round(e.duration_s, 1),
        })

    def _update_live(self, m: BrowserMonitor, upd: TrackerUpdate) -> None:
        session, t = m.session, m.session.time_in
        self.live = {
            "state": upd.state.value, "label": STATE_LABELS[upd.state], "reason": upd.reason,
            "distracted_for": round(upd.distracted_for, 1), "grace_s": upd.grace_s,
            "alerting": upd.alerting, "calibration_progress": round(upd.calibration_progress, 2),
            "closedness": round(upd.closedness, 2),
            "focus": round(session.focus_ratio, 3), "alerts": session.alerts,
            "tracked_s": round(session.tracked_seconds),
            "focused_s": round(t[AttentionState.FOCUSED]),
            "away_s": round(t[AttentionState.LOOKING_AWAY]),
            "closed_s": round(t[AttentionState.EYES_CLOSED]),
            "absent_s": round(t[AttentionState.NO_FACE]),
            "wasted_s": round(session.wasted_seconds),
            "ignored": session.score.ignored,
            "ignored_s": round(session.score.ignored_s),
            "restless_spells": len(session.score.restless_spells),
            "min_counted_s": session.cfg.min_counted_s,
            "paused": m.tracker.paused,
            "muted": m.muted,
        }

    # ----------------------------------------------------------- reads
    def status(self) -> dict:
        m = self.monitor
        running = m is not None
        return {
            "running": running,
            "receiving": self.receiving,
            "day_mode": self.day_mode,
            "camera_off": bool(m and m.camera_off.is_set()),
            "uptime_s": round(time.monotonic() - self.started_at) if running and self.started_at else 0,
            **(self.live if running else {}),
            "recent_alerts": list(self.recent_alerts),
            "summary": self.summary, "error": self.error,
        }


class UserSpace:
    """Everything that belongs to one account: files, live monitor, study days."""

    def __init__(self, name: str, root: Path):
        self.name, self.root = name, Path(root)
        self.timetable_file = self.root / "timetable.json"
        self.settings_file = self.root / "settings.json"
        self.ctrl = MonitorController(settings_file=self.settings_file, log_dir=self.root / "logs")
        self.days = DayManager(self.ctrl, paths={"days": self.root / "days", "timetable": self.timetable_file,
                                                 "settings": self.root / "study_settings.json"})


class App:
    """`local_user` = desktop mode: one person on this computer, no logins.
    Otherwise: hosted mode with accounts (same code, so it can scale out later)."""

    def __init__(self, data_dir: Path = DATA_DIR, invite_code: Optional[str] = None,
                 local_user: Optional[str] = None):
        self.accounts = Accounts(data_dir, invite_code)
        self.local_user = local_user
        self.allowed_hosts: set[str] = set()     # local mode: filled in once the port is known
        self._spaces: dict[str, UserSpace] = {}
        self._lock = threading.Lock()
        if local_user:
            self.space(local_user)
        for name in list(self.accounts.users):       # start everyone's day tracker (auto-ends need it)
            self.space(name)

    def space(self, user: str) -> UserSpace:
        with self._lock:
            if user not in self._spaces:
                self._spaces[user] = UserSpace(user, self.accounts.user_dir(user))
            return self._spaces[user]

    def stop_all(self) -> None:
        for sp in list(self._spaces.values()):
            sp.ctrl.stop()


def make_handler(app: App):
    accounts = app.accounts

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        # ---------------------------------------------------------- helpers
        def _send(self, body: bytes, ctype: str, code: int = 200, cache: str = "no-store", headers=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200, headers=()):
            self._send(json.dumps(obj).encode(), "application/json", code, headers=headers)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY:
                raise ValueError("too large")
            return json.loads(self.rfile.read(length) or b"{}")

        def _token(self) -> Optional[str]:
            c = SimpleCookie()
            try:
                c.load(self.headers.get("Cookie") or "")
            except Exception:
                return None
            return c[COOKIE].value if COOKIE in c else None

        def _https(self) -> bool:
            return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

        def _cookie(self, token: Optional[str]) -> tuple:
            secure = "; Secure" if self._https() else ""
            if token is None:
                return ("Set-Cookie", f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}")
            return ("Set-Cookie", f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000{secure}")

        def _client(self) -> str:
            fwd = self.headers.get("X-Forwarded-For")
            return fwd.split(",")[0].strip() if fwd else self.client_address[0]

        def _same_origin(self) -> bool:
            """Reject writes coming from other websites (CSRF)."""
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
            return urlparse(origin).netloc == host

        def _user(self) -> Optional[UserSpace]:
            if app.local_user:
                return app.space(app.local_user)
            name = accounts.user_for(self._token())
            return app.space(name) if name else None

        def _host_ok(self) -> bool:
            """Local mode has no login, so only answer requests addressed to this machine
            (blocks DNS-rebinding tricks by other websites)."""
            if not app.local_user:
                return True
            if (self.headers.get("Host") or "") in app.allowed_hosts:
                return True
            self._json({"error": "forbidden host"}, 403)
            return False

        # ---------------------------------------------------------- public routes
        def _public_get(self, path) -> bool:
            if path == "/login" and app.local_user:
                self._send(b"", "text/plain", 302, headers=[("Location", "/")])
            elif path == "/login":
                self._send(_LOGIN_FILE.read_bytes(), "text/html; charset=utf-8", cache="no-cache")
            elif path == "/api/auth/info":
                if app.local_user:
                    self._json({"app": "attention-monitor", "local": True, "user": app.local_user})
                else:
                    self._json({"app": "attention-monitor", "local": False, **accounts.info(),
                                "user": accounts.user_for(self._token())})
            elif path == "/models/face_landmarker.task" and _MODEL_FILE.is_file():
                self._send(_MODEL_FILE.read_bytes(), "application/octet-stream", cache="max-age=86400")
            elif path.startswith("/static/"):
                f = _STATIC_DIR / Path(path[len("/static/"):]).name   # no directory traversal
                if f.suffix in _STATIC_TYPES and f.is_file():
                    self._send(f.read_bytes(), _STATIC_TYPES[f.suffix], cache="no-cache")
                else:
                    self._json({"error": "not found"}, 404)
            else:
                return False
            return True

        def _auth_post(self, path, data):
            try:
                if path == "/api/auth/login":
                    token = accounts.login(data.get("username", ""), data.get("password", ""), self._client())
                elif path == "/api/auth/register":
                    token = accounts.register(data.get("username", ""), data.get("password", ""),
                                              data.get("invite", ""))
                elif path == "/api/auth/logout":
                    accounts.logout(self._token())
                    return self._json({"ok": True}, headers=[self._cookie(None)])
                elif path == "/api/auth/password":
                    user = accounts.user_for(self._token())
                    if not user:
                        return self._json({"error": "Please log in."}, 401)
                    accounts.change_password(user, data.get("old", ""), data.get("new", ""))
                    token = accounts.login(user, data.get("new", ""), self._client())
                else:
                    return self._json({"error": "not found"}, 404)
            except AuthError as exc:
                return self._json({"error": str(exc)}, 400)
            user = accounts.user_for(token)
            app.space(user)
            return self._json({"ok": True, "user": user}, headers=[self._cookie(token)])

        # ---------------------------------------------------------- GET
        def do_GET(self):
            if not self._host_ok():
                return
            path = self.path.split("?")[0]
            if self._public_get(path):
                return
            sp = self._user()
            if sp is None:
                if path.startswith("/api/"):
                    return self._json({"error": "Please log in."}, 401)
                return self._send(b"", "text/plain", 302, headers=[("Location", "/login")])
            query = parse_qs(urlparse(self.path).query)
            if path.startswith("/api/day") or path.startswith("/api/analysis"):
                return self._day_get(sp, path, query)
            if path == "/":
                return self._send(_UI_FILE.read_bytes(), "text/html; charset=utf-8", cache="no-cache")
            if path == "/api/status":
                return self._json(sp.ctrl.status())
            if path == "/api/timetable":
                return self._json(_read_json(sp.timetable_file, {}))
            if path == "/api/settings":
                return self._json(_read_json(sp.settings_file, {}))
            self._json({"error": "not found"}, 404)

        # ---------------------------------------------------------- study day
        def _day_get(self, sp: UserSpace, path, query):
            days = sp.days
            date = (query.get("date") or [None])[0]
            try:
                if path == "/api/day":
                    return self._json(days.status())
                if path == "/api/day/settings":
                    return self._json(load_settings(days.settings_file))
                if path == "/api/analysis/dates":
                    return self._json(days.recorded_dates())
                if path in ("/api/analysis/day", "/api/analysis/week"):
                    if not date:
                        date = days.status()["study_date"]
                    fn = days.day_analysis if path.endswith("day") else days.week_analysis
                    return self._json(fn(date))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"error": "not found"}, 404)

        def _day_post(self, sp: UserSpace, path, data):
            days = sp.days
            if path == "/api/day/start":
                days.start_day(data)
            elif path == "/api/day/end":
                days.end_day("manual")
            elif path == "/api/day/break":
                days.set_break(bool(data.get("on")))
            elif path == "/api/day/catchup":
                days.set_catchup(data.get("subject"))
            elif path == "/api/day/settings":
                return self._json(save_settings(data, days.settings_file))
            else:
                return self._json({"error": "not found"}, 404)
            return self._json(days.status())

        # ---------------------------------------------------------- PUT / POST
        def _write_prelude(self):
            """Origin check + body parse shared by PUT/POST. Returns (data, path) or None."""
            if not self._host_ok():
                return None
            if not self._same_origin():
                self._json({"error": "cross-site request refused"}, 403)
                return None
            try:
                data = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, 400)
                return None
            return data, self.path.split("?")[0]

        def do_PUT(self):
            pre = self._write_prelude()
            if pre is None:
                return
            data, path = pre
            sp = self._user()
            if sp is None:
                return self._json({"error": "Please log in."}, 401)
            if path != "/api/timetable":
                return self._json({"error": "not found"}, 404)
            if not isinstance(data, dict):
                return self._json({"error": "expected an object"}, 400)
            _write_json(sp.timetable_file, data)
            self._json({"ok": True})

        def do_POST(self):
            pre = self._write_prelude()
            if pre is None:
                return
            data, path = pre
            if path.startswith("/api/auth/"):
                if app.local_user:
                    return self._json({"error": "No accounts in the desktop app."}, 404)
                return self._auth_post(path, data if isinstance(data, dict) else {})
            sp = self._user()
            if sp is None:
                return self._json({"error": "Please log in."}, 401)
            if not isinstance(data, dict):
                return self._json({"error": "expected an object"}, 400)
            if path.startswith("/api/day"):
                return self._day_post(sp, path, data)
            ctrl = sp.ctrl
            try:
                if path == "/api/frames":
                    return self._json(ctrl.feed(data.get("samples") or []))
                if path == "/api/settings":
                    merged = {**_read_json(sp.settings_file, {}), **data}
                    _write_json(sp.settings_file, merged)
                    return self._json(merged)
                if path == "/api/start":
                    if ctrl.day_mode:
                        return self._json({"error": "A study day is running"}, 409)
                    ctrl.start(data)
                elif path == "/api/stop":
                    if ctrl.day_mode:
                        return self._json({"error": "End the study day instead"}, 409)
                    ctrl.stop()
                elif path == "/api/command":
                    ctrl.command(str(data.get("name", "")))
                else:
                    return self._json({"error": "not found"}, 404)
            except (TypeError, ValueError) as exc:
                return self._json({"error": str(exc)}, 400)
            self._json(ctrl.status())

    return Handler


class Server(ThreadingHTTPServer):
    # On Windows, address reuse lets a second copy bind the same port silently and
    # requests then hit either one at random. Refuse instead.
    allow_reuse_address = os.name != "nt"


def make_server(host: str, port: int, app: App) -> "Server":
    """Bind (raises OSError if the port is taken) and wire up the app."""
    server = Server((host, port), make_handler(app))
    server.daemon_threads = True
    if app.local_user:
        p = server.server_address[1]
        app.allowed_hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
    return server


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          invite_code: Optional[str] = None, data_dir: Path = DATA_DIR) -> None:
    app = App(data_dir, invite_code)
    try:
        server = make_server(host, port, app)
    except OSError:
        app.stop_all()
        raise SystemExit(f"Port {port} is already in use - is the app already running? "
                         f"Close the other copy, or start this one with --port {port + 1}.")
    server.daemon_threads = True
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    print(f"Attention Monitor: {url}  (Ctrl+C to quit)")
    if app.accounts.info()["first_user"]:
        print("No accounts yet: the first account you create is the owner and gets the existing data.")
    print("Sign-ups for others: " + ("open with your invite code." if app.accounts.invite_code
                                     else "closed (start with --invite-code CODE to allow them)."))
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_all()
        server.server_close()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="attention_monitor.web")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    p.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    p.add_argument("--invite-code", default=os.environ.get("ATTENTION_INVITE_CODE"),
                   help="code others need to create an account (default: sign-ups closed)")
    p.add_argument("--data-dir", type=Path, default=Path(os.environ.get("ATTENTION_DATA_DIR", DATA_DIR)),
                   help="where accounts and everyone's data are stored")
    a = p.parse_args(argv)
    serve(a.host, a.port, not a.no_browser, a.invite_code, a.data_dir)


if __name__ == "__main__":
    main()
