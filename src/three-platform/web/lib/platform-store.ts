/**
 * 平台状态的**唯一事实来源** —— K32 的核心。
 *
 * 原来的问题不是「默认平台错了」（审计勘误表已撤回「前端默认 Qoder」的说法）：
 * 首访默认 WorkBuddy、`PlatformScope` 按平台重挂载都是对的。真正的问题是**两份
 * 事实来源**：
 *
 *   · UI（`PlatformProvider`）读自己的 React state，初值是默认值，挂载后再从
 *     `localStorage` 补一次；
 *   · 请求（`lib/api.ts` 的 axios 拦截器）**直接**读 `window.localStorage`。
 *
 * 于是出现三类问题，全都不报错：
 *   1. 存储被禁/无痕模式下，拦截器那句 `localStorage.getItem` 会抛（原代码没有
 *      try/catch），一个平台切换器的小问题会把整页数据请求带崩；
 *   2. 多标签页：A 标签切到 Trae 后，B 标签的请求已经跟着 localStorage 走了，
 *      但 B 的界面还显示 WorkBuddy —— 「顶部显示 A 平台、数据却是 B 平台」；
 *   3. 非法/历史存储值（大小写、旧名）两边处理不一致，一边默认、一边原样发送。
 *
 * 本模块给两边同一份内存状态，持久化只作为**回退与同步**：
 *   `get()` 优先内存，内存没有才去读存储，都拿不到用默认值；任何一次存储访问都
 *   被 try/catch 包住（无痕/被禁/配额满都只会退化成「本会话内有效」）。
 *
 * 另一个目的是**消除水合导致的重复请求**：存储里的平台与首帧默认值不同时，原先
 * `PlatformScope` 的 key 从默认值变成真实值 → 整棵子树重挂载 → 页面首屏数据被
 * 请求两次。这里用 `epoch` 表达「需要重挂载」：只有**用户主动切换**和**其他标签页
 * 改了这个键**才 `+1`；水合只更新内存值，不动 epoch。
 *
 * 零依赖（不 import React / next），这样 Node 可以直接测（见 platform-store.test.mjs）。
 */

/** 平台枚举。与后端 `X-QTG-Platform` 的取值一一对应。 */
export type Platform = 'qoder' | 'trae' | 'workbuddy';

export const PLATFORMS: readonly Platform[] = ['qoder', 'trae', 'workbuddy'] as const;

/** 与 `PlatformProvider`、axios 拦截器共用的存储键（沿用线上既有键，不迁移）。 */
export const PLATFORM_STORAGE_KEY = 'qtg.platform';

/**
 * 首次访问的默认平台：WorkBuddy。
 *
 * 这不是拍脑袋：线上 `PlatformProvider` 的初值就是 `'workbuddy'`，审计勘误表也
 * 撤回了「首访必显示 Qoder」的结论。改成别的值会让老用户第一次打开时看到另一套
 * 数据，属于无授权行为。
 */
export const DEFAULT_PLATFORM: Platform = 'workbuddy';

export function isPlatform(v: unknown): v is Platform {
  return typeof v === 'string' && (PLATFORMS as readonly string[]).includes(v);
}

/** 只依赖 localStorage 的两个方法，方便测试注入假存储。 */
export interface PlatformStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}

export interface PlatformStoreOptions {
  /** 传 null 表示禁用持久化（无痕/存储被禁/SSR） */
  storage?: PlatformStorage | null;
  defaultPlatform?: Platform;
  /** 存储键，默认 `qtg.platform`（测试可换） */
  storageKey?: string;
}

export interface PlatformStore {
  /** 内存值（未水合时即默认值）——渲染用，保证 SSR 与首帧一致 */
  peek(): Platform;
  /** 请求上下文用：内存 → 持久化 → 默认值 */
  get(): Platform;
  /** 只返回「显式选择过」的平台（内存或持久化里有合法值），否则 null */
  explicit(): Platform | null;
  /**
   * 首次从持久化水合。**不增加 epoch**（避免水合时把页面子树重挂载、
   * 造成首屏数据请求两次），非法值顺手修正为默认值。
   */
  hydrate(): Platform;
  /** 用户主动切换：写内存 + 持久化 + epoch+1 + 通知 */
  set(platform: Platform): Platform;
  /** 其他标签页改了同一个键：值真的变了才更新内存、epoch+1、通知 */
  syncFromStorage(): Platform;
  /** 同 `syncFromStorage`，但由调用方给出原始值（测试与 storage 事件用） */
  setFromStorageValue(raw: string | null): Platform;
  /** 需要重挂载内容子树的代次（用户切换 / 跨标签页变化才 +1） */
  getEpoch(): number;
  /** 是否已完成首次水合（诊断用） */
  isHydrated(): boolean;
  subscribe(listener: () => void): () => void;
  /** 存储键（诊断/测试） */
  readonly storageKey: string;
}

