"use strict";

/* ================================================================ helpers */
const $ = (s, el = document) => el.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  sget(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  sset(k, v) { try { sessionStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(kid));
  return el;
}
const fmtBytes = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const kind = (f) => (f.mime_type.startsWith("video/") ? "video" : f.mime_type.startsWith("audio/") ? "audio" : f.mime_type.startsWith("image/") ? "image" : "file");
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ================================================================ API */
let token = store.get("mb_token") || "";
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { askToken(); throw new Error("This server needs an access token"); }
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    throw new Error(Array.isArray(d) ? d.map((e) => `${(e.loc || []).slice(-1)[0] || ""}: ${e.msg}`).join("\n") : d || res.statusText);
  }
  return body;
}
const contentUrl = (id) => `/files/${id}/content?inline=true${token ? `&access_token=${encodeURIComponent(token)}` : ""}`;

function askToken() {
  const dlg = $("#token-dialog");
  $("#token-btn").hidden = false;
  if (dlg.open) return;
  $("#token-input").value = token;
  dlg.showModal();
}
$("#token-dialog").addEventListener("close", () => {
  if ($("#token-dialog").returnValue === "ok") {
    token = $("#token-input").value.trim();
    store.set("mb_token", token);
    boot();
  }
});
$("#token-btn").addEventListener("click", askToken);

/* ================================================================ state */
const state = { files: [], ops: [], selected: null, checked: [], session: null, activeOp: null, busy: 0 };

const OP_META = {
  probe: ["🔍", "Inspect"], convert: ["🔁", "Convert"], trim: ["✂️", "Trim"], extract_audio: ["🎧", "Extract audio"],
  resize: ["📐", "Resize"], thumbnail: ["🖼️", "Thumbnail"], frames: ["🎞️", "Frames"], normalize_audio: ["🔊", "Normalize"],
  change_speed: ["⏩", "Speed"], concat: ["🔗", "Join clips"], replace_audio: ["🎵", "Soundtrack"], mute: ["🔇", "Mute"],
  make_gif: ["🌀", "GIF"], waveform: ["〰️", "Waveform"], fade: ["🌅", "Fade"], add_text: ["🔤", "Caption"],
  images_to_video: ["🎬", "Animate frames"], animate_image: ["✨", "Animate image"],
};
const ORDER = ["animate_image", "images_to_video", "trim", "add_text", "replace_audio", "concat", "convert", "make_gif",
  "fade", "change_speed", "resize", "extract_audio", "normalize_audio", "mute", "thumbnail", "frames", "waveform", "probe"];

function setBusy(delta) {
  state.busy = Math.max(0, state.busy + delta);
  $("#mascot").classList.toggle("working", state.busy > 0);
}

/* ================================================================ library */
async function loadFiles(selectId) {
  state.files = await api("/files");
  state.checked = state.checked.filter((id) => state.files.some((f) => f.id === id));
  renderFiles();
  if (selectId) select(selectId);
  else if (state.selected && !state.files.some((f) => f.id === state.selected)) select(null);
}

