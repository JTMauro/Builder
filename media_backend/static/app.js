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
// Signed-in state lives in an HttpOnly cookie; every request just needs our marker header.
async function api(path, opts = {}) {
  const headers = { "X-Builder": "1", ...(opts.headers || {}) };
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers, credentials: "same-origin" });
  if (res.status === 401 && !path.startsWith("/auth/")) { showLogin(); throw new Error("Please sign in"); }
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    throw new Error(Array.isArray(d) ? d.map((e) => `${(e.loc || []).slice(-1)[0] || ""}: ${e.msg}`).join("\n") : d || res.statusText);
  }
  return body;
}
const contentUrl = (id) => `/files/${id}/content?inline=true`;

/* ================================================================ state */
const state = { files: [], ops: [], selected: null, checked: [], session: null, activeOp: null, busy: 0, user: null, auth: null, loginMode: "login" };

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
  addMsg("bot", `Hi! I'm ${$("#bot-name").textContent}. Tell me what you want to make. I can start a Craft build from a website, app or game (or from scratch) and keep an eye on it, or work on audio and video: trim, add music, caption, or animate your drawings.`);
}
$("#chat-reset").addEventListener("click", () => {
  state.session = null;
  $("#chat-history").value = "";
  $("#chat-delete").hidden = true;
  greet();
  $("#chat-input").focus();
});
async function loadChatList() {
  const sel = $("#chat-history");
  try {
    const chats = await api("/assistant/sessions");
    sel.replaceChildren(h("option", { value: "", text: chats.length ? `Your saved chats (${chats.length})` : "No saved chats yet" }));
    for (const c of chats) {
      const when = c.updated_at ? new Date(c.updated_at * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
      sel.append(h("option", { value: c.id, text: `${c.title || "Untitled chat"}${when ? " · " + when : ""}` }));
    }
    sel.value = state.session || "";
  } catch { /* chat list is optional */ }
}
$("#chat-history").addEventListener("change", async (e) => {
  const id = e.target.value;
  if (!id) return;
  try {
    const t = await api(`/assistant/sessions/${id}`);
    state.session = id;
    log.replaceChildren();
    for (const m of t.messages) addMsg(m.role === "user" ? "user" : "bot", m.text);
    if (!t.messages.length) greet();
    $("#chat-delete").hidden = false;
  } catch (err) { addMsg("bot error", err.message); }
});
$("#chat-delete").addEventListener("click", async () => {
  if (!state.session || !confirm("Delete this chat? This can't be undone.")) return;
  await api(`/assistant/sessions/${state.session}`, { method: "DELETE" }).catch(() => {});
  $("#chat-reset").click();
  loadChatList();
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
    $("#chat-delete").hidden = false;
    loadChatList();
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

/* ================================================================ craft builds */
const craftState = { runs: [], projects: [], current: null, cursor: 0, polling: null, engine: null };

function showView(view) {
  document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.view === view)));
  document.querySelectorAll(".view-craft").forEach((el) => { el.hidden = view !== "craft"; });
  document.querySelectorAll(".view-media").forEach((el) => { el.hidden = view !== "media"; });
  store.set("mb_view", view);
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));

const STEP_ORDER = ["study", "build", "check", "done"];
const STATUS_WORDS = { starting: "Starting", running: "Working", waiting: "Needs you", done: "Ready", failed: "Stopped with a problem", stopped: "Stopped" };

async function loadCraft() {
  try {
    craftState.engine = await api("/craft/status");
    $("#engine-state").textContent = craftState.engine.engine ? (craftState.engine.version || "Engine ready") : "Engine not built";
    $("#engine-state").title = craftState.engine.message || "";
  } catch { /* status is informational */ }
  await refreshBuildList();
  const sel = $("#build-file");
  const keep = sel.value;
  sel.replaceChildren(h("option", { value: "", text: "No file" }), ...state.files.map((f) => h("option", { value: f.id, text: f.name })));
  sel.value = keep;
}

