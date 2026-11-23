// Export a stage character as a game-ready GLB with its moves baked in, then load it back and check it.
//
//   node export_glb.cjs STAGE_DIR job.json
//
// job.json: { model, moves: [...], morphs, inPlace, out: "x.glb", report: "report.json",
//             check: { clip, png, width, height } | null }
// Serves the stage folder (models/, clips/) and this folder (export.html, anim.js) to headless Chromium;
// three.js retargets the moves (anim.js) and GLTFExporter writes the binary glTF. Writes the report JSON:
// { exported: { clips, bytes }, loaded: { clips, morphTargets, meshes, bones, triangles } }.
// Used by Builder's export_character operation. GPU: SCENE_ANGLE as for scene_render.cjs.
const http = require("http"), fs = require("fs"), path = require("path");
let chromium;
try { ({ chromium } = require("playwright")); } catch { ({ chromium } = require(path.join(__dirname, "node_modules", "playwright"))); }

const [stage, jobPath] = process.argv.slice(2);
if (!jobPath) { console.error("usage: node export_glb.cjs STAGE_DIR job.json"); process.exit(2); }
const job = JSON.parse(fs.readFileSync(jobPath, "utf8"));
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".json": "application/json", ".png": "image/png", ".glb": "model/gltf-binary" };
const root = path.resolve(stage), view = path.resolve(__dirname);

function send(res, p) {
  if (!fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { "content-type": TYPES[path.extname(p)] || "application/octet-stream" }); fs.createReadStream(p).pipe(res);
}
const server = http.createServer((req, res) => {
  const url = decodeURIComponent(new URL(req.url, "http://x").pathname);
  if (url === "/__out" && req.method === "POST") {   // the page hands the GLB back here
    const parts = []; req.on("data", d => parts.push(d));
    req.on("end", () => { fs.writeFileSync(job.out, Buffer.concat(parts)); res.writeHead(200); res.end("ok"); });
    return;
  }
  if (url === "/__file") return send(res, job.out);
  if (url.startsWith("/__view/")) {
    const p = path.resolve(view, "." + url.slice(7));
    if (!p.startsWith(view + path.sep)) { res.writeHead(404); return res.end(); }
    return send(res, p);
  }
  const p = path.resolve(root, "." + url);
  if (!p.startsWith(root + path.sep)) { res.writeHead(404); return res.end(); }
  send(res, p);
});

(async () => {
  await new Promise(ok => server.listen(0, "127.0.0.1", ok));
  const angle = process.env.SCENE_ANGLE || "swiftshader";
  const b = await chromium.launch({ args: ["--use-gl=angle", `--use-angle=${angle}`, "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] });
  const W = job.check?.width || 640, H = job.check?.height || 640;
  const p = await b.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
  const errs = []; p.on("pageerror", e => errs.push(e.message));
  await p.goto(`http://127.0.0.1:${server.address().port}/__view/export.html`);
  await p.waitForFunction(() => window.__ready, null, { timeout: 120000 }).catch(() => { throw new Error("export page failed to load: " + errs.join(" | ")); });
  const t0 = Date.now();
  const exported = await p.evaluate(j => window.__export(j), { model: job.model, moves: job.moves, morphs: job.morphs !== false, inPlace: job.inPlace !== false });
  console.log(`exported ${exported.clips.length} clips, ${(exported.bytes / 1e6).toFixed(1)} MB in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
  const check = await p.evaluate(c => window.__check(c), job.check || {});
  if (check.png && job.check?.png) fs.writeFileSync(job.check.png, Buffer.from(check.png.split(",")[1], "base64"));
  delete check.png;
  fs.writeFileSync(job.report, JSON.stringify({ exported, ...check }, null, 1));
  if (errs.length) console.error("page errors:", errs.slice(0, 5).join(" | "));
  await b.close(); server.close();
})().catch(e => { console.error(e.message || e); process.exit(1); });
