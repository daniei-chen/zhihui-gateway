import * as THREE from "three";
import { clamp, cylinderBetween, makeTube, setCylinderBetween } from "../lib/utils.js";

const WHEEL_RADIUS = 0.66;
const CRANK_RADIUS = 0.23;
const BB = new THREE.Vector3(-0.05, 0.72, 0);
const REAR_HUB = new THREE.Vector3(-0.93, 0.67, 0);
const FRONT_HUB = new THREE.Vector3(0.96, 0.67, 0);
const HEAD_TOP = new THREE.Vector3(0.71, 1.45, 0);
const HEAD_BOTTOM = new THREE.Vector3(0.82, 0.98, 0);
const SEAT_TOP = new THREE.Vector3(-0.37, 1.53, 0);

const frameMat = new THREE.MeshStandardMaterial({ color: 0x0f5a58, roughness: 0.33, metalness: 0.55 });
const brassMat = new THREE.MeshStandardMaterial({ color: 0xc79a4b, roughness: 0.26, metalness: 0.9 });
const tireMat = new THREE.MeshStandardMaterial({ color: 0x162624, roughness: 0.82, metalness: 0.04 });
const rimMat = new THREE.MeshStandardMaterial({ color: 0xd9cbaa, roughness: 0.35, metalness: 0.72 });
const leatherMat = new THREE.MeshStandardMaterial({ color: 0x77422c, roughness: 0.65, metalness: 0.02 });
const gripMat = new THREE.MeshStandardMaterial({ color: 0x3b2c24, roughness: 0.85 });
const lampMat = new THREE.MeshStandardMaterial({
  color: 0xffedc2,
  emissive: 0xffc06a,
  emissiveIntensity: 0.35,
  roughness: 0.12,
  metalness: 0.1,
});

function makeWheel(radius) {
  const group = new THREE.Group();

  const tire = new THREE.Mesh(new THREE.TorusGeometry(radius, 0.065, 12, 56), tireMat);
  tire.castShadow = true;
  tire.receiveShadow = true;
  group.add(tire);

  const rim = new THREE.Mesh(new THREE.TorusGeometry(radius - 0.065, 0.018, 6, 48), rimMat);
  rim.castShadow = true;
  group.add(rim);

  const spokePositions = [];
  const spokes = 20;
  for (let i = 0; i < spokes; i++) {
    const a = (i / spokes) * Math.PI * 2;
    spokePositions.push(0, 0, 0, Math.cos(a) * (radius - 0.075), Math.sin(a) * (radius - 0.075), 0);
  }
  const spokeGeo = new THREE.BufferGeometry();
  spokeGeo.setAttribute("position", new THREE.Float32BufferAttribute(spokePositions, 3));
  const spokeMesh = new THREE.LineSegments(
    spokeGeo,
    new THREE.LineBasicMaterial({ color: 0xd9cfb9, transparent: true, opacity: 0.75 }),
  );
  group.add(spokeMesh);

  const hub = new THREE.Mesh(new THREE.CylinderGeometry(0.055, 0.055, 0.13, 12), brassMat);
  hub.rotation.x = Math.PI / 2;
  hub.castShadow = true;
  group.add(hub);

  return group;
}

function makeFender(radius, angle = 2.65) {
  const geo = new THREE.TorusGeometry(radius, 0.03, 6, 36, angle);
  const mesh = new THREE.Mesh(geo, frameMat);
  mesh.rotation.z = Math.PI / 2 - angle / 2;
  mesh.castShadow = true;
  return mesh;
}