/** 安全读存储：无痕模式、隐私设置、配额异常都只返回 null，不抛。 */
function safeGet(storage: PlatformStorage | null | undefined, key: string): string | null {
  if (!storage) return null;
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

/** 安全写存储：写不进去只意味着「本会话内有效」，不能影响切换本身。 */
function safeSet(storage: PlatformStorage | null | undefined, key: string, value: string): boolean {
  if (!storage) return false;
  try {
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function createPlatformStore(options: PlatformStoreOptions = {}): PlatformStore {
  const storageKey = options.storageKey ?? PLATFORM_STORAGE_KEY;
  const fallback = options.defaultPlatform ?? DEFAULT_PLATFORM;
  /** 传 undefined 与传 null 都表示「没有可用存储」——测试要能模拟无痕模式 */
  const storage = options.storage ?? null;

  let memory: Platform | null = null;
  let hydrated = false;
  let epoch = 0;
  const listeners = new Set<() => void>();

  const notify = (): void => {
    for (const fn of listeners) {
      try {
        fn();
      } catch {
        /* 单个订阅者抛错不能影响其他订阅者 */
      }
    }
  };

  const peek = (): Platform => memory ?? fallback;

  const explicit = (): Platform | null => {
    if (memory !== null) return memory;
    const raw = safeGet(storage, storageKey);
    return isPlatform(raw) ? raw : null;
  };

  const get = (): Platform => explicit() ?? fallback;

  const setFromStorageValue = (raw: string | null): Platform => {
    const next = isPlatform(raw) ? raw : fallback;
    if (next === memory) return peek();
    memory = next;
    // 其他标签页改了值：本页内容属于旧平台，必须重挂载并重取
    epoch += 1;
    notify();
    return peek();
  };

  return {
    storageKey,
    peek,
    get,
    explicit,

    hydrate() {
      if (hydrated) return peek();
      hydrated = true;
      const raw = safeGet(storage, storageKey);
      if (isPlatform(raw)) {
        memory = raw;
      } else if (memory === null) {
        // 首次访问（或存储里是旧值/脏值）：落盘默认值，让第一个请求就带对平台头；
        // 这也是原 Provider 的行为，保持不变。
        memory = fallback;
        safeSet(storage, storageKey, fallback);
      }
      // memory 已有值（例如在 effect 之前就被显式 set 过）时不覆盖：水合只负责
      // 「从持久化补一次」，不能把用户已经做出的选择改回默认值。
      notify();
      return peek();
    },

    set(platform) {
      if (!isPlatform(platform)) return peek(); // 运行时防御：类型之外的值一律忽略
      const changed = memory !== platform;
      memory = platform;
      safeSet(storage, storageKey, platform);
      // 值真的变了才重挂载内容子树：重复点同一个平台不应触发整页重取请求。
      if (changed) {
        epoch += 1;
        notify();
      }
      return peek();
    },

    syncFromStorage() {
      return setFromStorageValue(safeGet(storage, storageKey));
    },

    setFromStorageValue,

    getEpoch() {
      return epoch;
    },

    isHydrated() {
      return hydrated;
    },

    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/**
 * 浏览器侧取存储。
 *
 * `window.localStorage` 这个**属性访问本身**就可能抛（部分浏览器的隐私模式/
 * 被策略禁用时），所以连取它都要 try/catch——这正是原拦截器漏掉的那层。
 */
export function browserStorage(): PlatformStorage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage ?? null;
  } catch {
    return null;
  }
}

/** 全局单例：Provider 与 axios 拦截器都只认它。 */
export const platformStore: PlatformStore = createPlatformStore({storage: browserStorage()});