async function refreshBuildList() {
  try {
    [craftState.runs, craftState.projects] = await Promise.all([api("/craft/runs"), api("/craft/projects")]);
  } catch { return; }
  const list = $("#build-list");
  const rows = [];
  const runProjects = new Set();
  for (const r of craftState.runs) {
    if (r.project) runProjects.add(r.project);
    rows.push(h("button", { type: "button", class: `row build-item${craftState.current === r.id ? " selected" : ""}`, onclick: () => openRun(r.id) },
      h("span", { class: `status-dot ${r.status}` }),
      h("div", { class: "grow" }, h("strong", { text: r.request.slice(0, 70) }), h("small", { text: `${STATUS_WORDS[r.status] || r.status}${r.target && r.target !== "-" ? " · " + r.target : ""}` }))));
  }
  for (const p of craftState.projects) {
    if (runProjects.has(p.path)) continue;
    const sim = p.last_check && p.last_check.similarity != null ? ` · ${Math.round(p.last_check.similarity)}% alike` : "";
    rows.push(h("button", { type: "button", class: "row build-item", onclick: () => openProject(p) },
      h("span", { class: "status-dot done" }),
      h("div", { class: "grow" }, h("strong", { text: p.name }), h("small", { text: `${p.stack || "Project"}${sim}` }))));
  }
  list.replaceChildren(...(rows.length ? rows : [h("div", { class: "muted small-text", text: "Nothing built yet." })]));
}

$("#build-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const st = $("#build-form-status"), btn = $("#build-start");
  const body = { request: $("#build-request").value.trim(), target: $("#build-target").value.trim(), file_id: $("#build-file").value || null, rounds: Number($("#build-rounds").value || 2) };
  if (!body.request) { st.textContent = "Say what you want to make."; return; }
  btn.disabled = true; st.textContent = "";
  try {
    const run = await api("/craft/runs", { method: "POST", json: body });
    $("#build-request").value = "";
    await refreshBuildList();
    openRun(run.id);
  } catch (err) { st.textContent = err.message; } finally { btn.disabled = false; }
});

function resetBuildView() {
  $("#build-empty").hidden = true;
  $("#build-view").hidden = false;
  $("#feed").replaceChildren();
  $("#checks").replaceChildren(h("div", { class: "muted small-text", text: "No checks yet." }));
  $("#ask-card").hidden = true;
  $("#build-result").hidden = true;
}

function openRun(id) {
  if (craftState.polling) craftState.polling.cancelled = true;
  craftState.current = id;
  craftState.cursor = 0;
  craftState.project = null;
  resetBuildView();
  refreshBuildList();
  const token = { cancelled: false };
  craftState.polling = token;
  pollRun(id, token);
}

function openProject(p) {
  if (craftState.polling) craftState.polling.cancelled = true;
  craftState.current = null;
  craftState.project = p;
  resetBuildView();
  $("#build-title").textContent = `${p.name}: ${p.request}`;
  setPipeline("done", true);
  setStatusPill("done");
  $("#build-stop").hidden = true;
  if (p.last_check) renderChecks([p.last_check], p.path);
  feedLine("f-note", p.last_reply || p.note || "Saved project.");
  $("#followup-form").hidden = false;
}

async function pollRun(id, token) {
  let busy = false, lastStatus = null;
  while (!token.cancelled) {
    let r;
    try { r = await api(`/craft/runs/${id}?after=${craftState.cursor}&wait=25`); }
    catch (err) { feedLine("f-err", err.message); await sleep(3000); continue; }
    if (token.cancelled) break;
    craftState.cursor = r.cursor;
    for (const ev of r.events) renderEvent(ev);
    renderRun(r.run);
    if (r.run.status !== lastStatus) { lastStatus = r.run.status; refreshBuildList(); }
    const running = !r.run.finished_at;
    if (running && !busy) { setBusy(1); busy = true; }
    if (!running) { if (busy) setBusy(-1); busy = false; refreshBuildList(); break; }
  }
  if (busy) setBusy(-1);
}

function setStatusPill(status) {
  const pill = $("#build-status");
  pill.textContent = STATUS_WORDS[status] || status;
  pill.className = "pill " + ({ done: "ok", failed: "bad", stopped: "bad", waiting: "wait" }[status] || "run");
}

