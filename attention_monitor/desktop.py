"""Desktop app: runs the server on this computer and opens it in your browser.

    python -m attention_monitor.desktop          # or the packaged AttentionMonitor.exe

* No logins: one local user, and the server only answers this computer.
* Data lives in your user folder (Windows: %APPDATA%\\AttentionMonitor), so it
  survives updates and moving the program.
* A small window shows that it's running, with Open / Data folder / Quit.
  Starting it a second time just opens the running copy.
* The camera runs in the browser; browsers allow it on http://127.0.0.1 without HTTPS.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

from .web import App, make_server

APP_NAME = "AttentionMonitor"
LOCAL_USER = "local"
PORTS = range(8765, 8785)


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME / "data"


def running_copy(port: int) -> bool:
    """Is our desktop app already answering on this port?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/auth/info", timeout=0.6) as r:
            info = json.loads(r.read())
        return info.get("app") == "attention-monitor" and bool(info.get("local"))
    except Exception:
        return False


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 - opening a local folder in Explorer
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def start(data_dir: Path, port: Optional[int] = None):
    """Start the server on the first free port. Returns (server, url) or (None, url of running copy)."""
    candidates = [port] if port else list(PORTS)
    for p in candidates:
        if running_copy(p):
            return None, f"http://127.0.0.1:{p}/"
    app = App(data_dir, local_user=LOCAL_USER)
    for p in candidates:
        try:
            server = make_server("127.0.0.1", p, app)
        except OSError:
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        server.app = app
        return server, f"http://127.0.0.1:{server.server_address[1]}/"
    app.stop_all()
    raise SystemExit(f"No free port between {candidates[0]} and {candidates[-1]}.")


def control_window(url: str, data_dir: Path, on_quit) -> None:
    """Small always-available window. Falls back to waiting if tkinter is unavailable."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        print(f"Attention Monitor is running at {url}  (Ctrl+C to quit)")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        on_quit()
        return

    root = tk.Tk()
    root.title("Attention Monitor")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=16)
    frm.grid()
    ttk.Label(frm, text="Attention Monitor is running", font=("Segoe UI", 12, "bold")).grid(columnspan=3, sticky="w")
    ttk.Label(frm, text=f"Open it in your browser at {url}\nKeep this window open while you study.",
              justify="left").grid(columnspan=3, sticky="w", pady=(4, 12))
    ttk.Button(frm, text="Open in browser", command=lambda: webbrowser.open(url)).grid(row=2, column=0, padx=(0, 6))
    ttk.Button(frm, text="Data folder", command=lambda: open_folder(data_dir.parent)).grid(row=2, column=1, padx=6)

    def quit_app():
        root.destroy()
        on_quit()

    ttk.Button(frm, text="Quit", command=quit_app).grid(row=2, column=2, padx=(6, 0))
    root.protocol("WM_DELETE_WINDOW", quit_app)
    root.mainloop()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog=APP_NAME)
    p.add_argument("--data-dir", type=Path, default=default_data_dir(), help="where your data is stored")
    p.add_argument("--port", type=int, default=None, help="fixed port (default: first free from 8765)")
    p.add_argument("--no-browser", action="store_true", help="don't open the browser")
    p.add_argument("--no-window", action="store_true", help="no control window; run until Ctrl+C")
    a = p.parse_args(argv)

    server, url = start(a.data_dir, a.port)
    if not a.no_browser:
        webbrowser.open(url)
    if server is None:
        return                                    # another copy is already running; we just opened it

    def shutdown():
        server.app.stop_all()
        server.shutdown()
        server.server_close()

    if a.no_window:
        print(f"Attention Monitor is running at {url}  (data: {a.data_dir})  Ctrl+C to quit")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        shutdown()
    else:
        control_window(url, a.data_dir, shutdown)


if __name__ == "__main__":
    main()
