// Check a rigged GLB moves: retarget three.js Xbot clips (MIT) and CMU motion-capture clips onto it with
// tools/makehuman/view/anim.js and render frames with headless Chromium (Playwright, software WebGL).
//
//   node rigcheck.cjs job.json
//
// job.json: { "model": "/path/rigged.glb", "xbot": "/path/Xbot.glb", "cmu": { "hello": "/path/141_16-wave-hello.glb" },
//             "shots": [["walk", 0.3, "three"], ["hello", 1.2, "front"]], "out": "/dir", "size": [480, 640] }
// Writes out/<i>_<clip>.png and prints one JSON line: clips, bone count, errors, files.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const ANIM = path.join(__dirname, "..", "makehuman", "view", "anim.js");
const files = { "/anim.js": ANIM, "/model.glb": job.model, "/Xbot.glb": job.xbot };
for (const [label, p] of Object.entries(job.cmu || {})) files[`/cmu/${label}.glb`] = p;

const page = `<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;background:#e9ebec}</style>
<script type="importmap">{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}</script></head><body>
<script type="module">
import * as THREE from "three"; import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { retarget, CMU } from "./anim.js";
const r = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true }); r.setSize(innerWidth, innerHeight);
r.outputColorSpace = THREE.SRGBColorSpace; r.shadowMap.enabled = true; document.body.appendChild(r.domElement);
const scene = new THREE.Scene(); scene.background = new THREE.Color(0xe9ebec);
scene.add(new THREE.HemisphereLight(0xe6eeff, 0x6f665c, 1.2));
const key = new THREE.DirectionalLight(0xfff1e0, 2.4); key.position.set(1.6, 3.0, 2.2); key.castShadow = true; scene.add(key);
const floor = new THREE.Mesh(new THREE.CircleGeometry(6, 64), new THREE.MeshStandardMaterial({ color: 0xd5d9dc, roughness: 1 }));
floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; scene.add(floor);
const grid = new THREE.GridHelper(6, 12, 0x9aa0a6, 0xbfc4c8); grid.position.y = 0.001; scene.add(grid);
const cam = new THREE.PerspectiveCamera(30, innerWidth / innerHeight, 0.05, 50);
const L = new GLTFLoader();
const [m, bot] = await Promise.all([L.loadAsync("model.glb"), L.loadAsync("Xbot.glb")]);
const root = m.scene; scene.add(root);
let bones = 0; root.traverse(o => { if (o.isBone) bones++; if (o.isMesh) { o.castShadow = true; o.frustumCulled = false; } });
root.updateMatrixWorld(true); const box = new THREE.Box3().setFromObject(root, true);   // precise: follows the skinning
const H = Math.max(0.3, box.max.y - box.min.y);
const moves = retarget(bot.scene, bot.animations.filter(c => ["idle", "walk", "run"].includes(c.name)), root);
for (const label of ${JSON.stringify(Object.keys(job.cmu || {}))}) {
  const g = await L.loadAsync("cmu/" + label + ".glb"); const c = g.animations[0]; c.name = label;
  moves.push(...retarget(g.scene, [c], root, { rename: CMU, neck: -0.08, head: -0.14 }));
}
const mixer = new THREE.AnimationMixer(root);
window.__info = { clips: moves.map(c => c.name + " " + c.duration.toFixed(2)), bones, height: +H.toFixed(3) };
window.__show = (name, t, view) => {
  mixer.stopAllAction(); const c = moves.find(c => c.name === name); if (!c) return false;
  mixer.clipAction(c).play(); mixer.setTime(t); root.updateMatrixWorld(true);
  const V = { front: [0, 0.55, 3.0], three: [1.7, 0.7, 2.5], side: [3.0, 0.55, 0.01], back: [0, 0.6, -3.0] }[view] || [1.7, 0.7, 2.5];
  cam.position.set(V[0] * H, V[1] * H + 0.1 * H, V[2] * H); cam.lookAt(0, 0.5 * H, 0); r.render(scene, cam); return true;
};
window.__ready = true;
</script></body></html>`;

(async () => {
  const b = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const [w, h] = job.size || [480, 640];
  const p = await b.newPage({ viewport: { width: w, height: h } });
  const errs = [];
  p.on("pageerror", e => errs.push(e.message)); p.on("console", m => { if (m.type() === "error") errs.push(m.text().slice(0, 300)); });
  await p.route("http://rig.check/**", route => {
    const u = new URL(route.request().url()).pathname;
    if (u === "/" || u === "/index.html") return route.fulfill({ body: page, contentType: "text/html" });
    const f = files[decodeURIComponent(u)];
    if (!f || !fs.existsSync(f)) return route.fulfill({ status: 404, body: "" });
    return route.fulfill({ body: fs.readFileSync(f), contentType: u.endsWith(".js") ? "text/javascript" : "model/gltf-binary" });
  });
  await p.goto("http://rig.check/index.html");
  await p.waitForFunction(() => window.__ready || null, { timeout: 180000 }).catch(() => errs.push("timeout"));
  const info = await p.evaluate(() => window.__info || {});
  const out = [];
  fs.mkdirSync(job.out, { recursive: true });
  for (const [i, [clip, t, view]] of (job.shots || []).entries()) {
    const ok = await p.evaluate(([c, t, v]) => window.__show && window.__show(c, t, v), [clip, t, view]);
    if (!ok) { errs.push("no clip " + clip); continue; }
    const f = path.join(job.out, `${i}_${clip.replace(/\W+/g, "_")}.png`);
    await p.screenshot({ path: f }); out.push(f);
  }
  console.log(JSON.stringify({ ...info, files: out, errors: errs }));
  await b.close();
})();