function setPipeline(step, finishedOk) {
  const at = finishedOk ? 4 : Math.max(0, STEP_ORDER.indexOf(step));
  document.querySelectorAll("#pipeline li").forEach((li, i) => {
    li.classList.toggle("done", i < at || (finishedOk && i === 3));
    li.classList.toggle("now", !finishedOk && i === at);
  });
}

function renderRun(run) {
  $("#build-title").textContent = run.target && run.target !== "-" ? `${run.request}  ·  from ${run.target}` : run.request;
  setStatusPill(run.status);
  setPipeline(run.step || "study", run.status === "done");
  $("#build-stop").hidden = !!run.finished_at;
  renderAsk(run);
  if (run.checks.length) renderChecks(run.checks, run.project);
  const res = $("#build-result");
  if (run.finished_at) {
    res.hidden = false;
    res.className = `build-result ${run.status === "done" ? "ok" : "bad"}`;
    res.replaceChildren(run.status === "done" ? "Ready. " : "", run.message || STATUS_WORDS[run.status] || "");
    if (run.preview) res.append(" ", h("a", { href: run.preview, target: "_blank", rel: "noopener", text: "Open preview" }));
    $("#followup-form").hidden = !run.project;
  } else {
    $("#followup-form").hidden = true;
    if (run.preview) { res.hidden = false; res.className = "build-result"; res.replaceChildren("Preview is up: ", h("a", { href: run.preview, target: "_blank", rel: "noopener", text: run.preview })); }
  }
}

function renderAsk(run) {
  const card = $("#ask-card");
  const p = run.pending;
  if (!p) { card.hidden = true; card.dataset.id = ""; return; }
  if (card.dataset.id === p.id && !card.hidden) return;
  card.dataset.id = p.id;
  card.hidden = false;
  const send = async (answer) => {
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try { await api(`/craft/runs/${run.id}/answer`, { method: "POST", json: answer }); card.hidden = true; }
    catch (err) { feedLine("f-err", err.message); card.querySelectorAll("button").forEach((b) => { b.disabled = false; }); }
  };
  const kids = [h("h4", { text: p.kind === "approval" ? `Allow this? ${p.title}` : p.title })];
  if (p.detail) kids.push(h("pre", { text: p.detail }));
  const actions = h("div", { class: "ask-actions" });
  if (p.kind === "approval") {
    actions.append(
      h("button", { type: "button", class: "btn primary small", text: "Allow once", onclick: () => send({ allow: true }) }),
      h("button", { type: "button", class: "btn ghost small", text: "Always for this project", onclick: () => send({ allow: true, always: true }) }),
      h("button", { type: "button", class: "btn ghost small", text: "Refuse", onclick: () => send({ allow: false }) }));
  } else {
    for (const c of p.choices || []) actions.append(h("button", { type: "button", class: "btn ghost small", text: c, onclick: () => send({ choice: c }) }));
    const input = h("input", { placeholder: p.choices && p.choices.length ? "Or type your own answer" : "Your answer" });
    actions.append(input, h("button", { type: "button", class: "btn primary small", text: "Answer", onclick: () => send({ text: input.value.trim() || "No preference; use your best judgement." }) }));
  }
  kids.push(actions);
  card.replaceChildren(...kids);
}

function projectFileUrl(project, rel) { return `/craft/projects/file?path=${encodeURIComponent(project + "/" + rel)}`; }

function renderChecks(checks, project) {
  $("#checks").replaceChildren(...checks.map((c) => {
    const sim = c.similarity != null ? Math.round(c.similarity) : null;
    const shots = (c.artifacts || []).filter((a) => /\.(png|jpe?g|webp)$/i.test(a)).slice(0, 4);
    return h("div", { class: "check" },
      h("div", { class: "check-top" }, h("span", { text: `Check ${c.round} · ${c.passed ? "works" : "doesn't work yet"}` }), h("span", { text: sim != null ? `${sim}% alike` : "not compared" })),
      sim != null ? h("div", { class: "bar" }, h("span", { style: `width:${Math.max(2, Math.min(100, sim))}%` })) : null,
      c.note ? h("p", { text: c.note }) : null,
      shots.length && project ? h("div", { class: "shots" }, ...shots.map((a) => h("img", { src: projectFileUrl(project, a), alt: `Screenshot from check ${c.round}`, loading: "lazy", onclick: (e) => window.open(e.target.src, "_blank") }))) : null);
  }));
}

