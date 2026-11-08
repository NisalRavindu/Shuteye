# Attention Monitor

A study companion that watches your webcam and beeps when you lose focus. It follows your timetable through the day and shows you where your study time actually went.

- **Attention tracking:** looking away, eyes closed, leaving the desk, and "restless" bursts of quick glances. Each has its own grace period and beep.
- **Timetable:** a weekly planner you fill by painting slots with subjects.
- **Study days:** shows the current subject, the time left in it and what's next. Breaks between subjects are added automatically, catch-up time is suggested for anything you missed, and a day you forget to end closes automatically.
- **Analysis:** studied / wasted / own breaks / missed time per subject, for each day and each week.
- **Privacy:** the camera is processed in the browser, and **video never leaves your computer**.

## Three ways to run it

| | Who it's for | Data lives in |
|---|---|---|
| **Desktop app** (`AttentionMonitor.exe`) | One person on their own PC; no login | `%APPDATA%\AttentionMonitor\data` |
| **Server with accounts** (`python -m attention_monitor.web`) | Several people, each with their own login | `data/` next to the code |
| **Local OpenCV version** (`python -m attention_monitor`) | A plain video window, no web page | `sessions/` (CSV logs only) |

### 1. Desktop app

Download or build `AttentionMonitor.exe` and double-click it. It starts a small server on your computer, opens the app in your browser, and shows a window with **Open in browser**, **Data folder** and **Quit**. Keep that window open while you study. Opening the program again just reopens the running copy.

- It needs an internet connection the first time it loads the face model (from cdn.jsdelivr.net), and on any later load the browser hasn't cached.
- The program isn't code-signed, so the first time Windows says *"Windows protected your PC"*. Click **More info → Run anyway**.
- To build it yourself (Python 3.11+):
  ```powershell
  python -m pip install -r requirements-build.txt
  python build_exe.py            # -> dist/AttentionMonitor.exe (~15 MB)
  ```
  Or run it without building: `python -m attention_monitor.desktop`.

### 2. Server with accounts

```powershell
python -m attention_monitor.web                                # http://127.0.0.1:8765
python -m attention_monitor.web --invite-code some-secret-words   # let friends sign up
```

- **First account = owner.** It needs no code, and any data from before accounts existed moves into it.
- **Everyone else needs your invite code.** Without one, sign-ups stay closed.
- **Separate data:** each person's timetable, settings, days and live session live under `data/users/<name>/`.
- **Security:**
  - Passwords are stored only as salted PBKDF2 hashes.
  - Login cookies are random and HttpOnly, last 30 days, and are stored only as hashes.
  - After 5 wrong passwords, that account is locked for a minute.
  - Writes from other websites are refused.
- **Configuration:** `--host`, `--port`, `--data-dir`, `--invite-code`, or the environment variables `HOST`, `PORT`, `ATTENTION_DATA_DIR` and `ATTENTION_INVITE_CODE`.

### 3. Local OpenCV version

```powershell
python -m pip install -r requirements.txt
python -m attention_monitor --help
```

A video window with on-screen status, using the same tracking rules. Keys: **C** recalibrate · **P** pause · **M** mute · **Q** quit.

## How it works

**Calibration** takes 2 positions and about 12 seconds:
1. Look at your screen. At the **beep**, close your eyes until the **next beep**.
2. Look down at your book.

Any head position between or around those two counts as focused. The closed-eye sample sets the eyes-closed threshold between *your* open and closed eyes.

**Alerts** (defaults, all adjustable under ⚙ Settings):

| State | Detected when | Alert after |
|---|---|---|
| Looking away | head turned/tilted beyond your calibrated range, or eyes off to the side | 4 s |
| Eyes closed | eyes shut | 3 s |
| Not at camera | no face in view | 5 s |
| Restless | 4 quick breaks in a row, each within 30 s of the last | at once |

**Wasted time:**
- A break of 10 s or more counts in full.
- A shorter isolated break (grabbing a pen) is ignored, unless it's part of a restless burst.
- Your own breaks (camera off) aren't wasted, but they still count against the day's goal.

**Study days** run until the day-end hour (default 04:00 the next morning), so late-night study counts for the day you started. The app's **How it works** tab explains every rule using your current settings.

## Architecture

```
browser (static/camera.js)                    server (Python standard library only)
  camera -> MediaPipe FaceLandmarker            web.py        HTTP API, accounts / local mode
  -> head angles, gaze, eyelids  --numbers-->   tracker.py    calibration, states, alerts
  <- alert events (beeps play here)             session.py    wasted-time scoring
                                                studyday.py   timetable plan, study days, analysis
                                                accounts.py   users, hashed passwords, sessions
                                                desktop.py    desktop launcher (local mode)
```

- **The browser** sends about 15 small batches of numbers per second, roughly 3 KB/s.
- **The server** needs about 0.1% of a CPU core per person studying. Everything it saves is plain JSON per user, behind one place in the code (the paths in `UserSpace` and `DayManager`), so moving to a database later means replacing that layer.

## Hosting later

The server already supports multiple users, so hosting it means deploying `python -m attention_monitor.web` with:
- **HTTPS:** browsers only allow the camera on HTTPS sites (or on localhost). Behind an HTTPS proxy, send `X-Forwarded-Proto: https` so the login cookie is marked `Secure`.
- **A persistent disk** for `ATTENTION_DATA_DIR`, plus backups of it.
- **One always-on instance:** live sessions and the day-end timer run in memory.
- **To do first:** per-user time zones. Study days currently use the server's clock, which is fine on your own PC but not on a server running in UTC.

## Development

```powershell
python -m unittest discover -s tests -t .
```

The tests cover the tracker, scoring, study days (simulated full days with a fake clock), the browser-fed monitor, accounts, desktop mode, and the HTTP API.
