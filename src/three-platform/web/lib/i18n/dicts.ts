/**
 * 语言字典注册表（零依赖）—— K37「按语言拆包」的底层。
 *
 * 原来的做法是五种语言**全部静态 import**：
 *
 *     import zhCN from './locales/zh-CN.json';   //  99 KB
 *     import zhTW from './locales/zh-TW.json';   // 125 KB
 *     import en   from './locales/en.json';      // 131 KB
 *     import ja   from './locales/ja.json';      // 162 KB
 *     import ko   from './locales/ko.json';      // 150 KB
 *
 * 合计约 670 KB 源码，随**每一个**页面的首屏 JS 一起下载、解析、常驻内存，而一次
 * 会话只会用到其中一种语言。K37 的目标是「按语言拆包」：源语言（简体中文，也是
 * 缺键时的回退）保持静态，其余四种由 `lib/i18n/load.ts` 按需动态 import。
 *
 * 拆开以后必须解决「字典还没到」的中间态：`translate` 在目标语言字典未注册时
 * **回退到源语言**（与原先「缺键回退」是同一套语义），加载完成后由 Provider 触发
 * 一次重渲染。因此本模块只做注册/查找，不关心 React。
 *
 * 放在独立模块而不是塞进 `index.ts`：`index.ts` 静态 import 了 zh-CN.json（Node 的
 * type stripping 不支持 JSON import attributes），而这里要能被 `node --test` 直接
 * import 做单测。
 */

/** 字典形状：嵌套对象 + 字符串/复数形式；这里不关心具体值，只关心「有没有」。 */
export type DictLike = {[key: string]: unknown};

export interface DictRegistry {
  /** 注册一种语言的字典（同一语言重复注册后者覆盖） */
  register(locale: string, dict: DictLike): void;
  /** 取字典；未注册返回 undefined */
  get(locale: string): DictLike | undefined;
  has(locale: string): boolean;
  /** 已注册的语言数（诊断/测试） */
  size(): number;
  /** 清空（测试隔离用） */
  clear(): void;
}

export function createDictRegistry(): DictRegistry {
  const dicts = new Map<string, DictLike>();
  return {
    register(locale, dict) {
      if (!dict || typeof dict !== 'object') return;
      dicts.set(locale, dict);
    },
    get(locale) {
      return dicts.get(locale);
    },
    has(locale) {
      return dicts.has(locale);
    },
    size() {
      return dicts.size;
    },
    clear() {
      dicts.clear();
    },
  };
}

/**
 * 取「该用哪本字典」：目标语言 → 源语言。
 *
 * 只回退到源语言，不再逐层回退（`translate` 里仍有逐键回退，见 index.ts）：
 * 这里的含义是「整本字典还没加载」，返回源语言字典让界面先能显示，而不是留空。
 */
export function pickDict(
  registry: DictRegistry,
  locale: string,
  defaultLocale: string,
): DictLike | undefined {
  return registry.get(locale) ?? registry.get(defaultLocale);
}

/** 全局单例：`index.ts` 与 `lib/i18n/load.ts` 共用。 */
export const dictRegistry: DictRegistry = createDictRegistry();
