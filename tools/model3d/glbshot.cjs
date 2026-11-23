// Render GLB models side by side to one PNG with headless Chromium (Playwright, software WebGL), for checks.
//
//   node glbshot.cjs out.png a.glb [b.glb ...]      (env: SIZE=360x360 per model, VIEW=three|front|side|top)
//
// Each model is framed from its bounds on a light grey floor. Prints one JSON line with sizes and errors.
const { chromium } = require("playwright");
const fs = require("fs");

const [out, ...models] = process.argv.slice(2);
const [w, h] = (process.env.SIZE || "360x360").split("x").map(Number);
const view = process.env.VIEW || "three";
const page = `<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;background:#e9ebec;display:flex}</style>
<script type="importmap">{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}</script></head><body>
<script type="module">
import * as THREE from "three"; import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
const L = new GLTFLoader(), info = [];
for (let i = 0; i < ${models.length}; i++) {
  const r = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true }); r.setSize(${w}, ${h});
  r.outputColorSpace = THREE.SRGBColorSpace; document.body.appendChild(r.domElement);
  const scene = new THREE.Scene(); scene.background = new THREE.Color(0xe9ebec);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8278, 1.6));
  const key = new THREE.DirectionalLight(0xfff4e8, 2.2); key.position.set(2, 4, 3); scene.add(key);
  const g = await L.loadAsync("m" + i + ".glb"); const root = g.scene; scene.add(root); root.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(root, true), size = box.getSize(new THREE.Vector3()), c = box.getCenter(new THREE.Vector3());
  const R = Math.max(size.x, size.y, size.z) || 1;
  const floor = new THREE.Mesh(new THREE.CircleGeometry(R * 3, 48), new THREE.MeshStandardMaterial({ color: 0xd2d6d9, roughness: 1 }));
  floor.rotation.x = -Math.PI / 2; floor.position.y = box.min.y - 0.001; scene.add(floor);
  const cam = new THREE.PerspectiveCamera(30, ${w} / ${h}, R / 100, R * 100);
  const d = { three: [1.1, 0.7, 1.6], front: [0, 0.25, 2], side: [2, 0.25, 0.01], top: [0.01, 2, 0.4] }["${view}"] || [1.1, 0.7, 1.6];
  const dir = new THREE.Vector3(...d).normalize(); cam.position.copy(c).addScaledVector(dir, R * 2.3); cam.lookAt(c);
  r.render(scene, cam);
  info.push({ size: size.toArray().map(v => +v.toFixed(3)) });
}
window.__info = info; window.__ready = true;
</script></body></html>`;

(async () => {
  const b = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const p = await b.newPage({ viewport: { width: w * models.length, height: h } });
  const errs = [];
  p.on("pageerror", e => errs.push(e.message)); p.on("console", m => { if (m.type() === "error") errs.push(m.text().slice(0, 300)); });
  await p.route("http://glb.shot/**", route => {
    const u = new URL(route.request().url()).pathname;
    if (u === "/index.html") return route.fulfill({ body: page, contentType: "text/html" });
    const m = /^\/m(\d+)\.glb$/.exec(u);
    if (m && models[+m[1]]) return route.fulfill({ body: fs.readFileSync(models[+m[1]]), contentType: "model/gltf-binary" });
    return route.fulfill({ status: 404, body: "" });
  });
  await p.goto("http://glb.shot/index.html");
  await p.waitForFunction(() => window.__ready || null, { timeout: 120000 }).catch(() => errs.push("timeout"));
  const info = await p.evaluate(() => window.__info || []);
  await p.screenshot({ path: out });
  console.log(JSON.stringify({ models: info, errors: errs, png: out }));
  await b.close();
})();
