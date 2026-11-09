# Seaside Cycling - Selected Lightweight Direction

Selected source: https://github.com/JacoryOu/pelican-velo (MIT, Jacory Ou).
The upstream author preview was selected by the user. Original source bytes
and archive SHA256 are recorded in `vendor/pelican-velo/PROVENANCE.json`.
No upstream package scripts or installers were executed.

## Deliberate Changes

- Short red bill, round body and face markings for a stylized partridge;
  not the upstream pelican pouch or a literal zoological model.
- Vintage bicycle, opposed pedals, two-bone legs, flower basket and scarf.
- One boardwalk and a sunset sea. No town, island loop, audio, day/night
  system, environment map, bloom, film grain or fullscreen post-processing.
- Static meshes batched by material; planks instanced, spokes use lines.
- Parent permission plus intersection gates renderer download and creation.
  No ES module CORS exception or `allow-same-origin` is needed in the sandbox.
- Desktop <=420000 render pixels, mobile <=220000; no device-pixel-ratio boost.
  Desktop <=30 animation renders/s; mobile <=24. Mobile shadows disabled;
  desktop has one 512x512 map. Sustained slow cadence reduces pixel budget.
- Pause, offscreen, background and reduced-motion stop automatic rendering.
  Context failure keeps a local poster; drag and keyboard controls are bounded.

## Verification Boundary

Source tests check finite geometry, articulation, vertex/triangle/drawable
budgets, pixels, frame scheduling and lifecycle using injected renderers.
Offline OpenGL renders inspect real geometry and compile sea/sky fragments,
but use a different lighting pipeline and omit browser shadows. They are not
browser screenshots, actual draw-call timings, phone acceptance or FPS proof.
The supported browser reports local browser use disabled in Work settings.
Visual/browser performance acceptance is therefore blocked, not passed.

The scene script gzip budget includes its own bundled Three.js runtime; it does
not claim to reuse the sculpture's separate IIFE bundle. It does not reduce
the existing Thinker model or prove whole-homepage performance.
