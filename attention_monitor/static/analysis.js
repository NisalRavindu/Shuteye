/* Analysis tab: where today's / this week's planned time went. */
(() => {
  "use strict";
  const root = document.getElementById("an-root");
  if (!root) return;

  // Series in fixed order -> fixed colour slots (never re-assigned by rank or filter).
  const SERIES = [
    { key: "studied", label: "Studied", cls: "s1" },
    { key: "wasted", label: "Wasted (distracted)", cls: "s2" },
    { key: "breaks", label: "Own breaks", cls: "s3" },
    { key: "missed", label: "Missed (not monitored)", cls: "s4" },
  ];
  const DAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  let mode = "day", date = null, data = null, loading = 0;

  const $ = (id) => document.getElementById(id);
  const pad = (n) => String(n).padStart(2, "0");
  const esc = (x) => String(x).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const dur = (s) => { const m = Math.round((s || 0) / 60); return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${pad(m % 60)}m`; };
  const pct = (a, b) => (b > 0 ? Math.round((a / b) * 100) : 0);
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parse = (s) => new Date(s + "T12:00:00");
  const shift = (s, days) => { const d = parse(s); d.setDate(d.getDate() + days); return iso(d); };
  const nice = (s) => parse(s).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  const short = (s) => parse(s).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const clock = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };

  root.innerHTML = `
    <div class="an-bar">
      <div class="seg" id="an-mode" role="group" aria-label="Range">
        <button type="button" data-m="day">Day</button><button type="button" data-m="week">Week</button>
      </div>
      <button type="button" id="an-prev" aria-label="Previous">‹</button>
      <div class="an-date" id="an-date"></div>
      <button type="button" id="an-next" aria-label="Next">›</button>
      <button type="button" id="an-today">Today</button>
      <span class="an-status" id="an-status"></span>
    </div>
    <div class="an-note" id="an-note"></div>
    <div class="an-tiles" id="an-tiles"></div>
    <div class="card an-card" id="an-week-card" hidden>
      <h2>By day</h2>
      <div class="legend" id="an-legend-w"></div>
      <div id="an-week"></div>
    </div>
    <div class="card an-card">
      <h2>By subject</h2>
      <div class="legend" id="an-legend"></div>
      <div id="an-subjects"></div>
      <details class="an-table"><summary>Show as table</summary><div class="an-scroll" id="an-table"></div></details>
    </div>
    <div class="tip" id="an-tip" hidden></div>`;

  // ---------------------------------------------------------------- data
  let studyToday = iso(new Date());
  async function todayDate() {
    try { const r = await fetch("/api/day", { cache: "no-store" }); studyToday = (await r.json()).study_date; }
    catch {}
    return studyToday;
  }
  async function load() {
    if (!date) date = await todayDate();
    const ticket = ++loading;
    try {
      const r = await fetch(`/api/analysis/${mode}?date=${date}`, { cache: "no-store" });
      const d = await r.json();
      if (ticket !== loading) return;      // a newer request superseded this one
      data = d;
      render();
    } catch {
      $("an-note").textContent = "Couldn't load the analysis (is the server running?).";
    }
  }

  window.openAnalysis = (d, reason) => {
    mode = "day"; date = d;
    if (typeof showTab === "function") showTab("analysis");
    load();
  };
  document.addEventListener("tabshown", (e) => { if (e.detail === "analysis") load(); });

  $("an-mode").addEventListener("click", (e) => { const b = e.target.closest("[data-m]"); if (b) { mode = b.dataset.m; load(); } });
  $("an-prev").addEventListener("click", () => { date = shift(date, mode === "week" ? -7 : -1); load(); });
  $("an-next").addEventListener("click", () => { date = shift(date, mode === "week" ? 7 : 1); load(); });
  $("an-today").addEventListener("click", async () => { date = await todayDate(); load(); });

  // ---------------------------------------------------------------- render
  function legend(withPlan) {
    return SERIES.map((s) => `<span><i class="sg ${s.cls}"></i>${s.label}</span>`).join("")
      + (withPlan ? `<span><i class="plan"></i>Still planned</span>` : "");
  }

  function tile(label, value, sub, cls) {
    return `<div class="an-tile"><span>${cls ? `<i class="sg ${cls}"></i>` : ""}${label}</span><b>${value}</b><small>${sub || "&nbsp;"}</small></div>`;
  }

  function render() {
    const t = data.totals;
    root.querySelectorAll("#an-mode button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.m === mode)));
    $("an-date").textContent = mode === "day" ? nice(data.date) : `Week of ${short(data.week_start)}`;

    // status line + note
    let status = "", note = "", warn = false;
    if (mode === "day") {
      status = { concluded: data.ended_at ? `Ended at ${clock(data.ended_at)}` : "Ended", active: "In progress",
                 missed: "Never started", not_started: "Not started yet", upcoming: "Upcoming" }[data.status] || "";
      if (data.status === "concluded" && data.end_reason === "limit") {
        note = `This day wasn't ended manually, so it closed automatically at the day limit (${clock(data.window_end)}).`;
        warn = true;
      } else if (data.status === "missed") {
        note = "This study day was never started, so all of its planned time counts as missed.";
        warn = true;
      } else if (data.status === "active") {
        note = "The day is still running. Numbers cover planned time up to now.";
      }
    } else {
      const missedDays = data.days.filter((d) => d.status === "missed" && d.totals.planned > 0).length;
      const autoDays = data.days.filter((d) => d.end_reason === "limit").length;
      note = [missedDays ? `${missedDays} planned day${missedDays > 1 ? "s were" : " was"} never started.` : "",
              autoDays ? `${autoDays} day${autoDays > 1 ? "s" : ""} closed automatically at the day limit.` : ""].join(" ").trim();
      warn = !!note;
    }
    $("an-status").textContent = status;
    $("an-note").textContent = note;
    $("an-note").classList.toggle("warn", warn);

    // tiles (hero numbers)
    const due = t.planned_so_far;
    $("an-tiles").innerHTML =
      tile("Planned", dur(t.planned), due < t.planned ? `${dur(due)} due so far` : "goal")
      + tile("Studied", dur(t.studied), due ? `${pct(t.studied, due)}% of planned` + (t.catchup ? ` · ${dur(t.catchup)} caught up` : "") : "", "s1")
      + tile("Wasted", dur(t.wasted), "distracted while monitored", "s2")
      + tile("Own breaks", dur(t.breaks), "camera off, not wasted", "s3")
      + tile("Missed", dur(t.missed), "not started / not monitored", "s4");

    // by day (week only)
    $("an-week-card").hidden = mode !== "week";
    if (mode === "week") renderWeek();

    // by subject
    const rows = data.subjects;
    const anyPlan = rows.some((r) => r.planned - r.planned_so_far > 30);
    $("an-legend").innerHTML = legend(anyPlan);
    $("an-subjects").innerHTML = rows.length ? renderBars(rows)
      : `<div class="an-empty">Nothing was planned ${mode === "day" ? "for this day" : "this week"}.</div>`;
    $("an-table").innerHTML = rows.length ? renderTable(rows) : "";
  }

  function tipFor(name, r) {
    const parts = SERIES.map((s) => `${s.label}: <b>${dur(r[s.key])}</b>`);
    if (r.catchup) parts.push(`Caught up (incl. in studied): <b>${dur(r.catchup)}</b>`);
    const rest = r.planned - r.planned_so_far;
    if (rest > 30) parts.push(`Still planned: <b>${dur(rest)}</b>`);
    return `<b>${esc(name)}</b> · planned ${dur(r.planned)}<br>${parts.join("<br>")}`;
  }

  function renderBars(rows) {
    const total = (r) => SERIES.reduce((a, s) => a + r[s.key], 0) + Math.max(0, r.planned - r.planned_so_far);
    const scale = Math.max(...rows.map(total), 1);
    return rows.map((r) => {
      const segs = SERIES.filter((s) => r[s.key] > 0)
        .map((s) => `<div class="sg ${s.cls}" style="width:${(r[s.key] / scale) * 100}%"
                       data-tip="${esc(`<b>${esc(r.name)}</b><br>${s.label}: <b>${dur(r[s.key])}</b> (${pct(r[s.key], r.planned)}% of planned)`)}"></div>`);
      const rest = r.planned - r.planned_so_far;
      if (rest > 30) segs.push(`<div class="sg plan" style="width:${(rest / scale) * 100}%"
                                   data-tip="${esc(`<b>${esc(r.name)}</b><br>Still planned: <b>${dur(rest)}</b>`)}"></div>`);
      return `<div class="hrow" data-tip="${esc(tipFor(r.name, r))}">
        <div class="hlabel"><i style="background:${r.color}"></i><em>${esc(r.name)}</em></div>
        <div class="htrack">${segs.join("")}</div>
        <div class="hval">${dur(r.studied)} / ${dur(r.planned)}</div></div>`;
    }).join("");
  }

  function renderWeek() {
    const days = data.days;
    const sum = (d) => SERIES.reduce((a, s) => a + d.totals[s.key], 0);
    const rest = (d) => Math.max(0, d.totals.planned - d.totals.planned_so_far);
    const maxS = Math.max(...days.map((d) => sum(d) + rest(d)), 3600);
    const stepH = maxS > 6 * 3600 ? 2 : 1;                       // gridline every 1h or 2h
    const top = Math.ceil(maxS / 3600 / stepH) * stepH * 3600;
    let grid = "";
    for (let h = stepH; h * 3600 <= top; h += stepH) {
      const y = 200 - (h * 3600 / top) * 200;
      grid += `<div style="top:${y}px"><span>${h}h</span></div>`;
    }
    const today = studyToday;
    const cols = days.map((d, i) => {
      const segs = [];
      SERIES.forEach((s) => { if (d.totals[s.key] > 0) segs.push([s, d.totals[s.key]]); });
      const r = rest(d);
      const html = segs.map(([s, v], j) => `<div class="sg ${s.cls}${j === segs.length - 1 && !r ? " top" : ""}"
                                              style="height:${(v / top) * 100}%"></div>`).join("")
        + (r > 30 ? `<div class="sg plan top" style="height:${(r / top) * 100}%"></div>` : "");
      const tip = `<b>${DAY[i]} ${short(d.date)}</b> · ${({ concluded: "ended", active: "in progress", missed: "never started",
                    not_started: "not started", upcoming: "upcoming" })[d.status]}<br>`
        + `Planned: <b>${dur(d.totals.planned)}</b><br>`
        + SERIES.map((s) => `${s.label}: <b>${dur(d.totals[s.key])}</b>`).join("<br>");
      return `<div class="vcol" data-day="${d.date}" data-tip="${esc(tip)}" title="">${html}</div>`;
    }).join("");
    const labels = days.map((d, i) => `<div class="${d.date === today ? "today" : ""}"><b>${DAY[i]}</b>${
      d.totals.planned ? `${pct(d.totals.studied, d.totals.planned_so_far || d.totals.planned)}%` : "—"}</div>`).join("");
    $("an-legend-w").innerHTML = legend(days.some((d) => rest(d) > 30));
    $("an-week").innerHTML = `<div class="vwrap"><div class="vgrid">${grid}</div><div class="vchart">${cols}</div>
      <div class="vlabels">${labels}</div></div>`;
  }

  function renderTable(rows) {
    const cols = [["planned", "Planned"], ["studied", "Studied"], ["wasted", "Wasted"], ["breaks", "Own breaks"],
                  ["missed", "Missed"], ["catchup", "Caught up"], ["short", "Short of plan"]];
    const tot = data.totals;
    return `<table><thead><tr><th>Subject</th>${cols.map(([, l]) => `<th>${l}</th>`).join("")}</tr></thead><tbody>`
      + rows.map((r) => `<tr><td>${esc(r.name)}</td>${cols.map(([k]) => `<td>${dur(r[k])}</td>`).join("")}</tr>`).join("")
      + `<tr><td><b>Total</b></td>${cols.map(([k]) => `<td><b>${dur(tot[k])}</b></td>`).join("")}</tr></tbody></table>`;
  }

  // ---------------------------------------------------------------- tooltips & clicks
  const tip = $("an-tip");
  root.addEventListener("mousemove", (e) => {
    const el = e.target.closest("[data-tip]");
    if (!el) { tip.hidden = true; return; }
    tip.innerHTML = el.dataset.tip;
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + 14, y = e.clientY + 14;
    if (x + w > window.innerWidth - 8) x = e.clientX - w - 14;
    if (y + h > window.innerHeight - 8) y = e.clientY - h - 14;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  });
  root.addEventListener("mouseleave", () => { tip.hidden = true; });
  root.addEventListener("click", (e) => {            // click a day column -> open that day
    const col = e.target.closest(".vcol[data-day]");
    if (col) { mode = "day"; date = col.dataset.day; load(); }
  });
})();
