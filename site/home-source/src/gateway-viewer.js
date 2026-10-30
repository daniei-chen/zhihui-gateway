import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { createGatewayModel } from './gateway-model.js';

export function createGatewayViewer(host) {
  const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: 'low-power' });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.6));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.1;
  renderer.setClearColor(0xedf5ff, 0);
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.shadowMap.autoUpdate = false; renderer.shadowMap.needsUpdate = true;
  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer), room = new RoomEnvironment();
  const environment = pmrem.fromScene(room, .04);
  scene.environment = environment.texture; scene.environmentIntensity = .6; room.dispose(); pmrem.dispose();
  scene.add(new THREE.HemisphereLight(0xffffff, 0xc4d5ef, 1.5));
  const key = new THREE.DirectionalLight(0xffffff, 3.1); key.position.set(-3.8,6.5,4.5); key.castShadow = true;
  key.shadow.mapSize.set(1024,1024); key.shadow.camera.left = key.shadow.camera.bottom = -4;
  key.shadow.camera.right = key.shadow.camera.top = 4; key.shadow.camera.far = 20; key.shadow.normalBias = .025;
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xb8d5ff, 1.3); fill.position.set(4,3,-2); scene.add(fill);
  const model = createGatewayModel(); scene.add(model.group);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(200,200), new THREE.ShadowMaterial({ color: 0x6e8eae, opacity: .12 }));
  floor.rotation.x = -Math.PI/2; floor.receiveShadow = true; scene.add(floor);
  const camera = new THREE.PerspectiveCamera(34,1,.1,60);
  camera.position.set(4.2,3.0,7.8); camera.lookAt(0,1.6,0);
  host.append(renderer.domElement);
  let raf = 0, last = 0, elapsed = 0, playing = false, active = true, state = 'idle', disposed = false;
  const render = () => { if (!disposed && active && !document.hidden) renderer.render(scene,camera); };
  function draw(time) {
    raf = 0;
    if (!playing || !active || disposed || document.hidden) { last = 0; return; }
    if (time-last >= 33) {
      elapsed += last ? Math.min((time-last)/1000,.08) : 0;
      const quiet = state === 'focused', speed = state === 'submitting' ? .23 : .08;
      model.mark.position.y = 1.88 + Math.sin(elapsed*.9)*(quiet ? .008 : .035);
      model.blue.emissiveIntensity = state === 'success' ? .35 : state === 'submitting' ? .18 : .08;
      model.particles.forEach((particle,i) => { particle.position.copy(model.lanes[i].getPoint((elapsed*speed+i*.25)%1)); particle.visible = !quiet; });
      render(); last = time;
    }
    raf = requestAnimationFrame(draw);
  }
  function sync() {
    cancelAnimationFrame(raf); raf = 0; last = 0;
    if (playing && active && !disposed && !document.hidden) raf = requestAnimationFrame(draw);
    else render();
  }
  function resize() {
    if (!host.clientWidth || !host.clientHeight || disposed) return;
    renderer.setSize(host.clientWidth,host.clientHeight,false); camera.aspect=host.clientWidth/host.clientHeight;
    camera.position.set(4.2,3.0,camera.aspect < .8 ? 9 : 7.8); camera.lookAt(0,1.6,0); camera.updateProjectionMatrix(); render();
  }
  const observer = new ResizeObserver(resize); observer.observe(host); resize(); render();
  renderer.domElement.addEventListener('webglcontextlost', event => { event.preventDefault(); cancelAnimationFrame(raf); disposed = true; host.dispatchEvent(new CustomEvent('gateway:error',{bubbles:true})); });
  return {
    setPlayback(value, visible = true) { playing = value; active = visible; sync(); },
    setState(value) { state = value; },
    dispose() {
      disposed = true; cancelAnimationFrame(raf); observer.disconnect();
      const geometries = new Set(), materials = new Set();
      scene.traverse(child => { if (child.geometry) geometries.add(child.geometry); if (child.material) materials.add(child.material); });
      geometries.forEach(geometry => geometry.dispose()); materials.forEach(material => material.dispose());
      environment.dispose(); renderer.dispose(); renderer.domElement.remove();
    },
  };
}
