'use client';

import {createContext, useCallback, useContext, useEffect, useMemo, useState} from 'react';

import {DEFAULT_PLATFORM, PLATFORM_STORAGE_KEY, platformStore, type Platform} from './platform-store';
import {useHydrated} from './use-hydrated';

export type {Platform};
export {DEFAULT_PLATFORM};

/**
 * 平台切换（Qoder / Trae / WorkBuddy）。
 *
 * 与 `realm`（账号所在版本：国内 / 国际）是**两个独立维度**：
 *
 *   平台 = 你连的是哪个上游网关（Qoder 或 Trae）
 *   版本 = 该平台下这个账号属于国内站还是国际站
 *
 * 顶部两个切换器并存：先选平台，再看该平台下的版本。
 * 三平台共用智汇控制面；写操作按当前平台的真实能力分流，不跨平台兜底。
 *
 * 默认 WorkBuddy（与线上历史行为一致；审计勘误表已撤回「首访必显示 Qoder」）。
 *
 * **状态来源**：本组件不再自己读写 `localStorage`，而是订阅 `lib/platform-store.ts`
 * ——同一个 store 也是 axios 拦截器（请求头）的来源，避免「界面显示 A、请求发 B」。
 * 存储被禁/无痕/多标签页的处理与不变量都写在那个模块里，这里只做 React 绑定。
 */
interface PlatformCtx {
  platform: Platform;
  setPlatform: (p: Platform) => void;
  label: string;
  /**
   * 内容子树的挂载代次。
   *
   * `PlatformScope` 用它当 key，而**不是**用 `platform` 本身：持久化平台与首帧
   * 默认值不同时，水合会把 `platform` 从默认值改成真实值，若直接用 `platform`
   * 当 key，整棵子树会因此重挂载一次 —— 每个页面的首屏数据都被请求两遍。
   * epoch 只在「用户主动切换」与「其他标签页改了同一个键」时 +1。
   */
  epoch: number;
}

const Ctx = createContext<PlatformCtx | null>(null);

export function PlatformProvider({children}: {children: React.ReactNode}) {
  // 首帧（含 SSR 预渲染）一律默认值：直接读存储会让服务端与客户端首帧不一致
  const [platform, setPlatformState] = useState<Platform>(() => platformStore.peek());
  const [epoch, setEpoch] = useState<number>(() => platformStore.getEpoch());

  useEffect(() => {
    const apply = () => {
      setPlatformState(platformStore.peek());
      setEpoch(platformStore.getEpoch());
    };

    // 先订阅再水合：水合会通知一次（值可能从默认变成持久化值）
    const unsubscribe = platformStore.subscribe(apply);

    /**
     * 首次水合：读持久化值。**不增加 epoch**——这正是「不重复请求」的关键：
     * 页面已经挂载并按持久化平台发过请求（拦截器走 store 的同一份回退），
     * 这里只把界面显示同步过来，不重挂载子树。
     */
    platformStore.hydrate();

    // 跨标签页：storage 事件只会在**其他**标签页写入时触发（本标签页不触发），
    // 所以本标签页的切换由 store.set 自己通知。
    const onStorage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== PLATFORM_STORAGE_KEY) return;
      platformStore.syncFromStorage();
    };
    if (typeof window !== 'undefined') window.addEventListener('storage', onStorage);

    apply();
    return () => {
      unsubscribe();
      if (typeof window !== 'undefined') window.removeEventListener('storage', onStorage);
    };
  }, []);

  const setPlatform = useCallback((p: Platform) => {
    // store 会写内存 + 持久化 + epoch+1 + 通知；组件状态由订阅回填
    platformStore.set(p);
  }, []);

  const value = useMemo<PlatformCtx>(
    () => ({
      platform,
      setPlatform,
      label: platform === 'trae' ? 'Trae' : platform === 'workbuddy' ? 'WorkBuddy' : 'Qoder',
      epoch,
    }),
    [platform, setPlatform, epoch],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function usePlatform(): PlatformCtx {
  const ctx = useContext(Ctx);
  const hydrated = useHydrated();
  if (!ctx) throw new Error('usePlatform must be used within PlatformProvider');
  return hydrated ? ctx : {...ctx, platform: DEFAULT_PLATFORM, label: 'WorkBuddy', epoch: 0};
}

/**
 * 按平台强制重挂载内容子树。
 *
 * 在 `(main)/layout.tsx` 里包住 `children` 即可：平台真的变了，整棵子树卸载重建。
 *
 *  · 每一页的 `useAsyncAll` 都会按新平台重新走一次「首屏」——deps 里不必再各自
 *    写 platform，也不会因为某个页面漏写而把上一个平台的数据留在屏幕上；
 *  · 设置页的表单草稿、任务页的轮询定时器、日志页的分页游标、对话页的会话
 *    全部回到初始值。旧平台的状态一个都不许带进新平台。
 *
 * key 用 `epoch` 而不是 `platform`：见 `PlatformCtx.epoch` 的说明（水合不算切换）。
 *
 * 用 `display: contents` 包一层：不参与布局计算，DOM 与没有这一层时看起来一致。
 */
export function PlatformScope({children}: {children: React.ReactNode}) {
  const {epoch} = usePlatform();
  return <div key={epoch} className="contents">{children}</div>;
}
