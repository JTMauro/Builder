"use strict";
/* ================================================================ studio
   Make a scene without code: a stage and a place, a cast placed on a floor plan, a list of beats (who says what,
   moves, where they look, camera shots, walking, props). Preview plays it live in the stage's studio.html
   (?preview, the scene arrives by postMessage); Render runs scene_video as a background job; the director
   (direct_scene) plans one from plain words. Saved scenes live in the shared library (library_save_scene /
   library_get / library_list / library_delete_scene). Any of the ~2,400 CMU moves can be found (find_moves) and added
   to the stage (fetch_moves). Beats can use set props: sit on a bench, lean on a table, open a door.
   Uses app.js helpers: $, h, api, store, setBusy, contentUrl, loadFiles. */

const SHOTS = ["", "wide", "two", "medium", "close", "over"];
const studio = {
  loaded: false, stages: [], voices: {}, info: null, props: [], setProps: [], library: [],
  cast: [], beats: [], job: null, previewing: false,
};
const STUDIO_DRAFT = "mb_studio_draft", STUDIO_TPL = "mb_studio_templates";   // STUDIO_TPL: old browser-only templates (moved to the library once)
const PROP_MOVES = ["sit", "lean", "open"];
async function op(name, params) {   // run an operation and wait for it
  const job = await api(`/ops/${name}`, { method: "POST", json: params });
  if (job.status !== "succeeded") throw new Error(job.error || `${name} didn't finish`);
  return job;
}
const tplName = (s) => {   // library names: lower-case letters, digits, - or _, starting with a letter, up to 32
  let n = s.toLowerCase().trim().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
  if (!/^[a-z]/.test(n)) n = "scene-" + n;
  return n.slice(0, 32).replace(/-+$/, "");
};

const num = (v, d = 0) => (v === "" || v == null || Number.isNaN(+v) ? d : +v);
const opt = (value, text, selected) => h("option", { value, text: text ?? value, selected: selected ? true : null });
function selectEl(values, current, attrs = {}) {
  return h("select", attrs, values.map((v) => (Array.isArray(v) ? opt(v[0], v[1], v[0] === current) : opt(v, v || "—", v === current))));
}

async function loadStudio() {
  if (studio.loaded) return;
  studio.loaded = true;
  try {
    const r = await api("/studio/stages");
    studio.stages = r.stages.filter((s) => s.studio);
    studio.voices = r.voices;
  } catch (e) { $("#st-status").textContent = e.message; }
  const has = studio.stages.length > 0;
  $("#st-empty").hidden = has;
  $("#st-body").hidden = !has;
  if (!has) return;
  for (const r of ["720x1280", "1080x1920", "1080x1080"]) {   // vertical / square sizes for shorts
    if (![...$("#st-res").options].some((o) => o.value === r)) $("#st-res").append(opt(r, `${r}${r === "1080x1080" ? " (square)" : " (vertical)"}`));
  }
  $("#st-stage").replaceChildren(...studio.stages.map((s) => opt(s.name, s.name[0].toUpperCase() + s.name.slice(1))));
  let draft = null;
  try { draft = JSON.parse(store.get(STUDIO_DRAFT) || "null"); } catch { /* no draft */ }
  const name = studio.stages.some((s) => s.name === draft?.stage) ? draft.stage : studio.stages.some((s) => s.name === "teen") ? "teen" : studio.stages[0].name;
  await pickStage(name, draft);
  await migrateTemplates();
  renderTemplates();
  addMoveFinder();
}

async function pickStage(name, scene = null) {
  studio.info = studio.stages.find((s) => s.name === name);
  $("#st-stage").value = name;
  $("#st-set").replaceChildren(opt("", "Grey studio"), ...studio.info.sets.map((s) => opt(s, s.replace(/_/g, " "))));
  studio.props = [];
  try {
    const r = await fetch(`/stages/${encodeURIComponent(name)}/props/index.json`, { credentials: "same-origin" });
    if (r.ok) studio.props = await r.json();
  } catch { /* a stage without props */ }
  applyScene(scene || defaultScene());
  await loadSetProps();
}
async function loadSetProps() {   // the props on the chosen set that people can use (sit / lean / open)
  studio.setProps = [];
  const set = $("#st-set").value;
  if (set) {
    try {
      const r = await fetch(`/stages/${encodeURIComponent(studio.info.name)}/sets/${encodeURIComponent(set)}/set.json`, { credentials: "same-origin" });
      if (r.ok) {
        const spec = await r.json();
        const seen = {};
        for (const p of spec.props || []) {
          const id = p.id || (p.file || "").split("/")[0];
          if (!p.use || !id) continue;
          seen[id] = (seen[id] || 0) + 1;
          studio.setProps.push({ id: seen[id] > 1 ? `${id}#${seen[id]}` : id, uses: Object.keys(p.use) });
        }
      }
    } catch { /* no props */ }
  }
  renderBeats();
}

