// Motion for MakeHuman people in three.js r160.
//  - retarget(): copies Mixamo-skeleton clips (three.js Xbot, MIT) onto an MPFB "mixamo" rig in world space
//  - gestures(): point, wave, thumbs up, arms crossed, made by aiming bones, with finger curls
// Both return ordinary THREE.AnimationClips for the target skeleton, so they play with AnimationMixer
// and can be exported with GLTFExporter.
import * as THREE from "three";
const V = THREE.Vector3, Q = THREE.Quaternion;
const key = n => n.replace(/[:_]/g, "").toLowerCase();

export function boneMap(root) {
  const m = {};
  root.traverse(o => { if (o.isBone) m[key(o.name)] = o; });
  return m;
}
const wq = o => o.getWorldQuaternion(new Q());
const wp = o => o.getWorldPosition(new V());
const order = root => { const out = []; root.traverse(o => { if (o.isBone) out.push(o); }); return out; };   // parents first

// aim a bone so the direction to its child points along dir (world)
export function aim(b, c, dir) {
  b.updateWorldMatrix(true, false);
  const cur = wp(c).sub(wp(b)).normalize();
  const q = new Q().setFromUnitVectors(cur, dir.clone().normalize());
  const pq = wq(b.parent);
  b.quaternion.copy(pq.invert().multiply(q.multiply(wq(b))));
  b.updateWorldMatrix(false, true);
}
// rotate a bone in world space by an axis-angle
function turn(b, axis, ang) {
  b.updateWorldMatrix(true, false);
  const q = new Q().setFromAxisAngle(axis, ang).multiply(wq(b));
  b.quaternion.copy(wq(b.parent).invert().multiply(q));
  b.updateWorldMatrix(false, true);
}
const firstMappedChild = (b, M) => b.children.find(c => c.isBone && M[key(c.name)]);

/**
 * Retarget clips from a source Mixamo rig (src root, in its rest pose) onto the target rig (tgt root,
 * in its rest pose). Both face +Z. Returns baked clips for the target.
 */
export function retarget(srcRoot, clips, tgtRoot, { fps = 30, inPlace = true, lean = { idle: 0.015, walk: 0.07, run: 0.30, default: 0.015 }, neck = 0, head = 0 } = {}) {
  // a slight natural forward lean through the spine (radians at the top of the spine), head kept level
  // per move: how far the spine leans forward (radians at its top). The head keeps only a third of it, so he looks ahead.
  const leanOf = name => typeof lean === "number" ? lean : (lean[name] ?? lean.default ?? 0);
  const leanMap = L => ({ mixamorigspine: L / 3, mixamorigspine1: (2 * L) / 3, mixamorigspine2: L, mixamorigneck: L * 0.6 + neck, mixamorighead: L * 0.33 + neck + head });
  const xAxis = new V(1, 0, 0);
  const S = boneMap(srcRoot), T = boneMap(tgtRoot);
  const names = Object.keys(T).filter(k => S[k]);
  srcRoot.updateMatrixWorld(true); tgtRoot.updateMatrixWorld(true);
  const tRest = new Map(order(tgtRoot).map(b => [b, b.quaternion.clone()]));
  // 1. bend the target into the source's rest pose by matching bone directions
  // arms and legs differ in rest pose (MPFB rests in an A-pose, Mixamo in a T-pose); the spine, neck and
  // head are left alone, because bending them tilts the whole posture into a swayback lean
  for (const b of order(tgtRoot)) {
    const k = key(b.name); if (!S[k]) continue;
    if (!/shoulder|arm|hand|upleg|leg|foot|toe/.test(k)) continue;
    const c = firstMappedChild(b, T); if (!c) continue;
    const sc = S[key(c.name)];
    aim(b, c, wp(sc).sub(wp(S[k])).normalize());
  }
  tgtRoot.updateMatrixWorld(true);
  const sRef = {}, tRef = {};
  for (const k of names) { sRef[k] = wq(S[k]); tRef[k] = wq(T[k]); }
  const hipS = S["mixamorighips"], hipT = T["mixamorighips"];
  const scale = wp(hipT).y / Math.max(1e-6, wp(hipS).y), hipS0 = wp(hipS), hipT0 = wp(hipT);
  const tOrder = order(tgtRoot).filter(b => S[key(b.name)]);
  const out = [];
  const mixer = new THREE.AnimationMixer(srcRoot);
  for (const clip of clips) {
    const src = clip.clone();   // keep the hips track: its height gives the bounce; x/z are dropped below when inPlace
    mixer.stopAllAction(); const act = mixer.clipAction(src); act.play();
    const LEAN = leanMap(leanOf(clip.name));
    const n = Math.max(2, Math.round(clip.duration * fps) + 1), times = [];
    const q = new Map(tOrder.map(b => [b, []])), hp = [];
    for (let i = 0; i < n; i++) {
      const t = Math.min(clip.duration, i / fps); times.push(t); mixer.setTime(t); srcRoot.updateMatrixWorld(true);
      for (const b of tOrder) {
        const k = key(b.name);
        const W = wq(S[k]).multiply(sRef[k].clone().invert()).multiply(tRef[k]);
        if (LEAN[k]) W.premultiply(new Q().setFromAxisAngle(xAxis, LEAN[k]));
        b.parent.updateWorldMatrix(true, false);
        b.quaternion.copy(wq(b.parent).invert().multiply(W)); b.updateWorldMatrix(false, false);
        q.get(b).push(...b.quaternion.toArray());
      }
      const d = wp(hipS).sub(hipS0).multiplyScalar(scale); if (inPlace) { d.x = 0; d.z = 0; }
      const p = hipT0.clone().add(d); hp.push(...hipT.parent.worldToLocal(p).toArray());
    }
    const tracks = tOrder.map(b => new THREE.QuaternionKeyframeTrack(b.name + ".quaternion", times, q.get(b)));
    tracks.push(new THREE.VectorKeyframeTrack(hipT.name + ".position", times, hp));
    out.push(new THREE.AnimationClip(clip.name, clip.duration, tracks));
    mixer.uncacheClip(src);
  }
  // put the target back in its own rest pose
  for (const [b, qq] of tRest) b.quaternion.copy(qq);
  tgtRoot.updateMatrixWorld(true);
  return out;
}

