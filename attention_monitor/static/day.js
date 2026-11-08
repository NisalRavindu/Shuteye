/* Study-day card on the Monitor tab + the header Start/End day button. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  if (!$("daycard")) return;

  let D = null;              // last /api/day status
  let offset = 0;            // server clock - browser clock (s)
  let lastConcluded;         // undefined until first poll
  let busy = false;

  const pad = (n) => String(n).padStart(2, "0");
  const now = () => Date.now() / 1000 + offset;
  const clock = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  const left = (s) => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
                         return h ? `${h}:${pad(m)}:${pad(x)}` : `${m}:${pad(x)}`; };
  const dur = (s) => { const m = Math.round((s || 0) / 60); return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${pad(m % 60)}m`; };
  const esc = (x) => String(x).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  async function call(path, body) {
    if (busy) return;
    busy = true;
    try {
      const r = await fetch(path, body === undefined ? { cache: "no-store" }
        : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      accept(await r.json());
    } catch {} finally { busy = false; }
  }
  function accept(d) {
    if (!d || d.now === undefined) return;
    offset = d.now - Date.now() / 1000;
    const lc = d.last_concluded ? d.last_concluded.at : null;
    if (lastConcluded !== undefined && lc && lc !== lastConcluded && window.openAnalysis) {
      window.openAnalysis(d.last_concluded.date, d.last_concluded.reason);   // day just ended -> show analysis
    }
    lastConcluded = lc;
    D = d;
    render();
  }
  async function poll() {
    try { const r = await fetch("/api/day", { cache: "no-store" }); accept(await r.json()); } catch {}
    setTimeout(poll, 2000);
  }

  $("toggle").addEventListener("click", () => {
    if (!D) return;
    try { window.cam && window.cam.unlockAudio(); } catch {}
    if (D.active) {
      const msg = "End today's study day?\n\nAny planned time that's still ahead will count as missed. "
                + "You can reopen the day later if you change your mind.";
      if (confirm(msg)) call("/api/day/end", {});
    } else {
      call("/api/day/start", window.monitorSettings ? window.monitorSettings() : {});
    }
  });
  $("day-break").addEventListener("click", () => D && call("/api/day/break", { on: !D.manual_break }));
  $("day-resume").addEventListener("click", () => call("/api/day/start", window.monitorSettings ? window.monitorSettings() : {}));
  $("day-view").addEventListener("click", () => D && window.openAnalysis && window.openAnalysis(D.study_date));
  $("day-suggest").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-cu]");
    if (b) call("/api/day/catchup", { subject: b.dataset.cu || null });
  });

  function set(id, html) { const el = $(id); if (el.innerHTML !== html) el.innerHTML = html; }

  function render() {
    if (!D) return;
    const t = $("toggle");
    t.textContent = D.active ? "End day" : D.today.concluded ? "Reopen day" : "Start day";
    t.classList.toggle("stop", !!D.active);
    t.title = D.active ? `Ends automatically at ${clock(D.auto_end_at)} if you don't end it` : "";

    let kicker = "Today", title = "", time = "", next = "", dot = null, frac = null;
    $("day-break").hidden = $("day-resume").hidden = $("day-view").hidden = true;
    $("day-tiles").hidden = $("day-suggest").hidden = true;

    if (!D.active) {
      if (D.today.concluded) {
        title = "Today's study day has ended";
        next = "Reopen it from the button above if you want to keep going.";
        $("day-view").hidden = false;
      } else if (!D.today.planned) {
        title = "Nothing planned today";
        next = "Fill today in the <b>Timetable</b> tab, or start a quick session below.";
      } else {
        title = `${dur(D.today.planned)} planned`;
        next = D.today.remaining_blocks === 0 ? "All of today's blocks are already over. Starting now lets you catch up in free time."
             : D.today.first ? `First up: <b>${esc(D.today.first.name)}</b> at ${clock(D.today.first.s)}. Press <b>Start day</b> when you sit down.` : "";
      }
    } else {
      const cur = D.current, rem = cur.ends_at - now();
      const nextLine = D.next
        ? `Next: <b>${esc(D.next.name)}</b> at ${clock(D.next.starts_at)}`
          + (cur.kind === "study" && D.next.starts_at - cur.ends_at > 1 && D.next.starts_at - cur.ends_at <= 301
             ? ` · after a ${Math.round((D.next.starts_at - cur.ends_at) / 60)}-min break` : "")
        : "No more subjects today.";

      if (D.manual_break) {
        kicker = "On a break · camera off";
        title = "Break";
        if (cur.kind === "study") {
          time = `${esc(cur.name)}: ${left(rem)} left`;
          next = `${esc(cur.name)} is still scheduled, so this break counts against today's goal. It isn't counted as wasted.`;
        } else {
          next = nextLine;
        }
      } else if (cur.kind === "study") {
        kicker = "Now studying";
        title = esc(cur.name); dot = cur.color;
        time = `${left(rem)} left`;
        frac = 1 - rem / cur.length;
        next = nextLine;
      } else if (cur.kind === "break") {
        kicker = "Break";
        title = "Short break";
        time = `${left(rem)} left`;
        next = D.next ? `Up next: <b>${esc(D.next.name)}</b> at ${clock(D.next.starts_at)}` : "";
      } else if (D.catchup) {
        kicker = "Catching up";
        title = esc(D.catchup.name); dot = D.catchup.color;
        time = `${dur(D.catchup.target_s - D.catchup.done_s)} to go`;
        frac = D.catchup.done_s / D.catchup.target_s;
        next = nextLine;
      } else {
        kicker = "Free time";
        title = "Nothing scheduled";
        time = cur.ends_at < D.auto_end_at - 1 ? `until ${clock(cur.ends_at)} (${left(rem)})` : "";
        next = nextLine;
      }
      if (!D.monitoring) {
        next = `<span class="day-warn">Monitoring is off, so time isn't being tracked.</span> ` + next;
        $("day-resume").hidden = false;
      } else {
        if (!D.receiving && !D.manual_break) {
          next = `<span class="day-warn">No camera data is arriving, so this time counts as missed. Keep this page open with the camera allowed.</span> ` + next;
        }
        const b = $("day-break");
        b.hidden = false;
        b.textContent = D.manual_break ? "Back to studying" : "Take a break";
        b.classList.toggle("on", !!D.manual_break);
      }

      $("day-tiles").hidden = false;
      $("dt-goal").textContent = dur(D.totals.planned_so_far);
      $("dt-studied").textContent = dur(D.totals.studied);
      $("dt-wasted").textContent = dur(D.totals.wasted);
      $("dt-short").textContent = dur(D.totals.short);

      const sug = D.suggestions || [];
      if (D.catchup || sug.length) {
        $("day-suggest").hidden = false;
        let html = `<h3>Make up missing time</h3>`;
        if (D.catchup) {
          html += `<div class="sug"><span class="sw" style="background:${D.catchup.color}"></span>
                   Catching up on <b>${esc(D.catchup.name)}</b><button data-cu="">Stop catching up</button></div>`;
        } else {
          html += `<p>You're behind on these. They fit in today's free time:</p>`;
          const free = cur.kind === "free" && !D.manual_break;
          html += sug.map((s) => `<div class="sug"><span class="sw" style="background:${s.color}"></span>
                   <b>${esc(s.name)}</b> ${s.minutes} min <time>${clock(s.s)}–${clock(s.e)}</time>
                   ${free && s.s <= now() + 60 ? `<button data-cu="${esc(s.subject)}">Start now</button>` : ""}</div>`).join("");
        }
        set("day-suggest", html);
      }
    }

    set("day-kicker", kicker);
    set("day-title", title);
    set("day-time", time);
    set("day-next", next);
    $("day-dot").hidden = !dot;
    if (dot) $("day-dot").style.background = dot;
    $("day-barwrap").hidden = frac === null;
    if (frac !== null) {
      $("day-bar").style.width = Math.max(0, Math.min(1, frac)) * 100 + "%";
      $("day-bar").style.background = dot || "var(--muted)";
    }
  }

  setInterval(render, 500);   // smooth countdown between polls
  poll();
})();