function feedLine(cls, text) {
  const feed = $("#feed");
  const stick = feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 30;
  const line = h("div", { class: cls, text });
  feed.append(line);
  while (feed.childElementCount > 600) feed.firstChild.remove();
  if (stick) feed.scrollTop = feed.scrollHeight;
  return line;
}

function renderEvent(ev) {
  switch (ev.type) {
    case "step": feedLine("f-step", `▸ ${ev.text}`); break;
    case "activity": feedLine("f-act", `· ${ev.words}`); break;
    case "text": {
      const last = $("#feed").lastElementChild;
      if (last && last.classList.contains("f-text")) last.textContent += ev.text; else feedLine("f-text", ev.text);
      break;
    }
    case "error": feedLine("f-err", `! ${ev.message}${ev.remediation ? "\n  " + ev.remediation : ""}`); break;
    case "note": feedLine("f-note", `+ ${ev.text}`); break;
    case "answered": feedLine("f-note", `✓ You answered${ev.answer.allow === false ? ": refused" : ev.answer.allow ? ": allowed" : ""}`); break;
    case "check": feedLine("f-step", `✔ Check ${ev.check.round}: ${ev.check.similarity != null ? Math.round(ev.check.similarity) + "% alike, " : ""}${ev.check.note || ""}`); break;
    case "preview": feedLine("f-act", `Preview: ${ev.url}`); break;
    case "finished": feedLine(ev.ok ? "f-step" : "f-err", ev.ok ? `Done. ${ev.message}` : ev.message); break;
    case "log": feedLine("f-log", ev.text); break;
    default: break;
  }
}

$("#build-stop").addEventListener("click", async () => {
  if (!craftState.current) return;
  try { await api(`/craft/runs/${craftState.current}/stop`, { method: "POST" }); } catch (err) { feedLine("f-err", err.message); }
});
$("#followup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#followup-input").value.trim();
  if (!text) return;
  try {
    const run = craftState.current
      ? await api(`/craft/runs/${craftState.current}/followup`, { method: "POST", json: { request: text } })
      : await api(`/craft/projects/followup?path=${encodeURIComponent(craftState.project.path)}`, { method: "POST", json: { request: text } });
    $("#followup-input").value = "";
    await refreshBuildList();
    openRun(run.id);
  } catch (err) { feedLine("f-err", err.message); }
});

/* ================================================================ sign in */
function showLogin(status = state.auth) {
  const mode = status && status.needs_setup ? "setup" : state.loginMode;
  state.loginMode = mode;
  $("#user-wrap").hidden = true;
  const pill = $("#server-status");
  pill.textContent = "Signed out"; pill.className = "pill bad";
  const copy = {
    login: ["Welcome back", "Sign in to your Builder workshop.", "Sign in", "New here? Create an account"],
    signup: ["Join the workshop", "Make your own account. Your chats stay private to you.", "Create account", "Already have an account? Sign in"],
    setup: ["Set up Builder", "Create the first account. It will be the admin, who can add everyone else.", "Create admin account", ""],
  }[mode];
  $("#login-title").textContent = copy[0];
  $("#login-sub").textContent = copy[1];
  $("#login-submit").textContent = copy[2];
  const sw = $("#login-switch");
  sw.hidden = mode === "setup" || !(status && status.signup_open);
  sw.textContent = copy[3];
  $("#login-display-wrap").hidden = mode === "login";
  $("#login-password").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("#login-error").textContent = "";
  $("#login").hidden = false;
  setTimeout(() => $("#login-username").focus(), 50);
}
$("#login-switch").addEventListener("click", () => { state.loginMode = state.loginMode === "login" ? "signup" : "login"; showLogin(); });
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#login-submit"), err = $("#login-error");
  const body = { username: $("#login-username").value.trim(), password: $("#login-password").value, display_name: $("#login-display").value.trim() };
  if (!body.username || !body.password) { err.textContent = "Enter your username and password"; return; }
  btn.disabled = true; err.textContent = "";
  try {
    const path = { login: "/auth/login", signup: "/auth/signup", setup: "/auth/setup" }[state.loginMode];
    await api(path, { method: "POST", json: body });
    $("#login-password").value = "";
    $("#login").hidden = true;
    state.session = null;
    log.replaceChildren();
    await boot();
  } catch (ex) { err.textContent = ex.message; } finally { btn.disabled = false; }
});