function makeBasket() {
  const group = new THREE.Group();
  const wicker = new THREE.MeshStandardMaterial({ color: 0xa57744, roughness: 0.92 });
  const wickerDark = new THREE.MeshStandardMaterial({ color: 0x6e4a29, roughness: 0.95 });

  const body = new THREE.Mesh(new THREE.CylinderGeometry(0.28, 0.22, 0.34, 14, 1, true), wicker);
  body.position.y = 0.17;
  body.castShadow = true;
  body.material.side = THREE.DoubleSide;
  group.add(body);

  const bottom = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.22, 0.035, 14), wickerDark);
  bottom.position.y = 0.015;
  group.add(bottom);
  const rim = new THREE.Mesh(new THREE.TorusGeometry(0.28, 0.018, 5, 20), wickerDark);
  rim.rotation.x = Math.PI / 2;
  rim.position.y = 0.34;
  group.add(rim);
  const midRing = new THREE.Mesh(new THREE.TorusGeometry(0.255, 0.01, 4, 20), wickerDark);
  midRing.rotation.x = Math.PI / 2;
  midRing.position.y = 0.18;
  group.add(midRing);

  for (let i = 0; i < 14; i++) {
    const a = (i / 14) * Math.PI * 2;
    const bar = new THREE.Mesh(new THREE.CylinderGeometry(0.008, 0.008, 0.34, 4), wickerDark);
    bar.position.set(Math.cos(a) * 0.26, 0.17, Math.sin(a) * 0.26);
    bar.rotation.z = Math.sin(a) * 0.12;
    bar.rotation.x = Math.cos(a) * 0.12;
    group.add(bar);
  }

  // One very patient fish
  const fish = new THREE.Group();
  const fishMat = new THREE.MeshStandardMaterial({ color: 0x7fa9aa, roughness: 0.28, metalness: 0.55 });
  const fishBody = new THREE.Mesh(new THREE.SphereGeometry(1, 16, 12), fishMat);
  fishBody.scale.set(0.22, 0.09, 0.065);
  fishBody.castShadow = true;
  fish.add(fishBody);
  const tail = new THREE.Mesh(new THREE.ConeGeometry(0.075, 0.18, 3), fishMat);
  tail.rotation.z = Math.PI / 2;
  tail.position.x = -0.25;
  tail.scale.y = 0.55;
  fish.add(tail);
  const eye = new THREE.Mesh(
    new THREE.SphereGeometry(0.016, 8, 6),
    new THREE.MeshStandardMaterial({ color: 0x101617, roughness: 0.2 }),
  );
  eye.position.set(0.13, 0.035, 0.055);
  fish.add(eye);
  fish.position.set(0.02, 0.4, 0.02);
  fish.rotation.z = 0.35;
  fish.rotation.y = -0.2;
  group.add(fish);

  return group;
}

