"""Build the desktop app into a single executable.

    python -m pip install -r requirements-build.txt
    python build_exe.py                 # -> dist/AttentionMonitor.exe (Windows)

The packaged app contains only the standard-library server, the web page and
the face model; the camera and the model run in the browser.
"""
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = ROOT / "models" / "face_landmarker.task"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")
SEP = ";" if os.name == "nt" else ":"


def main() -> None:
    if not MODEL.exists():
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        print("Downloading the face model...")
        urllib.request.urlretrieve(MODEL_URL, MODEL)

    data = [
        ("attention_monitor/static", "attention_monitor/static"),
        ("attention_monitor/web_ui.html", "attention_monitor"),
        ("attention_monitor/login.html", "attention_monitor"),
        ("models/face_landmarker.task", "models"),
    ]
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
           "--name", "AttentionMonitor",
           # the desktop app never uses the local OpenCV version
           "--exclude-module", "cv2", "--exclude-module", "mediapipe", "--exclude-module", "numpy",
           "--exclude-module", "attention_monitor.monitor", "--exclude-module", "attention_monitor.detector"]
    for src, dst in data:
        cmd += ["--add-data", f"{ROOT / src}{SEP}{dst}"]
    cmd.append(str(ROOT / "run_desktop.py"))
    subprocess.run(cmd, check=True, cwd=ROOT)
    exe = ROOT / "dist" / ("AttentionMonitor.exe" if os.name == "nt" else "AttentionMonitor")
    print(f"\nBuilt {exe}  ({exe.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