const AUDIO_ICON = '<svg viewBox="0 0 100 40"><path d="M2 20h6l4-12 6 24 5-30 6 36 5-28 5 20 5-10 5 6 5-14 6 26 5-18 5 8 5-4 4 2h11" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function renderFiles() {
  const grid = $("#file-grid");
  grid.replaceChildren();
  $("#file-count").textContent = state.files.length ? `${state.files.length} file${state.files.length > 1 ? "s" : ""}` : "";
  if (!state.files.length) {
    grid.append(h("div", { class: "muted small-text", text: "Nothing here yet. Upload something to get building." }));
    return;
  }
  for (const f of state.files) {
    const k = kind(f);
    let thumb;
    if (k === "image") thumb = h("div", { class: "thumb" }, h("img", { src: contentUrl(f.id), alt: "", loading: "lazy" }));
    else if (k === "video") {
      const v = h("video", { src: `${contentUrl(f.id)}#t=0.5`, muted: true, preload: "metadata", playsinline: true, loop: true });
      v.muted = true;
      thumb = h("div", { class: "thumb" }, v);
    } else {
      thumb = h("div", { class: "thumb audio" });
      thumb.innerHTML = AUDIO_ICON;
    }
    const order = state.checked.indexOf(f.id);
    const card = h("button", {
      type: "button",
      class: `file-card${state.selected === f.id ? " selected" : ""}${order >= 0 ? " checked" : ""}`,
      title: `${f.name}\nClick to open. Shift-click to pick several (for joining or animating frames).`,
      onclick: (e) => (e.shiftKey || e.metaKey || e.ctrlKey ? toggleCheck(f.id) : select(f.id)),
      onmouseenter: () => { const v = card.querySelector("video"); if (v) v.play().catch(() => {}); },
      onmouseleave: () => { const v = card.querySelector("video"); if (v) { v.pause(); } },
    }, thumb,
      h("span", { class: "badge", text: order >= 0 ? `#${order + 1}` : k }),
      h("div", { class: "file-meta" }, h("div", { class: "file-name", text: f.name }), h("div", { class: "file-sub", text: `${fmtBytes(f.size)}${f.source !== "upload" && f.source !== "import" ? " · " + f.source : ""}` })));
    grid.append(card);
  }
}

function toggleCheck(id) {
  const i = state.checked.indexOf(id);
  if (i >= 0) state.checked.splice(i, 1); else state.checked.push(id);
  renderFiles();
  if (state.activeOp) renderForm(state.activeOp, true);
}

async function select(id) {
  state.selected = id;
  renderFiles();
  const prev = $("#preview"), probe = $("#probe");
  probe.replaceChildren();
  prev.className = "preview";
  if (!id) {
    prev.classList.add("empty");
    prev.replaceChildren(h("div", { class: "preview-empty", text: "Your selected file shows up here" }));
    $("#selected-label").textContent = "Pick a file from the library";
    return;
  }
  const f = state.files.find((x) => x.id === id);
  if (!f) return;
  $("#selected-label").textContent = f.name;
  const k = kind(f);
  if (k === "video") prev.replaceChildren(h("video", { src: contentUrl(id), controls: true, playsinline: true }));
  else if (k === "image") prev.replaceChildren(h("img", { src: contentUrl(id), alt: f.name }));
  else if (k === "audio") { prev.classList.add("audio-mode"); prev.replaceChildren(h("audio", { src: contentUrl(id), controls: true })); }
  else prev.replaceChildren(h("div", { class: "preview-empty", text: "No preview for this file type" }));
  const chips = [h("a", { class: "chip", href: contentUrl(id).replace("inline=true", "inline=false"), download: f.name, text: "⬇ Download" })];
  probe.append(...chips);
  if (k === "video" || k === "audio") {
    try {
      const p = await api(`/files/${id}/probe`);
      if (state.selected !== id) return;
      const c = [];
      if (p.duration_s != null) c.push(`${p.duration_s.toFixed(2)} s`);
      if (p.video) c.push(`${p.video.width}×${p.video.height}`, p.video.fps ? `${p.video.fps} fps` : null, p.video.codec);
      if (p.audio) c.push(`${p.audio.codec} ${p.audio.sample_rate / 1000} kHz`);
      if (!p.has_audio && p.has_video) c.push("no audio");
      probe.append(...c.filter(Boolean).map((t) => h("span", { class: "chip", text: t })));
    } catch { /* probe is best-effort */ }
  }
  if (state.activeOp) renderForm(state.activeOp, true);
}

/* uploads */
const drop = $("#dropzone");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
drop.addEventListener("drop", (e) => upload([...e.dataTransfer.files]));
$("#file-input").addEventListener("change", (e) => { upload([...e.target.files]); e.target.value = ""; });

