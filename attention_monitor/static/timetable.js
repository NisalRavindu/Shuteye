/* Timetable planner.
 *
 * Blocks are stored as real times ({day, start, end} in minutes), not as grid
 * cells, so the slot size can change at any time without moving or losing
 * anything. The slot size only controls the grid lines and how edits snap.
 */
(() => {
  "use strict";
  const root = document.getElementById("tt-root");
  if (!root) return;

  const API = "/api/timetable";
  const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const PALETTE = ["#4e79a7", "#f28e2b", "#e15759", "#59a14f", "#b07aa1", "#edc948",
                   "#76b7b2", "#ff9da7", "#9c755f", "#86bcb6", "#8cd17d", "#d37295"];
  const PRESETS = [[15, "15m"], [30, "30m"], [45, "45m"], [60, "1h"], [90, "1.5h"], [120, "2h"]];
  const rowHeight = (m) => (m <= 15 ? 22 : m <= 30 ? 28 : m <= 45 ? 34 : m <= 60 ? 44 : m <= 90 ? 54 : 64);

  // ------------------------------------------------------------------ state
  const blank = () => ({
    version: 1, subjects: [], entries: [],
    settings: { slot: 60, dayStart: 8 * 60, dayEnd: 22 * 60, days: [true, true, true, true, true, true, true] },
  });
  let S = blank();
  let tool = { kind: "select" };          // {kind:"select"} | {kind:"paint", subject} | {kind:"erase"}
  let selectedId = null;
  let undoStack = [], redoStack = [];
  let saveTimer = null;
  let drag = null;                        // active pointer / drag-and-drop interaction
  let dndSubject = null;                  // subject being dragged from the sidebar

  let _uid = Date.now();
  const uid = () => "e" + (_uid++).toString(36);
  const pad = (n) => String(n).padStart(2, "0");
  const fmt = (m) => `${pad(Math.floor(m / 60) % 24 + (m >= 1440 ? 24 : 0))}:${pad(Math.round(m % 60))}`;
  const hrs = (min) => (min ? `${Math.round(min / 6) / 10}h` : "");
  const esc = (x) => String(x).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const subj = (id) => S.subjects.find((s) => s.id === id);
  const textOn = (hex) => {
    const n = parseInt(hex.slice(1), 16);
    const L = (0.299 * ((n >> 16) & 255) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
    return L > 0.62 ? "#1d1d1b" : "#ffffff";
  };
  const minutesOf = (filter) => S.entries.filter(filter).reduce((a, e) => a + (e.end - e.start), 0);

  // ------------------------------------------------------------------ data operations
  function normalize(list) {
    const ok = list.filter((e) => subj(e.subject) && e.end > e.start)
      .sort((a, b) => a.day - b.day || a.start - b.start);
    const out = [];
    for (const e of ok) {
      const prev = out[out.length - 1];
      if (prev && prev.day === e.day && prev.subject === e.subject && prev.end >= e.start) {
        prev.end = Math.max(prev.end, e.end);           // merge touching blocks of the same subject
      } else {
        out.push({ ...e });
      }
    }
    return out;
  }

  /** Set [s, e) on `day` to `subject` (null = clear), splitting anything it overlaps. */
  function fillRange(list, day, s, e, subject) {
    const out = [];
    for (const en of list) {
      if (en.day !== day || en.end <= s || en.start >= e) { out.push(en); continue; }
      if (en.start < s) out.push({ ...en, id: uid(), end: s });
      if (en.end > e) out.push({ ...en, id: uid(), start: e });
    }
    if (subject) out.push({ id: uid(), day, start: s, end: e, subject });
    return out;
  }

  // ------------------------------------------------------------------ history
  const snapshot = () => JSON.stringify({ subjects: S.subjects, entries: S.entries });
  function mutate(fn) {
    const before = snapshot();
    fn();
    S.entries = normalize(S.entries);
    if (snapshot() === before) { render(); return false; }
    undoStack.push(before);
    if (undoStack.length > 200) undoStack.shift();
    redoStack = [];
    render();
    scheduleSave();
    return true;
  }
  function restore(snap) {
    const d = JSON.parse(snap);
    S.subjects = d.subjects; S.entries = d.entries;
    selectedId = null;
    if (tool.kind === "paint" && !subj(tool.subject)) tool = { kind: "select" };
    render(); scheduleSave();
  }
  function undo() { if (undoStack.length) { redoStack.push(snapshot()); restore(undoStack.pop()); } }
  function redo() { if (redoStack.length) { undoStack.push(snapshot()); restore(redoStack.pop()); } }

  // ------------------------------------------------------------------ persistence
  function sanitize(d) {
    const b = blank();
    const st = Object.assign(b.settings, d.settings || {});
    st.slot = Math.min(240, Math.max(5, Number(st.slot) || 60));
    st.dayStart = Math.min(23 * 60, Math.max(0, Number(st.dayStart) || 0));
    st.dayEnd = Math.min(24 * 60, Math.max(st.dayStart + 60, Number(st.dayEnd) || 1320));
    if (!Array.isArray(st.days) || st.days.length !== 7 || !st.days.some(Boolean)) st.days = b.settings.days;
    S = { version: 1, settings: st,
          subjects: (d.subjects || []).filter((s) => s && s.id && s.name)
            .map((s) => ({ id: String(s.id), name: String(s.name).slice(0, 40), color: /^#[0-9a-f]{6}$/i.test(s.color) ? s.color : PALETTE[0] })),
          entries: [] };
    S.entries = normalize((d.entries || []).filter((e) => e && e.day >= 0 && e.day < 7)
      .map((e) => ({ id: uid(), day: +e.day, start: +e.start, end: +e.end, subject: String(e.subject) })));
    return S;
  }
  // The timetable is stored on the server only (data/timetable.json).
  let loaded = false;
  async function load() {
    for (let attempt = 0; ; attempt++) {
      try {
        const r = await fetch(API, { cache: "no-store" });
        if (r.ok) {
          const data = await r.json();
          if (data && data.settings) sanitize(data);
          loaded = true;
          setSave("");
          render();
          return;
        }
      } catch {}
      setSave("Can't reach the server. Retrying…", true);
      await new Promise((ok) => setTimeout(ok, Math.min(10000, 1000 * (attempt + 1))));
    }
  }
  function scheduleSave(delay = 400) {
    if (!loaded) return;                  // never overwrite the server copy before it has loaded
    setSave("Saving…");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        const r = await fetch(API, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(S) });
        if (!r.ok) throw new Error(String(r.status));
        setSave("Saved");
      } catch {
        setSave("Not saved yet: can't reach the server. Retrying…", true);
        scheduleSave(5000);               // keep trying; the change stays on screen meanwhile
      }
    }, delay);
  }
  function setSave(text, err = false) { const el = $("tt-save"); el.textContent = text; el.classList.toggle("err", err); }

  // ------------------------------------------------------------------ skeleton
  root.innerHTML = `
  <div class="tt">
    <aside class="tt-side">
      <div class="tt-tools">
        <button type="button" data-tool="select" title="Select, move and resize blocks (Esc)">Select</button>
        <button type="button" data-tool="erase" title="Clear slots by clicking or dragging (E)">Eraser</button>
      </div>
      <div class="tt-label">Subjects</div>
      <ul class="tt-subjects" id="tt-subjects"></ul>
      <form class="tt-add" id="tt-add">
        <input id="tt-new" maxlength="40" placeholder="Add a subject…" autocomplete="off" aria-label="New subject name">
        <button type="submit">Add</button>
      </form>
      <div class="tt-total" id="tt-total"></div>
      <div class="tt-hint">
        <b>How to fill slots</b>
        <ul>
          <li><b>Click a subject</b>, then click a slot, or drag across several slots and days.</li>
          <li>Or <b>drag a subject</b> from this list onto a slot.</li>
          <li><b>Eraser</b> clears slots the same way.</li>
          <li><b>Select</b> mode: drag a block to move it, drag its bottom edge to resize, <kbd>Del</kbd> removes it.</li>
          <li>Double-click a name to rename it. Click the colour square to change its colour.</li>
          <li><kbd>1</kbd>–<kbd>9</kbd> pick a subject, <kbd>E</kbd> eraser, <kbd>Esc</kbd> select, <kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Ctrl</kbd>+<kbd>Y</kbd> undo / redo.</li>
        </ul>
      </div>
    </aside>
    <div class="tt-main">
      <div class="tt-bar">
        <div class="tt-group"><span class="tt-label">Slot</span><div class="seg" id="tt-slot"></div>
          <input type="number" id="tt-custom" min="5" max="240" step="5" placeholder="min" title="Custom slot length in minutes" aria-label="Custom slot length in minutes"></div>
        <div class="tt-group"><span class="tt-label">Hours</span><select id="tt-from" aria-label="Day starts"></select>–<select id="tt-to" aria-label="Day ends"></select></div>
        <div class="tt-group"><span class="tt-label">Days</span><div class="seg" id="tt-days"></div></div>
        <div class="tt-group" title="Added automatically between subjects that follow each other directly. Taken from the end of the first subject; not shown in the grid.">
          <span class="tt-label">Break</span><select id="tt-break" aria-label="Break between subjects"></select></div>
        <div class="tt-group" title="When a study day ends. Studying after midnight still counts for the day you started; a day you forget to end closes automatically at this time.">
          <span class="tt-label">Day ends</span><select id="tt-dayend" aria-label="Study day ends at"></select></div>
        <div class="tt-group tt-right">
          <button type="button" id="tt-undo" title="Undo (Ctrl+Z)">Undo</button>
          <button type="button" id="tt-redo" title="Redo (Ctrl+Y)">Redo</button>
          <button type="button" id="tt-clear">Clear all</button>
          <span class="tt-save" id="tt-save"></span>
        </div>
      </div>
      <div class="tt-note" id="tt-note"></div>
      <div class="tt-scroll"><div class="tt-grid" id="tt-grid"></div></div>
    </div>
  </div>`;
  const $ = (id) => document.getElementById(id);
  const grid = $("tt-grid");

  // static option lists
  $("tt-from").innerHTML = Array.from({ length: 24 }, (_, h) => `<option value="${h * 60}">${pad(h)}:00</option>`).join("");
  $("tt-to").innerHTML = Array.from({ length: 24 }, (_, h) => `<option value="${(h + 1) * 60}">${pad(h + 1)}:00</option>`).join("");

  // ------------------------------------------------------------------ rendering
  function render() { renderSide(); renderBar(); renderGrid(); }

  function renderSide() {
    root.classList.toggle("tt-painting", tool.kind !== "select");
    root.querySelectorAll("[data-tool]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tool === tool.kind)));
    $("tt-subjects").innerHTML = S.subjects.length ? S.subjects.map((s, i) => `
      <li class="tt-subj${tool.kind === "paint" && tool.subject === s.id ? " armed" : ""}" draggable="true" data-sid="${s.id}"
          title="Click to paint with ${esc(s.name)}, or drag onto a slot">
        <span class="tt-key">${i < 9 ? i + 1 : ""}</span>
        <label class="tt-swatch" style="background:${s.color}" title="Change colour"><input type="color" value="${s.color}" data-color="${s.id}" aria-label="Colour for ${esc(s.name)}"></label>
        <span class="tt-name" data-name="${s.id}">${esc(s.name)}</span>
        <span class="tt-hrs">${hrs(minutesOf((e) => e.subject === s.id))}</span>
        <button type="button" class="tt-x" data-rm="${s.id}" title="Delete ${esc(s.name)}" aria-label="Delete ${esc(s.name)}">×</button>
      </li>`).join("") : `<li class="tt-empty">No subjects yet. Add one below, e.g. Maths, Physics or Revision.</li>`;
    const total = minutesOf(() => true);
    $("tt-total").textContent = total ? `Planned: ${hrs(total)} per week` : "";
  }

  function renderBar() {
    const st = S.settings;
    $("tt-slot").innerHTML = PRESETS.map(([m, l]) => `<button type="button" data-slot="${m}" aria-pressed="${st.slot === m}">${l}</button>`).join("");
    const custom = !PRESETS.some(([m]) => m === st.slot);
    const ci = $("tt-custom");
    if (document.activeElement !== ci) ci.value = custom ? st.slot : "";
    ci.classList.toggle("on", custom);
    $("tt-from").value = String(st.dayStart - (st.dayStart % 60));
    $("tt-to").value = String(Math.ceil(st.dayEnd / 60) * 60);
    $("tt-days").innerHTML = DAY_NAMES.map((d, i) => `<button type="button" data-day="${i}" aria-pressed="${st.days[i]}">${d}</button>`).join("");
    $("tt-undo").disabled = !undoStack.length;
    $("tt-redo").disabled = !redoStack.length;
    $("tt-clear").disabled = !S.entries.length;
    const hidden = S.entries.filter((e) => !st.days[e.day] || e.start < st.dayStart || e.end > st.dayEnd).length;
    $("tt-note").textContent = hidden
      ? `${hidden} block${hidden > 1 ? "s are" : " is"} partly or fully outside the visible days/hours. They're kept, just not shown.` : "";
  }

  function geom() {
    const st = S.settings, rh = rowHeight(st.slot);
    return { st, slot: st.slot, rh, ppm: rh / st.slot, days: [0, 1, 2, 3, 4, 5, 6].filter((d) => st.days[d]) };
  }

  function renderGrid() {
    const { st, slot, rh, ppm, days } = geom();
    const H = Math.round((st.dayEnd - st.dayStart) * ppm);
    const today = (new Date().getDay() + 6) % 7;
    grid.style.gridTemplateColumns = `56px repeat(${days.length}, minmax(96px, 1fr))`;
    grid.style.setProperty("--rh", rh + "px");

    let labels = "";
    for (let m = st.dayStart; m < st.dayEnd; m += slot) {
      if (slot < 30 && m % 60 !== 0) continue;
      labels += `<div class="tt-time" style="top:${(m - st.dayStart) * ppm}px">${fmt(m)}</div>`;
    }
    const cols = days.map((d) => `<div class="tt-col${d === today ? " today" : ""}" data-day="${d}">${blocksFor(d, ppm)}</div>`).join("");
    grid.innerHTML = `<div class="tt-corner"></div>`
      + days.map((d) => `<div class="tt-dayhead">${DAY_NAMES[d]}<span>${hrs(minutesOf((e) => e.day === d))}</span></div>`).join("")
      + `<div class="tt-times" style="height:${H}px">${labels}</div>`
      + `<div class="tt-body" id="tt-body" style="grid-column: 2 / span ${days.length}; height:${H}px">${cols}</div>`;
  }

  function blocksFor(day, ppm) {
    const st = S.settings;
    return S.entries.filter((e) => e.day === day && e.end > st.dayStart && e.start < st.dayEnd).map((e) => {
      const s = Math.max(e.start, st.dayStart), en = Math.min(e.end, st.dayEnd);
      const top = (s - st.dayStart) * ppm, h = (en - s) * ppm;
      const sb = subj(e.subject), c = sb ? sb.color : "#888888", sel = e.id === selectedId;
      return `<div class="tt-block${sel ? " sel" : ""}" data-id="${e.id}"
                style="top:${top + 1}px;height:${Math.max(h - 2, 6)}px;background:${c};color:${textOn(c)}"
                title="${esc(sb ? sb.name : "")} ${fmt(e.start)}–${fmt(e.end)}">
                <b>${esc(sb ? sb.name : "?")}</b>${h >= 32 ? `<span>${fmt(e.start)}–${fmt(e.end)}</span>` : ""}
                <i class="tt-handle" data-handle="1"></i>
                ${sel ? `<button type="button" class="tt-del" data-del="${e.id}" title="Delete (Del)" aria-label="Delete block">×</button>` : ""}
              </div>`;
    }).join("");
  }

  // ------------------------------------------------------------------ geometry helpers
  const bodyCols = () => [...($("tt-body")?.children || [])];
  function locate(x, y) {
    const cols = bodyCols();
    if (!cols.length) return null;
    let ci = 0;
    cols.forEach((c, i) => { if (x >= c.getBoundingClientRect().left) ci = i; });
    const r = cols[ci].getBoundingClientRect();
    const { st, ppm } = geom();
    const min = Math.max(st.dayStart, Math.min(st.dayEnd - 0.01, st.dayStart + (y - r.top) / ppm));
    return { ci, day: +cols[ci].dataset.day, min };
  }
  const snapDown = (m) => { const { st, slot } = geom(); return st.dayStart + Math.floor((m - st.dayStart) / slot) * slot; };
  const snapRound = (m) => { const { st, slot } = geom(); return st.dayStart + Math.round((m - st.dayStart) / slot) * slot; };

  /** The ranges an in-progress interaction would change: [{ci, day, s, e}] */
  function pending() {
    if (!drag) return [];
    const { st, slot } = geom();
    const cols = bodyCols();
    if (drag.mode === "paint" || drag.mode === "dnd") {
      const { a, b } = drag;
      const s = snapDown(Math.min(a.min, b.min));
      const e = Math.min(st.dayEnd, snapDown(Math.max(a.min, b.min)) + slot);
      const out = [];
      for (let ci = Math.min(a.ci, b.ci); ci <= Math.max(a.ci, b.ci); ci++) out.push({ ci, day: +cols[ci].dataset.day, s, e });
      return out;
    }
    if (drag.mode === "move" && drag.moved) {
      const dur = drag.e.end - drag.e.start;
      let s = snapRound(drag.loc.min - drag.off);
      s = Math.max(st.dayStart, Math.min(s, st.dayEnd - dur));
      return [{ ci: drag.loc.ci, day: drag.loc.day, s, e: s + dur }];
    }
    if (drag.mode === "resize" && drag.moved) {
      let e = snapRound(drag.loc.min);
      if (e <= drag.e.start) e = drag.e.start + slot;
      e = Math.min(e, st.dayEnd);
      const ci = cols.findIndex((c) => +c.dataset.day === drag.e.day);
      return ci < 0 ? [] : [{ ci, day: drag.e.day, s: drag.e.start, e }];
    }
    return [];
  }

  function showPreview() {
    clearPreview();
    const cols = bodyCols();
    const { st, ppm } = geom();
    const subjectId = drag.mode === "dnd" ? dndSubject
      : drag.mode === "paint" ? (tool.kind === "paint" ? tool.subject : null) : drag.e.subject;
    const sb = subjectId && subj(subjectId);
    for (const r of pending()) {
      const el = document.createElement("div");
      el.className = "tt-preview" + (sb ? "" : " erase");
      el.style.top = (r.s - st.dayStart) * ppm + "px";
      el.style.height = Math.max((r.e - r.s) * ppm, 6) + "px";
      if (sb) { el.style.background = sb.color; el.style.color = textOn(sb.color); }
      el.textContent = `${sb ? sb.name + " · " : "Clear · "}${fmt(r.s)}–${fmt(r.e)}`;
      cols[r.ci].appendChild(el);
    }
    if ((drag.mode === "move" || drag.mode === "resize") && drag.moved) {
      grid.querySelector(`.tt-block[data-id="${drag.e.id}"]`)?.classList.add("ghosted");
    }
  }
  function clearPreview() { grid.querySelectorAll(".tt-preview").forEach((el) => el.remove()); }

  // ------------------------------------------------------------------ pointer interactions on the grid
  grid.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0 || ev.target.closest("[data-del]")) return;
    const body = ev.target.closest(".tt-body");
    if (!body) return;
    const loc = locate(ev.clientX, ev.clientY);
    if (!loc) return;
    if (tool.kind === "select") {
      const blockEl = ev.target.closest(".tt-block");
      if (!blockEl) { if (selectedId) { selectedId = null; renderGrid(); } return; }
      const e = S.entries.find((x) => x.id === blockEl.dataset.id);
      if (!e) return;
      drag = { mode: ev.target.closest("[data-handle]") ? "resize" : "move", e, loc, off: loc.min - e.start,
               x0: ev.clientX, y0: ev.clientY, moved: false };
    } else {
      drag = { mode: "paint", a: loc, b: loc };
      showPreview();
    }
    ev.preventDefault();
    try { body.setPointerCapture(ev.pointerId); } catch {}
  });

  grid.addEventListener("pointermove", (ev) => {
    if (!drag || drag.mode === "dnd") return;
    const loc = locate(ev.clientX, ev.clientY);
    if (!loc) return;
    if (drag.mode === "paint") { drag.b = loc; showPreview(); return; }
    if (!drag.moved && Math.hypot(ev.clientX - drag.x0, ev.clientY - drag.y0) < 4) return;
    drag.moved = true; drag.loc = loc;
    showPreview();
  });

  grid.addEventListener("pointerup", () => {
    if (!drag || drag.mode === "dnd") return;
    const d = drag, ranges = pending();
    drag = null;
    clearPreview();
    if (d.mode === "paint") {
      const subject = tool.kind === "paint" ? tool.subject : null;
      mutate(() => { for (const r of ranges) S.entries = fillRange(S.entries, r.day, r.s, r.e, subject); });
    } else if (!d.moved) {
      selectedId = d.e.id; renderGrid();                    // plain click: select it
    } else if (ranges.length) {
      const r = ranges[0];
      mutate(() => {
        S.entries = S.entries.filter((x) => x.id !== d.e.id);
        S.entries = fillRange(S.entries, r.day, r.s, r.e, d.e.subject);
      });
      const moved = S.entries.find((x) => x.day === r.day && x.start <= r.s && x.end >= r.e && x.subject === d.e.subject);
      selectedId = moved ? moved.id : null;
      renderGrid();
    } else {
      renderGrid();
    }
  });
  grid.addEventListener("pointercancel", () => { drag = null; clearPreview(); renderGrid(); });

  grid.addEventListener("click", (ev) => {
    const del = ev.target.closest("[data-del]");
    if (del) { deleteBlock(del.dataset.del); }
  });
  function deleteBlock(id) {
    selectedId = null;
    mutate(() => { S.entries = S.entries.filter((x) => x.id !== id); });
  }

  // ------------------------------------------------------------------ drag-and-drop from the sidebar
  grid.addEventListener("dragover", (ev) => {
    if (!dndSubject || !ev.target.closest(".tt-body")) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "copy";
    const loc = locate(ev.clientX, ev.clientY);
    if (!loc) return;
    if (drag && drag.mode === "dnd" && drag.a.ci === loc.ci && snapDown(drag.a.min) === snapDown(loc.min)) return;
    drag = { mode: "dnd", a: loc, b: loc };
    showPreview();
  });
  grid.addEventListener("dragleave", (ev) => {
    if (!ev.relatedTarget || !grid.contains(ev.relatedTarget)) { clearPreview(); if (drag?.mode === "dnd") drag = null; }
  });
  grid.addEventListener("drop", (ev) => {
    if (!dndSubject) return;
    ev.preventDefault();
    const loc = locate(ev.clientX, ev.clientY);
    const id = dndSubject;
    clearPreview(); drag = null;
    if (!loc) return;
    const { st, slot } = geom();
    const s = snapDown(loc.min), e = Math.min(st.dayEnd, s + slot);
    mutate(() => { S.entries = fillRange(S.entries, loc.day, s, e, id); });
  });

  // ------------------------------------------------------------------ sidebar
  const subjList = $("tt-subjects");
  subjList.addEventListener("dragstart", (ev) => {
    const li = ev.target.closest("[data-sid]");
    if (!li) return;
    dndSubject = li.dataset.sid;
    ev.dataTransfer.setData("text/plain", subj(dndSubject)?.name || "");
    ev.dataTransfer.effectAllowed = "copy";
  });
  subjList.addEventListener("dragend", () => { dndSubject = null; clearPreview(); if (drag?.mode === "dnd") drag = null; });

  subjList.addEventListener("click", (ev) => {
    const rm = ev.target.closest("[data-rm]");
    if (rm) { removeSubject(rm.dataset.rm); return; }
    if (ev.target.closest(".tt-swatch") || ev.target.closest("input")) return;
    const li = ev.target.closest("[data-sid]");
    if (li) armSubject(li.dataset.sid, true);
  });
  subjList.addEventListener("dblclick", (ev) => {
    const name = ev.target.closest("[data-name]");
    if (name) startRename(name);
  });
  subjList.addEventListener("change", (ev) => {
    const c = ev.target.closest("[data-color]");
    if (c) mutate(() => { const s = subj(c.dataset.color); if (s) s.color = c.value; });
  });

  function armSubject(id, toggle) {
    if (toggle && tool.kind === "paint" && tool.subject === id) tool = { kind: "select" };
    else tool = { kind: "paint", subject: id };
    selectedId = null;
    renderSide(); renderGrid();
  }
  function setTool(kind) { tool = { kind }; selectedId = null; renderSide(); renderGrid(); }
  root.querySelectorAll("[data-tool]").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));

  function removeSubject(id) {
    const s = subj(id);
    if (!s) return;
    const n = S.entries.filter((e) => e.subject === id).length;
    if (n && !confirm(`Delete "${s.name}" and its ${n} block${n > 1 ? "s" : ""} from the timetable?`)) return;
    if (tool.kind === "paint" && tool.subject === id) tool = { kind: "select" };
    mutate(() => {
      S.subjects = S.subjects.filter((x) => x.id !== id);
      S.entries = S.entries.filter((e) => e.subject !== id);
    });
  }

  function startRename(span) {
    const s = subj(span.dataset.name);
    if (!s) return;
    span.innerHTML = `<input value="${esc(s.name)}" maxlength="40" aria-label="Rename subject">`;
    const input = span.querySelector("input");
    input.focus(); input.select();
    let done = false;
    const finish = (save) => {
      if (done) return; done = true;
      const v = input.value.trim();
      if (save && v && v !== s.name) mutate(() => { s.name = v; }); else renderSide();
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
    input.addEventListener("blur", () => finish(true));
  }

  $("tt-add").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = $("tt-new");
    const name = input.value.trim();
    if (!name) return;
    input.value = "";
    const existing = S.subjects.find((s) => s.name.toLowerCase() === name.toLowerCase());
    if (existing) { armSubject(existing.id, false); return; }
    const used = new Set(S.subjects.map((s) => s.color));
    const color = PALETTE.find((c) => !used.has(c)) || PALETTE[S.subjects.length % PALETTE.length];
    const id = "s" + uid();
    mutate(() => { S.subjects.push({ id, name: name.slice(0, 40), color }); });
    armSubject(id, false);          // ready to paint straight away
  });

  // ------------------------------------------------------------------ toolbar
  function setSetting(patch) {
    Object.assign(S.settings, patch);
    selectedId = null;
    render(); scheduleSave();
  }
  $("tt-slot").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-slot]");
    if (b) setSetting({ slot: +b.dataset.slot });
  });
  $("tt-custom").addEventListener("change", (ev) => {
    const v = Math.round(Number(ev.target.value) / 5) * 5;
    if (v >= 5 && v <= 240) setSetting({ slot: v }); else renderBar();
  });
  $("tt-from").addEventListener("change", (ev) => {
    const v = +ev.target.value;
    setSetting({ dayStart: v, dayEnd: Math.max(S.settings.dayEnd, v + 60) });
  });
  $("tt-to").addEventListener("change", (ev) => {
    const v = +ev.target.value;
    setSetting({ dayEnd: v, dayStart: Math.min(S.settings.dayStart, v - 60) });
  });
  $("tt-days").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-day]");
    if (!b) return;
    const days = [...S.settings.days];
    days[+b.dataset.day] = !days[+b.dataset.day];
    if (days.some(Boolean)) setSetting({ days });
  });
  // Study-day settings live on the server (they drive the day tracker, not the grid).
  $("tt-break").innerHTML = [0, 1, 2, 3, 4, 5].map((m) => `<option value="${m}">${m ? m + " min" : "None"}</option>`).join("");
  $("tt-dayend").innerHTML = Array.from({ length: 9 }, (_, h) => `<option value="${h}">${pad(h)}:00 next day</option>`).join("");
  async function daySettings(patch) {
    try {
      const r = await fetch("/api/day/settings", patch ? { method: "POST", headers: { "Content-Type": "application/json" },
                                                            body: JSON.stringify(patch) } : { cache: "no-store" });
      const s = await r.json();
      $("tt-break").value = String(s.break_min);
      $("tt-dayend").value = String(s.day_end_hour);
    } catch {}
  }
  $("tt-break").addEventListener("change", (e) => daySettings({ break_min: +e.target.value }));
  $("tt-dayend").addEventListener("change", (e) => daySettings({ day_end_hour: +e.target.value }));
  daySettings();

  $("tt-undo").addEventListener("click", undo);
  $("tt-redo").addEventListener("click", redo);
  $("tt-clear").addEventListener("click", () => {
    if (confirm("Remove every block from the timetable? (Subjects are kept, and you can undo this.)")) {
      selectedId = null;
      mutate(() => { S.entries = []; });
    }
  });

  // ------------------------------------------------------------------ keyboard
  document.addEventListener("keydown", (ev) => {
    if (root.closest("[hidden]")) return;                       // tab not visible
    if (ev.target.closest("input, select, textarea")) return;
    const k = ev.key.toLowerCase(), mod = ev.ctrlKey || ev.metaKey;
    if (mod && k === "z") { ev.preventDefault(); ev.shiftKey ? redo() : undo(); }
    else if (mod && k === "y") { ev.preventDefault(); redo(); }
    else if (mod) return;
    else if (k === "escape") setTool("select");
    else if (k === "e") setTool("erase");
    else if ((k === "delete" || k === "backspace") && selectedId) { ev.preventDefault(); deleteBlock(selectedId); }
    else if (/^[1-9]$/.test(k) && S.subjects[+k - 1]) armSubject(S.subjects[+k - 1].id, false);
  });

  render();
  load();
})();
