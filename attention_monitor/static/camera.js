/* Browser half of the monitor.
 *
 * Opens the camera, runs MediaPipe's FaceLandmarker on each (mirrored) frame and
 * turns the result into a few numbers - head angles, gaze, eyelids - exactly as
 * attention_monitor/detector.py does. Only those numbers are sent to the server
 * (~15 per second, in small batches). Video never leaves this computer.
 * The server answers with alert events, which are played here as beeps.
 */
import { FaceLandmarker, FilesetResolver } from "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs";

const WASM = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm";
const MODEL = "/models/face_landmarker.task";
const FPS = 15;                 // analyses per second
const SEND_MS = 200;            // batch interval
const MAX_BUFFER = 150;         // keep at most ~10 s of numbers if the server is unreachable

// Landmark indices (MediaPipe 478-point face mesh) - same as detector.py
const EYE_A = [33, 362], EYE_B = [133, 263], IRIS = [468, 473];
const EAR_PTS = [[33, 160, 158, 133, 153, 144], [362, 385, 387, 263, 373, 380]];
const OUTLINE = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377,
                 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109];
const STATE_VAR = { focused: "--focused", looking_away: "--away", eyes_closed: "--closed", no_face: "--absent",
                    calibrating: "--calib", paused: "--paused" };
const BEEPS = {
  looking_away: [[880, 180], [660, 220]], eyes_closed: [[1200, 120], [1200, 120], [1200, 120]],
  no_face: [[520, 300], [390, 400]], restless: [[600, 120], [800, 120], [1000, 200]], cue: [[660, 120], [990, 250]],
};

const $ = (id) => document.getElementById(id);
const canvas = $("cam");
const ctx = canvas.getContext("2d", { willReadFrequently: false });
const video = Object.assign(document.createElement("video"), { playsInline: true, muted: true, autoplay: true });

let landmarker = null, modelPromise = null, modelError = null;
let stream = null, starting = false, camError = null;
let wanted = false, lastStatus = null;
let buffer = [], lastSent = 0, lastAnalysis = 0, sending = false, lastT = 0;
let liveState = "focused", sendError = false;
let frames = 0, fpsStart = performance.now(), fps = 0;

// ------------------------------------------------------------------ model & camera
function loadModel() {
  if (!modelPromise) {
    modelPromise = (async () => {
      const files = await FilesetResolver.forVisionTasks(WASM);
      for (const delegate of ["GPU", "CPU"]) {
        try {
          landmarker = await FaceLandmarker.createFromOptions(files, {
            baseOptions: { modelAssetPath: MODEL, delegate },
            runningMode: "VIDEO", numFaces: 1,
            outputFaceBlendshapes: true, outputFacialTransformationMatrixes: true,
            minFaceDetectionConfidence: 0.5, minFacePresenceConfidence: 0.5, minTrackingConfidence: 0.5,
          });
          return landmarker;
        } catch (e) { if (delegate === "CPU") throw e; }
      }
    })().catch((e) => { modelError = "Couldn't load the face model. Check your internet connection and reload."; modelPromise = null; throw e; });
  }
  return modelPromise;
}

async function startCamera() {
  if (stream || starting) return;
  starting = true; camError = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: "user" }, audio: false });
    video.srcObject = stream;
    await video.play();
  } catch (e) {
    stream = null;
    camError = e && e.name === "NotAllowedError"
      ? "Camera access was blocked. Allow the camera for this site (address-bar icon), then reload."
      : `Couldn't open the camera${e && e.name ? ` (${e.name})` : ""}. Is another app using it?`;
  } finally { starting = false; render(); }
}

function stopCamera() {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  video.srcObject = null;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (buffer.length) send();
}

// ------------------------------------------------------------------ geometry (mirrors detector.py)
function rotationRows(d) {
  // MediaPipe packs the 4x4 matrix column by column; detect it via the [0,0,0,1] last row.
  const colMajor = Math.abs(d[3]) + Math.abs(d[7]) + Math.abs(d[11]) < 1e-6;
  const at = (r, c) => (colMajor ? d[c * 4 + r] : d[r * 4 + c]);
  return [[at(0, 0), at(0, 1), at(0, 2)], [at(1, 0), at(1, 1), at(1, 2)], [at(2, 0), at(2, 1), at(2, 2)]];
}

