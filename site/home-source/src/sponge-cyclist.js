import * as THREE from 'three';
import {BIKINI_LAYOUT} from './bikini-layout.js';
import {characterEye} from './character-surface.js';

const Y = new THREE.Vector3(0, 1, 0);
const point = a => new THREE.Vector3(...a);

export function buildSpongeCyclist({group, mesh, egg, box, link, curve, mergeStatic}) {
  const cycle = new THREE.Group();
  cycle.position.set(...BIKINI_LAYOUT.cyclist.position);
  cycle.scale.setScalar(BIKINI_LAYOUT.cyclist.scale); group.add(cycle);
  cycle.name = 'Detailed mechanical bicycle and forward-facing SpongeBob';
  const wheels = [];
  for (const x of [-1, 1]) {
    const wheel = new THREE.Group(); wheel.position.set(x, .59, 0);
    wheel.name = x < 0 ? 'Rear wheel' : 'Front wheel'; cycle.add(wheel);
    mesh(new THREE.TorusGeometry(.55, .043, 10, 64), 'tire', [0, 0, 0], wheel, 'Rubber tire');
    for (const side of [-1, 1]) {
      mesh(new THREE.TorusGeometry(.541, .018, 6, 56), 'shorts', [0, 0, side * .030], wheel, 'Tan tire sidewall');
      mesh(new THREE.TorusGeometry(.507, .016, 6, 64), 'metal', [0, 0, side * .018], wheel, 'Machined brake track');
      const flange = mesh(new THREE.CylinderGeometry(.071, .071, .012, 16), 'metal', [0, 0, side * .065], wheel, 'Hub flange');
      flange.rotation.x = Math.PI / 2;
      for (let j = 0; j < 16; j++) {
        const a = j / 16 * Math.PI * 2 + (side > 0 ? .12 : 0), b = a + (j % 2 ? -.65 : .65);
        link([Math.cos(b) * .060, Math.sin(b) * .060, side * .068], [Math.cos(a) * .497, Math.sin(a) * .497, side * .015], .0038, 'metal', wheel);
        link([Math.cos(a) * .478, Math.sin(a) * .478, side * .018], [Math.cos(a) * .500, Math.sin(a) * .500, side * .018], .008, 'brass', wheel);
      }
      link([0, 0, side * .07], [0, 0, side * .126], .023, 'metal', wheel);
      link([0, 0, side * .13], [.07, .036, side * .13], .012, 'sole', wheel, 'Quick release lever');
    }
    const hub = mesh(new THREE.CylinderGeometry(.041, .041, .14, 16), 'metal', [0, 0, 0], wheel, 'Axle hub'); hub.rotation.x = Math.PI / 2;
    link([0, .47, 0], [0, .515, 0], .009, 'brass', wheel, 'Tire valve');
    for (let i = 0; i < 48; i++) {
      const a = i / 48 * Math.PI * 2;
      link([Math.cos(a - .009) * .592, Math.sin(a - .009) * .592, -.018], [Math.cos(a + .009) * .592, Math.sin(a + .009) * .592, .018], .003, 'sole', wheel);
    }
    mergeStatic(wheel); wheels.push(wheel);
  }
  const fixed = new THREE.Group(); cycle.add(fixed); fixed.name = 'Frame fork cockpit and brake assemblies';
  const crankPoint = [0, .65, 0], seat = [-.48, 1.49, 0], head = [.76, 1.51, 0];
  for (const [a, b, radius] of [[seat, crankPoint, .039], [seat, head, .034], [head, crankPoint, .043]]) link(a, b, radius, 'frame', fixed);
  for (const side of [-1, 1]) {
    link([-.96, .59, side * .08], [-.48, 1.49, side * .045], .023, 'frame', fixed, 'Paired seat stay');
    link([-.96, .59, side * .08], [0, .65, side * .052], .026, 'frame', fixed, 'Paired chain stay');
    curve([[.76, 1.47, side * .055], [.86, 1.19, side * .075], [.94, .82, side * .08], [1, .59, side * .08]], .029, 'frame', fixed, 'Twin curved fork blades');
    const dropout = mesh(new THREE.TorusGeometry(.034, .012, 6, 16), 'metal', [1, .59, side * .08], fixed, 'Fork dropout'); dropout.scale.y = 1.35;
    link([-.98, .59, side * .08], [-.93, .65, side * .08], .028, 'metal', fixed);
    // Rim brakes and cables terminate at the actual front/rear calipers.
    const x = side < 0 ? -.85 : .93, y = 1.08;
    for (const z of [-.065, .065]) {
      link([x, y + .07, z], [x + .055, y - .08, z], .014, 'metal', fixed, 'Brake caliper arm');
      box([x + .055, y - .08, z], [.06, .05, .025], .008, 'sole', fixed, 'Rim brake pad');
    }
  }
  for (const [a, b, radius] of [[[-.48, 1.49, 0], [-.53, 1.69, 0], .025], [[.76, 1.45, 0], [.74, 1.69, 0], .050], [[.74, 1.69, 0], [.72, 1.87, 0], .025]]) link(a, b, radius, 'metal', fixed);
  link([-.70, 1.66, -.065], [-.31, 1.66, -.065], .012, 'metal', fixed, 'Saddle rail');
  link([-.70, 1.66, .065], [-.31, 1.66, .065], .012, 'metal', fixed);
  egg([-.55, 1.72, 0], [.26, .065, .14], 'sole', fixed, 'Saddle');
  curve([[.72, 1.89, -.34], [.88, 1.95, -.23], [.88, 1.95, .23], [.72, 1.89, .34]], .023, 'metal', fixed, 'Swept handlebar');
  for (const side of [-1, 1]) {
    link([.72, 1.89, side * .24], [.72, 1.89, side * .39], .036, 'sole', fixed, 'Handlebar grip');
    link([.80, 1.90, side * .22], [.89, 1.84, side * .31], .012, 'metal', fixed, 'Brake lever');
    curve([[.81, 1.90, side * .22], [1.05, 1.66, side * .24], [.90, 1.39, side * .12], side < 0 ? [.93, 1.15, 0] : [-.85, 1.15, 0]], .007, 'sole', fixed, 'Bowden brake cable');
  }
  link([.70, 1.67, 0], [.95, 1.69, 0], .014, 'metal', fixed);
  const lamp = mesh(new THREE.CylinderGeometry(.061, .061, .08, 18), 'metal', [.98, 1.69, 0], fixed, 'Front lamp'); lamp.rotation.z = Math.PI / 2;
  const lens = mesh(new THREE.CircleGeometry(.050, 18), 'white', [1.025, 1.69, 0], fixed); lens.rotation.y = Math.PI / 2;
  box([-.16, 1.29, .044], [.23, .055, .013], .007, 'white', fixed, 'Frame badge');
  const bb = mesh(new THREE.CylinderGeometry(.063, .063, .18, 16), 'metal', crankPoint, fixed, 'Bottom bracket'); bb.rotation.x = Math.PI / 2;
  // The tapered, open basket clears the tire and grips; flowers stay below
  // the rider's eyeline. Supports connect to the actual fork and stem.
  const basket = new THREE.Group(); basket.name = 'Front wicker basket with fresh flowers'; fixed.add(basket);
  const lower = [1.28, 1.38, 0], upper = [1.30, 1.85, 0];
  box(lower, [.39, .035, .46], .015, 'basket', basket, 'Basket floor');
  for (const y of [1.40, 1.51, 1.63, 1.74, 1.85]) {
    const t = (y - 1.38) / .47, hx = .20 + .06 * t, hz = .23 + .09 * t, x = 1.28 + .02 * t;
    curve([[x-hx,y,-hz],[x+hx,y,-hz],[x+hx,y,hz],[x-hx,y,hz],[x-hx,y,-hz]], .012, 'basket', basket, 'Woven basket horizontal rail');
  }
  for (const side of [-1, 1]) {
    for (let i = -3; i <= 3; i++) link([lower[0]+side*.20,lower[1],i*.075],[upper[0]+side*.26,upper[1],i*.105],.009,'basketLight',basket,'Basket upright weave');
    for (let i = -2; i <= 2; i++) link([lower[0]+i*.09,lower[1],side*.23],[upper[0]+i*.12,upper[1],side*.32],.009,'basketLight',basket);
    curve([[.78,1.75,side*.11],[1.02,1.70,side*.15],[1.05,1.57,side*.23]],.012,'metal',basket,'Basket stem bracket');
    link([1,.63,side*.10],[1.29,1.38,side*.21],.011,'metal',basket,'Basket fork stay');
  }
  for (const [x,y,z,color] of [[1.15,2.03,-.13,'petal'],[1.37,2.10,.03,'flowerWhite'],[1.47,1.98,.17,'flowerCoral'],[1.19,1.99,.18,'flowerWhite'],[1.40,2.02,-.17,'petal']]) {
    curve([[1.29,1.45,0],[(x+1.29)/2,1.80,z/2],[x,y,z]],.009,'leaf',basket,'Fresh flower stem');
    const bloom=new THREE.Group();bloom.position.set(x,y,z);bloom.rotation.set(-.35,0,.20);basket.add(bloom);
    for(let i=0;i<6;i++){const a=i*Math.PI/3;const petal=egg([Math.cos(a)*.052,Math.sin(a)*.052,0],[.045,.033,.018],color,bloom,'Flower petal');petal.rotation.z=a;}
    egg([0,0,.018],[.023,.023,.018],'flowerCenter',bloom,'Flower center');
    const leaf=egg([(x+1.29)/2,1.78,z/2+.035],[.075,.020,.033],'leaf',basket,'Bouquet leaf');leaf.rotation.z=x<1.3?.6:-.6;
  }
  mergeStatic(fixed);

  function sprocket(radius, teeth, z, parent, name) {
    const shape = new THREE.Shape();
    for (let i = 0; i < teeth * 4; i++) {
      const a = i / (teeth * 4) * Math.PI * 2, r = radius * ([.96, 1, 1, .91][i % 4]);
      if (i === 0) shape.moveTo(Math.cos(a) * r, Math.sin(a) * r); else shape.lineTo(Math.cos(a) * r, Math.sin(a) * r);
    }
    shape.closePath();
    const hole = new THREE.Path(); hole.absarc(0, 0, radius * .65, 0, Math.PI * 2, true); shape.holes.push(hole);
    const item = mesh(new THREE.ExtrudeGeometry(shape, {depth: .012, bevelEnabled: false}), 'metal', [0, 0, z], parent, name);
    for (let i = 0; i < 5; i++) {const a = i * Math.PI * .4; link([0, 0, z + .006], [Math.cos(a) * radius * .78, Math.sin(a) * radius * .78, z + .006], .012, 'metal', parent);}
    return item;
  }
  const crank = new THREE.Group(); crank.position.set(...crankPoint); cycle.add(crank); crank.name = '42 tooth chainring and opposed cranks';
  sprocket(.17, 42, .164, crank, '42 tooth driving chainring');
  for (const side of [-1, 1]) link([0, 0, side * .13], [0, side * .21, side * .18], .020, 'metal', crank, 'Opposed crank arm');
  mergeStatic(crank);
  const cassette = new THREE.Group(); cassette.position.set(-1, .59, .08); cycle.add(cassette); cassette.name = 'Stepped rear cassette';
  for (let i = 0; i < 5; i++) sprocket(.17 * (30 - i * 3) / 42, 30 - i * 3, .012 + i * .018, cassette, 'Rear sprocket');
  mergeStatic(cassette);

  // External tangents keep the two straight chain runs taut between unequal gears.
  const front = new THREE.Vector2(0, .65), rear = new THREE.Vector2(-1, .59), R = .17, r = R * 18 / 42;
  const d = rear.clone().sub(front), axis = Math.atan2(d.y, d.x), alpha = Math.acos((R - r) / d.length());
  const top = axis - alpha, bottom = axis + alpha;
  const chainPoints = [];
  for (let i = 0; i <= 24; i++) {const a = top + (bottom - Math.PI * 2 - top) * i / 24; chainPoints.push(new THREE.Vector3(Math.cos(a) * R, .65 + Math.sin(a) * R, .17));}
  for (let i = 0; i <= 16; i++) {const a = bottom + (top - bottom) * i / 16; chainPoints.push(new THREE.Vector3(-1 + Math.cos(a) * r, .59 + Math.sin(a) * r, .17));}
  const chainPath = new THREE.CurvePath();
  chainPoints.forEach((p, i) => chainPath.add(new THREE.LineCurve3(p, chainPoints[(i + 1) % chainPoints.length])));
  const chain = new THREE.InstancedMesh(new THREE.BoxGeometry(.035, .015, .022), crank.children[0].material, 76);
  chain.name = '76 animated chain links on closed drivetrain'; chain.castShadow = true; cycle.add(chain);
  const dummy = new THREE.Object3D(), chainLength = chainPath.getLength();
  const pedals = [-1, 1].map(side => {
    const pedal = new THREE.Group(); pedal.name = 'Level platform pedal'; cycle.add(pedal);
    box([0, 0, 0], [.20, .035, .16], .008, 'sole', pedal);
    for (const z of [-.082, .082]) box([0, 0, z], [.12, .022, .009], .003, 'brass', pedal, 'Pedal reflector');
    mergeStatic(pedal); return pedal;
  });

  const rider = new THREE.Group(); rider.name = 'SpongeBob - facing bicycle +X, articulated riding pose';
  rider.position.set(-.42, 0, 0); cycle.add(rider);
  const torso = new THREE.Group(); torso.rotation.y = Math.PI / 2; rider.add(torso);
  torso.name = 'Forward-facing torso'; torso.userData.forward = [0, 0, 1];
  const body=box([0, 2.67, 0], [1.02, 1.04, .68], .10, 'sponge', torso, 'Yellow sponge body');
  const vertices=body.geometry.attributes.position;
  for(let i=0;i<vertices.count;i++){
    const x=vertices.getX(i),y=vertices.getY(i),z=vertices.getZ(i);
    vertices.setXYZ(i,x*(1+.018*Math.sin(y*19+z*3)),y+.009*Math.sin(x*20+z*4),z+.008*Math.sin(x*17+y*11));
  }
  body.geometry.computeVertexNormals();
  box([0, 2.065, 0], [1.02, .17, .65], .025, 'white', torso, 'White shirt');
  box([0, 1.865, 0], [1.02, .27, .66], .045, 'shorts', torso, 'Brown square pants');
  for (const x of [-.35, -.1, .15, .37]) box([x, 1.96, .337], [.13, .034, .016], .003, 'belt', torso);
  for (const side of [-1, 1]) {
    const x = side * .215;
    characterEye({mesh,egg,parent:torso,center:[x,2.87,.338],radii:[.210,.233,.155],iris:'iris',gaze:.06,name:'Large white eye'});
    for (let i = 0; i < 3; i++) {
      const dx = (i - 1) * .093;
      link([x + dx, 3.06, .39], [x + dx * 1.25, 3.15 + (i === 1 ? .026 : 0), .38], .010, 'smile', torso);
    }
    egg([side * .33, 2.61, .328], [.12, .073, .039], 'sponge', torso, 'Integrated smile cheek');
    egg([side * .35, 2.625, .360], [.065, .035, .010], 'cheek', torso);
    box([side * .54, 2.24, 0], [.15, .21, .35], .045, 'white', torso, 'Short white sleeve');
    // Arms use bike coordinates: shoulders at the two lateral sides, hands on grips.
    const shoulder = [0, 2.24, side * .51], elbow = [.60, 2.10, side * .49], hand = [1.14, 1.92, side * .31];
    curve([shoulder, elbow, hand], .031, 'sponge', rider, 'Arm reaches handlebar');
    egg(hand, [.064, .052, .052], 'sponge', rider, 'Hand on handlebar');
    for (let i = 0; i < 3; i++) egg([1.16 + i * .012, 1.90 - i * .014, side * .335], [.027, .022, .016], 'sponge', rider);
  }
  egg([.015, 2.72, .477], [.068, .085, .17], 'sponge', torso, 'Rounded yellow nose');
  curve([[-.31, 2.59, .361], [-.19, 2.51, .350], [0, 2.49, .350], [.19, 2.51, .350], [.31, 2.59, .361]], .011, 'smile', torso);
  for (const x of [-.059, .059]) box([x, 2.47, .367], [.092, .11, .035], .009, 'white', torso, 'Two front teeth');
  for (const [x, y, radius] of [[-.41, 3.04, .048], [.40, 3.06, .042], [-.43, 2.74, .032], [.43, 2.41, .041], [-.31, 2.28, .050], [.20, 2.28, .027], [-.43, 2.43, .025]]) {
    egg([x, y, .338], [radius, radius * .74, .011], 'pore', torso, 'Ochre sponge pore');
  }
  for (const side of [-1, 1]) for (let i = 0; i < 4; i++) egg([side * .503, 2.36 + i * .19, (i % 2 ? -.10 : .10)], [.012, .034, .040], 'pore', torso);
  const knot = box([0, 2.09, .345], [.063, .06, .025], .008, 'tie', torso, 'Red tie knot'); knot.rotation.z = Math.PI / 4;
  const tie = mesh(new THREE.ConeGeometry(.070, .22, 4), 'tie', [0, 1.98, .348], torso, 'Red tie'); tie.rotation.z = Math.PI; tie.rotation.y = Math.PI / 4; tie.scale.z = .25;
  mergeStatic(torso); mergeStatic(rider, {recursive: false});
  const legs = [-1, 1].map(side => ({
    side, upper: link([-.42, 1.72, side * .30], [.05, 1.12, side * .28], .030, 'sponge', cycle),
    lower: link([.05, 1.12, side * .28], [0, .8, side * .24], .027, 'sponge', cycle),
    sock: link([0, .8, side * .24], [0, 1.03, side * .24], .039, 'white', cycle),
    stripe: link([0, .98, side * .24], [0, 1, side * .24], .041, 'tie', cycle),
    foot: egg([0, .8, side * .24], [.15, .062, .082], 'sole', cycle, 'Black shoe on pedal'),
  }));
  function connect(item, a, b) {
    item.position.copy(a).add(b).multiplyScalar(.5); item.scale.y = a.distanceTo(b);
    item.quaternion.setFromUnitVectors(Y, b.clone().sub(a).normalize());
  }
  function update(time) {
    const angle = time * 1.8;
    wheels.forEach(wheel => {wheel.rotation.z = -angle * 42 / 18;});
    crank.rotation.z = -angle; cassette.rotation.z = -angle * 42 / 18;
    for (let i = 0; i < chain.count; i++) {
      const t = ((i / chain.count - angle * R / chainLength) % 1 + 1) % 1;
      const p = chainPath.getPointAt(t), tangent = chainPath.getTangentAt(t);
      dummy.position.copy(p); dummy.rotation.z = Math.atan2(tangent.y, tangent.x); dummy.updateMatrix(); chain.setMatrixAt(i, dummy.matrix);
    }
    chain.instanceMatrix.needsUpdate = true;
    legs.forEach(({side, upper, lower, sock, stripe, foot}, i) => {
      const phase = angle + (side < 0 ? Math.PI : 0);
      const pedal = new THREE.Vector3(Math.sin(phase) * .21, .65 + Math.cos(phase) * .21, side * .24);
      pedals[i].position.copy(pedal);
      const ankle = pedal.clone().add(new THREE.Vector3(-.015, .068, 0));
      const hip = point([-.42, 1.74, side * .30]);
      const direction = ankle.clone().sub(hip), midpoint = hip.clone().add(ankle).multiplyScalar(.5);
      const bend = Math.sqrt(Math.max(0, .68 ** 2 - direction.lengthSq() * .25));
      const knee = midpoint.add(new THREE.Vector3(-direction.y, direction.x, 0).normalize().multiplyScalar(bend));
      connect(upper, hip, knee); connect(lower, knee, ankle);
      connect(sock, ankle, ankle.clone().lerp(knee, .38)); connect(stripe, ankle.clone().lerp(knee, .31), ankle.clone().lerp(knee, .34));
      foot.position.copy(ankle).add(new THREE.Vector3(.055, -.004, 0));
    });
  }
  update(0);
  return {cycle, rider, torso, wheels, legs, pedals, crank, cassette, chain, update};
}
