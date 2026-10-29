'use client';

import {createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode} from 'react';
import {authApi} from '@/lib/api';
import {BASE_PATH} from '@/lib/base-path';
import {useHydrated} from '@/lib/use-hydrated';
import type {Me, Role} from '@/lib/types';

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
  loading: boolean;
  isAdmin: boolean;
  role: Role | null;
  refresh: () => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({children}: {children: ReactNode}) {
  const [me, setMe] = useState<Me | null>(null);
  // Cached display identity is not proof of an authenticated server session.
  const [loading, setLoading] = useState(true);
  const hydrated = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const data = await authApi.me();
      setMe(data);
      writeCachedMe(data);
    } catch (e) {
      // 只有 401（会话真的失效）才清登录态。网络抖动 / 5xx 时保留缓存：
      // 以前一律 setMe(null) 会在后端瞬时故障时把有效会话用户踢回登录页。
      const status = (e as {response?: {status?: number}}).response?.status;
      if (status === 401) {
        setMe(null);
        writeCachedMe(null);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const res = await authApi.login(username, password);
    const next = {username: res.username, role: res.role as Role};
    setMe(next);
    writeCachedMe(next);
    setLoading(false);
  }, []);

  const logout = useCallback(async () => {
    const result = await authApi.logout();
    if (!result.ok) throw new Error('Logout was not confirmed by the server.');
    setMe(null);
    writeCachedMe(null);
    // 裸跳转不走 next/router，basePath 不会自动生效，必须显式拼上
    if (typeof window !== 'undefined') window.location.href = `${BASE_PATH}/login`;
  }, []);

  useEffect(() => {
    if (hydrated.current) return;
    hydrated.current = true;
    // 先用缓存立即还原界面，再后台校验一次
    const cached = readCachedMe();
    if (cached) {
      setMe(cached);
    }
    refresh();
  }, [refresh]);

  return (
    <AuthContext.Provider
      value={{me, loading, isAdmin: me?.role === 'admin', role: me?.role ?? null, refresh, login, logout}}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  const hydrated = useHydrated();
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用');
  return hydrated ? ctx : {...ctx, me: null, loading: true, isAdmin: false, role: null};
}