/** Euler angles (x, y, z) in degrees, identical to OpenCV's cv2.RQDecomp3x3. */
function rqAngles(M) {
  const mul = (A, B) => A.map((row) => [0, 1, 2].map((j) => row[0] * B[0][j] + row[1] * B[1][j] + row[2] * B[2][j]));
  const giv = (s, c) => { const z = 1 / Math.sqrt(c * c + s * s + 2.220446049250313e-16); return [s * z, c * z]; };
  let [s, c] = giv(M[2][1], M[2][2]);
  const Qx = [[1, 0, 0], [0, c, s], [0, -s, c]];
  let R = mul(M, Qx);
  [s, c] = giv(-R[2][0], R[2][2]);
  const Qy = [[c, 0, -s], [0, 1, 0], [s, 0, c]];
  const M2 = mul(R, Qy);
  [s, c] = giv(M2[1][0], M2[1][1]);
  const Qz = [[c, s, 0], [-s, c, 0], [0, 0, 1]];
  R = mul(M2, Qz);
  // OpenCV's ambiguity fix: keep R's first two diagonal entries positive.
  if (R[0][0] < 0) {
    if (R[1][1] < 0) { Qz[0][0] *= -1; Qz[0][1] *= -1; Qz[1][0] *= -1; Qz[1][1] *= -1; }
    else { Qz[0][0] *= -1; Qz[0][1] *= -1; Qz[2][0] *= -1; Qz[2][1] *= -1; }
  } else if (R[1][1] < 0) { Qz[1][0] *= -1; Qz[1][1] *= -1; Qz[2][0] *= -1; Qz[2][1] *= -1; }
  const deg = 180 / Math.PI, cl = (v) => Math.max(-1, Math.min(1, v));
  return [Math.acos(cl(Qx[1][1])) * (Qx[1][2] >= 0 ? 1 : -1) * deg,
          Math.acos(cl(Qy[0][0])) * (Qy[2][0] >= 0 ? 1 : -1) * deg,
          Math.acos(cl(Qz[0][0])) * (Qz[0][1] >= 0 ? 1 : -1) * deg];
}
const wrap = (a) => ((a + 180) % 360 + 360) % 360 - 180;
const r4 = (v) => Math.round(v * 1e4) / 1e4;

function features(res, w, h) {
  if (!res.faceLandmarks || !res.faceLandmarks.length) return { face: false };
  const lm = res.faceLandmarks[0];
  const P = (i) => [lm[i].x * w, lm[i].y * h];
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1];
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

  let yaw = 0, pitch = 0, roll = 0;
  const m = res.facialTransformationMatrixes && res.facialTransformationMatrixes[0];
  if (m && m.data && m.data.length >= 16) [pitch, yaw, roll] = rqAngles(rotationRows(m.data)).map(wrap);

  const b = {};
  for (const c of (res.faceBlendshapes && res.faceBlendshapes[0] && res.faceBlendshapes[0].categories) || []) b[c.categoryName] = c.score;
  const closure = ((b.eyeBlinkLeft || 0) + (b.eyeBlinkRight || 0)) / 2;
  const down = ((b.eyeLookDownLeft || 0) + (b.eyeLookDownRight || 0)) / 2;

  let gaze = 0.5;
  if (lm.length > Math.max(...IRIS)) {
    const ratios = [];
    for (let k = 0; k < 2; k++) {
      const a = P(EYE_A[k]), axis = sub(P(EYE_B[k]), a), den = dot(axis, axis);
      if (den > 1e-6) ratios.push(dot(sub(P(IRIS[k]), a), axis) / den);
    }
    if (ratios.length) gaze = ratios.reduce((x, y) => x + y) / ratios.length;
  }
  const ears = [];
  for (const [p1, p2, p3, p4, p5, p6] of EAR_PTS) {
    const width = dist(P(p1), P(p4));
    if (width > 1e-3) ears.push((dist(P(p2), P(p6)) + dist(P(p3), P(p5))) / (2 * width));
  }
  const ear = ears.length ? ears.reduce((x, y) => x + y) / ears.length : 0.3;
  return { face: true, yaw: r4(yaw), pitch: r4(pitch), roll: r4(roll), gaze: r4(gaze),
           closure: r4(closure), down: r4(down), ear: r4(ear) };
}

// ------------------------------------------------------------------ loop
function analyze(now) {
  const w = video.videoWidth, h = video.videoHeight;
  if (!w || !h) return;
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  ctx.setTransform(-1, 0, 0, 1, w, 0);            // mirror, like the Python version
  ctx.drawImage(video, 0, 0, w, h);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  const t = Math.max(Math.round(now), lastT + 1);   // strictly increasing for VIDEO mode
  lastT = t;
  let res;
  try { res = landmarker.detectForVideo(canvas, t); } catch { return; }
  const s = features(res, w, h);
  s.t = t;
  buffer.push(s);
  if (buffer.length > MAX_BUFFER) buffer.splice(0, buffer.length - MAX_BUFFER);
  if (res.faceLandmarks && res.faceLandmarks.length) drawOutline(res.faceLandmarks[0], w, h);
  frames++;
  if (now - fpsStart > 2000) { fps = frames * 1000 / (now - fpsStart); frames = 0; fpsStart = now; }
}