async function upload(files) {
  if (!files.length) return;
  const status = $("#upload-progress");
  setBusy(1);
  let last = null;
  try {
    for (const [i, file] of files.entries()) {
      status.textContent = `Uploading ${i + 1} of ${files.length}: ${file.name}`;
      const fd = new FormData();
      fd.append("file", file);
      last = await api("/files", { method: "POST", body: fd });
    }
    status.textContent = "";
    await loadFiles(last && last.id);
  } catch (e) {
    status.textContent = `Upload failed: ${e.message}`;
  } finally { setBusy(-1); }
}

/* ================================================================ tools */
function renderTiles() {
  const tiles = $("#op-tiles");
  tiles.replaceChildren();
  const ops = [...state.ops].sort((a, b) => ORDER.indexOf(a.name) - ORDER.indexOf(b.name));
  for (const op of ops) {
    const [ico, nm] = OP_META[op.name] || ["🛠️", op.name];
    tiles.append(h("button", {
      type: "button", class: `op-tile${state.activeOp === op.name ? " active" : ""}`, title: op.description,
      onclick: () => { state.activeOp = op.name; renderTiles(); renderForm(op.name); },
    }, h("span", { class: "ico", text: ico }), h("span", { class: "nm", text: nm })));
  }
}

const LABELS = { file_id: "File", video_id: "Video", audio_id: "Soundtrack", file_ids: "Files (in order)" };
const label = (k) => LABELS[k] || k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

function resolve(schema, prop) {
  // pydantic emits anyOf [{type: X}, {type: null}] for optional fields
  if (prop.anyOf) {
    const real = prop.anyOf.find((p) => p.type !== "null") || {};
    return { ...real, ...prop, anyOf: undefined, nullable: true };
  }
  if (prop.$ref) return { ...schema.$defs[prop.$ref.split("/").pop()], ...prop };
  return prop;
}

function fileOptions(filter, chosen) {
  const sel = h("select", {});
  sel.append(h("option", { value: "", text: "— choose a file —" }));
  for (const f of state.files) {
    if (filter && !filter(f)) continue;
    const o = h("option", { value: f.id, text: `${f.name} (${kind(f)})` });
    if (f.id === chosen) o.selected = true;
    sel.append(o);
  }
  return sel;
}

function renderForm(name, keepValues = false) {
  const op = state.ops.find((o) => o.name === name);
  if (!op) return;
  const form = $("#op-form"), box = $("#op-fields");
  const old = keepValues ? readForm() : {};
  form.hidden = false;
  $("#op-title").textContent = `${(OP_META[name] || ["🛠️"])[0]} ${(OP_META[name] || [0, name])[1]}`;
  $("#op-desc").textContent = op.description;
  box.replaceChildren();
  const schema = op.params_schema, required = new Set(schema.required || []);
  for (const [key, raw] of Object.entries(schema.properties || {})) {
    const p = resolve(schema, raw);
    let input, wide = false;
    const val = old[key] !== undefined ? old[key] : p.default;
    if (key === "file_id" || key === "video_id" || key === "audio_id") {
      const filter = key === "video_id" ? (f) => kind(f) === "video" : key === "audio_id" ? (f) => kind(f) !== "image" : null;
      let chosen = old[key] || (key === "audio_id" ? null : state.selected);
      if (key === "audio_id" && !chosen) chosen = state.checked.find((id) => { const f = state.files.find((x) => x.id === id); return f && kind(f) === "audio"; });
      input = fileOptions(filter, chosen);
    } else if (key === "file_ids") {
      wide = true;
      const ids = state.checked.length ? state.checked : [];
      input = h("div", { class: "probe", "data-ids": ids.join(",") },
        ids.length ? ids.map((id, i) => h("span", { class: "chip", text: `${i + 1}. ${(state.files.find((f) => f.id === id) || {}).name || id}` }))
          : h("span", { class: "muted small-text", text: "Shift-click files in the library, in order, to add them here." }));
    } else if (p.enum) {
      input = h("select", {});
      if (p.nullable) input.append(h("option", { value: "", text: "—" }));
      for (const v of p.enum) { const o = h("option", { value: v, text: String(v).replace(/_/g, " ") }); if (v === val) o.selected = true; input.append(o); }
    } else if (p.type === "boolean") {
      input = h("input", { type: "checkbox" });
      input.checked = !!val;
    } else if (p.type === "number" || p.type === "integer") {
      input = h("input", { type: "number", step: p.type === "integer" ? "1" : "any", min: p.minimum ?? p.exclusiveMinimum, max: p.maximum, placeholder: p.nullable ? "optional" : "" });
      if (val != null) input.value = val;
    } else {
      wide = (p.maxLength || 0) > 100;
      input = wide ? h("textarea", { rows: 2, maxlength: p.maxLength }) : h("input", { type: "text", placeholder: p.nullable ? "optional" : "" });
      if (val != null) input.value = val;
    }
    input.dataset.key = key;
    input.dataset.type = p.type || (p.enum ? "string" : "");
    const isCheck = p.type === "boolean";
    const field = h("label", { class: `field${wide ? " wide" : ""}${isCheck ? " check" : ""}` },
      isCheck ? [input, h("span", { text: label(key) })] : [h("span", { text: label(key) + (required.has(key) ? " *" : "") }), input],
      p.description && !isCheck && !/_ids?$/.test(key) ? h("small", { text: p.description }) : null);
    box.append(field);
  }
  $("#op-status").textContent = "";
}

