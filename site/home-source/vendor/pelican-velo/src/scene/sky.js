import * as THREE from "three";

const vertexShader = /* glsl */ `
  varying vec3 vDir;
  varying vec3 vWorldPos;

  void main() {
    vDir = normalize(position);
    vec4 world = modelMatrix * vec4(position, 1.0);
    vWorldPos = world.xyz;
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;

const fragmentShader = /* glsl */ `
  precision highp float;

  varying vec3 vDir;
  varying vec3 vWorldPos;

  uniform float uTime;
  uniform float uNight;
  uniform vec3 uSunDir;
  uniform vec3 uMoonDir;
  uniform vec3 uTop;
  uniform vec3 uMid;
  uniform vec3 uHorizon;
  uniform vec3 uSunColor;
  uniform vec3 uCloudColor;
  uniform vec3 uDeep;

  float hash21(vec2 p) {
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
  }

  float hash31(vec3 p) {
    p = fract(p * 0.1031);
    p += dot(p, p.yzx + 33.33);
    return fract((p.x + p.y) * p.z);
  }

  float noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    float a = hash21(i);
    float b = hash21(i + vec2(1.0, 0.0));
    float c = hash21(i + vec2(0.0, 1.0));
    float d = hash21(i + vec2(1.0, 1.0));
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
  }

  float fbm(vec2 p) {
    float value = 0.0;
    float amplitude = 0.5;
    mat2 rot = mat2(0.8, 0.6, -0.6, 0.8);
    for (int i = 0; i < 5; i++) {
      value += amplitude * noise(p);
      p = rot * p * 2.02 + 11.3;
      amplitude *= 0.5;
    }
    return value;
  }

  vec3 stars(vec3 dir) {
    vec3 p = dir * 380.0;
    vec3 cell = floor(p);
    float rnd = hash31(cell);
    float star = smoothstep(0.9975, 0.9998, rnd);
    float twinkle = 0.55 + 0.45 * sin(uTime * (1.4 + rnd * 3.0) + rnd * 80.0);
    vec3 tint = mix(vec3(0.72, 0.84, 1.0), vec3(1.0, 0.86, 0.66), hash31(cell + 4.2));
    return tint * star * twinkle * smoothstep(0.03, 0.28, dir.y);
  }

  void main() {
    vec3 d = normalize(vDir);
    float h = clamp(d.y, 0.0, 1.0);

    float midBand = smoothstep(0.0, 0.15, h);
    float topBand = smoothstep(0.08, 0.5, h);
    vec3 col = mix(uHorizon, uMid, midBand);
    col = mix(col, uTop, topBand);
    col = mix(col, uHorizon * 0.62, smoothstep(0.55, 0.0, h) * 0.22);

    // Sun halo and disc
    float sunDot = max(dot(d, normalize(uSunDir)), 0.0);
    float sunHalo = pow(sunDot, 7.0);
    float sunDisk = smoothstep(0.99945, 0.99982, sunDot);
    col += uSunColor * sunHalo * 0.46;
    col += uSunColor * sunDisk * 3.2;
    col += uSunColor * pow(sunDot, 80.0) * 0.8;

    // Moon
    float moonDot = max(dot(d, normalize(uMoonDir)), 0.0);
    float moonDisk = smoothstep(0.99945, 0.99980, moonDot);
    float moonHalo = pow(moonDot, 38.0);
    vec3 moonColor = vec3(0.72, 0.82, 1.0);
    col += moonColor * moonDisk * 1.6 * uNight;
    col += moonColor * moonHalo * 0.12 * uNight;

    // Stars
    col += stars(d) * (0.015 + uNight * 1.6);

    // Cloud bands, projected onto a plane toward the horizon
    float horizonMask = smoothstep(0.015, 0.10, d.y) * (1.0 - smoothstep(0.30, 0.72, d.y));
    vec2 cuv = d.xz / max(d.y + 0.12, 0.10) * 0.72;
    cuv.x += uTime * 0.004;
    float cloudNoise = fbm(cuv * 0.65 + vec2(uTime * 0.002, 0.0));
    cloudNoise += 0.38 * fbm(cuv * 2.2 - vec2(uTime * 0.004, 0.0));
    float cloud = smoothstep(0.58, 0.88, cloudNoise) * horizonMask;
    cloud *= 0.72 + 0.28 * smoothstep(0.15, 0.42, d.y);
    vec3 cloudTint = mix(uCloudColor, uHorizon, 0.35);
    col = mix(col, cloudTint, cloud * (0.34 + 0.18 * (1.0 - uNight)));

    // Thin atmospheric band at the horizon
    float band = exp(-abs(d.y - 0.016) * 52.0);
    col += uSunColor * band * 0.10;

    // Below horizon, let the sky fall away into deep water colour
    float below = smoothstep(0.0, -0.16, d.y);
    col = mix(col, uDeep, below * 0.94);

    // Subtle dithering to avoid banding
    float dither = (hash21(gl_FragCoord.xy * 0.7) - 0.5) / 255.0;
    col += dither;

    gl_FragColor = vec4(col, 1.0);
  }
`;

export function makeSky() {
  const uniforms = {
    uTime: { value: 0 },
    uNight: { value: 0 },
    uSunDir: { value: new THREE.Vector3(-0.6, 0.18, -0.78).normalize() },
    uMoonDir: { value: new THREE.Vector3(0.52, 0.48, -0.7).normalize() },
    uTop: { value: new THREE.Color(0x1b5a70) },
    uMid: { value: new THREE.Color(0xd97a72) },
    uHorizon: { value: new THREE.Color(0xffa15c) },
    uSunColor: { value: new THREE.Color(0xffd39a) },
    uCloudColor: { value: new THREE.Color(0xffd7b0) },
    uDeep: { value: new THREE.Color(0x082f39) },
  };

  const material = new THREE.ShaderMaterial({
    uniforms,
    vertexShader,
    fragmentShader,
    side: THREE.BackSide,
    depthWrite: false,
    fog: false,
  });

  const geometry = new THREE.SphereGeometry(390, 64, 32);
  const mesh = new THREE.Mesh(geometry, material);
  mesh.frustumCulled = false;
  mesh.renderOrder = -1000;
  mesh.name = "sky";
  return { mesh, uniforms };
}