function defaultScene() {
  const models = studio.info.models;
  const two = models.length > 1;
  return {
    set: studio.info.sets.includes("park") ? "park" : "",
    cast: two ? [{ id: "a", model: models.includes("teen") ? "teen" : models[0], voice: "am_puck", at: [-0.7, 0], face: 35 },
      { id: "b", model: models.find((m) => m !== "teen") || models[1], voice: "am_liam", at: [0.7, 0], face: -35 }]
      : [{ id: "a", model: models[0] || "teen", voice: "am_puck", at: [0, 0], face: 0 }],
    beats: [
      { shot: { type: "wide" }, actor: "a", move: "wave", look: two ? "b" : "camera", say: "Hey! Over here!" },
      ...(two ? [{ actor: "b", look: "a", move: "laugh", say: "There you are!", shot: { type: "over", actor: "a", target: "b" } }] : []),
    ],
  };
}

/* ---------------------------------------------------------------- scene <-> editor */
function applyScene(sc) {
  $("#st-set").value = sc.set && studio.info.sets.includes(sc.set) ? sc.set : "";
  studio.cast = (sc.cast?.length ? sc.cast : [{ id: "a", model: "teen" }]).slice(0, 6).map((c) => ({
    id: String(c.id).slice(0, 20), model: studio.info.models.includes(c.model) ? c.model : studio.info.models[0] || "teen",
    voice: c.voice || "am_puck", x: num(c.at?.[0]), z: num(c.at?.[1]), face: num(c.face),
  }));
  studio.beats = (sc.beats || []).map((b) => {
    const prop = b.prop ?? (typeof b.hold === "object" || b.hold === "none" ? b.hold : null);
    return {
      actor: b.actor || "", say: b.say || "", move: b.move || "", look: b.look || "",
      shot: b.shot?.type || "", shotActor: b.shot?.actor || "", shotTarget: b.shot?.target || "", glide: num(b.shot?.move),
      wx: b.walk_to ? b.walk_to[0] : "", wz: b.walk_to ? b.walk_to[1] : "", run: !!b.run,
      hold: typeof b.hold === "number" ? b.hold : 2.5,
      prop: prop === "none" ? "none" : prop?.prop || "", hand: prop?.hand || "right", on: b.on || "",
    };
  });
  if (!studio.beats.length) studio.beats.push(blankBeat());
  for (const [k, id] of [["resolution", "#st-res"], ["fps", "#st-fps"], ["music", "#st-music"], ["look", "#st-look"], ["quality", "#st-quality"]]) {
    if (sc[k] != null) $(id).value = sc[k];
  }
  if (sc.captions != null) $("#st-captions").checked = !!sc.captions;
  renderCast();
  renderBeats();
}

const blankBeat = () => ({ actor: "", say: "", move: "", look: "", shot: "", shotActor: "", shotTarget: "", glide: 0, wx: "", wz: "", run: false, hold: 2.5, prop: "", hand: "right", on: "" });