function readForm() {
  const out = {};
  for (const el of $("#op-fields").querySelectorAll("[data-key]")) {
    const k = el.dataset.key, t = el.dataset.type;
    if (el.dataset.ids !== undefined) { out[k] = el.dataset.ids ? el.dataset.ids.split(",") : []; continue; }
    if (el.type === "checkbox") { out[k] = el.checked; continue; }
    const v = el.value.trim();
    if (v === "") continue;
    out[k] = t === "number" || t === "integer" ? Number(v) : v;
  }
  return out;
}

$("#op-close").addEventListener("click", () => { state.activeOp = null; $("#op-form").hidden = true; renderTiles(); });
$("#op-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const params = readForm(), btn = $("#op-run"), status = $("#op-status");
  btn.disabled = true; status.textContent = "Starting…";
  try {
    const job = await api("/jobs", { method: "POST", json: { operation: state.activeOp, params } });
    status.textContent = "";
    trackJob(job);
  } catch (err) {
    status.textContent = err.message;
  } finally { btn.disabled = false; }
});

/* ================================================================ jobs */
const jobEls = new Map();
function renderJob(job) {
  let el = jobEls.get(job.id);
  const [ico, nm] = OP_META[job.operation] || ["🛠️", job.operation];
  const secs = job.finished_at && job.started_at ? ` · ${(job.finished_at - job.started_at).toFixed(1)} s` : "";
  const body = h("div", { class: "grow" },
    h("div", {}, h("strong", { text: `${ico} ${nm}` }), h("span", { class: "muted", text: ` · ${job.status}${secs}` })),
    job.error ? h("div", { class: "err", text: job.error }) : null,
    job.outputs && job.outputs.length ? h("div", {}, ...job.outputs.map((o) => h("a", { onclick: () => select(o.id), text: `${o.name} ` }))) : null);
  const fresh = h("div", { class: `job ${job.status}` }, h("span", { class: "dot" }), body);
  if (el) el.replaceWith(fresh); else {
    const list = $("#jobs");
    if (!jobEls.size) list.replaceChildren();
    list.prepend(fresh);
  }
  jobEls.set(job.id, fresh);
}

async function trackJob(job) {
  renderJob(job);
  setBusy(1);
  try {
    while (job.status === "queued" || job.status === "running") {
      job = await api(`/jobs/${job.id}?wait=20`);
      renderJob(job);
    }
    if (job.status === "succeeded") await loadFiles(job.outputs[0] && job.outputs[0].id);
  } catch (e) {
    renderJob({ ...job, status: "failed", error: e.message });
  } finally { setBusy(-1); }
}

