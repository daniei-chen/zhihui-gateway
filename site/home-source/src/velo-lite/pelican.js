// Adapted for Zhihui from Jacory Ou, Pelican / Velo (MIT). See vendor/pelican-velo.
// Changed: bounded geometry, short-beaked partridge styling and no audio/night effects.
import * as THREE from "three";
import { clamp, createTaperedTube, cylinderBetween, setCylinderBetween, makeTube } from "../../vendor/pelican-velo/src/lib/utils.js";

const featherMat = new THREE.MeshStandardMaterial({
  color: 0xbba27c,
  roughness: 0.72,
  metalness: 0.0,
});

const featherShadeMat = new THREE.MeshStandardMaterial({
  color: 0x867460,
  roughness: 0.8,
});

const darkFeatherMat = new THREE.MeshStandardMaterial({
  color: 0x4d4942,
  roughness: 0.82,
  metalness: 0.02,
});

const beakMat = new THREE.MeshStandardMaterial({
  color: 0xe7a33f,
  roughness: 0.36,
  metalness: 0.02,
});

const pouchMat = new THREE.MeshStandardMaterial({
  color: 0xdc8540,
  roughness: 0.52,
  metalness: 0.0,
});

const footMat = new THREE.MeshStandardMaterial({ color: 0xdb8238, roughness: 0.55, metalness: 0.02 });

const scarfMat = new THREE.MeshStandardMaterial({
  color: 0xb8412f,
  roughness: 0.82,
  metalness: 0.0,
  side: THREE.DoubleSide,
});

const capMat = new THREE.MeshStandardMaterial({ color: 0x163d4b, roughness: 0.62, metalness: 0.05 });
const capBandMat = new THREE.MeshStandardMaterial({ color: 0x0f2d38, roughness: 0.6 });
const goldMat = new THREE.MeshStandardMaterial({ color: 0xd1a44f, roughness: 0.3, metalness: 0.9 });

const eyeWhiteMat = new THREE.MeshStandardMaterial({ color: 0xf7ebd6, roughness: 0.28 });
const irisMat = new THREE.MeshStandardMaterial({ color: 0xd9a13f, roughness: 0.2, emissive: 0x2a1805, emissiveIntensity: 0.2 });
const pupilMat = new THREE.MeshStandardMaterial({ color: 0x050807, roughness: 0.15 });
const glintMat = new THREE.MeshBasicMaterial({ color: 0xffffff });