function sceneParams() {
  const ids = studio.cast.map((c) => c.id);
  const cast = studio.cast.map((c) => ({ id: c.id, model: c.model, voice: c.voice, at: [num(c.x), num(c.z)], face: num(c.face) }));
  const beats = studio.beats.map((b) => {
    const o = { hold: num(b.hold, 2.5) };
    if (b.actor && ids.includes(b.actor)) o.actor = b.actor;
    if (b.say.trim()) o.say = b.say.trim();
    if (b.move) o.move = b.move;
    if (b.look) o.look = b.look;
    if (b.shot) {
      o.shot = { type: b.shot, move: num(b.glide) };
      if (b.shotActor) o.shot.actor = b.shotActor;
      if (b.shotTarget) o.shot.target = b.shotTarget;
    }
    if (b.wx !== "" && b.wz !== "") { o.walk_to = [num(b.wx), num(b.wz)]; if (b.run) o.run = true; }
    if (b.prop) o.prop = b.prop === "none" ? "none" : { prop: b.prop, hand: b.hand };
    if (b.on && PROP_MOVES.includes(b.move)) o.on = b.on;
    return o;
  });
  return {
    stage: studio.info.name, set: $("#st-set").value || null, cast, beats,
    resolution: $("#st-res").value, fps: num($("#st-fps").value, 30), music: $("#st-music").value, look: $("#st-look").value,
    quality: $("#st-quality").value, captions: $("#st-captions").checked, voice: studio.cast[0]?.voice || "am_puck",
  };
}
function saveDraft() { store.set(STUDIO_DRAFT, JSON.stringify(sceneParams())); }

/* ---------------------------------------------------------------- cast + floor plan */
function renderCast() {
  const list = $("#st-cast");
  const voices = Object.entries(studio.voices).map(([k, v]) => [k, `${k} · ${v}`]);
  list.replaceChildren(...studio.cast.map((c, i) => h("div", { class: "row cast-row" },
    h("span", { class: "dot", style: `background:${castColor(i)}` }),
    h("label", { class: "field" }, h("span", { text: "Name" }), h("input", { value: c.id, maxlength: 20, oninput: (e) => { renameActor(c, e.target.value); } })),
    h("label", { class: "field" }, h("span", { text: "Character" }), selectEl(studio.info.models, c.model, { onchange: (e) => { c.model = e.target.value; saveDraft(); } })),
    h("label", { class: "field" }, h("span", { text: "Voice" }), selectEl(voices, c.voice, { onchange: (e) => { c.voice = e.target.value; saveDraft(); } })),
    h("label", { class: "field num" }, h("span", { text: "x (m)" }), h("input", { type: "number", step: "0.1", value: c.x, oninput: (e) => { c.x = num(e.target.value); renderFloor(); saveDraft(); } })),
    h("label", { class: "field num" }, h("span", { text: "z (m)" }), h("input", { type: "number", step: "0.1", value: c.z, oninput: (e) => { c.z = num(e.target.value); renderFloor(); saveDraft(); } })),
    h("label", { class: "field num" }, h("span", { text: "Facing °" }), h("input", { type: "number", step: "15", value: c.face, oninput: (e) => { c.face = num(e.target.value); renderFloor(); saveDraft(); } })),
    h("button", { type: "button", class: "btn ghost small", "aria-label": `Remove ${c.id}`, disabled: studio.cast.length < 2 ? true : null,
      onclick: () => { studio.cast.splice(i, 1); renderCast(); renderBeats(); saveDraft(); } }, "✕"),
  )));
  $("#st-add-cast").disabled = studio.cast.length >= 6;
  renderFloor();
}
const castColor = (i) => ["#ff7a1a", "#1fb5a8", "#7c5cff", "#d6363d", "#1b9e5a", "#c9a005"][i % 6];

function renameActor(c, name) {
  const id = name.trim().replace(/\s+/g, "_").slice(0, 20);
  if (!id || studio.cast.some((x) => x !== c && x.id === id)) return;
  for (const b of studio.beats) for (const k of ["actor", "look", "shotActor", "shotTarget"]) if (b[k] === c.id) b[k] = id;
  c.id = id;
  renderBeats(); renderFloor(); saveDraft();
}

