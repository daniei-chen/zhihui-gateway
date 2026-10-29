'use client';

/**
 * 语言上下文。
 *
 * 首帧固定渲染源语言（服务端预渲染时读不到 localStorage），挂载后再切到
 * 用户保存 / 浏览器语言——与 RealmProvider 同一取舍，代价是非中文用户
 * 整页加载时会闪一下中文，但避免了服务端与客户端首帧不一致的水合报错。
 * 站内跳转是客户端路由，切换后不会再闪。
 *
 * K37：非源语言的字典不再静态打包，改为按需 `import()`（见 `lib/i18n/load.ts`）。
 * 因此多了一个中间态：语言已选中、字典还在路上——这时界面继续显示源语言，
 * 加载完成后本组件触发一次重渲染换成目标语言。`dictEpoch` 就是那个触发器
 * （字典注册在模块级 Map 里，React 看不到它变化）。
 */
import {createContext, useCallback, useContext, useEffect, useMemo, useRef, useState} from 'react';

import {DEFAULT_LOCALE, LOCALES, STORAGE_KEY, detectLocale, type Locale} from './config';
import {setActiveLocale, tp, translator, type TParams} from './index';
import {ensureLocaleDict, isLocaleLoaded} from './load';
import {useHydrated} from '../use-hydrated';

const sourceTranslator = translator(DEFAULT_LOCALE);
const sourcePhrase = (source: string) => source;

export type TFn = (key: string, params?: TParams) => string;

interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: TFn;
  locales: readonly Locale[];
  /** 短语表翻译：参数是**中文源文**（用于设置页这类数据表文案） */
  tp: (source: string) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({children}: {children: React.ReactNode}) {
  const [locale, setLocaleState] = useState<Locale>(DEFAULT_LOCALE);
  /** 语言包加载完成的代次：仅用于触发重渲染，不是业务状态 */
  const [dictEpoch, setDictEpoch] = useState(0);
  const selectionRef = useRef({version: 0});

  useEffect(() => {
    let alive = true;
    const selection = selectionRef.current;
    const request = ++selection.version;
    const applyLocale = (next: Locale) => {
      if (!alive || request !== selection.version) return;
      setLocaleState(next);
      setDictEpoch((v) => v + 1);
    };

    const target = detectLocale();
    if (isLocaleLoaded(target)) {
      // 源语言（或已被其他入口加载过）：同步切换，不闪
      if (target !== DEFAULT_LOCALE) applyLocale(target);
      return () => {
        alive = false;
        selection.version++;
      };
    }
    void ensureLocaleDict(target).then((ok) => {
      if (ok) applyLocale(target);
      // 加载失败（离线/旧 chunk 已被清理）：保持源语言，不报错——语言包是增强
    });
    return () => {
      alive = false;
      selection.version++;
    };
  }, []);

  // 渲染期同步给模块级入口：lib/format.ts 这类纯函数在子组件渲染时被调用，
  // 放到 effect 里会晚一帧，出现“文案已换、日期格式还是旧语言”的错位。
  setActiveLocale(locale);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    // A late startup dictionary must not overwrite an explicit user selection.
    const selection = selectionRef.current;
    const request = ++selection.version;
    // 先切界面语言：字典没到之前 `translate` 会回退到源语言，
    // 用户看到的是「还没翻译完」而不是「点了没反应」。
    setLocaleState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* 存不进就算了，本次会话内切换依然生效 */
    }
    if (!isLocaleLoaded(next)) {
      void ensureLocaleDict(next).then((ok) => {
        if (ok && request === selection.version) setDictEpoch((v) => v + 1);
      });
    }
  }, []);

  // dictEpoch 参与依赖：字典加载完成后 t 的引用要变，消费方才会重渲染
  // eslint-disable-next-line react-hooks/exhaustive-deps -- Async dictionary registration changes outside React.
  const t = useMemo(() => translator(locale), [locale, dictEpoch]);

  const value = useMemo<I18nContextValue>(
    () => ({locale, setLocale, t, tp, locales: LOCALES}),
    [locale, setLocale, t],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext);
  const hydrated = useHydrated();
  if (!value) throw new Error('useI18n 必须在 I18nProvider 内使用');
  return hydrated ? value : {...value, locale: DEFAULT_LOCALE, t: sourceTranslator, tp: sourcePhrase};
}

/** 只要翻译函数时的简写。 */
export function useT(): TFn {
  return useI18n().t;
}