// ---- gestures --------------------------------------------------------------------------------
const FINGERS = ["Index", "Middle", "Ring", "Pinky"];
function hand(T, side) {
  const g = n => T[key("mixamorig" + side + n)];
  return { hand: g("Hand"), f: Object.fromEntries(FINGERS.map(f => [f, [1, 2, 3].map(i => g("Hand" + f + i))])), thumb: [1, 2, 3].map(i => g("HandThumb" + i)) };
}
// curl fingers toward the palm: amount 0 straight .. 1 fist
function curl(T, side, amounts, thumb = 0.6) {
  const h = hand(T, side);
  const across = wp(h.f.Pinky[0]).sub(wp(h.f.Index[0])).normalize();
  const along = wp(h.f.Middle[1]).sub(wp(h.f.Middle[0])).normalize();
  // palm side: fingers rest slightly curled, so the middle finger's bend points toward the palm
  const m = h.f.Middle, d1 = wp(m[1]).sub(wp(m[0])).normalize(), d2 = wp(m[2]).sub(wp(m[1])).normalize();
  let palm = d2.clone().sub(d1); palm.addScaledVector(along, -palm.dot(along));
  if (palm.length() < 1e-3) { palm = new V().crossVectors(along, across); if (wp(h.thumb[0]).sub(wp(h.hand)).dot(palm) < 0) palm.negate(); }
  palm.normalize();
  const sign = new V().copy(along).applyQuaternion(new Q().setFromAxisAngle(across, 0.3)).dot(palm) > along.dot(palm) ? 1 : -1;
  FINGERS.forEach(f => { const a = amounts[f] ?? amounts.all ?? 0; h.f[f].forEach((b, i) => turn(b, across, sign * a * [1.0, 1.3, 0.9][i])); });
  if (thumb) h.thumb.forEach((b, i) => turn(b, along, (side === "Right" ? 1 : -1) * thumb * [0.35, 0.5, 0.45][i]));
  return { across, palm, along, sign };
}
function thumbUp(T, side) {   // thumb straight up
  const h = hand(T, side);
  aim(h.thumb[0], h.thumb[1], new V(0, 1, 0.15)); aim(h.thumb[1], h.thumb[2], new V(0, 1, 0.1));
}
const snap = bones => new Map(bones.map(b => [b, b.quaternion.clone()]));
function clipFrom(name, root, poses, times) {
  const bones = order(root);
  const tracks = bones.map(b => new THREE.QuaternionKeyframeTrack(b.name + ".quaternion", times, poses.flatMap(p => p.get(b).toArray())));
  return new THREE.AnimationClip(name, times[times.length - 1], tracks);
}

