import * as THREE from 'three';
import {RoundedBoxGeometry} from 'three/addons/geometries/RoundedBoxGeometry.js';
import {mergeGeometries} from 'three/addons/utils/BufferGeometryUtils.js';
import {buildBikiniNeighborhood} from './bikini-neighborhood.js';
import {buildSpongeCyclist} from './sponge-cyclist.js';

const Y = new THREE.Vector3(0, 1, 0);
const vec = a => new THREE.Vector3(...a);

// User-requested SpongeBob fan art; no third-party character mesh or textures.
export function createCoastalModel() {
  const group = new THREE.Group(), materials = {};
  group.name = 'Bikini Bottom - Conch Street and seaside cycling fan art';
  for (const [name, color] of Object.entries({
    sponge: 0xf8cf38, pore: 0xc09b27, poreEdge: 0xe8bc2d, cheek: 0xe9a65a,
    white: 0xfff9e8, iris: 0x62b6cd, pupil: 0x172a36, smile: 0x996337,
    shorts: 0x97704e, sole: 0x3b3a35, tie: 0xc65143, belt: 0x4f4336,
    frame: 0x579b92, metal: 0xb7c5bd, tire: 0x334c4a, rim: 0xeee5cf,
    sand: 0xe9dbb6, path: 0xe4d2ad, grass: 0x849c83, rock: 0xaba99a,
    foam: 0xe3ede0,
    basket: 0xbb9264, basketLight: 0xd4b58a, petal: 0xdcb0c5,
    flowerWhite: 0xfff5df, flowerCoral: 0xe7a18d, flowerCenter: 0xe8bd54,
  })) materials[name] = new THREE.MeshStandardMaterial({
    color, roughness: name === 'iris' ? .3 : .76, metalness: name === 'metal' ? .65 : 0,
  });
  function mesh(geometry, material, position, parent = group, name = '') {
    const item = new THREE.Mesh(geometry, materials[material] || material);
    if (position) item.position.set(...position);
    item.castShadow = item.receiveShadow = true; item.name = name; parent.add(item); return item;
  }
  function egg(position, scale, material, parent, name) {
    const item = mesh(new THREE.SphereGeometry(1, 14, 9), material, position, parent, name);
    item.scale.set(...scale); return item;
  }
  function box(position, size, radius, material, parent, name) {
    const geometry = Math.max(...size) < .14 || radius <= .02 ? new THREE.BoxGeometry(...size)
      : new RoundedBoxGeometry(...size, radius > .08 ? 2 : 1, radius);
    return mesh(geometry, material, position, parent, name);
  }
  function connect(item, a, b) {
    item.position.copy(a).add(b).multiplyScalar(.5); item.scale.y = a.distanceTo(b);
    item.quaternion.setFromUnitVectors(Y, b.clone().sub(a).normalize());
  }
  function link(a, b, radius, material, parent = group, name = '') {
    const item = mesh(new THREE.CylinderGeometry(radius, radius, 1, 10), material, null, parent, name);
    connect(item, vec(a), vec(b)); return item;
  }
  function curve(points, radius, material, parent = group, name = '') {
    return mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(points.map(vec)), Math.max(12, Math.min(64, points.length * 2)), radius, 5, false), material, null, parent, name);
  }


  // Batch immutable detail by material; animated groups keep independent pivots.
  function mergeStatic(root, {recursive = true} = {}) {
    root.updateWorldMatrix(true, true);
    const inverse = root.matrixWorld.clone().invert(), buckets = new Map(), originals = [];
    const collect = item => {
      if (!item.isMesh || item.isInstancedMesh) return;
      const geometry = item.geometry.index ? item.geometry.toNonIndexed() : item.geometry.clone();
      for (const name of Object.keys(geometry.attributes)) if (!['position', 'normal'].includes(name)) geometry.deleteAttribute(name);
      geometry.applyMatrix4(inverse.clone().multiply(item.matrixWorld));
      const key = item.material.uuid + ':' + item.castShadow + ':' + item.receiveShadow;
      if (!buckets.has(key)) buckets.set(key, {material: item.material, castShadow: item.castShadow, receiveShadow: item.receiveShadow, geometries: []});
      buckets.get(key).geometries.push(geometry); originals.push(item);
    };
    if (recursive) root.traverse(collect); else [...root.children].forEach(collect);
    const disposed = new Set();
    for (const item of originals) {
      if (item.name) {
        const anchor = new THREE.Object3D(); anchor.name = item.name;
        anchor.position.copy(item.position); anchor.quaternion.copy(item.quaternion); anchor.scale.copy(item.scale);
        item.geometry.computeBoundingBox();
        anchor.userData.localBounds = {min:item.geometry.boundingBox.min.toArray(),max:item.geometry.boundingBox.max.toArray()};
        item.parent.add(anchor);
      }
      item.removeFromParent();
      if (!disposed.has(item.geometry)) {item.geometry.dispose(); disposed.add(item.geometry);}
    }
    for (const {material, geometries, castShadow, receiveShadow} of buckets.values()) {
      const geometry = mergeGeometries(geometries, false);
      geometries.forEach(g => g.dispose());
      const item = mesh(geometry, material, null, root, 'Batched static detail');
      item.castShadow = castShadow; item.receiveShadow = receiveShadow;
    }
  }

  const terrain = new THREE.Group(); terrain.name = 'Continuous beach, no display plinth'; group.add(terrain);
  const shoreline = x => -28.35 + Math.sin(x * .20) * .26 + Math.sin(x * .71) * .045;
  const sandGeometry = new THREE.PlaneGeometry(120, 64, 80, 12);
  const sandPosition = sandGeometry.attributes.position;
  for (let i = 0; i < sandPosition.count; i++) {
    const x = sandPosition.getX(i), along = (sandPosition.getY(i) + 32) / 64;
    sandPosition.setXYZ(i, x, .012 + Math.min(1, along * 32) * .064, shoreline(x) + along * 64);
  }
  // Remapping the original XY plane to the shore reverses its winding.
  const index = sandGeometry.index;
  for (let i = 0; i < index.count; i += 3) {
    const b = index.getX(i + 1); index.setX(i + 1, index.getX(i + 2)); index.setX(i + 2, b);
  }
  sandGeometry.computeVertexNormals();
  mesh(sandGeometry, 'sand', [0, 0, 0], terrain, 'Unbroken sandy shore').castShadow = false;
  const road = mesh(new THREE.PlaneGeometry(120, 1.35), 'path', [0, .083, .55], terrain, 'Coastal cycle path');
  road.rotation.x = -Math.PI / 2; road.castShadow = false;

  const oceanMaterial = new THREE.ShaderMaterial({
    uniforms: {time: {value: 0}, deep: {value: new THREE.Color(0x3c8995)},
      shallow: {value: new THREE.Color(0x9bc6bb)}, haze: {value: new THREE.Color(0xb3d9db)}},
    vertexShader: `varying vec3 world; uniform float time;
      void main(){vec3 p=position; p.z+=sin(p.x*.65+time*.65)*.008+sin(p.y*1.5+time*.9)*.006;
      world=(modelMatrix*vec4(p,1.)).xyz; gl_Position=projectionMatrix*viewMatrix*vec4(world,1.);}`,
    fragmentShader: `varying vec3 world;uniform float time;uniform vec3 deep;uniform vec3 shallow;uniform vec3 haze;
      void main(){float distanceToShore=clamp((-world.z-28.)/24.,0.,1.);
      float ripples=sin(world.x*2.7+world.z*5.3+time*.7)*sin(world.z*10.-time*.9);
      vec3 water=mix(shallow,deep,distanceToShore*.76)+ripples*.018;
      float sunLine=exp(-pow((world.x-world.z*.40+1.)/(.40-world.z*.10),2.));
      float sparkle=pow(max(0.,sin(world.z*17.+time*.65)*sin(world.x*12.+world.z*3.)),7.);
      water+=vec3(.32,.42,.35)*sunLine*(.05+sparkle*.7);
      float fog=1.-exp(-pow(length(world-cameraPosition)*.009,2.));
      gl_FragColor=vec4(mix(water,haze,fog),1.);
      #include <tonemapping_fragment>
      #include <colorspace_fragment>
      }`,
  });
  const ocean = mesh(new THREE.PlaneGeometry(240, 240, 30, 30), oceanMaterial, [0, -.008, 0], terrain, 'Animated open sea');
  ocean.rotation.x = -Math.PI / 2; ocean.castShadow = ocean.receiveShadow = false;
  const waves = [];
  for (let i = 0; i < 18; i++) {
    const x = -14 + i * 1.7;
    const wave = curve([0, .45, .95, 1.35].map(dx => [x + dx, .017, shoreline(x + dx) - .08]), .012, 'foam', terrain);
    wave.castShadow = false; waves.push(wave);
  }
  const reflections = [];
  const reflectionMaterial = new THREE.MeshBasicMaterial({color: 0xffdeb0, transparent: true, opacity: .26, depthWrite: false});
  for (let i = 0; i < 24; i++) {
    const z = -29 - i * .74;
    const glint = mesh(new THREE.PlaneGeometry(1, 1), reflectionMaterial,
      [z * .40 - 1, .011, z], terrain, 'Broken sunset glint');
    glint.rotation.x = -Math.PI / 2; glint.scale.set(.13 + i * .026, .018 + i * .002, 1);
    glint.castShadow = false; glint.userData.baseWidth = glint.scale.x; reflections.push(glint);
  }
  for (let i = 0; i < 12; i++) {
    const x = -6.2 + i * 1.14, z = 5.3 + Math.sin(i * 2.4) * .35;
    egg([x, .12, z], [.11 + i % 3 * .04, .08, .09], 'rock', terrain);
    if (i % 3 === 0) for (let k = 0; k < 3; k++) {
      const blade = mesh(new THREE.ConeGeometry(.02, .22 + k * .04, 5), 'grass', [x + .2 + k * .045, .20, z], terrain);
      blade.rotation.z = (k - 1) * .23;
    }
  }

  const helpers = {group, materials, mesh, egg, box, link, curve, mergeStatic};
  const neighborhood = buildBikiniNeighborhood(helpers);
  const cyclist = buildSpongeCyclist(helpers);
  const {rider, wheels, legs} = cyclist;
  function update(time) {
    cyclist.update(time); neighborhood.update(time);
    oceanMaterial.uniforms.time.value = time;
    waves.forEach((wave, i) => { wave.position.z = Math.sin(time * .60 + i * .25) * .045; });
    reflections.forEach((glint, i) => { glint.scale.x = glint.userData.baseWidth * (1 + Math.sin(time * .9 + i) * .24); });
  }
  update(0);
  function dispose() {
    const geometries = new Set(), mats = new Set();
    group.traverse(item => {
      if (item.geometry) geometries.add(item.geometry); if (item.material) mats.add(item.material);
      if (item.isInstancedMesh) item.dispose();
    });
    geometries.forEach(g => g.dispose()); mats.forEach(m => m.dispose());
  }
  return {group, update, dispose, rider, wheels, legs, waves, reflections, materials, road, ocean, shoreline, cyclist, neighborhood};
}

