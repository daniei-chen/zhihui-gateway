/**
 * K33（前端侧）：跨发布/旧静态资源的**回退体验**。
 *
 * 背景：前端是静态导出 + 版本目录（`release/release-web-v2.sh` 会保留 N 个旧版本，
 * `_next/static/**` 带内容哈希）。但用户可能：
 *   · 停留在旧标签页，点导航时按**旧 buildId** 去取 RSC payload / 懒加载 chunk，
 *     而旧版本目录已被清理 → 404，浏览器里表现为「点了没反应」或白屏；
 *   · 旧 HTML 里引用的 chunk hash 在新部署里不存在 → `ChunkLoadError`；
 *   · 前进/后退命中 bfcache 里的旧页面，再次导航同样失败。
 *
 * 这些错误在 Next 里表现为若干固定文案（各浏览器/micro-frontend 不同），React 的
 * 错误边界会把它们抛给我们。正确的处理是**刷新到当前版本**，而不是留在错误页：
 * 用户数据在前端没有本地状态（都来自接口），刷新代价很小。
 *
 * 但「刷新」必须**防环**：如果新版本本身就坏了（或者用户访问的路径在新版本里不存在），
 * 无脑自动刷新会把用户锁进「白屏 → 刷新 → 白屏」的死循环。所以本模块提供：
 *   1. `isStaleBundleError` —— 只对「资源/构建不匹配」类错误给出自动刷新建议，
 *      普通渲染异常仍然走原来的错误页；
 *   2. `createReloadGuard` —— 用 sessionStorage 记录「本标签页最近一次自动刷新」，
 *      时间窗内只允许自动刷新一次；存储不可用（无痕/被禁）时**一律不自动刷新**，
 *      只显示手动按钮（宁可多一次点击，也不能把用户困在刷新环里）。
 *
 * 零依赖，供 `app/error.tsx` / `app/(main)/error.tsx` 使用，也便于 Node 直接测。
 */

/** 用到的存储能力（sessionStorage/localStorage 都满足，便于测试注入） */
export interface ReloadGuardStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** 自动刷新记录：时间窗内刷新过几次 */
export interface ReloadRecord {
  /** 首次记录的毫秒时间戳 */
  ts: number;
  /** 时间窗内自动刷新的次数 */
  count: number;
}

export const STALE_RELOAD_KEY = 'wb.stale-build-reload';
/** 默认时间窗：10 分钟。窗口内的后续失败一律交给手动按钮。 */
export const STALE_RELOAD_WINDOW_MS = 10 * 60 * 1000;
/** 默认窗口内允许的自动刷新次数：1 次。 */
export const STALE_RELOAD_MAX = 1;

/**
 * 这些短语来自真实报错的文案，覆盖 Chromium / Firefox / Safari 与 Next 的 RSC 路径。
 * 大小写不敏感；只匹配「资源/构建不匹配」，不匹配普通业务异常。
 */
const STALE_PATTERNS: readonly RegExp[] = [
  /ChunkLoadError/i,
  /Loading chunk [\w/.-]+ failed/i,
  /Loading CSS chunk/i,
  /Failed to fetch dynamically imported module/i, // Firefox
  /error loading dynamically imported module/i, // Firefox（小写变体）
  /Importing a module script failed/i, // Safari
  /Failed to load chunk/i,
  /Unable to load chunk/i,
  /Failed to fetch RSC payload/i, // Next 15 App Router 客户端导航
  /Failed to fetch server response/i,
  /Unexpected token '<'.*is not valid JSON/i, // 旧路径返回了 HTML（404 页面）
];

/** 错误对象/字符串是否是「旧构建或旧资源」导致的 */
export function isStaleBundleError(error: unknown): boolean {
  if (error == null) return false;
  const parts: string[] = [];
  if (typeof error === 'string') {
    parts.push(error);
  } else if (typeof error === 'object') {
    const e = error as {name?: unknown; message?: unknown; digest?: unknown; cause?: unknown};
    if (typeof e.name === 'string') parts.push(e.name);
    if (typeof e.message === 'string') parts.push(e.message);
    if (typeof e.digest === 'string') parts.push(e.digest);
    if (typeof e.cause === 'string') parts.push(e.cause);
  }
  const text = parts.join(' | ');
  return STALE_PATTERNS.some((re) => re.test(text));
}