function showUser(user) {
  state.user = user;
  const wrap = $("#user-wrap");
  if (!state.auth || !state.auth.auth_enabled || !user) { wrap.hidden = true; return; }
  wrap.hidden = false;
  $("#user-name").textContent = user.display_name;
  $("#user-avatar").textContent = (user.display_name || user.username).trim().charAt(0).toUpperCase();
  $("#menu-users").hidden = !user.is_admin;
  $("#menu-settings").hidden = !user.is_admin;
  // First visit as an admin with no Claude API key: open Settings so it can be added straight away.
  if (user.is_admin && location.hash !== "#connect") {
    api("/settings").then((st) => {
      let asked = false;
      try { asked = localStorage.getItem("builder-asked-key") === "1"; localStorage.setItem("builder-asked-key", "1"); } catch {}
      if (!st.claude_key.set && !asked) openSettings();
    }).catch(() => {});
  }
  if (location.hash === "#settings" && user.is_admin) { history.replaceState(null, "", location.pathname); openSettings(); }
  // The desktop app's tray menu opens Builder at #connect: go straight to Connect Claude Code.
  if (location.hash === "#connect") { history.replaceState(null, "", location.pathname); openTokens(); }
}
const menu = $("#user-menu");
$("#user-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  menu.hidden = !menu.hidden;
  $("#user-btn").setAttribute("aria-expanded", String(!menu.hidden));
});
document.addEventListener("click", () => { menu.hidden = true; $("#user-btn").setAttribute("aria-expanded", "false"); });
document.querySelectorAll("dialog [data-close]").forEach((b) => b.addEventListener("click", (e) => { e.preventDefault(); b.closest("dialog").close(); }));
menu.addEventListener("click", async (e) => {
  const action = e.target.closest("[data-action]")?.dataset.action;
  if (action === "logout") {
    await api("/auth/logout", { method: "POST" }).catch(() => {});
    state.loginMode = "login"; state.session = null; log.replaceChildren();
    $("#chat-history").replaceChildren(h("option", { value: "", text: "Your saved chats" }));
    showLogin();
  } else if (action === "tokens") { openTokens(); }
  else if (action === "users") { openUsers(); }
  else if (action === "settings") { openSettings(); }
  else if (action === "password") { $("#password-form").reset(); $("#pw-status").textContent = ""; $("#password-dialog").showModal(); }
});

