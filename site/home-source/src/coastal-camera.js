export const COASTAL_VIEWS = Object.freeze({
  town: {target: [1.8, 2.4, -2.5], yaw: .30, pitch: .30, width: 29, height: 10.5, depth: 5},
  side: {target: [-.8, 1.04, 4.35], yaw: .65, pitch: .13, width: 3.30, height: 2.55, depth: .5},
  front: {target: [-.6, .94, 4.4], yaw: 1.05, pitch: .14, width: 2.65, height: 2.25, depth: .5},
  homes: {target: [-3, 2.8, -.6], yaw: -.10, pitch: .22, width: 17.2, height: 9.4, depth: 2.7},
  restaurant: {target: [10.2, 2.65, -5.7], yaw: -.25, pitch: .22, width: 11.4, height: 8.0, depth: 2.5},
});

export function coastalRadius(view, aspect) {
  const p = COASTAL_VIEWS[view] || COASTAL_VIEWS.town;
  return Math.max(p.height / 2, p.width / (2 * Math.max(.3, aspect))) / Math.tan(35 * Math.PI / 360) + p.depth;
}