function makeFeather(length, width, material, thickness = 0.014) {
  const shape = new THREE.Shape();
  shape.moveTo(0, 0);
  shape.bezierCurveTo(length * 0.16, width, length * 0.72, width * 0.82, length, 0);
  shape.bezierCurveTo(length * 0.66, -width * 0.44, length * 0.2, -width * 0.52, 0, 0);
  const geo = new THREE.ExtrudeGeometry(shape, {
    depth: thickness,
    bevelEnabled: true,
    bevelSize: 0.004,
    bevelThickness: 0.003,
    bevelSegments: 1,
    steps: 1,
    curveSegments: 5,
  });
  geo.translate(0, 0, -thickness / 2);
  const mesh = new THREE.Mesh(geo, material);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

function makeWing(side) {
  const wing = new THREE.Group();
  const shoulder = new THREE.Vector3(0.05, 2.16, side * 0.37);
  wing.position.copy(shoulder);
  wing.rotation.y = side * -0.02;

  const armCurve = new THREE.CatmullRomCurve3([
    new THREE.Vector3(0, 0, 0),
    new THREE.Vector3(0.2, -0.12, side * 0.08),
    new THREE.Vector3(0.38, -0.3, side * 0.045),
    new THREE.Vector3(0.5, -0.49, side * 0.035),
  ]);
  const arm = createTaperedTube(armCurve, (t) => 0.115 * (1 - t * 0.55), featherMat, { tubular: 12, radial: 7 });
  arm.geometry.scale(1, 1, 0.72);
  wing.add(arm);

  // Feather fan sweeping behind the arm
  const count = 5;
  for (let i = 0; i < count; i++) {
    const t = i / (count - 1);
    const length = 0.40 + Math.sin(t * Math.PI * 0.85) * 0.25;
    const width = 0.08 + (1 - t) * 0.04;
    const mat = i >= count - 3 ? darkFeatherMat : featherShadeMat;
    const feather = makeFeather(length, width, mat);
    feather.position.set(-0.02 + t * 0.12, -0.02 - t * 0.06, side * (0.02 + t * 0.05));
    feather.rotation.z = Math.PI + 0.08 + t * 0.34;
    feather.rotation.y = side * (0.16 + t * 0.3);
    feather.rotation.x = side * (0.08 + t * 0.17);
    feather.scale.setScalar(0.93 + t * 0.22);
    wing.add(feather);
  }

  // Shoulder coverts
  for (let i = 0; i < 2; i++) {
    const blob = new THREE.Mesh(new THREE.SphereGeometry(1, 12, 8), i % 2 ? featherMat : featherShadeMat);
    blob.scale.set(0.16, 0.08, 0.12);
    blob.position.set(0.03 + i * 0.05, 0.02 - i * 0.025, side * (0.03 + i * 0.018));
    blob.rotation.z = -0.1;
    blob.castShadow = true;
    wing.add(blob);
  }

  // Wing hand, resting on the grip
  const hand = new THREE.Group();
  hand.position.set(0.52, -0.51, side * 0.035);
  const palm = new THREE.Mesh(new THREE.SphereGeometry(0.085, 12, 8), featherMat);
  palm.scale.set(1.2, 0.8, 0.9);
  hand.add(palm);
  for (let i = 0; i < 3; i++) {
    const finger = new THREE.Mesh(new THREE.CapsuleGeometry(0.018, 0.075, 4, 6), featherShadeMat);
    finger.rotation.z = -0.9 + i * 0.08;
    finger.rotation.x = (i - 1) * 0.34;
    finger.position.set(0.05, -0.05, (i - 1) * 0.035);
    hand.add(finger);
  }
  wing.add(hand);

  return wing;
}

function makeScarfRibbon(start, phase, width, length) {
  const N = 22;
  const positions = new Float32Array((N + 1) * 2 * 3);
  const indices = [];
  for (let i = 0; i < N; i++) {
    const a = i * 2;
    indices.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setIndex(indices);
  const mesh = new THREE.Mesh(geometry, scarfMat);
  mesh.castShadow = true;
  mesh.frustumCulled = false;

  const center = new THREE.Vector3();
  const widthDir = new THREE.Vector3();
  const p1 = new THREE.Vector3();
  const p2 = new THREE.Vector3();

  function update(time, speed) {
    const speedFactor = clamp(speed / 7, 0, 1.3);
    for (let i = 0; i <= N; i++) {
      const t = i / N;
      const sway = Math.sin(time * (3.1 + speedFactor * 2.2) - t * 4.4 + phase) * (0.028 + t * 0.095) * (0.5 + speedFactor);
      center.set(
        start.x - t * length * (0.72 + speedFactor * 0.34),
        start.y - t * 0.16 + sway + Math.sin(t * Math.PI) * 0.04,
        start.z + Math.sin(t * 2.4 + phase * 1.3 + time * 1.6) * (0.035 + t * 0.055),
      );
      const twist = Math.sin(t * 4.2 + time * 2.4 + phase) * 0.55 + 0.15;
      const halfWidth = width * (1 - t * 0.35);
      widthDir.set(0, Math.sin(twist) * halfWidth, Math.cos(twist) * halfWidth);
      p1.copy(center).add(widthDir);
      p2.copy(center).sub(widthDir);
      positions[i * 6] = p1.x;
      positions[i * 6 + 1] = p1.y;
      positions[i * 6 + 2] = p1.z;
      positions[i * 6 + 3] = p2.x;
      positions[i * 6 + 4] = p2.y;
      positions[i * 6 + 5] = p2.z;
    }
    geometry.attributes.position.needsUpdate = true;
    geometry.computeVertexNormals();
  }

  return { mesh, update };
}

function makeLeg(side) {
  const thigh = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 1, 8), footMat);
  const shin = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 1, 8), footMat);
  thigh.castShadow = true;
  shin.castShadow = true;
  thigh.scale.set(0.05, 1, 0.05);
  shin.scale.set(0.036, 1, 0.036);

  const knee = new THREE.Mesh(new THREE.SphereGeometry(0.062, 10, 8), footMat);
  knee.castShadow = true;
  const ankle = new THREE.Mesh(new THREE.SphereGeometry(0.045, 10, 8), footMat);

  const foot = new THREE.Group();
  const palm = new THREE.Mesh(new THREE.SphereGeometry(1, 12, 8), footMat);
  palm.scale.set(0.09, 0.028, 0.075);
  palm.position.y = 0.02;
  foot.add(palm);
  for (let i = 0; i < 3; i++) {
    const toe = new THREE.Mesh(new THREE.CapsuleGeometry(0.018, 0.12, 4, 6), footMat);
    toe.rotation.z = -Math.PI / 2;
    toe.rotation.y = (i - 1) * 0.32;
    toe.position.set(0.1, 0.02, (i - 1) * 0.045);
    foot.add(toe);
  }

  return { side, thigh, shin, knee, ankle, foot };
}