function renderFloor() {
  const svg = $("#st-floor");
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
  const kids = [el("circle", { cx: 0, cy: 0, r: 4, class: "floor-ring" })];
  for (let g = -4; g <= 4; g += 1) kids.push(el("line", { x1: g, y1: -4, x2: g, y2: 4, class: "floor-grid" }), el("line", { x1: -4, y1: g, x2: 4, y2: g, class: "floor-grid" }));
  kids.push(el("path", { d: "M-0.35 4.35 L0 3.85 L0.35 4.35 Z", class: "floor-cam" }));
  studio.beats.forEach((b) => {   // walk targets
    if (b.wx === "" || b.wz === "") return;
    const i = Math.max(0, studio.cast.findIndex((c) => c.id === (b.actor || studio.cast[0]?.id)));
    kids.push(el("path", { d: `M${num(b.wx) - 0.15} ${num(b.wz) - 0.15}L${num(b.wx) + 0.15} ${num(b.wz) + 0.15}M${num(b.wx) + 0.15} ${num(b.wz) - 0.15}L${num(b.wx) - 0.15} ${num(b.wz) + 0.15}`, stroke: castColor(i), class: "floor-walk" }));
  });
  studio.cast.forEach((c, i) => {
    const f = (c.face * Math.PI) / 180;
    const g = el("g", { class: "floor-actor", tabindex: 0, "aria-label": `${c.id} at ${c.x}, ${c.z}` });
    g.append(el("line", { x1: c.x, y1: c.z, x2: c.x + Math.sin(f) * 0.6, y2: c.z + Math.cos(f) * 0.6, stroke: castColor(i), class: "floor-face" }),
      el("circle", { cx: c.x, cy: c.z, r: 0.28, fill: castColor(i) }));
    const t = el("text", { x: c.x, y: c.z + 0.1, class: "floor-label" }); t.textContent = c.id.slice(0, 3); g.append(t);
    g.addEventListener("pointerdown", (e) => dragActor(e, c));
    g.addEventListener("keydown", (e) => {
      const d = { ArrowLeft: [-0.1, 0], ArrowRight: [0.1, 0], ArrowUp: [0, -0.1], ArrowDown: [0, 0.1] }[e.key];
      if (!d) return;
      e.preventDefault(); c.x = +(c.x + d[0]).toFixed(2); c.z = +(c.z + d[1]).toFixed(2); renderCast(); saveDraft();
    });
    kids.push(g);
  });
  svg.replaceChildren(...kids);
}
function dragActor(e, c) {
  e.preventDefault();
  const svg = $("#st-floor");
  const move = (ev) => {
    const pt = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(svg.getScreenCTM().inverse());
    c.x = +Math.max(-4, Math.min(4, pt.x)).toFixed(1); c.z = +Math.max(-4, Math.min(4, pt.y)).toFixed(1);
    renderFloor();
  };
  const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up); renderCast(); saveDraft(); };
  addEventListener("pointermove", move);
  addEventListener("pointerup", up);
}