function drawOutline(lm, w, h) {
  const color = getComputedStyle(document.documentElement).getPropertyValue(STATE_VAR[liveState] || "--focused").trim() || "#4cc472";
  ctx.strokeStyle = color;
  ctx.lineWidth = Math.max(1.5, w / 400);
  ctx.beginPath();
  OUTLINE.forEach((i, k) => { const x = lm[i].x * w, y = lm[i].y * h; k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.closePath();
  ctx.stroke();
}

function tick() {
  const now = performance.now();
  if (stream && landmarker && video.readyState >= 2 && now - lastAnalysis >= 1000 / FPS - 2) {
    lastAnalysis = now;
    analyze(now);
  }
  if (buffer.length && now - lastSent >= SEND_MS) send();
}
(function raf() { tick(); requestAnimationFrame(raf); })();
// Hidden tabs pause requestAnimationFrame; a worker timer keeps analysis going (at a reduced rate).
const ticker = new Worker(URL.createObjectURL(new Blob(["setInterval(() => postMessage(0), 100)"], { type: "text/javascript" })));
ticker.onmessage = () => { if (document.hidden) tick(); };

async function send() {
  if (sending || !buffer.length) return;
  sending = true;
  lastSent = performance.now();
  const batch = buffer.splice(0, buffer.length);
  try {
    const r = await fetch("/api/frames", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify({ samples: batch }) });
    const d = await r.json();
    sendError = false;
    if (d.state) liveState = d.state;
    for (const e of d.events || []) {
      if (e.type === "cue") beep("cue");
      else if (e.type === "alert" && e.beep) beep(e.issue);
    }
    if (d.running === false || d.camera_off) { wanted = false; stopCamera(); }
  } catch {
    sendError = true;
    buffer.unshift(...batch.slice(-MAX_BUFFER));    // retry with the next batch
  } finally {
    sending = false;
  }
}

// ------------------------------------------------------------------ sound
let actx = null;
function audio() {
  if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
  if (actx.state === "suspended") actx.resume();
  return actx;
}
document.addEventListener("pointerdown", () => { try { audio(); } catch {} });   // browsers need a click first

function beep(kind) {
  const vol = Number(($("volume") || {}).value ?? 70);
  if (!(vol > 0)) return;
  let ac;
  try { ac = audio(); } catch { return; }
  const peak = Math.pow(vol / 100, 1.6) * 0.5;
  let t = ac.currentTime + 0.02;
  for (const [f, ms] of BEEPS[kind] || BEEPS.looking_away) {
    const d = ms / 1000, o = ac.createOscillator(), g = ac.createGain();
    o.type = "sine"; o.frequency.value = f;
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(peak, t + 0.006);
    g.gain.setValueAtTime(peak, t + d - 0.006);
    g.gain.linearRampToValueAtTime(0, t + d);
    o.connect(g).connect(ac.destination);
    o.start(t); o.stop(t + d + 0.01);
    t += d + 0.04;
  }
}

// ------------------------------------------------------------------ page integration
function render() {
  const ph = $("placeholder"), warn = $("camwarn");
  const s = lastStatus || {};
  let text = "";
  if (s.error) text = s.error;
  else if (!s.running) text = "Camera off. Press <b>&nbsp;Start day&nbsp;</b> to follow your timetable, or start a quick session.";
  else if (s.camera_off) text = "Camera off: you're on a break.";
  else if (camError) text = camError;
  else if (modelError) text = modelError;
  else if (!stream) text = "Starting camera…";
  else if (!landmarker) text = "Loading the face model…";
  ph.hidden = !text;
  if (text && ph.innerHTML !== text) ph.innerHTML = text;
  canvas.style.visibility = stream && !s.camera_off ? "visible" : "hidden";

  let w = "";
  if (stream && landmarker && s.running) {
    if (sendError) w = "Can't reach the server, so this time isn't being recorded. Check your connection.";
    else if (document.hidden || (fps && fps < 4)) w = "Tracking slows down while this tab is hidden. Keep it visible, e.g. in its own window.";
  }
  warn.hidden = !w;
  warn.textContent = w;
}

/** Called by the page with every /api/status result. */
function sync(status) {
  lastStatus = status;
  if (status.state) liveState = status.state;
  const want = !!status.running && !status.camera_off;
  if (want && !wanted) { loadModel().catch(() => {}).finally(render); startCamera(); }
  if (!want && wanted) stopCamera();
  wanted = want;
  render();
}

document.addEventListener("visibilitychange", render);
window.cam = { sync, beep, unlockAudio: audio, debug: { rqAngles, rotationRows, features } };
if (window.__pendingStatus) sync(window.__pendingStatus);