/* personal tokens for Claude Code */
async function openTokens() {
  $("#token-new").hidden = true;
  $("#tokens-dialog").showModal();
  await renderTokens();
}
async function renderTokens() {
  const list = $("#token-list");
  const tokens = await api("/auth/tokens").catch(() => []);
  list.replaceChildren(...(tokens.length ? tokens.map((t) => h("div", { class: "row" },
    h("div", { class: "grow" }, h("strong", { text: t.name }), h("small", { text: `…${t.hint} · made ${new Date(t.created_at * 1000).toLocaleDateString()}${t.last_used_at ? " · last used " + new Date(t.last_used_at * 1000).toLocaleDateString() : " · never used"}` })),
    h("button", { class: "btn ghost small", type: "button", text: "Revoke", onclick: async () => { await api(`/auth/tokens/${t.id}`, { method: "DELETE" }); renderTokens(); } })))
    : [h("div", { class: "muted small-text", text: "No tokens yet." })]));
}
function copyBlock(title, text, note) {
  const pre = h("pre", { text });
  const btn = h("button", { class: "btn ghost small", type: "button", text: "Copy" });
  btn.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(text); btn.textContent = "Copied ✓"; }
    catch { btn.textContent = "Select and copy the text above"; }
    setTimeout(() => { btn.textContent = "Copy"; }, 2000);
  });
  return h("div", { class: "connect-way" }, h("p", {}, h("strong", { text: title })), note ? h("p", { class: "muted small-text", text: note }) : null, pre, btn);
}
async function showConnectWays(token) {
  const info = await api("/connect/info").catch(() => ({ local_url: location.origin, public_url: "" }));
  const local = info.local_url || location.origin;
  const pub = info.public_url;
  const ways = [];
  if (pub) {
    ways.push(copyBlock("Claude chats anywhere: claude.ai, the Claude apps, Claude Code on the web",
      `${pub}/c/${token}/mcp/`,
      "In Claude: Settings › Connectors › Add custom connector, name it Builder and paste this address. Every chat, including Claude Code on the web, can then use Builder's tools. The address holds your token, so keep it private."));
    ways.push(copyBlock("Claude Code on another computer",
      `claude mcp add --transport http builder -s user ${pub}/mcp/ --header "Authorization: Bearer ${token}"`));
  } else {
    ways.push(h("div", { class: "connect-way" }, h("p", {}, h("strong", { text: "Claude chats anywhere" })),
      h("p", { class: "muted small-text", text: state.user && state.user.is_admin
        ? "Turn on Remote access in ⚙️ Settings first, then make a token here to get a connector address for claude.ai and Claude Code on the web."
        : "Ask an admin to turn on Remote access in Settings; then make a token here to get a connector address." })));
  }
  ways.push(copyBlock("Claude Code on this computer",
    `claude mcp add --transport http builder -s user ${local}/mcp/ --header "Authorization: Bearer ${token}"`));
  const base = pub || local;
  ways.push(copyBlock("Other apps (HTTP API, full reference at /docs)",
    `Address:  ${base}\nHeader:   Authorization: Bearer ${token}\n\ncurl -H "Authorization: Bearer ${token}" -F file=@clip.mp4 ${base}/files\ncurl -H "Authorization: Bearer ${token}" ${base}/operations`,
    pub ? "Apps on this computer can use the local address instead: " + local : "Apps on other networks need Remote access on."));
  $("#connect-ways").replaceChildren(...ways);
  $("#token-new").hidden = false;
}
$("#token-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const t = await api("/auth/tokens", { method: "POST", json: { name: $("#token-name").value.trim() || "Claude" } });
  $("#token-name").value = "";
  await showConnectWays(t.token);
  renderTokens();
});

/* settings (admins): Claude API key and remote access */
async function openSettings() {
  $("#key-status").textContent = ""; $("#remote-status").textContent = ""; $("#key-input").value = "";
  $("#settings-dialog").showModal();
  await renderSettings();
}
async function renderSettings() {
  const st = await api("/settings").catch((e) => { $("#key-status").textContent = e.message; return null; });
  if (!st) return;
  $("#key-state").textContent = st.claude_key.set ? `✓ A key is saved (${st.claude_key.hint}).` : "No key yet: Rivet and Craft builds won't work until you add one.";
  $("#key-remove").hidden = !st.claude_key.set;
  const r = st.remote;
  const mode = r.running ? r.mode : (r.configured || "off");
  document.querySelectorAll('input[name="remote-mode"]').forEach((i) => { i.checked = i.value === mode; });
  $("#named-fields").hidden = mode !== "named";
  $("#public-url").value = r.public_url || "";
  $("#tunnel-token").placeholder = r.tunnel_token_set ? "Saved. Leave empty to keep it" : "Paste the tunnel token";
  $("#remote-status").textContent = r.running ? `On: ${r.url}` : r.error ? `⚠ ${r.error}` : (r.available ? "Off" : "Off (cloudflared isn't installed here)");
  $("#settings-foot").textContent = `Builder ${st.version} · data in ${st.data_dir}`;
}
document.querySelectorAll('input[name="remote-mode"]').forEach((i) => i.addEventListener("change", () => {
  $("#named-fields").hidden = $('input[name="remote-mode"]:checked').value !== "named";
}));
$("#key-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("#key-input").value.trim();
  if (!key) return;
  $("#key-status").textContent = "Checking the key with Anthropic…";
  try {
    const r = await api("/settings/claude-key", { method: "PUT", json: { key } });
    $("#key-input").value = "";
    $("#key-status").textContent = r.warning || "Saved. Rivet and Craft builds can use it now.";
    renderSettings();
  } catch (err) { $("#key-status").textContent = `⚠ ${err.message}`; }
});
$("#key-remove").addEventListener("click", async () => {
  if (!confirm("Remove the Claude API key from this computer?")) return;
  await api("/settings/claude-key", { method: "DELETE" });
  $("#key-status").textContent = "Removed.";
  renderSettings();
});
$("#remote-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mode = $('input[name="remote-mode"]:checked').value;
  $("#remote-status").textContent = mode === "off" ? "Turning off…" : "Connecting to Cloudflare…";
  try {
    const r = await api("/settings/remote", { method: "PUT", json: { mode, tunnel_token: $("#tunnel-token").value.trim(), public_url: $("#public-url").value.trim() } });
    $("#tunnel-token").value = "";
    $("#remote-status").textContent = r.running ? `On: ${r.url}. Make a token in 🔌 Connect to get connector addresses.` : "Off";
  } catch (err) { $("#remote-status").textContent = `⚠ ${err.message}`; }
});