/* ---------------------------------------------------------------- beats */
function renderBeats() {
  const ids = studio.cast.map((c) => c.id);
  const actorOpts = [["", `(${ids[0] || "first"})`], ...ids.map((x) => [x, x])];
  const box = $("#st-beats");
  box.replaceChildren(...studio.beats.map((b, i) => {
    const set = (k, rerender = false) => (e) => { b[k] = e.target.type === "checkbox" ? e.target.checked : e.target.value; saveDraft(); if (rerender) renderBeats(); if (k === "wx" || k === "wz" || k === "actor") renderFloor(); };
    const field = (label, control, cls = "") => h("label", { class: `field ${cls}` }, h("span", { text: label }), control);
    return h("div", { class: "beat" },
      h("div", { class: "beat-head" }, h("strong", { text: `Beat ${i + 1}` }),
        h("span", { class: "beat-tools" },
          h("button", { type: "button", class: "btn ghost small", "aria-label": `Move beat ${i + 1} up`, disabled: i === 0 ? true : null, onclick: () => { studio.beats.splice(i - 1, 0, studio.beats.splice(i, 1)[0]); renderBeats(); saveDraft(); } }, "↑"),
          h("button", { type: "button", class: "btn ghost small", "aria-label": `Move beat ${i + 1} down`, disabled: i === studio.beats.length - 1 ? true : null, onclick: () => { studio.beats.splice(i + 1, 0, studio.beats.splice(i, 1)[0]); renderBeats(); saveDraft(); } }, "↓"),
          h("button", { type: "button", class: "btn ghost small", "aria-label": `Copy beat ${i + 1}`, onclick: () => { studio.beats.splice(i + 1, 0, { ...b }); renderBeats(); saveDraft(); } }, "⧉"),
          h("button", { type: "button", class: "btn ghost small", "aria-label": `Remove beat ${i + 1}`, disabled: studio.beats.length < 2 ? true : null, onclick: () => { studio.beats.splice(i, 1); renderBeats(); renderFloor(); saveDraft(); } }, "✕"))),
      h("div", { class: "beat-grid" },
        field("Who", selectEl(actorOpts, b.actor, { onchange: set("actor") })),
        field("Says", h("input", { value: b.say, maxlength: 600, placeholder: "(nothing)", oninput: set("say") }), "wide2"),
        field("Move", selectEl(["", ...new Set([...(studio.info.moves || []), ...(studio.setProps.length ? ["lean", "open"] : [])])], b.move, { onchange: set("move", true) })),
        ...(PROP_MOVES.includes(b.move) && studio.setProps.some((p) => p.uses.includes(b.move)) ? [
          field("On", selectEl([["", "(no prop)"], ...studio.setProps.filter((p) => p.uses.includes(b.move)).map((p) => [p.id, p.id.replace(/_/g, " ").replace(/ 01$/, "")])], b.on, { onchange: set("on") })),
        ] : []),
        field("Looks at", selectEl([["", "(as before)"], ["camera", "camera"], ...ids.map((x) => [x, x])], b.look, { onchange: set("look") })),
        field("Shot", selectEl(SHOTS.map((s) => [s, s || "(keep)"]), b.shot, { onchange: set("shot", true) })),
        ...(b.shot ? [
          field("Shot on", selectEl(actorOpts, b.shotActor, { onchange: set("shotActor") })),
          ...(b.shot === "over" ? [field("Looking at", selectEl(actorOpts, b.shotTarget, { onchange: set("shotTarget") }))] : []),
          field("Glide (s)", h("input", { type: "number", min: 0, max: 20, step: 0.5, value: b.glide, oninput: set("glide") }), "num"),
        ] : []),
        field("Walk to x", h("input", { type: "number", step: 0.1, value: b.wx, placeholder: "—", oninput: set("wx") }), "num"),
        field("z", h("input", { type: "number", step: 0.1, value: b.wz, placeholder: "—", oninput: set("wz") }), "num"),
        h("label", { class: "field check" }, h("input", { type: "checkbox", checked: b.run ? true : null, onchange: set("run") }), h("span", { text: "Run" })),
        ...(studio.props.length ? [
          field("Holds", selectEl([["", "(as before)"], ["none", "nothing"], ...studio.props.map((p) => [p, p.replace(/_/g, " ").replace(/ 01$/, "")])], b.prop, { onchange: set("prop", true) })),
          ...(b.prop && b.prop !== "none" ? [field("Hand", selectEl([["right", "right"], ["left", "left"]], b.hand, { onchange: set("hand") }))] : []),
        ] : []),
        field("Hold (s)", h("input", { type: "number", min: 0, max: 30, step: 0.5, value: b.hold, oninput: set("hold") }), "num"),
      ));
  }));
  $("#st-add-beat").disabled = studio.beats.length >= 60;
}

/* ---------------------------------------------------------------- preview, render, director */
function showStage(which) {   // "frame" | "video" | "none"
  $("#st-frame").hidden = which !== "frame";
  $("#st-video").hidden = which !== "video";
  $("#st-placeholder").hidden = which !== "none";
  if (which !== "video") $("#st-video").pause();
  if (which !== "frame") $("#st-frame").src = "about:blank";
}
function preview() {
  saveDraft();
  showStage("frame");
  $("#st-frame").src = `/stages/${encodeURIComponent(studio.info.name)}/studio.html?preview`;
  $("#st-status").textContent = "Loading the preview…";
}
addEventListener("message", (e) => {
  const frame = $("#st-frame");
  if (e.origin !== location.origin || e.source !== frame.contentWindow || e.data?.type !== "studio-waiting") return;
  frame.contentWindow.postMessage({ type: "builder-scene", scene: sceneParams() }, location.origin);
  $("#st-status").textContent = "Preview: voices come with Render; it loops.";
});