export function createBike() {
  const group = new THREE.Group();
  group.name = "bicycle";

  const wheels = new THREE.Group();
  const rearWheel = makeWheel(WHEEL_RADIUS);
  rearWheel.position.copy(REAR_HUB);
  const frontWheel = makeWheel(WHEEL_RADIUS);
  frontWheel.position.copy(FRONT_HUB);
  wheels.add(rearWheel, frontWheel);
  group.add(wheels);

  // Fenders
  const rearFender = makeFender(0.735, 2.5);
  rearFender.position.copy(REAR_HUB);
  group.add(rearFender);
  const frontFender = makeFender(0.735, 2.15);
  frontFender.position.copy(FRONT_HUB);
  frontFender.rotation.z -= 0.9;
  group.add(frontFender);

  // Frame
  const frame = new THREE.Group();
  frame.add(cylinderBetween(BB, SEAT_TOP, 0.042, frameMat, 10));
  frame.add(cylinderBetween(SEAT_TOP, HEAD_TOP, 0.046, frameMat, 10));
  frame.add(cylinderBetween(BB, HEAD_BOTTOM, 0.048, frameMat, 10));
  frame.add(cylinderBetween(HEAD_BOTTOM, HEAD_TOP, 0.055, frameMat, 12));
  for (const z of [-0.045, 0.045]) {
    const rear = REAR_HUB.clone().setZ(z);
    const bb = BB.clone().setZ(z);
    const seat = SEAT_TOP.clone().setZ(z);
    frame.add(cylinderBetween(rear, bb, 0.024, frameMat, 8));
    frame.add(cylinderBetween(rear, seat, 0.027, frameMat, 8));
  }
  for (const z of [-0.065, 0.065]) {
    const top = HEAD_BOTTOM.clone().setZ(z * 0.4).setY(HEAD_BOTTOM.y + 0.03);
    const hub = FRONT_HUB.clone().setZ(z);
    frame.add(cylinderBetween(top, hub, 0.028, frameMat, 8));
  }
  frame.add(cylinderBetween(SEAT_TOP, SEAT_TOP.clone().add(new THREE.Vector3(-0.015, 0.1, 0)), 0.028, brassMat, 8));
  group.add(frame);

  // Saddle
  const saddle = new THREE.Mesh(new THREE.SphereGeometry(1, 20, 14), leatherMat);
  saddle.scale.set(0.24, 0.065, 0.15);
  saddle.position.set(-0.4, 1.67, 0);
  saddle.castShadow = true;
  group.add(saddle);
  const saddleNose = new THREE.Mesh(new THREE.SphereGeometry(1, 16, 10), leatherMat);
  saddleNose.scale.set(0.17, 0.045, 0.075);
  saddleNose.position.set(-0.2, 1.665, 0);
  group.add(saddleNose);
  for (const z of [-0.1, 0.1]) {
    const spring = new THREE.Mesh(new THREE.TorusGeometry(0.045, 0.01, 4, 12, Math.PI * 1.7), brassMat);
    spring.position.set(-0.5, 1.58, z);
    spring.rotation.y = Math.PI / 2;
    group.add(spring);
  }

  // Handlebar
  const barPoints = [
    new THREE.Vector3(0.58, 1.64, -0.37),
    new THREE.Vector3(0.63, 1.7, -0.22),
    new THREE.Vector3(0.65, 1.71, 0),
    new THREE.Vector3(0.63, 1.7, 0.22),
    new THREE.Vector3(0.58, 1.64, 0.37),
  ];
  group.add(makeTube(barPoints, 0.026, brassMat, { tubular: 32, radial: 6 }));
  group.add(cylinderBetween(HEAD_TOP, new THREE.Vector3(0.64, 1.7, 0), 0.034, brassMat, 8));
  for (const z of [-0.37, 0.37]) {
    const grip = new THREE.Mesh(new THREE.CylinderGeometry(0.038, 0.038, 0.16, 10), gripMat);
    grip.rotation.x = Math.PI / 2;
    grip.position.set(0.565, 1.63, z);
    group.add(grip);
    const lever = new THREE.Mesh(new THREE.TorusGeometry(0.075, 0.008, 4, 12, Math.PI * 0.7), brassMat);
    lever.position.set(0.58, 1.58, z * 0.92);
    lever.rotation.y = z > 0 ? 0 : Math.PI;
    group.add(lever);
  }

  // Bell
  const bellGroup = new THREE.Group();
  const bellDome = new THREE.Mesh(new THREE.SphereGeometry(0.07, 14, 8, 0, Math.PI * 2, 0, Math.PI / 2), brassMat);
  bellDome.castShadow = true;
  const bellBase = new THREE.Mesh(new THREE.CylinderGeometry(0.075, 0.075, 0.02, 12), brassMat);
  bellBase.position.y = 0.0;
  bellGroup.add(bellDome, bellBase);
  bellGroup.position.set(0.6, 1.735, 0.26);
  group.add(bellGroup);

  const bellWaves = [];
  const waveMat = new THREE.MeshBasicMaterial({
    color: 0xffd89c,
    transparent: true,
    opacity: 0,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    side: THREE.DoubleSide,
    toneMapped: false,
  });
  for (let i = 0; i < 3; i++) {
    const ring = new THREE.Mesh(new THREE.TorusGeometry(0.09, 0.004, 4, 28), waveMat.clone());
    ring.rotation.x = Math.PI / 2;
    ring.position.copy(bellGroup.position);
    ring.visible = false;
    group.add(ring);
    bellWaves.push({ mesh: ring, life: 0, delay: i * 0.08 });
  }

  // Basket + fish
  const basket = makeBasket();
  basket.position.set(1.18, 1.02, 0);
  group.add(basket);
  const basketBracket = cylinderBetween(
    new THREE.Vector3(0.91, 1.03, 0),
    new THREE.Vector3(1.02, 1.05, 0),
    0.015,
    brassMat,
    5,
  );
  group.add(basketBracket);

  // Headlamp
  const lampBody = new THREE.Mesh(new THREE.CylinderGeometry(0.095, 0.075, 0.17, 12), brassMat);
  lampBody.rotation.z = Math.PI / 2;
  lampBody.position.set(0.9, 1.29, 0);
  group.add(lampBody);
  const lampLens = new THREE.Mesh(new THREE.CircleGeometry(0.08, 16), lampMat);
  lampLens.position.set(0.99, 1.29, 0);
  lampLens.rotation.y = Math.PI / 2;
  group.add(lampLens);

  const headlight = new THREE.SpotLight(0xffd39a, 0, 22, 0.42, 0.75, 1.4);
  headlight.position.set(1.02, 1.3, 0);
  headlight.target.position.set(7, 0.15, 0);
  group.add(headlight, headlight.target);
  const lampHalo = new THREE.Sprite(
    new THREE.SpriteMaterial({
      map: (() => {
        const canvas = document.createElement("canvas");
        canvas.width = 128;
        canvas.height = 128;
        const ctx = canvas.getContext("2d");
        const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
        g.addColorStop(0, "rgba(255,236,190,0.9)");
        g.addColorStop(0.3, "rgba(255,200,120,0.4)");
        g.addColorStop(1, "rgba(255,180,90,0)");
        ctx.fillStyle = g;
        ctx.fillRect(0, 0, 128, 128);
        const tex = new THREE.CanvasTexture(canvas);
        tex.colorSpace = THREE.SRGBColorSpace;
        return tex;
      })(),
      transparent: true,
      opacity: 0.2,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    }),
  );
  lampHalo.position.set(1.08, 1.29, 0);
  lampHalo.scale.setScalar(0.34);
  group.add(lampHalo);

  // Rear reflector
  const reflector = new THREE.Mesh(
    new THREE.CircleGeometry(0.035, 12),
    new THREE.MeshStandardMaterial({ color: 0xff5b36, emissive: 0xff4a22, emissiveIntensity: 0.25 }),
  );
  reflector.position.set(-1.18, 0.89, 0);
  reflector.rotation.y = -Math.PI / 2;
  group.add(reflector);

  // Drivetrain
  const chainring = new THREE.Group();
  const ring = new THREE.Mesh(new THREE.TorusGeometry(0.205, 0.017, 6, 32), brassMat);
  chainring.add(ring);
  const chainringDisc = new THREE.Mesh(new THREE.CylinderGeometry(0.195, 0.195, 0.012, 24), brassMat);
  chainringDisc.rotation.x = Math.PI / 2;
  chainring.add(chainringDisc);
  for (let i = 0; i < 18; i++) {
    const a = (i / 18) * Math.PI * 2;
    const tooth = new THREE.Mesh(new THREE.BoxGeometry(0.035, 0.022, 0.018), brassMat);
    tooth.position.set(Math.cos(a) * 0.217, Math.sin(a) * 0.217, 0);
    tooth.rotation.z = a;
    chainring.add(tooth);
  }
  chainring.position.copy(BB);
  group.add(chainring);

  const cog = new THREE.Mesh(new THREE.CylinderGeometry(0.095, 0.095, 0.02, 16), brassMat);
  cog.rotation.x = Math.PI / 2;
  cog.position.copy(REAR_HUB).add(new THREE.Vector3(0, 0, 0.05));
  group.add(cog);

  const chainPoints = [
    new THREE.Vector3(BB.x, BB.y + 0.21, 0.055),
    new THREE.Vector3(REAR_HUB.x, REAR_HUB.y + 0.1, 0.055),
    new THREE.Vector3(REAR_HUB.x - 0.08, REAR_HUB.y - 0.1, 0.055),
    new THREE.Vector3(BB.x, BB.y - 0.21, 0.055),
  ];
  group.add(
    makeTube(chainPoints, 0.009, new THREE.MeshStandardMaterial({ color: 0x2b2e2a, roughness: 0.5, metalness: 0.8 }), {
      tubular: 64,
      radial: 4,
      closed: true,
    }),
  );

  const crankArmL = new THREE.Mesh(new THREE.CylinderGeometry(0.019, 0.019, 1, 6), brassMat);
  const crankArmR = new THREE.Mesh(new THREE.CylinderGeometry(0.019, 0.019, 1, 6), brassMat);
  crankArmL.castShadow = true;
  crankArmR.castShadow = true;
  group.add(crankArmL, crankArmR);

  const pedalGeo = new THREE.BoxGeometry(0.15, 0.026, 0.1);
  const pedalMat = new THREE.MeshStandardMaterial({ color: 0x293c38, roughness: 0.7, metalness: 0.2 });
  const pedalL = new THREE.Mesh(pedalGeo, pedalMat);
  const pedalR = new THREE.Mesh(pedalGeo, pedalMat);
  pedalL.castShadow = true;
  pedalR.castShadow = true;
  group.add(pedalL, pedalR);

  const pedalPosL = new THREE.Vector3();
  const pedalPosR = new THREE.Vector3();
  let crankAngle = 0;
  let bellPulse = 0;
  let bellTime = 0;

  function updatePedals() {
    pedalPosL.set(
      BB.x + Math.cos(crankAngle) * CRANK_RADIUS,
      BB.y + Math.sin(crankAngle) * CRANK_RADIUS,
      0.28,
    );
    pedalPosR.set(
      BB.x + Math.cos(crankAngle + Math.PI) * CRANK_RADIUS,
      BB.y + Math.sin(crankAngle + Math.PI) * CRANK_RADIUS,
      -0.28,
    );
    setCylinderBetween(crankArmL, BB.clone().setZ(0.22), pedalPosL.clone().setZ(0.25));
    setCylinderBetween(crankArmR, BB.clone().setZ(-0.22), pedalPosR.clone().setZ(-0.25));
    pedalL.position.copy(pedalPosL);
    pedalR.position.copy(pedalPosR);
    pedalL.rotation.z = 0;
    pedalR.rotation.z = 0;
  }

  updatePedals();

  function update(dt, speed, distance, night) {
    const spin = distance / WHEEL_RADIUS;
    rearWheel.rotation.z = -spin;
    frontWheel.rotation.z = -spin;
    cog.rotation.z = -spin;

    crankAngle = -spin * 0.44;
    updatePedals();
    chainring.rotation.z = crankAngle;

    bellPulse = Math.max(0, bellPulse - dt);
    if (bellPulse > 0) {
      bellTime += dt;
      bellGroup.rotation.z = Math.sin(bellTime * 28) * 0.14 * bellPulse;
      for (const wave of bellWaves) {
        wave.delay -= dt;
        if (wave.delay <= 0) {
          wave.life += dt;
          const k = clamp(wave.life / 0.9);
          wave.mesh.visible = true;
          wave.mesh.scale.setScalar(0.5 + k * 6.5);
          wave.mesh.position.y = bellGroup.position.y + 0.02;
          wave.mesh.material.opacity = (1 - k) * 0.5;
        }
      }
    } else {
      bellGroup.rotation.z *= 0.9;
      for (const wave of bellWaves) {
        if (wave.life > 0.9) wave.mesh.visible = false;
      }
    }

    headlight.intensity = night * 6;
    lampMat.emissiveIntensity = 0.22 + night * 1.5;
    lampHalo.material.opacity = 0.1 + night * 0.38;
    reflector.material.emissiveIntensity = 0.22 + night * 1.2;
  }

  function ringBell() {
    bellPulse = 1;
    bellTime = 0;
    for (let i = 0; i < bellWaves.length; i++) {
      bellWaves[i].life = 0;
      bellWaves[i].delay = i * 0.09;
      bellWaves[i].mesh.visible = false;
      bellWaves[i].mesh.material.opacity = 0;
    }
  }

  return {
    group,
    update,
    ringBell,
    rearWheel,
    frontWheel,
    bellGroup,
    headlight,
    getPedals() {
      return { left: pedalPosL, right: pedalPosR };
    },
    get crankAngle() {
      return crankAngle;
    },
  };
}
