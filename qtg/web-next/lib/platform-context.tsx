'use client';

import {createContext, useCallback, useContext, useEffect, useMemo, useState} from 'react';

/**
 * 平台切换（Qoder / Trae / WorkBuddy）。
 *
 * 与 `realm`（账号所在版本：国内 / 国际）是**两个独立维度**：
 *
 *   平台 = 你连的是哪个上游网关（Qoder 或 Trae）
 *   版本 = 该平台下这个账号属于国内站还是国际站
 *
 * 顶部两个切换器并存：先选平台，再看该平台下的版本。
 * 三合一网关：第三平台 WorkBuddy 已经接入（只读聚合，写操作仍在原面板）。
 *
 * 默认 Qoder（主平台）。
 */
export type Platform = 'qoder' | 'trae' | 'workbuddy';

const STORAGE_KEY = 'qtg.platform';

interface PlatformCtx {
  platform: Platform;
  setPlatform: (p: Platform) => void;
  label: string;
}

const Ctx = createContext<PlatformCtx | null>(null);

export function PlatformProvider({children}: {children: React.ReactNode}) {
  const [platform, setPlatformState] = useState<Platform>('qoder');

  // 首屏后读 localStorage：直接读会让服务端渲染与客户端不一致
  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      if (saved === 'qoder' || saved === 'trae' || saved === 'workbuddy') {
        setPlatformState(saved);
      }
    } catch {
      /* 隐私模式下可能抛错，用默认值即可 */
    }
  }, []);

  const setPlatform = useCallback((p: Platform) => {
    setPlatformState(p);
    try {
      window.localStorage.setItem(STORAGE_KEY, p);
    } catch {
      /* 存不进不影响本次会话内的切换 */
    }
  }, []);

  const value = useMemo<PlatformCtx>(
    () => ({
      platform,
      setPlatform,
      label: platform === 'trae' ? 'Trae' : platform === 'workbuddy' ? 'WorkBuddy' : 'Qoder',
    }),
    [platform, setPlatform],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function usePlatform(): PlatformCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('usePlatform must be used within PlatformProvider');
  return ctx;
}