async function runJob(operation, params, label, onDone) {
  if (studio.job) return;
  const status = $("#st-status");
  setBusy(1);
  const t0 = Date.now();
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    status.textContent = `${label}… ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }, 1000);
  $("#st-render").disabled = $("#st-direct-go").disabled = true;
  try {
    let job = await api("/jobs", { method: "POST", json: { operation, params } });
    studio.job = job.id;
    while (job.status === "queued" || job.status === "running") job = await api(`/jobs/${job.id}?wait=10`);
    if (job.status !== "succeeded") throw new Error(job.error || "It stopped without a result");
    const vid = job.outputs.find((o) => o.mime_type.startsWith("video/")) || job.outputs[0];
    const v = $("#st-video");
    v.src = contentUrl(vid.id);
    showStage("video");
    const dl = $("#st-download");
    dl.href = `/files/${vid.id}/content`; dl.setAttribute("download", vid.name); dl.hidden = false;
    status.textContent = `Done: ${vid.name}${job.data?.duration_s ? ` (${job.data.duration_s} s)` : ""}. It's in your audio & video library too.`;
    if (typeof loadFiles === "function") loadFiles().catch(() => {});
    onDone?.(job);
  } catch (e) {
    status.textContent = "";
    throw e;
  } finally {
    clearInterval(tick);
    studio.job = null;
    setBusy(-1);
    $("#st-render").disabled = $("#st-direct-go").disabled = false;
  }
}

function friendly(msg) {
  if (/ANTHROPIC|credential|api key|authentication/i.test(msg)) {
    return "The director needs a Claude API key. An admin can add one in Settings (user menu › Settings).";
  }
  return msg;
}

async function render() {
  saveDraft();
  $("#st-download").hidden = true;
  try {
    await runJob("scene_video", sceneParams(), "Rendering (voices, frames, sound)");
  } catch (e) { $("#st-status").textContent = `Render failed: ${e.message}`; }
}

async function direct() {
  const text = $("#st-direct").value.trim();
  const out = $("#st-direct-out");
  if (text.length < 3) { out.textContent = "Say a little more about what should happen."; return; }
  out.replaceChildren();
  try {
    await runJob("direct_scene", { request: text, stage: studio.info.name, resolution: $("#st-res").value }, "The director is at work", (job) => {
      const plan = job.data?.plan;
      out.replaceChildren(h("span", { text: job.data?.title ? `“${job.data.title}” is ready. ` : "Ready. " }),
        plan ? h("button", { type: "button", class: "btn small", text: "Edit this plan", onclick: () => { applyScene({ ...plan, beats: plan.beats }); saveDraft(); } }) : "");
    });
  } catch (e) {
    out.replaceChildren(h("div", { class: "form-error", role: "alert", text: friendly(e.message) }));
  }
}

/* ---------------------------------------------------------------- templates (the shared library) */
async function refreshLibrary() {
  try { studio.library = (await op("library_list", {})).data.scenes || []; } catch (e) { $("#st-status").textContent = `Saved scenes: ${e.message}`; }
}
async function renderTemplates() {
  await refreshLibrary();
  const names = studio.library.map((t) => t.name).sort();
  $("#st-tpl-list").replaceChildren(...(names.length ? names.map((n) => opt(n)) : [opt("", "No saved scenes yet")]));
  $("#st-tpl-load").disabled = $("#st-tpl-del").disabled = !names.length;
}
async function migrateTemplates() {   // scenes saved in this browser before the library: move them there once
  let old = {};
  try { old = JSON.parse(store.get(STUDIO_TPL) || "{}"); } catch { old = {}; }
  const names = Object.keys(old);
  if (!names.length) return;
  const left = {};
  for (const n of names) {
    try { await op("library_save_scene", { name: tplName(n), params: old[n], description: `From this browser: ${n}` }); }
    catch (e) { if (!/exists/.test(e.message)) left[n] = old[n]; }
  }
  if (Object.keys(left).length) store.set(STUDIO_TPL, JSON.stringify(left)); else store.set(STUDIO_TPL, "{}");
}

