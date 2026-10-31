import * as THREE from "three";

export const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
export const lerp = (a, b, t) => a + (b - a) * t;
export const invLerp = (a, b, v) => clamp((v - a) / (b - a));
export const smoothstep = (a, b, v) => {
  const t = invLerp(a, b, v);
  return t * t * (3 - 2 * t);
};
export const damp = (current, target, lambda, dt) =>
  lerp(current, target, 1 - Math.exp(-lambda * dt));
export const expDamp = (current, target, smoothing, dt) =>
  lerp(current, target, 1 - Math.pow(smoothing, dt * 60));
export const rand = (a, b) => a + Math.random() * (b - a);
export const randInt = (a, b) => Math.floor(rand(a, b + 1));
export const mod = (n, m) => ((n % m) + m) % m;

export const UP = new THREE.Vector3(0, 1, 0);
export const FORWARD = new THREE.Vector3(1, 0, 0);

export function srgb(hex) {
  return new THREE.Color(hex);
}

export function createTaperedTube(curve, radiusAt, material, options = {}) {
  const tubular = options.tubular ?? 32;
  const radial = options.radial ?? 12;
  const frames = curve.computeFrenetFrames(tubular, false);
  const positions = [];
  const normals = [];
  const uvs = [];
  const indices = [];

  for (let i = 0; i <= tubular; i++) {
    const t = i / tubular;
    const point = curve.getPointAt(t);
    const normal = frames.normals[i];
    const binormal = frames.binormals[i];
    const radius = radiusAt(t);

    for (let j = 0; j <= radial; j++) {
      const v = (j / radial) * Math.PI * 2;
      const sin = Math.sin(v);
      const cos = -Math.cos(v);
      const nx = normal.x * cos + binormal.x * sin;
      const ny = normal.y * cos + binormal.y * sin;
      const nz = normal.z * cos + binormal.z * sin;
      positions.push(point.x + nx * radius, point.y + ny * radius, point.z + nz * radius);
      normals.push(nx, ny, nz);
      uvs.push(t * (options.uRepeat ?? 1), j / radial);
    }
  }

  for (let i = 1; i <= tubular; i++) {
    for (let j = 1; j <= radial; j++) {
      const a = (radial + 1) * (i - 1) + (j - 1);
      const b = (radial + 1) * i + (j - 1);
      const c = (radial + 1) * i + j;
      const d = (radial + 1) * (i - 1) + j;
      indices.push(a, b, d, b, c, d);
    }
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute("normal", new THREE.Float32BufferAttribute(normals, 3));
  geometry.setAttribute("uv", new THREE.Float32BufferAttribute(uvs, 2));
  geometry.setIndex(indices);
  geometry.computeBoundingSphere();

  const mesh = new THREE.Mesh(geometry, material);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

export function cylinderBetween(a, b, radius, material, radial = 8) {
  const dir = new THREE.Vector3().subVectors(b, a);
  const len = dir.length();
  const geometry = new THREE.CylinderGeometry(radius, radius, 1, radial, 1);
  const mesh = new THREE.Mesh(geometry, material);
  mesh.position.copy(a).addScaledVector(dir, 0.5);
  mesh.quaternion.setFromUnitVectors(UP, dir.normalize());
  mesh.scale.set(1, len, 1);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

export function setCylinderBetween(mesh, a, b) {
  const dir = new THREE.Vector3().subVectors(b, a);
  const len = Math.max(0.0001, dir.length());
  mesh.position.copy(a).addScaledVector(dir, 0.5);
  mesh.quaternion.setFromUnitVectors(UP, dir.normalize());
  mesh.scale.set(1, len, 1);
}

export function makeTube(points, radius, material, { tubular = 64, radial = 7, closed = false } = {}) {
  const curve = new THREE.CatmullRomCurve3(points, closed, "catmullrom", 0.5);
  const geometry = new THREE.TubeGeometry(curve, tubular, radius, radial, closed);
  const mesh = new THREE.Mesh(geometry, material);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

export function mergeMeshGroup(meshes) {
  // Small local helper: returns the meshes as-is, but keeps the call site expressive.
  return meshes;
}

export function makeCanvasTexture(width, height, draw, { repeat = null, srgb = true } = {}) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  draw(ctx, width, height);
  const texture = new THREE.CanvasTexture(canvas);
  texture.anisotropy = 4;
  if (srgb) texture.colorSpace = THREE.SRGBColorSpace;
  if (repeat) {
    texture.wrapS = THREE.RepeatWrapping;
    texture.wrapT = THREE.RepeatWrapping;
    texture.repeat.set(repeat[0], repeat[1]);
  }
  texture.needsUpdate = true;
  return texture;
}

export function createCloudTexture() {
  return makeCanvasTexture(512, 256, (ctx, w, h) => {
    ctx.clearRect(0, 0, w, h);
    const blobs = [
      [0.22, 0.62, 0.17, 0.17],
      [0.36, 0.49, 0.22, 0.23],
      [0.51, 0.58, 0.25, 0.2],
      [0.67, 0.48, 0.2, 0.21],
      [0.79, 0.6, 0.15, 0.15],
      [0.48, 0.68, 0.3, 0.13],
    ];
    for (const [x, y, rx, ry] of blobs) {
      const g = ctx.createRadialGradient(
        x * w,
        y * h,
        Math.min(rx, ry) * w * 0.05,
        x * w,
        y * h,
        Math.max(rx, ry) * w,
      );
      g.addColorStop(0, "rgba(255, 242, 220, 0.62)");
      g.addColorStop(0.45, "rgba(255, 235, 210, 0.32)");
      g.addColorStop(1, "rgba(255, 230, 205, 0)");
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.ellipse(x * w, y * h, rx * w, ry * h, 0, 0, Math.PI * 2);
      ctx.fill();
    }
  });
}

export function createGlowTexture() {
  return makeCanvasTexture(256, 256, (ctx, w, h) => {
    const g = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, w / 2);
    g.addColorStop(0, "rgba(255,246,218,1)");
    g.addColorStop(0.12, "rgba(255,224,168,0.9)");
    g.addColorStop(0.35, "rgba(255,187,108,0.3)");
    g.addColorStop(0.7, "rgba(255,168,95,0.06)");
    g.addColorStop(1, "rgba(255,150,90,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, w, h);
  });
}

export function createSignTexture() {
  return makeCanvasTexture(1024, 320, (ctx, w, h) => {
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#e8d9be";
    ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(8,47,52,0.45)";
    ctx.lineWidth = 6;
    ctx.strokeRect(18, 18, w - 36, h - 36);
    ctx.fillStyle = "#0a3d42";
    ctx.textAlign = "center";
    ctx.font = "500 92px Georgia, serif";
    ctx.fillText("PELICAN POINT", w / 2, 140);
    ctx.font = "34px Consolas, monospace";
    ctx.letterSpacing = "8px";
    ctx.fillText("CAUSEWAY LOOP · 1.2 KM", w / 2, 210);
    ctx.fillStyle = "#bf5b37";
    ctx.fillRect(w / 2 - 92, 236, 184, 4);
    ctx.font = "26px Consolas, monospace";
    ctx.fillStyle = "#0a3d42";
    ctx.fillText("ONE BIRD · ONE BICYCLE", w / 2, 282);
  });
}

export function createEnvTexture() {
  return makeCanvasTexture(1024, 512, (ctx, w, h) => {
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, "#0d4c57");
    g.addColorStop(0.42, "#8aa8a0");
    g.addColorStop(0.56, "#f0bf8e");
    g.addColorStop(0.6, "#d58e63");
    g.addColorStop(0.63, "#0a3138");
    g.addColorStop(1, "#02181c");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, w, h);

    const sun = ctx.createRadialGradient(w * 0.28, h * 0.55, 0, w * 0.28, h * 0.55, w * 0.18);
    sun.addColorStop(0, "rgba(255,250,226,1)");
    sun.addColorStop(0.08, "rgba(255,220,160,0.95)");
    sun.addColorStop(0.35, "rgba(255,175,100,0.32)");
    sun.addColorStop(1, "rgba(255,160,90,0)");
    ctx.fillStyle = sun;
    ctx.fillRect(0, 0, w, h);
  });
}

export function createNoiseTexture(size = 256) {
  return makeCanvasTexture(size, size, (ctx, w, h) => {
    const image = ctx.createImageData(w, h);
    for (let i = 0; i < image.data.length; i += 4) {
      const n = Math.random() * 255;
      image.data[i] = n;
      image.data[i + 1] = n;
      image.data[i + 2] = n;
      image.data[i + 3] = 255;
    }
    ctx.putImageData(image, 0, 0);
  });
}

export function disposeObject(object) {
  object.traverse((child) => {
    if (child.geometry) child.geometry.dispose();
    if (child.material) {
      const mats = Array.isArray(child.material) ? child.material : [child.material];
      for (const mat of mats) {
        for (const key of Object.keys(mat)) {
          const value = mat[key];
          if (value && value.isTexture) value.dispose();
        }
        mat.dispose();
      }
    }
  });
}