function updateLeg(leg, hip, footTarget, lengths) {
  const dir = new THREE.Vector3().subVectors(footTarget, hip);
  const d = clamp(dir.length(), 0.22, lengths[0] + lengths[1] - 0.006);
  dir.normalize();
  const a = (lengths[0] * lengths[0] - lengths[1] * lengths[1] + d * d) / (2 * d);
  const h = Math.sqrt(Math.max(0, lengths[0] * lengths[0] - a * a));
  const base = hip.clone().addScaledVector(dir, a);
  const perp = new THREE.Vector3(-dir.y, dir.x, 0).normalize();
  if (perp.lengthSq() < 0.01) perp.set(1, 0, 0);
  const kneePos = base.addScaledVector(perp, h);

  setCylinderBetween(leg.thigh, hip, kneePos);
  setCylinderBetween(leg.shin, kneePos, footTarget);
  leg.thigh.scale.set(0.05, leg.thigh.scale.y, 0.05);
  leg.shin.scale.set(0.036, leg.shin.scale.y, 0.036);
  leg.knee.position.copy(kneePos);
  leg.ankle.position.copy(footTarget);
  leg.foot.position.copy(footTarget).add(new THREE.Vector3(0.035, 0.05, 0));
  leg.foot.rotation.y = leg.side * 0.06;
}