export function createCoastalSky() {
  const material = new THREE.ShaderMaterial({side: THREE.BackSide, depthWrite: false, toneMapped: false,
    uniforms: {sunDirection: {value: new THREE.Vector3(-.40, .046, -1).normalize()},
      zenith: {value: new THREE.Color(0x39aaca)}, horizon: {value: new THREE.Color(0x91d3d0)}},
    vertexShader: `varying vec3 ray;void main(){ray=position;vec4 p=projectionMatrix*mat4(mat3(viewMatrix))*vec4(position,1.);gl_Position=p.xyww;}`,
    fragmentShader: `varying vec3 ray;uniform vec3 sunDirection;uniform vec3 zenith;uniform vec3 horizon;
      void main(){vec3 direction=normalize(ray);float elevation=max(0.,direction.y);
      vec3 sky=mix(horizon,zenith,smoothstep(0.,.58,elevation));
      float distanceToSun=length(direction-sunDirection);
      float glow=exp(-distanceToSun*8.5)*.24;
      float veil=sin(direction.x*16.+direction.y*24.)*sin(direction.x*4.-direction.y*39.);
      sky+=vec3(.10,.055,.035)*veil*.12*exp(-pow((direction.y-.12)*8.,2.));
      sky=mix(sky,vec3(.93,.98,.83),glow);
      gl_FragColor=vec4(sky,1.);
      #include <colorspace_fragment>
      }`,
  });
  const sky = new THREE.Mesh(new THREE.SphereGeometry(1, 32, 20), material);
  sky.name = 'Aquatic cartoon atmosphere with soft distant sunlight';
  sky.frustumCulled = false; sky.renderOrder = -1;
  return {sky, dispose() {sky.geometry.dispose(); material.dispose();}};
}