/** Build gesture clips starting from a base pose (e.g. frame 0 of the retargeted idle). */
export function gestures(root, basePose) {
  const T = boneMap(root), bones = order(root), b = n => T[key("mixamorig" + n)];
  const reset = () => { for (const x of bones) x.quaternion.copy(basePose.get(x)); root.updateMatrixWorld(true); };
  const base = (reset(), snap(bones));
  const out = [];
  // point forward with the right hand
  reset();
  aim(b("RightArm"), b("RightForeArm"), new V(-0.32, 0.12, 0.94)); aim(b("RightForeArm"), b("RightHand"), new V(-0.22, 0.14, 0.97));
  aim(b("RightHand"), b("RightHandMiddle1"), new V(-0.2, 0.1, 0.98));
  curl(T, "Right", { Middle: 1, Ring: 1, Pinky: 1, Index: -0.25 }, 0.9);
  const point = snap(bones);
  out.push(clipFrom("point", root, [base, point, point, base], [0, 0.45, 1.9, 2.4]));
  // wave with the right hand
  reset();
  // lift the shoulder with the arm, keep the elbow about shoulder height and bend the forearm up
  { const sh = b("RightShoulder"), ar = b("RightArm"); const d = wp(ar).sub(wp(sh)).normalize(); d.y += 0.22; aim(sh, ar, d); }
  aim(b("RightArm"), b("RightForeArm"), new V(-0.9, 0.08, 0.3)); curl(T, "Right", { all: 0.05 }, 0.1);
  aim(b("RightForeArm"), b("RightHand"), new V(-0.3, 1, 0.12)); aim(b("RightHand"), b("RightHandMiddle1"), new V(-0.25, 1, 0.1));
  const w1 = snap(bones);
  aim(b("RightForeArm"), b("RightHand"), new V(0.25, 1, 0.12)); aim(b("RightHand"), b("RightHandMiddle1"), new V(0.35, 1, 0.1));
  const w2 = snap(bones);
  out.push(clipFrom("wave", root, [base, w1, w2, w1, w2, w1, base], [0, 0.4, 0.75, 1.1, 1.45, 1.8, 2.3]));
  // thumbs up with the right hand
  reset();
  aim(b("RightArm"), b("RightForeArm"), new V(-0.22, -0.72, 0.66)); aim(b("RightForeArm"), b("RightHand"), new V(-0.08, 0.3, 0.95));
  aim(b("RightHand"), b("RightHandMiddle1"), new V(-0.1, 0.2, 0.97));
  curl(T, "Right", { all: 1 }, 0); thumbUp(T, "Right");
  const tu = snap(bones);
  out.push(clipFrom("thumbs up", root, [base, tu, tu, base], [0, 0.45, 1.9, 2.4]));
  // arms crossed, fingers wrapped round the opposite arm
  reset();
  aim(b("LeftArm"), b("LeftForeArm"), new V(-0.06, -0.88, 0.44)); aim(b("LeftForeArm"), b("LeftHand"), new V(-0.96, 0.14, 0.2)); aim(b("LeftHand"), b("LeftHandMiddle1"), new V(-0.45, -0.15, -0.88));
  aim(b("RightArm"), b("RightForeArm"), new V(0.06, -0.86, 0.48)); aim(b("RightForeArm"), b("RightHand"), new V(0.96, 0.2, 0.18)); aim(b("RightHand"), b("RightHandMiddle1"), new V(0.45, -0.1, -0.88));
  curl(T, "Left", { all: 0.55 }, 0.3); curl(T, "Right", { all: 0.55 }, 0.3);
  const cr = snap(bones);
  out.push(clipFrom("arms crossed", root, [base, cr, cr, base], [0, 0.6, 3.4, 4.0]));
  reset();
  return out;
}
