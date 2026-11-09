import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// Loaded on approach, entirely from the local site. No business API is involved.
export async function createSculptureViewer(host, onInteraction) {
  const scene = new THREE.Scene();
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.65));
  renderer.setClearColor(0xf7f7f3, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.shadowMap.autoUpdate = false;
  renderer.shadowMap.needsUpdate = true;
  const canvas = renderer.domElement;
  canvas.tabIndex = 0;
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', '白色思考者三维模型。拖动或用方向键旋转，按加减键缩放，按 Home 恢复全貌。');
  host.append(canvas);
  const camera = new THREE.PerspectiveCamera(34, 1, .05, 40);
  const controls = new OrbitControls(camera, canvas);
  controls.enablePan = false;
  controls.enableDamping = false;
  controls.enableZoom = false; // Keep wheel and one-finger vertical scroll on the page.
  controls.minPolarAngle = .43;
  controls.maxPolarAngle = 1.72;
  controls.touches.ONE = THREE.TOUCH.ROTATE;
  controls.touches.TWO = THREE.TOUCH.DOLLY_ROTATE;
  controls.addEventListener('start', onInteraction);
  scene.add(new THREE.HemisphereLight(0xffffff, 0xb9b8a9, 1.25));
  const key = new THREE.DirectionalLight(0xfffaf2, 3.2);
  key.position.set(-3.5, 6, 5);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.camera.left = key.shadow.camera.bottom = -2.5;
  key.shadow.camera.right = key.shadow.camera.top = 2.5;
  key.shadow.camera.near = .1;
  key.shadow.camera.far = 16;
  key.shadow.normalBias = .008;
  key.shadow.bias = -.0001;
  key.shadow.radius = 4;
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, .85);
  fill.position.set(4, 2, 1); scene.add(fill);
  const rim = new THREE.DirectionalLight(0xffffff, 1.7);
  rim.position.set(1, 4, -4); scene.add(rim);
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.ShadowMaterial({ color: 0x646754, opacity: .15 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -.004;
  ground.receiveShadow = true;
  scene.add(ground);
  const draco = new DRACOLoader().setDecoderPath('/vendor/draco/').setWorkerLimit(1);
  const loader = new GLTFLoader().setDRACOLoader(draco);
  let object, broken = false, lastTime = 0, rotating = false, frame = 0, visible = true;
  const material = new THREE.MeshStandardMaterial({ color: 0xfaf9f5, roughness: .84, metalness: 0 });
  function render() {
    if (!broken && object && visible && !document.hidden) renderer.render(scene, camera);
  }
  function frameLoop(time) {
    frame = 0;
    if (!rotating || broken || !visible || document.hidden) { lastTime = 0; return; }
    if (time - lastTime >= 33) {
      // Move the camera, never limbs or parts of the scanned sculpture.
      const delta = lastTime ? Math.min((time - lastTime) / 1000, .08) : 0;
      const offset = camera.position.clone().sub(controls.target);
      offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), delta * .075);
      camera.position.copy(controls.target).add(offset);
      controls.update();
      lastTime = time;
    }
    frame = requestAnimationFrame(frameLoop);
  }
  function syncLoop() {
    cancelAnimationFrame(frame); frame = 0; lastTime = 0;
    if (rotating && visible && !broken && !document.hidden) frame = requestAnimationFrame(frameLoop);
    else render();
  }
  function setView(view = 'full') {
    const narrow = host.clientWidth / Math.max(host.clientHeight, 1) < .8;
    if (view === 'detail') {
      controls.target.set(0, 1.77, .05);
      camera.position.set(1.65, 2.16, 2.45);
    } else {
      controls.target.set(0, 1.20, 0);
      camera.position.set(narrow ? 3.35 : 2.95, narrow ? 2.25 : 2.13, narrow ? 5.1 : 4.5);
    }
    camera.lookAt(controls.target); controls.update(); render();
  }
  function resize() {
    if (broken) return;
    const width = host.clientWidth, height = host.clientHeight;
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height; camera.updateProjectionMatrix();
    render();
  }
  const sizeObserver = new ResizeObserver(resize);
  sizeObserver.observe(host);
  controls.addEventListener('change', render);
  canvas.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '=', '-', 'Home'].includes(event.key)) return;
    event.preventDefault(); onInteraction();
    if (event.key === 'Home') { setView(); host.dispatchEvent(new CustomEvent('sculpture:view', { detail: 'full', bubbles: true })); return; }
    const offset = camera.position.clone().sub(controls.target);
    const spherical = new THREE.Spherical().setFromVector3(offset);
    if (event.key === 'ArrowLeft') spherical.theta -= .12;
    if (event.key === 'ArrowRight') spherical.theta += .12;
    if (event.key === 'ArrowUp') spherical.phi = Math.max(.43, spherical.phi - .1);
    if (event.key === 'ArrowDown') spherical.phi = Math.min(1.72, spherical.phi + .1);
    if (['+', '='].includes(event.key)) spherical.radius = Math.max(2.3, spherical.radius * .9);
    if (event.key === '-') spherical.radius = Math.min(8, spherical.radius * 1.1);
    camera.position.copy(controls.target).add(new THREE.Vector3().setFromSpherical(spherical));
    controls.update(); render();
  });
  canvas.addEventListener('webglcontextlost', event => {
    event.preventDefault(); broken = true; cancelAnimationFrame(frame);
    host.dispatchEvent(new CustomEvent('sculpture:error', { bubbles: true }));
  });
  try {
    const gltf = await loader.loadAsync('/models/thinker.glb');
    object = gltf.scene;
    object.traverse(child => {
      if (!child.isMesh) return;
      child.material?.dispose(); child.material = material;
      child.castShadow = true; child.receiveShadow = true;
    });
    scene.add(object); resize(); setView(); render();
  } catch (error) {
    sizeObserver.disconnect(); controls.dispose(); renderer.dispose(); canvas.remove(); material.dispose();
    throw error;
  } finally { draco.dispose(); }
  return {
    setView,
    setPlayback(playing, inView = true) { rotating = playing; visible = inView; syncLoop(); },
    dispose() { broken = true; cancelAnimationFrame(frame); sizeObserver.disconnect(); controls.dispose(); renderer.dispose(); material.dispose(); scene.traverse(child => child.geometry?.dispose()); canvas.remove(); },
  };
}
