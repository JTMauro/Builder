// Motion for MakeHuman people in three.js r160.
//  - retarget(): copies Mixamo-skeleton clips (three.js Xbot, MIT) onto an MPFB "mixamo" rig in world space
//  - gestures(): point, wave, thumbs up, arms crossed, made by aiming bones, with finger curls
// Both return ordinary THREE.AnimationClips for the target skeleton, so they play with AnimationMixer
// and can be exported with GLTFExporter.
import * as THREE from "three";
const V = THREE.Vector3, Q = THREE.Quaternion;
const key = n => n.replace(/[:_]/g, "").toLowerCase();

export function boneMap(root, rename = null) {
  const m = {};
  root.traverse(o => { if (o.isBone || (rename && rename[o.name])) { const n = rename ? rename[o.name] : o.name; if (n) m[key(rename ? "mixamorig" + n : n)] = o; } });
  return m;
}
// CMU motion capture (ASF/AMC skeleton names) to Mixamo names
export const CMU = {
  root: "Hips", lfemur: "LeftUpLeg", ltibia: "LeftLeg", lfoot: "LeftFoot", ltoes: "LeftToeBase",
  rfemur: "RightUpLeg", rtibia: "RightLeg", rfoot: "RightFoot", rtoes: "RightToeBase",
  lowerback: "Spine", upperback: "Spine1", thorax: "Spine2", lowerneck: "Neck", head: "Head",
  lclavicle: "LeftShoulder", lhumerus: "LeftArm", lradius: "LeftForeArm", lhand: "LeftHand",
  rclavicle: "RightShoulder", rhumerus: "RightArm", rradius: "RightForeArm", rhand: "RightHand",
};
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
export function retarget(srcRoot, clips, tgtRoot, { fps = 30, inPlace = true, lean = { idle: 0.015, walk: 0.07, run: 0.30, default: 0.015 }, neck = 0, head = 0, rename = null } = {}) {
  // a slight natural forward lean through the spine (radians at the top of the spine), head kept level
  // per move: how far the spine leans forward (radians at its top). The head keeps only a third of it, so he looks ahead.
  const leanOf = name => typeof lean === "number" ? lean : (lean[name] ?? lean.default ?? 0);
  const leanMap = L => ({ mixamorigspine: L / 3, mixamorigspine1: (2 * L) / 3, mixamorigspine2: L, mixamorigneck: L * 0.6 + neck, mixamorighead: L * 0.33 + neck + head });
  const xAxis = new V(1, 0, 0);
  const S = boneMap(srcRoot, rename), T = boneMap(tgtRoot);
  const names = Object.keys(T).filter(k => S[k]);
  srcRoot.updateMatrixWorld(true); tgtRoot.updateMatrixWorld(true);
  // turn the source to face the same way as the target (from the line between the hips)
  const flat = (a, b) => { const v = wp(a).sub(wp(b)); v.y = 0; return v.normalize(); };
  const F = new Q().setFromUnitVectors(flat(S["mixamorigleftupleg"], S["mixamorigrightupleg"]), flat(T["mixamorigleftupleg"], T["mixamorigrightupleg"]));
  const Fi = F.clone().invert();
  const swq = o => F.clone().multiply(wq(o));                 // source world rotation, turned to face the target's way
  const swp = o => wp(o).applyQuaternion(F);
  const tRest = new Map(order(tgtRoot).map(b => [b, b.quaternion.clone()]));
  // 1. bend the target into the source's rest pose by matching bone directions
  // arms and legs differ in rest pose (MPFB rests in an A-pose, Mixamo in a T-pose); the spine, neck and
  // head are left alone, because bending them tilts the whole posture into a swayback lean
  for (const b of order(tgtRoot)) {
    const k = key(b.name); if (!S[k]) continue;
    if (!/shoulder|arm|hand|upleg|leg|foot|toe/.test(k)) continue;
    const c = b.children.find(ch => ch.isBone && T[key(ch.name)] && S[key(ch.name)]); if (!c) continue;
    const sc = S[key(c.name)];
    aim(b, c, swp(sc).sub(swp(S[k])).normalize());
  }
  tgtRoot.updateMatrixWorld(true);
  const sRef = {}, tRef = {};
  for (const k of names) { sRef[k] = swq(S[k]); tRef[k] = wq(T[k]); }
  const hipS = S["mixamorighips"], hipT = T["mixamorighips"];
  const scale = wp(hipT).y / Math.max(1e-6, wp(hipS).y), hipS0 = swp(hipS), hipT0 = wp(hipT);
  const tOrder = order(tgtRoot).filter(b => S[key(b.name)]);
  const out = [];
  const mixer = new THREE.AnimationMixer(srcRoot);
  for (const clip of clips) {
    const src = clip.clone();   // keep the hips track: its height gives the bounce; x/z are dropped below when inPlace
    mixer.stopAllAction(); const act = mixer.clipAction(src); act.play();
    const LEAN = leanMap(leanOf(clip.name));
    // each clip starts facing the target's forward: undo the actor's heading at the first frame
    mixer.setTime(0); srcRoot.updateMatrixWorld(true);
    const Y0 = new Q().setFromUnitVectors(flat(S["mixamorigleftupleg"], S["mixamorigrightupleg"]).applyQuaternion(F), flat(T["mixamorigleftupleg"], T["mixamorigrightupleg"]));
    const yawFix = Math.abs(1 - Y0.w) > 1e-4 ? Y0 : null;
    const n = Math.max(2, Math.round(clip.duration * fps) + 1), times = [];
    const q = new Map(tOrder.map(b => [b, []])), hp = [];
    for (let i = 0; i < n; i++) {
      const t = Math.min(clip.duration, i / fps); times.push(t); mixer.setTime(t); srcRoot.updateMatrixWorld(true);
      for (const b of tOrder) {
        const k = key(b.name);
        const W = swq(S[k]).multiply(sRef[k].clone().invert()).multiply(tRef[k]);
        if (yawFix) W.premultiply(yawFix);
        if (LEAN[k]) W.premultiply(new Q().setFromAxisAngle(xAxis, LEAN[k]));
        b.parent.updateWorldMatrix(true, false);
        b.quaternion.copy(wq(b.parent).invert().multiply(W)); b.updateWorldMatrix(false, false);
        q.get(b).push(...b.quaternion.toArray());
      }
      const d = swp(hipS).sub(hipS0).multiplyScalar(scale); if (yawFix) d.applyQuaternion(yawFix); if (inPlace) { d.x = 0; d.z = 0; }
      const p = hipT0.clone().add(d); hp.push(...hipT.parent.worldToLocal(p).toArray());
    }
    void Fi;
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

// signed angle from a to b around axis (both perpendicular-ish to axis)
function angleAround(a, b, axis) {
  const pa = a.clone().addScaledVector(axis, -a.dot(axis)).normalize(), pb = b.clone().addScaledVector(axis, -b.dot(axis)).normalize();
  return Math.atan2(new V().crossVectors(pa, pb).dot(axis), pa.dot(pb));
}
function palmNormal(T, side) {
  const h = hand(T, side), across = wp(h.f.Pinky[0]).sub(wp(h.f.Index[0])).normalize(), along = wp(h.f.Middle[0]).sub(wp(h.hand)).normalize();
  const m = h.f.Middle, d1 = wp(m[1]).sub(wp(m[0])).normalize(), d2 = wp(m[2]).sub(wp(m[1])).normalize();
  let palm = d2.clone().sub(d1); palm.addScaledVector(along, -palm.dot(along));
  if (palm.length() < 1e-3) palm = new V().crossVectors(along, across);
  return { palm: palm.normalize(), along };
}
// roll the hand around its own axis so the palm faces dir (amount 0..1)
function rollPalm(T, side, dir, amount = 1) {
  const h = hand(T, side), { palm, along } = palmNormal(T, side);
  turn(h.hand, along, angleAround(palm, dir, along) * amount);
}
const ease = x => x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x);
const RIGHT = /mixamorigright(shoulder|arm|forearm|hand)/, BOTH = /mixamorig(left|right)(shoulder|arm|forearm|hand)/;

/**
 * Gesture clips layered over the idle: the arms doing the gesture follow it, the rest of the body
 * keeps playing idleClip. basePose is the idle's first frame (bone -> quaternion).
 */
export function gestures(root, basePose, idleClip) {
  const T = boneMap(root), bones = order(root), b = n => T[key("mixamorig" + n)];
  const reset = () => { for (const x of bones) x.quaternion.copy(basePose.get(x)); root.updateMatrixWorld(true); };
  const fps = 30;
  // idle samplers for the bones a gesture leaves alone
  const idleTrack = {}; if (idleClip) for (const tr of idleClip.tracks) idleTrack[tr.name] = tr;
  const sampler = name => { const tr = idleTrack[name]; if (!tr) return null; const it = tr.createInterpolant(); return t => Array.from(it.evaluate(t % idleClip.duration)); };
  function bake(name, dur, poseAt, active) {
    const n = Math.round(dur * fps) + 1, times = [...Array(n)].map((_, i) => Math.min(dur, i / fps));
    const vals = new Map(bones.map(x => [x, []]));
    for (const t of times) { const pose = poseAt(t); for (const x of bones) if (active.test(key(x.name))) vals.get(x).push(...pose.get(x).toArray()); }
    const tracks = [];
    for (const x of bones) {
      const nm = x.name + ".quaternion";
      if (active.test(key(x.name))) tracks.push(new THREE.QuaternionKeyframeTrack(nm, times, vals.get(x)));
      else { const s = sampler(nm); tracks.push(new THREE.QuaternionKeyframeTrack(nm, times, s ? times.flatMap(t => s(t)) : times.flatMap(() => basePose.get(x).toArray()))); }
    }
    const hips = bones.find(x => /hips$/.test(key(x.name))); const hs = hips && sampler(hips.name + ".position");
    if (hs) tracks.push(new THREE.VectorKeyframeTrack(hips.name + ".position", times, times.flatMap(t => hs(t))));
    return new THREE.AnimationClip(name, dur, tracks);
  }
  // in -> hold -> out between the base pose and one held pose, eased
  function held(pose, dur, tin = 0.45, tout = 0.5) {
    const base = (reset(), snap(bones));
    return t => { const u = ease(t / tin) * (1 - ease((t - (dur - tout)) / tout)); const m = new Map(); for (const x of bones) m.set(x, base.get(x).clone().slerp(pose.get(x), u)); return m; };
  }
  const out = [];
  // point forward with the right hand
  reset();
  aim(b("RightArm"), b("RightForeArm"), new V(-0.32, 0.12, 0.94)); aim(b("RightForeArm"), b("RightHand"), new V(-0.22, 0.14, 0.97));
  aim(b("RightHand"), b("RightHandMiddle1"), new V(-0.2, 0.1, 0.98)); rollPalm(T, "Right", new V(0, -1, 0), 0.8);
  curl(T, "Right", { Middle: 1, Ring: 1, Pinky: 1, Index: -0.25 }, 0.9);
  out.push(bake("point", 2.4, held(snap(bones), 2.4), RIGHT));
  // thumbs up with the right hand
  reset();
  aim(b("RightArm"), b("RightForeArm"), new V(-0.22, -0.72, 0.66)); aim(b("RightForeArm"), b("RightHand"), new V(-0.08, 0.3, 0.95));
  aim(b("RightHand"), b("RightHandMiddle1"), new V(-0.1, 0.2, 0.97)); rollPalm(T, "Right", new V(1, 0, 0), 0.8);
  curl(T, "Right", { all: 1 }, 0); thumbUp(T, "Right");
  out.push(bake("thumbs up", 2.4, held(snap(bones), 2.4), RIGHT));
  // wave: palm forward, a smooth side-to-side swing with the wrist trailing the forearm
  {
    const dur = 2.8;
    reset();
    const hang = { a: wp(b("RightForeArm")).sub(wp(b("RightArm"))).normalize(), f: wp(b("RightHand")).sub(wp(b("RightForeArm"))).normalize(), h: wp(b("RightHandMiddle1")).sub(wp(b("RightHand"))).normalize() };
    const poseAt = t => {
      reset();
      const up = ease(t / 0.55) * (1 - ease((t - (dur - 0.6)) / 0.6));
      const live = ease((t - 0.35) / 0.35) * (1 - ease((t - (dur - 0.75)) / 0.4));
      const ph = Math.max(0, t - 0.4) * Math.PI * 2 * 1.8;
      const sw = Math.sin(ph) * live, wr = Math.sin(ph - 0.75) * live;
      { const sh = b("RightShoulder"), ar = b("RightArm"); const d = wp(ar).sub(wp(sh)).normalize(); d.y += 0.16 * up; aim(sh, ar, d); }
      aim(b("RightArm"), b("RightForeArm"), hang.a.clone().lerp(new V(-0.88, 0.12 + 0.04 * sw, 0.34).normalize(), up).normalize());
      aim(b("RightForeArm"), b("RightHand"), hang.f.clone().lerp(new V(-0.12 + 0.22 * sw, 1, 0.16).normalize(), up).normalize());
      aim(b("RightHand"), b("RightHandMiddle1"), hang.h.clone().lerp(new V(-0.12 + 0.55 * wr, 1, 0.12).normalize(), up).normalize());
      rollPalm(T, "Right", new V(0, 0, 1), up);
      curl(T, "Right", { all: -0.12 * up, Index: -0.16 * up, Pinky: -0.05 * up }, 0.05);
      return snap(bones);
    };
    out.push(bake("wave", dur, poseAt, RIGHT));
  }
  // arms crossed, fingers wrapped round the opposite arm
  reset();
  aim(b("LeftArm"), b("LeftForeArm"), new V(-0.06, -0.88, 0.44)); aim(b("LeftForeArm"), b("LeftHand"), new V(-0.96, 0.14, 0.2)); aim(b("LeftHand"), b("LeftHandMiddle1"), new V(-0.45, -0.15, -0.88));
  aim(b("RightArm"), b("RightForeArm"), new V(0.06, -0.86, 0.48)); aim(b("RightForeArm"), b("RightHand"), new V(0.96, 0.2, 0.18)); aim(b("RightHand"), b("RightHandMiddle1"), new V(0.45, -0.1, -0.88));
  curl(T, "Left", { all: 0.55 }, 0.3); curl(T, "Right", { all: 0.55 }, 0.3);
  out.push(bake("arms crossed", 4.0, held(snap(bones), 4.0, 0.6, 0.6), BOTH));
  reset();
  return out;
}

/**
 * Motion from video: a Builder `video_to_motion` result (MediaPipe world landmarks) to a clip for a
 * Mixamo-style rig in its rest pose (facing +Z). Hips and chest turn to match the hip and shoulder lines,
 * limbs are aimed bone by bone, the head follows the ears and nose, and the hips are lowered so the
 * lowest foot stays on the floor. Face scores (doc.morphs) become morph-target tracks when the meshes
 * have those morphs. Returns a THREE.AnimationClip.
 */
export function fromLandmarks(doc, root, { name = "video", meshes = [] } = {}) {
  const M = boneMap(root), B = n => M[key("mixamorig" + n)];
  const bones = order(root), rest = new Map(bones.map(b => [b, b.quaternion.clone()])), restPos = new Map(bones.map(b => [b, b.position.clone()]));
  root.updateMatrixWorld(true);
  const restW = new Map(bones.map(b => [b, wq(b)]));
  const P = i => f => new V(f[i][0], -f[i][1], -f[i][2]);   // MediaPipe (x left-of-person, y down, z away) -> three (y up, z toward camera)
  const L = Object.fromEntries(doc.points.map((n, i) => [n, P(i)]));
  const mid = (a, b) => a.clone().add(b).multiplyScalar(0.5);
  const frameQ = (xDir, upDir) => {   // rotation taking the rest axes (x = left, y = up) to these directions
    const x = xDir.clone().normalize(), z = new V().crossVectors(x, upDir).normalize(), y = new V().crossVectors(z, x);
    return new Q().setFromRotationMatrix(new THREE.Matrix4().makeBasis(x, y, z));
  };
  const setWorld = (b, q) => { b.quaternion.copy(wq(b.parent).invert().multiply(q)); b.updateWorldMatrix(false, true); };
  const hips = B("Hips"), legLen = wp(B("LeftUpLeg")).distanceTo(wp(B("LeftLeg"))) + wp(B("LeftLeg")).distanceTo(wp(B("LeftFoot")));
  const footY0 = Math.min(wp(B("LeftFoot")).y, wp(B("RightFoot")).y), hipY0 = wp(hips).y;
  const fps = doc.fps, frames = doc.body || [], times = frames.map((_, i) => i / fps);
  const qv = new Map(bones.map(b => [b, []])), hv = [];
  const limbs = [["LeftArm", "LeftForeArm", "left_shoulder", "left_elbow"], ["LeftForeArm", "LeftHand", "left_elbow", "left_wrist"],
    ["RightArm", "RightForeArm", "right_shoulder", "right_elbow"], ["RightForeArm", "RightHand", "right_elbow", "right_wrist"],
    ["LeftUpLeg", "LeftLeg", "left_hip", "left_knee"], ["LeftLeg", "LeftFoot", "left_knee", "left_ankle"],
    ["RightUpLeg", "RightLeg", "right_hip", "right_knee"], ["RightLeg", "RightFoot", "right_knee", "right_ankle"],
    ["LeftFoot", "LeftToeBase", "left_heel", "left_foot_index"], ["RightFoot", "RightToeBase", "right_heel", "right_foot_index"],
    ["LeftHand", "LeftHandMiddle1", "left_wrist", "left_index"], ["RightHand", "RightHandMiddle1", "right_wrist", "right_index"]];
  let srcLeg = 0;
  for (const f of frames) {
    for (const b of bones) { b.quaternion.copy(rest.get(b)); b.position.copy(restPos.get(b)); }
    root.updateMatrixWorld(true);
    const lh = L.left_hip(f), rh = L.right_hip(f), ls = L.left_shoulder(f), rs = L.right_shoulder(f), hm = mid(lh, rh), sm = mid(ls, rs);
    const qh = frameQ(lh.clone().sub(rh), sm.clone().sub(hm)), qs = frameQ(ls.clone().sub(rs), sm.clone().sub(hm));
    setWorld(hips, qh.clone().multiply(restW.get(hips)));
    const sp = [B("Spine"), B("Spine1"), B("Spine2")];
    sp.forEach((b, i) => b && setWorld(b, qh.clone().slerp(qs, (i + 1) / 3).multiply(restW.get(b))));
    const le = L.left_ear(f), re = L.right_ear(f), nose = L.nose(f), em = mid(le, re);
    const fwd = nose.clone().sub(em), side = le.clone().sub(re), up = new V().crossVectors(fwd, side).normalize();
    const qHead = frameQ(side, up.dot(sm.clone().sub(hm)) < 0 ? up.negate() : up);
    if (B("Neck")) setWorld(B("Neck"), qs.clone().slerp(qHead, 0.4).multiply(restW.get(B("Neck"))));
    if (B("Head")) setWorld(B("Head"), qHead.clone().multiply(restW.get(B("Head"))));
    for (const [bn, cn, a, b] of limbs) { const bb = B(bn), cc = B(cn); if (bb && cc) aim(bb, cc, L[b](f).sub(L[a](f))); }
    // keep the lowest foot on the floor: hips height from the source leg, scaled to this rig
    const footLow = Math.min(L.left_heel(f).y, L.right_heel(f).y, L.left_foot_index(f).y, L.right_foot_index(f).y);
    srcLeg = Math.max(srcLeg, lh.distanceTo(L.left_knee(f)) + L.left_knee(f).distanceTo(L.left_ankle(f)));
    const hipH = (hm.y - footLow) * (legLen / Math.max(srcLeg, 1e-3));
    const want = new V(wp(hips).x, footY0 + hipH + (hipY0 - footY0 - legLen) * 0.6, wp(hips).z);
    hips.position.copy(hips.parent.worldToLocal(want)); hips.updateWorldMatrix(false, true);
    for (const b of bones) qv.get(b).push(...b.quaternion.toArray());
    hv.push(...hips.position.toArray());
  }
  for (const b of bones) { b.quaternion.copy(rest.get(b)); b.position.copy(restPos.get(b)); }
  const tracks = [];
  if (frames.length) {
    for (const b of bones) tracks.push(new THREE.QuaternionKeyframeTrack(b.name + ".quaternion", times, qv.get(b)));
    tracks.push(new THREE.VectorKeyframeTrack(hips.name + ".position", times, hv));
  }
  // face: one track per morph the meshes have
  const morphs = doc.morphs || [];
  if (morphs.length) {
    const mt = morphs.map((_, i) => i / fps);
    for (const mesh of meshes) {
      const dict = mesh.morphTargetDictionary || {};
      for (const n of Object.keys(dict)) {
        if (!morphs.some(m => m && n in m)) continue;
        let last = 0; const vals = morphs.map(m => (last = m && n in m ? m[n] : last));
        tracks.push(new THREE.NumberKeyframeTrack(`${mesh.uuid}.morphTargetInfluences[${n}]`, mt, vals));
      }
    }
  }
  return new THREE.AnimationClip(name, -1, tracks);
}
