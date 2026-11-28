'use client';

import {createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode} from 'react';
import {authApi, systemApi} from '@/lib/api';
import {BASE_PATH} from '@/lib/base-path';
import {useHydrated} from '@/lib/use-hydrated';
import type {Me, OriginCheck, Role} from '@/lib/types';

const ME_CACHE_KEY = 'wb-me';

/** 缓存登录态，避免每次整页加载都先空一下再填充 */
function readCachedMe(): Me | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.sessionStorage.getItem(ME_CACHE_KEY);
    return raw ? (JSON.parse(raw) as Me) : null;
  } catch {
    return null;
  }
}

function writeCachedMe(me: Me | null): void {
  if (typeof window === 'undefined') return;
  try {
    if (me) window.sessionStorage.setItem(ME_CACHE_KEY, JSON.stringify(me));
    else window.sessionStorage.removeItem(ME_CACHE_KEY);
  } catch {
    /* 隐私模式下 sessionStorage 可能不可用，忽略 */
  }
}

interface AuthContextValue {
  me: Me | null;
  /**
   * D-41：本次访问路径下的同源锚点状态（横幅 / 安全页状态栏）。
   *
   * 与服务端身份分开取：身份响应 `/api/me` 的 body 被逐字钉死为
   * `{username, role}`（`test_authorization_generation` 守着），锚点状态在
   * 管理端端点 `GET /api/system/origin-status`。取不到时置空 —— 缺字段一律
   * 「不提示」，不凭空弹警告。
   */
  origin: OriginCheck | null;
  loading: boolean;
  isAdmin: boolean;
  canAddAccounts: boolean;
  role: Role | null;
  refresh: () => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({children}: {children: ReactNode}) {
  const [me, setMe] = useState<Me | null>(null);
  const [origin, setOrigin] = useState<OriginCheck | null>(null);
  // Cached display identity is not proof of an authenticated server session.
  const [loading, setLoading] = useState(true);
  const hydrated = useRef(false);
  const sequence = useRef(0);
  const pendingRead = useRef<{controller: AbortController; promise: Promise<void>} | null>(null);

  const invalidateRead = useCallback(() => {
    sequence.current++;
    pendingRead.current?.controller.abort();
    pendingRead.current = null;
  }, []);

  const refresh = useCallback((): Promise<void> => {
    if (pendingRead.current) return pendingRead.current.promise;
    const generation = ++sequence.current;
    const controller = new AbortController();
    const current = () => generation === sequence.current && !controller.signal.aborted;
    const promise = Promise.resolve().then(async () => {
      if (!current()) return;
      try {
        // 锚点状态与身份并行取；它失败不影响登录态（缺字段 = 不提示）。
        const [identity, origin] = await Promise.allSettled([
          authApi.me(controller.signal),
          systemApi.originStatus(controller.signal),
        ]);
        if (!current()) return;
        if (identity.status === 'fulfilled') {
          setMe(identity.value);
          writeCachedMe(identity.value);
        } else {
          throw identity.reason;
        }
        setOrigin(origin.status === 'fulfilled' ? origin.value : null);
      } catch (e) {
        if (!current()) return;
        // Only an authoritative 401 clears identity. Offline/5xx retains the
        // last display state; an older session read never overrides a login.
        const status = (e as {response?: {status?: number}}).response?.status;
        if (status === 401) {
          setMe(null);
          setOrigin(null);
          writeCachedMe(null);
        }
      } finally {
        if (current()) {
          pendingRead.current = null;
          setLoading(false);
        }
      }
    });
    pendingRead.current = {controller, promise};
    return promise;
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    invalidateRead();
    const generation = sequence.current;
    // A cached identity must not redirect a failed new login into the console.
    setMe(null);
    setOrigin(null);
    writeCachedMe(null);
    try {
      const res = await authApi.login(username, password);
      if (generation !== sequence.current) return;
      const next = {username: res.username, role: res.role as Role};
      setMe(next);
      writeCachedMe(next);
    } finally {
      if (generation === sequence.current) setLoading(false);
    }
  }, [invalidateRead]);

  const logout = useCallback(async () => {
    invalidateRead();
    const generation = sequence.current;
    const result = await authApi.logout();
    if (!result.ok) throw new Error('Logout was not confirmed by the server.');
    if (generation !== sequence.current) return;
    setMe(null);
    setOrigin(null);
    writeCachedMe(null);
    // 裸跳转不走 next/router，basePath 不会自动生效，必须显式拼上
    if (typeof window !== 'undefined') window.location.href = `${BASE_PATH}/login`;
  }, [invalidateRead]);

  useEffect(() => {
    if (hydrated.current) return;
    hydrated.current = true;
    // 先用缓存立即还原界面，再后台校验一次
    const cached = readCachedMe();
    if (cached) {
      setMe(cached);
    }
    void refresh();
    return () => {
      invalidateRead();
      hydrated.current = false;
    };
  }, [refresh, invalidateRead]);

  return (
    <AuthContext.Provider
      value={{me, origin, loading, isAdmin: me?.role === 'admin',
        canAddAccounts: me?.role === 'admin' || me?.role === 'account_admin',
        role: me?.role ?? null, refresh, login, logout}}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  const hydrated = useHydrated();
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用');
  return hydrated ? ctx : {...ctx, me: null, origin: null, loading: true,
    isAdmin: false, canAddAccounts: false, role: null};
}