/* people (admins) */
async function openUsers() { $("#users-dialog").showModal(); await renderUsers(); }
async function renderUsers() {
  const users = await api("/auth/users").catch(() => []);
  $("#user-list").replaceChildren(...users.map((u) => h("div", { class: "row" },
    h("span", { class: "avatar", text: u.display_name.charAt(0).toUpperCase() }),
    h("div", { class: "grow" }, h("strong", { text: u.display_name }), h("small", { text: `@${u.username}` })),
    u.is_admin ? h("span", { class: "tag", text: "admin" }) : null,
    u.id === state.user.id ? h("span", { class: "muted small-text", text: "you" }) :
      h("button", { class: "btn ghost small", type: "button", text: "Remove", onclick: async () => {
        if (!confirm(`Remove ${u.display_name}? Their sign-ins and tokens stop working.`)) return;
        await api(`/auth/users/${u.id}`, { method: "DELETE" }); renderUsers();
      } }))));
}
$("#add-user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const status = $("#nu-status");
  try {
    await api("/auth/users", { method: "POST", json: { username: $("#nu-username").value.trim(), display_name: $("#nu-display").value.trim(), password: $("#nu-password").value, is_admin: $("#nu-admin").checked } });
    status.textContent = "Added. Share their username and starting password with them.";
    e.target.reset();
    renderUsers();
  } catch (err) { status.textContent = err.message; }
});

$("#password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const st = $("#pw-status");
  try {
    await api("/auth/password", { method: "POST", json: { current_password: $("#pw-current").value, new_password: $("#pw-new").value } });
    $("#password-dialog").close();
    state.loginMode = "login";
    showLogin();
    $("#login-error").textContent = "Password changed. Sign in with your new password.";
  } catch (err) { st.textContent = err.message; }
});

/* ================================================================ boot */
async function boot() {
  const pill = $("#server-status");
  try {
    state.auth = await api("/auth/status");
    if (state.auth.auth_enabled && !state.auth.user) { showLogin(state.auth); return; }
    showUser(state.auth.user);
    const [info, ops] = await Promise.all([api("/assistant/info"), api("/operations")]);
    $("#bot-name").textContent = info.name;
    mascot.setAttribute("title", `${info.name}: click to chat`);
    $("#chat-input").placeholder = `Ask ${info.name}: e.g. animate my frames at 12 fps with the theme music`;
    state.ops = ops;
    renderTiles();
    await loadFiles();
    loadChatList();
    loadCraft();
    showView(store.get("mb_view") === "media" ? "media" : "craft");
    pill.textContent = "Workshop open"; pill.className = "pill ok";
    if (!log.children.length) greet();
  } catch (e) {
    if ($("#login").hidden) { pill.textContent = "Server offline"; pill.className = "pill bad"; }
  }
}

runIntro();
boot();