/* ================================================================ chat */
const log = $("#chat-log");
function addMsg(who, text, extra = []) {
  const m = h("div", { class: `msg ${who}` }, text, ...extra);
  log.append(m);
  log.scrollTop = log.scrollHeight;
  return m;
}
function greet() {
  log.replaceChildren();
  addMsg("bot", `Hi! I'm ${$("#bot-name").textContent}. Tell me what you want to make: trim a clip, add music, caption it, or animate your drawings. I'll build it and check the result.`);
}
$("#chat-reset").addEventListener("click", async () => {
  if (state.session) api(`/assistant/sessions/${state.session}`, { method: "DELETE" }).catch(() => {});
  state.session = null;
  greet();
});
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form").requestSubmit(); }
});
$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chat-input"), text = input.value.trim();
  if (!text) return;
  input.value = "";
  addMsg("user", text);
  const ids = $("#chat-attach").checked ? [...new Set([...(state.selected ? [state.selected] : []), ...state.checked])] : [];
  const typing = addMsg("bot", "", [h("span", { class: "typing" }, h("span"), h("span"), h("span"))]);
  $("#chat-send").disabled = true;
  setBusy(1);
  try {
    const res = await api("/assistant/chat", { method: "POST", json: { message: text, session_id: state.session, file_ids: ids.length ? ids : null } });
    state.session = res.session_id;
    typing.remove();
    const extras = [];
    if (res.tool_calls.length) extras.push(h("div", { class: "steps" }, ...res.tool_calls.map((c) => h("span", { text: `${c.ok ? "✓" : "✗"} ${(OP_META[c.tool] || [0, c.tool])[1]}` }))));
    if (res.new_files.length) {
      const finals = res.new_files.slice(-4);
      extras.push(h("div", { class: "made" }, ...finals.map((f) => h("button", { type: "button", onclick: () => select(f.id), text: `▶ ${f.name}` }))));
    }
    addMsg("bot", res.reply || "(no reply)", extras);
    if (res.new_files.length) await loadFiles(res.new_files[res.new_files.length - 1].id);
  } catch (err) {
    typing.remove();
    addMsg("bot error", err.message);
  } finally { $("#chat-send").disabled = false; setBusy(-1); }
});

/* ================================================================ mascot intro */
const mascot = $("#mascot"), intro = $("#intro");
const ARM = () => mascot.querySelector(".m-arm");
const WHOLE = () => mascot.querySelector(".m-whole");
let skipping = false;

function dock() {
  if (mascot.classList.contains("docked")) return;
  for (const a of document.getAnimations()) {
    if (a.effect && a.effect.target && (mascot.contains(a.effect.target) || a.effect.target === mascot)) a.cancel();
  }
  mascot.classList.remove("live");
  mascot.classList.add("docked");
  mascot.style.cssText = "";
  $("#mascot-slot").append(mascot);
  intro.classList.add("gone");
  document.body.classList.remove("intro-running", "intro-revealing");
  store.sset("mb_intro_seen", "1");
}

function impact(n) {
  intro.classList.remove("shake"); void intro.offsetWidth; intro.classList.add("shake");
  $(".nail-shaft").style.height = `${[30, 14, 0][n]}px`;
  const sparks = $("#sparks");
  for (let i = 0; i < 14; i++) {
    const a = Math.PI * (1 + Math.random()), d = 40 + Math.random() * 90;
    const s = h("span", { class: "spark" });
    s.style.setProperty("--dx", `${Math.cos(a) * d}px`);
    s.style.setProperty("--dy", `${Math.sin(a) * d}px`);
    sparks.append(s);
    setTimeout(() => s.remove(), 600);
  }
  const bang = h("span", { class: "bang", text: ["TAP!", "TAP!", "BANG!"][n] });
  sparks.append(bang);
  setTimeout(() => bang.remove(), 650);
}

async function step(anim) {
  if (skipping) throw new Error("skip");
  await anim.finished;
  if (skipping) throw new Error("skip");
}

