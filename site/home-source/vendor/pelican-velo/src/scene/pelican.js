import * as THREE from "three";
import { clamp, createTaperedTube, cylinderBetween, setCylinderBetween } from "../lib/utils.js";

const featherMat = new THREE.MeshPhysicalMaterial({
  color: 0xf3e7d2,
  roughness: 0.72,
  metalness: 0.0,
  sheen: 1,
  sheenColor: new THREE.Color(0xffe8c2),
  sheenRoughness: 0.72,
  clearcoat: 0.06,
  clearcoatRoughness: 0.8,
});

const featherShadeMat = new THREE.MeshPhysicalMaterial({
  color: 0xd8ccb7,
  roughness: 0.8,
  sheen: 0.6,
  sheenColor: new THREE.Color(0xf3d7b3),
  sheenRoughness: 0.9,
});

const darkFeatherMat = new THREE.MeshStandardMaterial({
  color: 0x5d6258,
  roughness: 0.82,
  metalness: 0.02,
});

const beakMat = new THREE.MeshPhysicalMaterial({
  color: 0xe7a33f,
  roughness: 0.36,
  metalness: 0.02,
  clearcoat: 0.35,
  clearcoatRoughness: 0.4,
});

const pouchMat = new THREE.MeshPhysicalMaterial({
  color: 0xdc8540,
  roughness: 0.52,
  metalness: 0.0,
  clearcoat: 0.18,
  clearcoatRoughness: 0.5,
});

const footMat = new THREE.MeshStandardMaterial({ color: 0xdb8238, roughness: 0.55, metalness: 0.02 });

