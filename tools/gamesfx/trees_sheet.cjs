// Contact sheet for tree GLBs: each tree on a ground plane against a sky, seen from DIST metres at eye height,
// with a 1.8 m reference figure. Headless Chromium (Playwright, software WebGL) + three.js r160.
//
//   node tools/gamesfx/trees_sheet.cjs out.png a.glb [b.glb ...]
//   env: DIST=20 (metres; "fit" frames the whole tree), CELL=420x560, COLS=3, ELEV=1.7 (camera height), YAW=0.6,
//        LOOKY=<m> (look-at height; default half the tree height, max 14)
//
// Prints one JSON line with per-model bounds and any page errors.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const [out, ...models] = process.argv.slice(2);
const [w, h] = (process.env.CELL || "420x560").split("x").map(Number);
const cols = +(process.env.COLS || 3);
const rows = Math.ceil(models.length / cols);
const dist = process.env.DIST || "20";
const elev = +(process.env.ELEV || 1.7);
const looky = +(process.env.LOOKY || -1);
const yaw = +(process.env.YAW || 0.6);
const labels = models.map(m => path.basename(m, ".glb"));

const page = `<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;background:#222}
#g{display:grid;grid-template-columns:repeat(${cols},${w}px)}.c{position:relative}.c span{position:absolute;left:6px;top:4px;font:13px sans-serif;color:#fff;text-shadow:0 0 3px #000}</style>
<script type="importmap">{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}</script></head><body><div id="g"></div>
<script type="module">
import * as THREE from "three"; import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
const L = new GLTFLoader(), info = [], labels = ${JSON.stringify(labels)};
for (let i = 0; i < labels.length; i++) {
  const div = document.createElement("div"); div.className = "c"; document.getElementById("g").appendChild(div);
  const r = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true }); r.setSize(${w}, ${h});
  r.outputColorSpace = THREE.SRGBColorSpace; r.toneMapping = THREE.ACESFilmicToneMapping; r.toneMappingExposure = 1.0;
  r.shadowMap.enabled = true; r.shadowMap.type = THREE.PCFSoftShadowMap;
  div.appendChild(r.domElement); const sp = document.createElement("span"); sp.textContent = labels[i]; div.appendChild(sp);
  const scene = new THREE.Scene();
  const sky = new THREE.Color(0x9cc4e4); scene.background = sky; scene.fog = new THREE.Fog(0xb8d3ea, 60, 220);
  scene.add(new THREE.HemisphereLight(0xcfe3ff, 0x6a6050, 1.6));
  const sun = new THREE.DirectionalLight(0xfff1dc, 2.6); sun.position.set(30, 45, 25); sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048); const sc = sun.shadow.camera; sc.left = sc.bottom = -20; sc.right = sc.top = 20; sc.near = 1; sc.far = 150;
  sun.shadow.bias = -0.0005; sun.shadow.normalBias = 0.04; scene.add(sun); scene.add(sun.target);
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(600, 600), new THREE.MeshStandardMaterial({ color: 0x7d7a5c, roughness: 1 }));
  ground.rotation.x = -Math.PI / 2; ground.receiveShadow = true; scene.add(ground);
  const g = await L.loadAsync("m" + i + ".glb"); const root = g.scene; scene.add(root);
  root.traverse(o => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
  root.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(root, true), size = box.getSize(new THREE.Vector3());
  const H = box.max.y, rad = Math.max(size.x, size.z) / 2;
  // 1.8 m reference figure (body + head)
  const fm = new THREE.MeshStandardMaterial({ color: 0xc0392b, roughness: 0.7 });
  const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.22, 1.2, 4, 12), fm); body.position.set(0, 0.82, 0);
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.13, 16, 12), fm); head.position.set(0, 1.67, 0);
  const fig = new THREE.Group(); fig.add(body, head); fig.traverse(o => o.castShadow = true);
  const dir = new THREE.Vector3(Math.sin(${yaw}), 0, Math.cos(${yaw}));
  const side = new THREE.Vector3(dir.z, 0, -dir.x);
  fig.position.copy(side.clone().multiplyScalar(-Math.min(rad * 0.6 + 1.2, 4.5))).addScaledVector(dir, 2.0); scene.add(fig);
  let D = "${dist}" === "fit" ? Math.max(8, H * 1.25) : +"${dist}";
  const cam = new THREE.PerspectiveCamera(50, ${w} / ${h}, 0.1, 500);
  cam.position.copy(dir.clone().multiplyScalar(D)); cam.position.y = ${elev};
  const ty = ${looky} >= 0 ? ${looky} : Math.min(H * 0.5, 14);
  cam.lookAt(0, ty, 0);
  const half = Math.atan2(Math.max(H - ty, ty) + 1.0, D);
  cam.fov = Math.min(80, Math.max(30, THREE.MathUtils.radToDeg(half) * 2.05)); cam.updateProjectionMatrix();
  sun.target.position.set(0, 0, 0);
  r.render(scene, cam);
  let tris = 0; root.traverse(o => { if (o.isMesh) tris += (o.geometry.index ? o.geometry.index.count : o.geometry.attributes.position.count) / 3; });
  info.push({ name: labels[i], size: size.toArray().map(v => +v.toFixed(2)), minY: +box.min.y.toFixed(2), tris });
}
window.__info = info; window.__ready = true;
</script></body></html>`;

(async () => {
  const b = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const p = await b.newPage({ viewport: { width: w * cols, height: h * rows } });
  const errs = [];
  p.on("pageerror", e => errs.push(e.message));
  p.on("console", m => { if (m.type() === "error" || m.type() === "warning") errs.push(m.text().slice(0, 300)); });
  await p.route("http://trees.sheet/**", route => {
    const u = new URL(route.request().url()).pathname;
    if (u === "/index.html") return route.fulfill({ body: page, contentType: "text/html" });
    const m = /^\/m(\d+)\.glb$/.exec(u);
    if (m && models[+m[1]]) return route.fulfill({ body: fs.readFileSync(models[+m[1]]), contentType: "model/gltf-binary" });
    return route.fulfill({ status: 404, body: "" });
  });
  await p.goto("http://trees.sheet/index.html");
  await p.waitForFunction(() => window.__ready || null, { timeout: 300000 }).catch(() => errs.push("timeout"));
  const info = await p.evaluate(() => window.__info || []);
  await p.screenshot({ path: out, fullPage: true });
  console.log(JSON.stringify({ models: info, errors: errs, png: out }));
  await b.close();
})();