/* ---------------------------------------------------------------- find any CMU move */
function addMoveFinder() {
  if ($("#st-move-find")) return;
  const input = h("input", { id: "st-move-find", maxlength: 120, placeholder: "Find a move: sword fight, sit down, drunk walk…", "aria-label": "Find a move" });
  const out = h("div", { class: "small-text", id: "st-move-results", "aria-live": "polite" });
  const go = async () => {
    const words = input.value.trim();
    if (!words) return;
    out.textContent = "Searching…";
    try {
      const r = (await op("find_moves", { words, stage: studio.info.name, limit: 8 })).data;
      out.replaceChildren(...(r.moves.length ? r.moves.map((m) => h("button", {
        type: "button", class: "btn ghost small", title: `${m.title} · ${m.category} · CMU ${m.id}`,
        text: `${m.on_stage ? "✓ " : "+ "}${m.stage_label || m.label} (${Math.round(m.seconds)} s)`,
        onclick: () => addMove(m, out),
      })) : [h("span", { text: "Nothing found; try other words." })]));
    } catch (e) { out.textContent = e.message; }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  const row = h("div", { class: "row" }, input, h("button", { type: "button", class: "btn small", text: "Find", onclick: go }));
  $("#st-add-beat").after(h("div", { class: "move-finder" }, h("strong", { text: "Any move (2,400 from CMU motion capture)" }), row, out));
}
async function addMove(m, out) {
  const label = m.stage_label || m.label;
  out.textContent = `Adding “${label}” to the stage…`;
  try {
    if (!m.on_stage) await op("fetch_moves", { stage: studio.info.name, moves: [m.id], labels: [label] });
    if (!studio.info.moves.includes(label)) studio.info.moves.push(label);
    renderBeats();
    out.textContent = `“${label}” is ready: pick it as a beat's Move.`;
  } catch (e) { out.textContent = e.message; }
}

/* ---------------------------------------------------------------- wiring */
$("#st-stage").addEventListener("change", (e) => { pickStage(e.target.value); showStage("none"); saveDraft(); });
$("#st-set").addEventListener("change", () => { saveDraft(); loadSetProps(); });
for (const id of ["#st-res", "#st-fps", "#st-music", "#st-look", "#st-quality", "#st-captions"]) $(id).addEventListener("change", saveDraft);
$("#st-add-cast").addEventListener("click", () => {
  let n = studio.cast.length; let id;
  do { id = String.fromCharCode(97 + n++); } while (studio.cast.some((c) => c.id === id));
  studio.cast.push({ id, model: studio.info.models[studio.cast.length % Math.max(1, studio.info.models.length)] || "teen", voice: "am_puck", x: 0, z: 1, face: 0 });
  renderCast(); renderBeats(); saveDraft();
});
$("#st-add-beat").addEventListener("click", () => { studio.beats.push(blankBeat()); renderBeats(); saveDraft(); });
$("#st-preview").addEventListener("click", preview);
$("#st-render").addEventListener("click", render);
$("#st-direct-go").addEventListener("click", direct);
$("#st-tpl-save").addEventListener("click", async () => {
  const raw = $("#st-tpl-name").value.trim();
  if (!raw) { $("#st-tpl-name").focus(); return; }
  const name = tplName(raw);
  const exists = studio.library.some((t) => t.name === name);
  if (exists && !confirm(`Replace the saved scene “${name}”?`)) return;
  try {
    await op("library_save_scene", { name, params: sceneParams(), description: raw, overwrite: exists });
    $("#st-tpl-name").value = "";
    await renderTemplates(); $("#st-tpl-list").value = name;
    $("#st-status").textContent = `Saved “${name}” to the library.`;
  } catch (e) { $("#st-status").textContent = `Couldn't save: ${e.message}`; }
});
$("#st-tpl-load").addEventListener("click", async () => {
  const name = $("#st-tpl-list").value;
  if (!name) return;
  try {
    const sc = (await op("library_get", { name, kind: "scene" })).data.scene_video_params;
    if (sc.stage && sc.stage !== studio.info.name && studio.stages.some((s) => s.name === sc.stage)) await pickStage(sc.stage, sc);
    else { applyScene(sc); await loadSetProps(); }
    saveDraft();
  } catch (e) { $("#st-status").textContent = `Couldn't load: ${e.message}`; }
});
$("#st-tpl-del").addEventListener("click", async () => {
  const name = $("#st-tpl-list").value;
  if (!name || !confirm(`Delete the saved scene “${name}”?`)) return;
  try { await op("library_delete_scene", { name }); await renderTemplates(); } catch (e) { $("#st-status").textContent = e.message; }
});