const scarfMat = new THREE.MeshPhysicalMaterial({
  color: 0xb8412f,
  roughness: 0.82,
  metalness: 0.0,
  sheen: 0.7,
  sheenColor: new THREE.Color(0xf08e69),
  sheenRoughness: 0.8,
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
    curveSegments: 10,
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
  const arm = createTaperedTube(armCurve, (t) => 0.115 * (1 - t * 0.55), featherMat, { tubular: 24, radial: 10 });
  arm.geometry.scale(1, 1, 0.72);
  wing.add(arm);

  // Feather fan sweeping behind the arm
  const count = 8;
  for (let i = 0; i < count; i++) {
    const t = i / (count - 1);
    const length = 0.72 + Math.sin(t * Math.PI * 0.85) * 0.55;
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
  for (let i = 0; i < 4; i++) {
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
  const webShape = new THREE.Shape();
  webShape.moveTo(0, 0.06);
  webShape.lineTo(0.19, 0.011);
  webShape.lineTo(0.19, -0.011);
  webShape.lineTo(0, -0.06);
  const webGeo = new THREE.ShapeGeometry(webShape);
  webGeo.rotateX(-Math.PI / 2);
  const web = new THREE.Mesh(webGeo, new THREE.MeshStandardMaterial({ color: 0xd9823d, roughness: 0.6, side: THREE.DoubleSide }));
  web.position.y = 0.012;
  foot.add(web);

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

  const body = new THREE.Mesh(new THREE.SphereGeometry(1, 40, 28), featherMat);
  body.scale.set(0.64, 0.5, 0.44);
  body.position.set(-0.08, 1.99, 0);
  body.rotation.z = 0.09;
  body.castShadow = true;
  body.receiveShadow = true;
  root.add(body);

  // A few overlapping coverts keep the back from reading as a single smooth egg.
  for (let row = 0; row < 3; row++) {
    for (let i = 0; i < 4; i++) {
      const tuft = new THREE.Mesh(new THREE.SphereGeometry(1, 12, 8), row === 1 ? featherShadeMat : featherMat);
      tuft.scale.set(0.14, 0.045, 0.1);
      tuft.position.set(-0.3 + i * 0.13, 2.37 - row * 0.1, (i - 1.5) * 0.12 + (row % 2) * 0.05);
      tuft.rotation.z = 0.15;
      tuft.castShadow = true;
      root.add(tuft);
    }
  }

  // Tail feathers
  for (let i = 0; i < 4; i++) {
    const t = i / 3;
    const feather = makeFeather(0.37 + Math.sin(t * Math.PI) * 0.13, 0.05, i % 2 ? featherShadeMat : featherMat);
    feather.position.set(-0.6, 1.98 + (t - 0.5) * 0.04, (t - 0.5) * 0.13);
    feather.rotation.z = Math.PI + 0.14;
    feather.rotation.y = (t - 0.5) * 0.42;
    root.add(feather);
  }

  // Neck
  const neckCurve = new THREE.CatmullRomCurve3([
    new THREE.Vector3(0.12, 2.2, 0),
    new THREE.Vector3(0.23, 2.4, 0.015),
    new THREE.Vector3(0.2, 2.62, -0.01),
    new THREE.Vector3(0.31, 2.83, 0),
    new THREE.Vector3(0.44, 2.97, 0),
  ]);
  const neck = createTaperedTube(neckCurve, (t) => 0.17 - t * 0.075, featherMat, { tubular: 36, radial: 14 });
  root.add(neck);

  // Head assembly
  const headPivot = new THREE.Group();
  headPivot.position.set(0.48, 3.06, 0);
  root.add(headPivot);

  const head = new THREE.Mesh(new THREE.SphereGeometry(1, 32, 24), featherMat);
  head.scale.set(0.29, 0.275, 0.275);
  head.castShadow = true;
  head.receiveShadow = true;
  headPivot.add(head);

  const cheekL = new THREE.Mesh(new THREE.SphereGeometry(1, 16, 12), featherShadeMat);
  cheekL.scale.set(0.13, 0.13, 0.09);
  cheekL.position.set(0.05, -0.06, 0.23);
  headPivot.add(cheekL);
  const cheekR = cheekL.clone();
  cheekR.position.z = -0.23;
  headPivot.add(cheekR);

  // Eyes
  const eyes = [];
  for (const side of [-1, 1]) {
    const eye = new THREE.Group();
    eye.position.set(0.11, 0.06, side * 0.235);
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

  // Beak
  const beakGroup = new THREE.Group();
  headPivot.add(beakGroup);

  const billCurve = new THREE.CatmullRomCurve3([
    new THREE.Vector3(0.18, -0.015, 0),
    new THREE.Vector3(0.43, -0.015, 0),
    new THREE.Vector3(0.7, -0.05, 0),
    new THREE.Vector3(0.94, -0.13, 0),
  ]);
  const bill = createTaperedTube(billCurve, (t) => 0.135 * (1 - t * 0.78), beakMat, { tubular: 28, radial: 14 });
  bill.geometry.scale(1, 0.57, 1.38);
  beakGroup.add(bill);

  const hook = new THREE.Mesh(new THREE.ConeGeometry(0.028, 0.1, 8), beakMat);
  hook.position.set(0.955, -0.165, 0);
  hook.rotation.z = 2.5;
  beakGroup.add(hook);

  const pouch = new THREE.Mesh(new THREE.SphereGeometry(1, 28, 18), pouchMat);
  pouch.scale.set(0.355, 0.16, 0.19);
  pouch.position.set(0.52, -0.185, 0);
  pouch.rotation.z = -0.05;
  pouch.castShadow = true;
  beakGroup.add(pouch);

  const mouthLine = new THREE.Mesh(new THREE.TorusGeometry(0.12, 0.006, 4, 18, Math.PI), pupilMat);
  mouthLine.position.set(0.22, -0.045, 0.19);
  mouthLine.rotation.set(Math.PI / 2, 0, -0.2);
  mouthLine.scale.set(1, 1, 1);
  beakGroup.add(mouthLine);

  // Captain's cap
  const cap = new THREE.Group();
  cap.position.set(-0.01, 0.245, 0);
  cap.rotation.z = -0.06;
  const crown = new THREE.Mesh(new THREE.SphereGeometry(0.205, 24, 14, 0, Math.PI * 2, 0, Math.PI / 2), capMat);
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
  const legLengths = [0.6, 0.58];

  // Interaction targets
  const hitMeshes = [body, head, bill, pouch, wingL, wingR];
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
    headPivot.position.y = 3.06 + Math.sin(time * 2.1) * 0.008;
    beakGroup.rotation.z = squawkWave * 0.22;
    pouch.scale.y = 0.16 * (1 + squawkWave * 0.25);
    pouch.scale.x = 0.355 * (1 + squawkWave * 0.04);

    // Scarf motion
    scarfA.update(time, speed);
    scarfB.update(time, speed);

    // Wings subtly follow the bars
    wingL.rotation.z = Math.sin(time * 2.1) * 0.008;
    wingR.rotation.z = Math.sin(time * 2.1 + 0.7) * 0.008;
    wingL.rotation.x = 0.01 + Math.sin(time * 1.4) * 0.005;
    wingR.rotation.x = 0.01 + Math.sin(time * 1.4 + 0.4) * 0.005;

    // Legs pedalling
    const footL = new THREE.Vector3(pedals.left.x, pedals.left.y + 0.06, 0.27);
    const footR = new THREE.Vector3(pedals.right.x, pedals.right.y + 0.06, -0.27);
    updateLeg(legL, hipL, footL, legLengths);
    updateLeg(legR, hipR, footR, legLengths);

    // Tail / head micro motion
    body.rotation.z = 0.09 + Math.sin(time * 1.7) * 0.008 + lean * 0.12;
  }

  return {
    root,
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
