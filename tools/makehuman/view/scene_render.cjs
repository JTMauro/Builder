// Render a scene from a stage page (talk.html + its assets) to PNG frames, frame-exact.
//
//   node scene_render.cjs STAGE_DIR scene.json OUT_DIR
//
// scene.json: { width, height, fps, duration, beats: [...], lines: [...], keep?: [frame numbers to save], probe?: [times] }
// (see media_backend/scene.py; keep and a two-argument window.__step(dt, draw) need studio.html)
// Writes OUT_DIR/000001.png ... and OUT_DIR/events.json (foot contacts, for footstep sounds).
// GPU: set SCENE_ANGLE=d3d11 (Windows), metal (macOS) or vulkan; the default is a software renderer
// that works anywhere but is slow (about 1-2 s per 720p frame).
const http = require("http"), fs = require("fs"), path = require("path");
let chromium;
try { ({ chromium } = require("playwright")); } catch { ({ chromium } = require(path.join(__dirname, "node_modules", "playwright"))); }

const [stage, scenePath, out] = process.argv.slice(2);
if (!out) { console.error("usage: node scene_render.cjs STAGE_DIR scene.json OUT_DIR"); process.exit(2); }
const scene = JSON.parse(fs.readFileSync(scenePath, "utf8"));
const JPG = scene.frame_format === "jpg";   // big renders: ~0.6 MB a 1080p frame instead of ~3.6 MB
const W = scene.width || 1280, H = scene.height || 720, FPS = scene.fps || 30, N = Math.round((scene.duration || 5) * FPS);
fs.mkdirSync(out, { recursive: true });

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".json": "application/json", ".png": "image/png" };
const root = path.resolve(stage);
const server = http.createServer((req, res) => {
  const p = path.resolve(root, "." + decodeURIComponent(new URL(req.url, "http://x").pathname));
  if (!p.startsWith(root) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { "content-type": TYPES[path.extname(p)] || "application/octet-stream" }); fs.createReadStream(p).pipe(res);
});

(async () => {
  await new Promise(ok => server.listen(0, "127.0.0.1", ok));
  const angle = process.env.SCENE_ANGLE || "swiftshader";
  const b = await chromium.launch({ args: ["--use-gl=angle", `--use-angle=${angle}`, "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] });
  const p = await b.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
  const errs = []; p.on("pageerror", e => errs.push(e.message));
  await p.addInitScript(s => { window.__scene = s; }, scene);
  await p.goto(`http://127.0.0.1:${server.address().port}/${scene.page || "index.html"}`);
  await p.waitForFunction(() => window.__ready, null, { timeout: 300000 });
  if (await p.evaluate(() => window.__ready) === "error") throw new Error("stage failed to load: " + (await p.textContent("#status")));
  await p.evaluate(() => window.__prepare());
  const t0 = Date.now();
  // scene.keep: only these frame numbers (1-based) are drawn and saved (stills, comics); [] simulates without drawing
  const keep = Array.isArray(scene.keep) ? new Set(scene.keep) : null;
  for (let i = 0; i < N; i++) {
    const draw = !keep || keep.has(i + 1);
    const data = await p.evaluate(async ([dt, draw, jpg]) => { await window.__step(dt, draw); return draw ? document.querySelector("#stage canvas").toDataURL(jpg ? "image/jpeg" : "image/png", 0.95) : null; }, [1 / FPS, draw, JPG]);
    if (data) fs.writeFileSync(path.join(out, String(i + 1).padStart(6, "0") + (JPG ? ".jpg" : ".png")), Buffer.from(data.split(",")[1], "base64"));
    if (i % FPS === 0) console.log(`frame ${i + 1}/${N} (${((Date.now() - t0) / 1000).toFixed(0)} s)`);
  }
  fs.writeFileSync(path.join(out, "events.json"), JSON.stringify(await p.evaluate(() => window.__events())));
  if (errs.length) console.error("page errors:", errs.slice(0, 5).join(" | "));
  console.log(`done: ${N} frames in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
  await b.close(); server.close();
})().catch(e => { console.error(e.message || e); process.exit(1); });
