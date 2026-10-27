/**
 * 首屏之后才挂载图表（K37：chart chunk 不再进首屏包）。
 *
 * 为什么需要这个 hook：`next/dynamic({ssr:false})` 只是把图表拆成独立 chunk，
 * 组件一渲染就会去请求那个 chunk —— 对首屏可见的图表来说，这仍然发生在
 * 「加载事件」之前，等于没有延后。这里把「是否该渲染图表」推迟到浏览器空闲
 * （或首帧之后的一个短 timeout），配合 dynamic 才是真正的「首屏后异步加载」：
 *
 *   首屏 HTML/JS 不含 recharts → 空闲时请求 chart chunk → 渲染图表。
 *
 * 退路：Safari 旧版和部分环境没有 requestIdleCallback，用 120ms timeout 兜底；
 * 两者都会在 1.2s 内必然触发（requestIdleCallback 的 timeout 参数），所以数据
 * 已经到位的页面不会因为「浏览器一直忙」而永远看不到图。
 *
 * 注意：本 hook 只负责**时机**，不负责 chunk 拆分；拆分由调用方的
 * `next/dynamic` 完成。两者缺一都不是懒加载。
 */
'use client';

import {useEffect, useState} from 'react';

/** 首屏（load 之后的空闲帧）之前返回 false，之后返回 true。 */
export function useAfterFirstPaint(): boolean {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (typeof window === 'undefined') return;

    type IdleWindow = Window & {
      requestIdleCallback?: (cb: () => void, opts?: {timeout?: number}) => number;
      cancelIdleCallback?: (handle: number) => void;
    };
    const idleWindow = window as IdleWindow;

    if (typeof idleWindow.requestIdleCallback === 'function') {
      const handle = idleWindow.requestIdleCallback(() => setReady(true), {timeout: 1200});
      return () => idleWindow.cancelIdleCallback?.(handle);
    }

    const timer = setTimeout(() => setReady(true), 120);
    return () => clearTimeout(timer);
  }, []);

  return ready;
}