function safeGet(storage: ReloadGuardStorage | null | undefined, key: string): string | null {
  if (!storage) return null;
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(storage: ReloadGuardStorage | null | undefined, key: string, value: string): boolean {
  if (!storage) return false;
  try {
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

function safeRemove(storage: ReloadGuardStorage | null | undefined, key: string): void {
  if (!storage) return;
  try {
    storage.removeItem(key);
  } catch {
    /* 删不掉不影响判断（窗口会自然过期） */
  }
}

function parseRecord(raw: string | null): ReloadRecord | null {
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as {ts?: unknown; count?: unknown};
    if (typeof v.ts !== 'number' || !Number.isFinite(v.ts)) return null;
    const count = typeof v.count === 'number' && Number.isFinite(v.count) ? v.count : 0;
    return {ts: v.ts, count};
  } catch {
    return null;
  }
}

export interface ReloadGuardOptions {
  /** 时间窗（毫秒），默认 10 分钟 */
  windowMs?: number;
  /** 窗口内允许的自动刷新次数，默认 1 */
  max?: number;
  /** 存储键，默认 `wb.stale-build-reload` */
  key?: string;
}

export interface ReloadGuard {
  /**
   * 这次失败是否值得**自动**刷新一次。
   * 存储不可用 / 窗口内已自动刷过 / 记录损坏 → false（交给手动按钮，绝不循环）。
   */
  shouldAutoReload(now?: number): boolean;
  /** 记录一次自动刷新（应在真正 reload 之前调用） */
  markAutoReload(now?: number): void;
  /** 清空记录（例如用户手动刷新成功后，或检测到新构建可用） */
  clear(): void;
  /** 当前记录（诊断用） */
  record(): ReloadRecord | null;
}

/**
 * 创建「自动刷新防环」守卫。
 *
 * 用 sessionStorage 而不是 localStorage：自动刷新是**每个标签页**的体验问题；
 * 新标签页是全新加载，本来就不会命中旧资源，不该被别的标签页的历史记录拖累。
 */
export function createReloadGuard(
  storage: ReloadGuardStorage | null | undefined,
  options: ReloadGuardOptions = {},
): ReloadGuard {
  const key = options.key ?? STALE_RELOAD_KEY;
  const windowMs = options.windowMs ?? STALE_RELOAD_WINDOW_MS;
  const max = options.max ?? STALE_RELOAD_MAX;

  /**
   * 存储是否**可写**：构造时探测一次。
   *
   * 只读/被禁/配额满的存储读得到 null、写会抛；若不识别这种情况，`shouldAutoReload`
   * 每次都认为「没记录 → 可以刷」，而 `markAutoReload` 又写不进去 → 无限刷新环。
   * 宁可退化成手动按钮。
   */
  const writable = (() => {
    if (!storage) return false;
    try {
      storage.setItem(`${key}.probe`, '1');
      storage.removeItem(`${key}.probe`);
      return true;
    } catch {
      return false;
    }
  })();

  const read = (): ReloadRecord | null => parseRecord(safeGet(storage, key));

  return {
    shouldAutoReload(now = Date.now()) {
      // 没有可用/可写存储：无法保证「只刷一次」，就不自动刷新（防止死循环）
      if (!writable) return false;
      const rec = read();
      if (!rec) return true;
      if (now - rec.ts > windowMs) return true; // 旧记录已过期
      return rec.count < max;
    },

    markAutoReload(now = Date.now()) {
      const rec = read();
      const next: ReloadRecord =
        rec && now - rec.ts <= windowMs ? {ts: rec.ts, count: rec.count + 1} : {ts: now, count: 1};
      safeSet(storage, key, JSON.stringify(next));
      safeRemove(storage, `${key}.probe`);
    },

    clear() {
      safeRemove(storage, key);
      safeRemove(storage, `${key}.probe`);
    },

    record: read,
  };
}

/** 取当前标签页的 sessionStorage（拿不到就返回 null，由调用方降级为手动刷新） */
export function sessionStorageOrNull(): ReloadGuardStorage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage ?? null;
  } catch {
    return null;
  }
}

/**
 * 刷新到当前版本。
 *
 * 保留路径与查询串（用户还在同一页），只重新拉取 HTML 与资源；不用
 * `location.reload()` 的原因是它在部分浏览器里会复用 bfcache 里的旧文档，
 * 而这里要的恰恰是「重新要一份新的构建产物」。
 */
export function reloadCurrentPage(): void {
  if (typeof window === 'undefined') return;
  try {
    window.location.replace(window.location.href);
  } catch {
    window.location.reload();
  }
}
