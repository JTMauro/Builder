// Render a scene from a stage page (talk.html + its assets) to PNG frames, frame-exact.
//
//   node scene_render.cjs STAGE_DIR scene.json OUT_DIR
//
// scene.json: { width, height, fps, duration, beats: [...], lines: [...] }  (see media_backend/scene.py)
// Writes OUT_DIR/000001.png ... and OUT_DIR/events.json (foot contacts, for footstep sounds).
// GPU: set SCENE_ANGLE=d3d11 (Windows), metal (macOS) or vulkan; the default is a software renderer
// that works anywhere but is slow (about 1-2 s per 720p frame).
const http = require("http"), fs = require("fs"), path = require("path");
let chromium;
try { ({ chromium } = require("playwright")); } catch { ({ chromium } = require(path.join(__dirname, "node_modules", "playwright"))); }

const [stage, scenePath, out] = process.argv.slice(2);
if (!out) { console.error("usage: node scene_render.cjs STAGE_DIR scene.json OUT_DIR"); process.exit(2); }
const scene = JSON.parse(fs.readFileSync(scenePath, "utf8"));
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
  for (let i = 0; i < N; i++) {
    const data = await p.evaluate(async dt => { await window.__step(dt); return document.querySelector("#stage canvas").toDataURL("image/png"); }, 1 / FPS);
    fs.writeFileSync(path.join(out, String(i + 1).padStart(6, "0") + ".png"), Buffer.from(data.split(",")[1], "base64"));
    if (i % FPS === 0) console.log(`frame ${i + 1}/${N} (${((Date.now() - t0) / 1000).toFixed(0)} s)`);
  }
  fs.writeFileSync(path.join(out, "events.json"), JSON.stringify(await p.evaluate(() => window.__events())));
  if (errs.length) console.error("page errors:", errs.slice(0, 5).join(" | "));
  console.log(`done: ${N} frames in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
  await b.close(); server.close();
})().catch(e => { console.error(e.message || e); process.exit(1); });