async function runIntro() {
  if (reduceMotion || store.sget("mb_intro_seen")) { dock(); return; }
  document.body.classList.add("intro-running");
  await document.fonts?.ready?.catch?.(() => {});
  const s = Math.max(0.6, Math.min(1, window.innerWidth / 1100));
  const W = 220 * s, H = 260 * s;
  const nail = $("#nail").getBoundingClientRect();
  // Hammer head centre lands at (256, 128) in SVG units when the arm is rotated 70deg.
  const x = nail.left + nail.width / 2 - 252 * s;
  const y = nail.top + 4 - 132 * s;
  Object.assign(mascot.style, { width: `${W}px`, height: `${H}px`, transform: `translate(${x}px, ${window.innerHeight + 40}px)` });
  mascot.classList.add("live");
  try {
    await step(mascot.animate(
      [{ transform: `translate(${x}px, ${window.innerHeight + 40}px)` }, { transform: `translate(${x}px, ${y - 30}px)`, offset: .75 }, { transform: `translate(${x}px, ${y}px)` }],
      { duration: 850, easing: "cubic-bezier(.2,.8,.3,1)", fill: "forwards" }));
    for (let i = 0; i < 3; i++) {
      await step(ARM().animate([{ transform: "rotate(0deg)" }, { transform: "rotate(-30deg)" }], { duration: i ? 200 : 320, easing: "ease-out", fill: "forwards" }));
      await step(ARM().animate([{ transform: "rotate(-30deg)" }, { transform: "rotate(70deg)" }], { duration: 110, easing: "cubic-bezier(.6,0,1,1)", fill: "forwards" }));
      impact(i);
      await step(ARM().animate([{ transform: "rotate(70deg)" }, { transform: "rotate(62deg)" }, { transform: "rotate(0deg)" }], { duration: 260, easing: "ease-out", fill: "forwards" }));
    }
    // happy hop, then the doors swing open
    intro.classList.add("open");
    document.body.classList.add("intro-revealing");
    await step(WHOLE().animate([{ transform: "translateY(0)" }, { transform: "translateY(-26px)" }, { transform: "translateY(0)" }], { duration: 420, easing: "ease-in-out" }));
    // fly up into the corner next to the title
    const slot = $("#mascot-slot").getBoundingClientRect();
    const k = slot.width / W;
    await step(mascot.animate(
      [{ transform: `translate(${x}px, ${y}px) scale(1)` },
        { transform: `translate(${(x + slot.left) / 2}px, ${Math.min(y, slot.top) - 120}px) scale(${(1 + k) / 2}) rotate(-12deg)`, offset: .55 },
        { transform: `translate(${slot.left}px, ${slot.top}px) scale(${k})` }],
      { duration: 1000, easing: "cubic-bezier(.45,0,.2,1)", fill: "forwards" }));
    await sleep(250);
  } catch { /* skipped */ }
  dock();
  mascot.classList.add("wave");
  setTimeout(() => mascot.classList.remove("wave"), 1000);
}
$("#skip-intro").addEventListener("click", () => { skipping = true; dock(); });
mascot.addEventListener("click", () => {
  if (!mascot.classList.contains("docked")) return;
  mascot.classList.remove("wave"); void mascot.offsetWidth; mascot.classList.add("wave");
  setTimeout(() => mascot.classList.remove("wave"), 1000);
  $("#chat-input").focus();
});

/* ================================================================ boot */
async function boot() {
  const pill = $("#server-status");
  try {
    await api("/health");
    const [info, ops] = await Promise.all([api("/assistant/info"), api("/operations")]);
    $("#bot-name").textContent = info.name;
    mascot.setAttribute("title", `${info.name}: click to chat`);
    $("#chat-input").placeholder = `Ask ${info.name}: e.g. animate my frames at 12 fps with the theme music`;
    state.ops = ops;
    renderTiles();
    await loadFiles();
    pill.textContent = "Workshop open"; pill.className = "pill ok";
    if (!log.children.length) greet();
  } catch (e) {
    pill.textContent = e.message.includes("token") ? "Needs access token" : "Server offline"; pill.className = "pill bad";
  }
}

runIntro();
boot();
