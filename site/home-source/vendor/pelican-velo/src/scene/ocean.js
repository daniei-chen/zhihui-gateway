import * as THREE from "three";

const vertexShader = /* glsl */ `
  precision highp float;

  varying vec3 vWorldPos;
  varying vec3 vNormalW;
  varying float vHeight;
  varying vec2 vGrid;

  uniform float uTime;

  void addWave(vec2 p, vec2 dir, float k, float amp, float speed, float phase, inout float height, inout vec2 grad) {
    float angle = dot(p, dir) * k + uTime * speed + phase;
    height += amp * sin(angle);
    grad += amp * k * dir * cos(angle);
  }

  void main() {
    vec3 p = position;
    vec2 xz = p.xz;
    float h = 0.0;
    vec2 grad = vec2(0.0);

    addWave(xz, normalize(vec2(0.82, 0.57)), 0.061, 0.80, 0.82, 0.0, h, grad);
    addWave(xz, normalize(vec2(-0.63, 0.78)), 0.089, 0.43, 1.07, 1.7, h, grad);
    addWave(xz, normalize(vec2(0.18, -0.98)), 0.137, 0.26, 1.31, 4.1, h, grad);
    addWave(xz, normalize(vec2(0.96, -0.28)), 0.221, 0.14, 1.79, 2.4, h, grad);
    addWave(xz, normalize(vec2(-0.87, -0.49)), 0.374, 0.072, 2.31, 5.2, h, grad);
    addWave(xz, normalize(vec2(0.46, 0.89)), 0.672, 0.038, 2.83, 3.1, h, grad);

    p.y += h;
    vHeight = h;

    vec3 world = (modelMatrix * vec4(p, 1.0)).xyz;
    vWorldPos = world;
    vNormalW = normalize(mat3(modelMatrix) * vec3(-grad.x, 1.0, -grad.y));
    vGrid = xz;

    gl_Position = projectionMatrix * viewMatrix * vec4(world, 1.0);
  }
`;

const fragmentShader = /* glsl */ `
  precision highp float;

  varying vec3 vWorldPos;
  varying vec3 vNormalW;
  varying float vHeight;
  varying vec2 vGrid;

  uniform float uTime;
  uniform float uNight;
  uniform vec3 uSunDir;
  uniform vec3 uSunColor;
  uniform vec3 uDeep;
  uniform vec3 uShallow;
  uniform vec3 uHorizon;
  uniform vec3 uFoam;

  float hash21(vec2 p) {
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
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
    float v = 0.0;
    float a = 0.5;
    for (int i = 0; i < 4; i++) {
      v += a * noise(p);
      p = p * 2.03 + 7.7;
      a *= 0.5;
    }
    return v;
  }

  void main() {
    vec3 N = normalize(vNormalW);

    // Fine wind ripples
    vec2 p = vWorldPos.xz;
    float t = uTime;
    float d1 = sin(p.x * 1.74 + p.y * 0.62 + t * 2.1) * 0.5
             + sin(p.x * -0.93 + p.y * 1.91 + t * 1.6) * 0.5;
    float d2 = sin(p.x * 2.67 - p.y * 1.12 + t * 2.8) * 0.5
             + sin(p.x * 0.41 + p.y * 3.13 - t * 2.2) * 0.5;
    N = normalize(N + vec3(d1 * 0.028, 0.0, d2 * 0.028));

    vec3 V = normalize(cameraPosition - vWorldPos);
    vec3 L = normalize(uSunDir);
    vec3 H = normalize(L + V);

    float fresnel = pow(1.0 - max(dot(N, V), 0.0), 4.0);
    float sunSpec = pow(max(dot(N, H), 0.0), 620.0);
    float sunGlint = pow(max(dot(N, H), 0.0), 70.0);

    float facing = smoothstep(-0.1, 0.8, N.y);
    vec3 water = mix(uDeep, uShallow, facing * 0.7);
    water = mix(water, uHorizon, fresnel * 0.34);

    // Long glitter path under the sun
    vec3 sunFlattened = normalize(vec3(L.x, 0.12, L.z));
    float path = pow(max(dot(normalize(vec3(V.x, 0.0, V.z)), -sunFlattened), 0.0), 4.0);
    float sparkle = smoothstep(0.72, 0.98, fbm(p * 1.7 + t * 0.15));
    water += uSunColor * (sunSpec * 1.4 + sunGlint * 0.05 + path * sparkle * 0.045);

    // Wave-crest foam
    float crest = smoothstep(1.04, 1.32, vHeight);
    float foamNoise = fbm(p * 1.9 + vec2(t * 0.08, -t * 0.05));
    float foam = crest * smoothstep(0.58, 0.92, foamNoise) * 0.12;

    // Shoreline foam around the little island
    float r = length(p);
    float shore = 1.0 - smoothstep(14.0, 21.0, r);
    float bands = sin(r * 2.1 - t * 1.4 + fbm(p * 0.5) * 4.0) * 0.5 + 0.5;
    foam += shore * smoothstep(0.55, 0.95, bands) * smoothstep(0.35, 0.9, foamNoise) * 0.22;
    float shallows = 1.0 - smoothstep(12.0, 24.0, r);
    water = mix(water, uShallow * 1.35, shallows * 0.16);

    vec3 color = mix(water, uFoam, clamp(foam, 0.0, 0.85) * (0.4 + 0.6 * (1.0 - uNight)));

    // Aerial perspective
    float dist = length(cameraPosition - vWorldPos);
    float fog = smoothstep(55.0, 340.0, dist);
    color = mix(color, uHorizon, fog * 0.94);

    // Subtle dithering
    color += (hash21(gl_FragCoord.xy * 0.7) - 0.5) / 255.0;

    gl_FragColor = vec4(color, 1.0);
  }
`;

export function makeOcean(scene) {
  const geometry = new THREE.PlaneGeometry(1000, 1000, 220, 220);
  geometry.rotateX(-Math.PI / 2);

  const uniforms = {
    uTime: { value: 0 },
    uNight: { value: 0 },
    uSunDir: { value: new THREE.Vector3(-0.6, 0.18, -0.78).normalize() },
    uSunColor: { value: new THREE.Color(0xffd39a) },
    uDeep: { value: new THREE.Color(0x04262e) },
    uShallow: { value: new THREE.Color(0x14565b) },
    uHorizon: { value: new THREE.Color(0xd9a17c) },
    uFoam: { value: new THREE.Color(0xd7e2d4) },
  };

  const material = new THREE.ShaderMaterial({
    uniforms,
    vertexShader,
    fragmentShader,
    side: THREE.FrontSide,
    fog: false,
  });

  const mesh = new THREE.Mesh(geometry, material);
  mesh.position.y = 0;
  mesh.receiveShadow = false;
  mesh.frustumCulled = false;
  mesh.name = "ocean";
  scene.add(mesh);

  return { mesh, uniforms };
}