export function createPelican() {
  const root = new THREE.Group();
  root.name = "pelican";

  const body = new THREE.Mesh(new THREE.SphereGeometry(1, 24, 16), featherMat);
  body.scale.set(0.64, 0.5, 0.44);
  body.position.set(-0.08, 1.99, 0);
  body.rotation.z = 0.09;
  body.castShadow = true;
  body.receiveShadow = true;
  root.add(body);

  // A few overlapping coverts keep the back from reading as a single smooth egg.
  for (let row = 0; row < 3; row++) {
    for (let i = 0; i < 2; i++) {
      const tuft = new THREE.Mesh(new THREE.SphereGeometry(1, 12, 8), row === 1 ? featherShadeMat : featherMat);
      tuft.scale.set(0.14, 0.045, 0.1);
      tuft.position.set(-0.3 + i * 0.13, 2.37 - row * 0.1, (i - 1.5) * 0.12 + (row % 2) * 0.05);
      tuft.rotation.z = 0.15;
      tuft.castShadow = true;
      root.add(tuft);
    }
  }

  // Tail feathers
  for (let i = 0; i < 2; i++) {
    const t = i / 3;
    const feather = makeFeather(0.37 + Math.sin(t * Math.PI) * 0.13, 0.05, i % 2 ? featherShadeMat : featherMat);
    feather.position.set(-0.6, 1.98 + (t - 0.5) * 0.04, (t - 0.5) * 0.13);
    feather.rotation.z = Math.PI + 0.14;
    feather.rotation.y = (t - 0.5) * 0.42;
    root.add(feather);
  }

  // A short, continuous neck keeps the silhouette partridge-like from every angle.
  const neckCurve = new THREE.CatmullRomCurve3([
    new THREE.Vector3(0.13, 2.18, 0), new THREE.Vector3(0.26, 2.38, 0),
    new THREE.Vector3(0.40, 2.56, 0),
  ]);
  root.add(createTaperedTube(neckCurve, t => 0.21 - t * 0.045, featherMat, {tubular:12, radial:8}));

  // Head assembly
  const headPivot = new THREE.Group();
  headPivot.position.set(0.48, 2.65, 0);
  root.add(headPivot);

  const head = new THREE.Mesh(new THREE.SphereGeometry(1, 20, 14), featherMat);
  head.scale.set(0.29, 0.275, 0.275);
  head.castShadow = true;
  head.receiveShadow = true;
  headPivot.add(head);

  // Eyes
  const eyes = [];
  for (const side of [-1, 1]) {
    const eye = new THREE.Group();
    eye.position.set(0.11, 0.06, side * 0.30);
    const white = new THREE.Mesh(new THREE.SphereGeometry(0.062, 14, 10), eyeWhiteMat);
    white.scale.set(1, 0.92, 0.82);
    const iris = new THREE.Mesh(new THREE.SphereGeometry(0.043, 12, 8), irisMat);
    iris.position.set(0.018, 0, side * 0.025);
    const pupil = new THREE.Mesh(new THREE.SphereGeometry(0.023, 10, 8), pupilMat);
    pupil.position.set(0.032, 0, side * 0.042);
    const glint = new THREE.Mesh(new THREE.SphereGeometry(0.008, 6, 4), glintMat);
    glint.position.set(0.048, 0.012, side * 0.052);
    eye.add(white, iris, pupil, glint);
    headPivot.add(eye);
    eyes.push(eye);
  }

  // Short red bill: no pelican pouch or long mandible.
  const bill = new THREE.Mesh(new THREE.ConeGeometry(0.092, 0.23, 12), beakMat);
  bill.rotation.z = -Math.PI / 2; bill.position.set(0.32, -0.025, 0);
  headPivot.add(bill);
  beakMat.color.setHex(0xb6583c);
  const face = new THREE.MeshStandardMaterial({color:0xf4e7ce,roughness:0.85});
  const mask = new THREE.MeshStandardMaterial({color:0x35352f,roughness:0.9});
  for (const side of [-1,1]) {
    const facePatch = new THREE.Mesh(new THREE.SphereGeometry(1,16,10),face);
    facePatch.position.set(0.06,-0.015,side*0.18);facePatch.scale.set(0.23,0.23,0.12);
    headPivot.add(facePatch);
    const stripe = makeTube([
      new THREE.Vector3(0.23,0.035,side*0.235),new THREE.Vector3(0.10,0.11,side*0.29),
      new THREE.Vector3(-0.08,0.09,side*0.26),new THREE.Vector3(-0.19,0.015,side*0.19)
    ],0.016,mask,{tubular:12,radial:5});
    headPivot.add(stripe);
  }

  // Captain's cap
  const cap = new THREE.Group();
  cap.position.set(-0.01, 0.245, 0);
  cap.rotation.z = -0.06;
  const crown = new THREE.Mesh(new THREE.SphereGeometry(0.205, 18, 12, 0, Math.PI * 2, 0, Math.PI / 2), capMat);
  crown.scale.set(1.02, 0.72, 1);
  crown.castShadow = true;
  cap.add(crown);
  const band = new THREE.Mesh(new THREE.CylinderGeometry(0.207, 0.207, 0.052, 24), capBandMat);
  band.position.y = 0.012;
  cap.add(band);
  const bandTrim = new THREE.Mesh(new THREE.TorusGeometry(0.209, 0.008, 4, 28), goldMat);
  bandTrim.rotation.x = Math.PI / 2;
  bandTrim.position.y = 0.037;
  cap.add(bandTrim);
  const brim = new THREE.Mesh(new THREE.CylinderGeometry(0.268, 0.268, 0.022, 24), capBandMat);
  brim.position.set(0.055, -0.018, 0);
  brim.scale.z = 1.04;
  cap.add(brim);
  const badge = new THREE.Mesh(new THREE.CylinderGeometry(0.032, 0.032, 0.012, 12), goldMat);
  badge.rotation.z = Math.PI / 2;
  badge.position.set(0.2, 0.02, 0);
  cap.add(badge);
  headPivot.add(cap);

  // Scarf around the neck
  const scarfRing = new THREE.Mesh(new THREE.TorusGeometry(0.19, 0.045, 8, 24), scarfMat);
  scarfRing.rotation.x = Math.PI / 2;
  scarfRing.rotation.z = 0.08;
  scarfRing.position.set(0.17, 2.42, 0);
  scarfRing.castShadow = true;
  root.add(scarfRing);
  const knot = new THREE.Mesh(new THREE.SphereGeometry(0.075, 12, 8), scarfMat);
  knot.position.set(0.08, 2.37, 0.17);
  root.add(knot);

  const scarfA = makeScarfRibbon(new THREE.Vector3(0.08, 2.37, 0.16), 0.2, 0.07, 1.02);
  const scarfB = makeScarfRibbon(new THREE.Vector3(0.07, 2.34, 0.08), 2.1, 0.055, 0.82);
  root.add(scarfA.mesh, scarfB.mesh);

  // Wings
  const wingL = makeWing(-1);
  const wingR = makeWing(1);
  root.add(wingL, wingR);

  // Legs
  const legL = makeLeg(-1);
  const legR = makeLeg(1);
  root.add(legL.thigh, legL.shin, legL.knee, legL.ankle, legL.foot);
  root.add(legR.thigh, legR.shin, legR.knee, legR.ankle, legR.foot);

  const hipL = new THREE.Vector3(-0.22, 1.75, 0.2);
  const hipR = new THREE.Vector3(-0.22, 1.75, -0.2);
  const legLengths = [0.69, 0.67];

  // Interaction targets
  const hitMeshes = [body, head, bill, wingL, wingR];
  root.traverse((child) => {
    if (child.isMesh && hitMeshes.includes(child)) child.userData.pelican = true;
  });

  let blinkTimer = 3.4;
  let blink = 0;
  let squawk = 0;
  let squawkTime = 0;
  let headLook = 0;

  function triggerBlink() {
    blink = 0.16;
  }

  function triggerSquawk() {
    squawk = 1;
    squawkTime = 0;
    blink = 0.14;
  }

  function update(dt, time, speed, pedals, lean) {
    root.position.y = Math.sin(time * 2.1) * 0.008 + Math.sin(time * 4.2) * 0.002;

    // Idle breathing
    const breath = 1 + Math.sin(time * 1.35) * 0.006;
    body.scale.set(0.64 * breath, 0.5 * breath, 0.44 * breath);

    // Blink
    blinkTimer -= dt;
    if (blinkTimer <= 0 && blink <= 0) {
      triggerBlink();
      blinkTimer = 3.2 + Math.random() * 4.2;
    }
    blink = Math.max(0, blink - dt);
    const blinkAmt = blink > 0 ? Math.sin((blink / 0.16) * Math.PI) : 0;
    for (const eye of eyes) eye.scale.y = 1 - blinkAmt * 0.92;

    // Head look + squawk
    squawk = Math.max(0, squawk - dt * 1.8);
    if (squawk > 0) squawkTime += dt;
    const squawkWave = squawk > 0 ? Math.sin(squawk * Math.PI) : 0;
    headLook = Math.sin(time * 0.48) * 0.16 + Math.sin(time * 0.21 + 1.3) * 0.08;
    headPivot.rotation.y = headLook + lean * 0.38;
    headPivot.rotation.z = Math.sin(time * 0.72) * 0.025 - squawkWave * 0.16;
    headPivot.rotation.x = Math.sin(time * 0.61 + 0.4) * 0.018;
    headPivot.position.y = 2.65 + Math.sin(time * 2.1) * 0.008;


    // Scarf motion
    scarfA.update(time, speed);
    scarfB.update(time, speed);

    // Wings subtly follow the bars
    wingL.rotation.z = Math.sin(time * 2.1) * 0.008;
    wingR.rotation.z = Math.sin(time * 2.1 + 0.7) * 0.008;
    wingL.rotation.x = 0.01 + Math.sin(time * 1.4) * 0.005;
    wingR.rotation.x = 0.01 + Math.sin(time * 1.4 + 0.4) * 0.005;

    // Legs pedalling
    const footL = new THREE.Vector3(pedals.left.x, pedals.left.y + 0.01 - root.position.y, 0.27);
    const footR = new THREE.Vector3(pedals.right.x, pedals.right.y + 0.01 - root.position.y, -0.27);
    updateLeg(legL, hipL, footL, legLengths);
    updateLeg(legR, hipR, footR, legLengths);

    // Tail / head micro motion
    body.rotation.z = 0.09 + Math.sin(time * 1.7) * 0.008 + lean * 0.12;
  }

  return {
    root,
    body, wingL, wingR,
    legs: [legL,legR],
    animated: [body,headPivot,wingL,wingR,...eyes,scarfA.mesh,scarfB.mesh,
      ...[legL,legR].flatMap(l=>[l.thigh,l.shin,l.knee,l.ankle,l.foot])],
    update,
    triggerSquawk,
    triggerBlink,
    setHeadLook(v) {
      headLook = v;
    },
    get headPivot() {
      return headPivot;
    },
  };
}
