'use client';

import {useEffect, useRef} from 'react';

export function ConsoleAtmosphere() {
  const light = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = light.current;
    if (!node) return;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const precise = matchMedia('(hover: hover) and (pointer: fine)');
    let frame = 0;
    let x = 0;
    let y = 0;
    let surface: HTMLElement | null = null;
    const reset = () => {
      cancelAnimationFrame(frame);
      frame = 0;
      node.removeAttribute('data-active');
      surface?.style.removeProperty('--surface-light');
      surface = null;
    };
    const paint = () => {
      frame = 0;
      if (document.hidden || reduced.matches || !precise.matches) return;
      const size = node.offsetWidth;
      node.style.transform = `translate3d(${x - size / 2}px,${y - size / 2}px,0)`;
      node.setAttribute('data-active', '');
      if (surface) {
        const rect = surface.getBoundingClientRect();
        surface.style.setProperty('--surface-x', `${x - rect.left}px`);
        surface.style.setProperty('--surface-y', `${y - rect.top}px`);
        surface.style.setProperty('--surface-light', '1');
      }
    };
    const move = (event: PointerEvent) => {
      if (event.pointerType !== 'mouse' || reduced.matches || !precise.matches || document.hidden) return;
      x = event.clientX;
      y = event.clientY;
      const next = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-slot="stat-card"]') : null;
      if (surface !== next) {
        surface?.style.removeProperty('--surface-light');
        surface = next;
      }
      if (!frame) frame = requestAnimationFrame(paint);
    };
    document.addEventListener('pointermove', move, {passive: true});
    document.documentElement.addEventListener('pointerleave', reset);
    document.addEventListener('visibilitychange', reset);
    window.addEventListener('blur', reset);
    window.addEventListener('scroll', reset, {passive: true});
    reduced.addEventListener('change', reset);
    precise.addEventListener('change', reset);
    return () => {
      reset();
      document.removeEventListener('pointermove', move);
      document.documentElement.removeEventListener('pointerleave', reset);
      document.removeEventListener('visibilitychange', reset);
      window.removeEventListener('blur', reset);
      window.removeEventListener('scroll', reset);
      reduced.removeEventListener('change', reset);
      precise.removeEventListener('change', reset);
    };
  }, []);

  return <div ref={light} className="zh-console-aura" aria-hidden="true" />;
}
